"""Drift guard binding the 09 autonomy template's injected environment to the
evaluator's server-owned settings resolver (issue #440).

The 09 stack injects a fixed set of GBAW_OPERATIONS_* variables into the
evaluator Lambda. The evaluator's ``resolve_autonomy_evaluator_settings`` reads
those exact variables and fails closed unless every one is present and the two
independent enable gates hold. If the template and the resolver drift apart — a
renamed key, a dropped binding — a deployed-but-broken evaluator would only be
caught in production. These tests catch it at build time by:

* extracting the evaluator's injected env keys straight from the template;
* asserting the resolver's required autonomy keys are all injected;
* proving the injected env (with AutonomyMode=operate) resolves cleanly; and
* proving the injected env (with AutonomyMode=disabled) fails closed — the
  default-disabled posture the whole stack depends on.
"""

# Standard library
import pathlib

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template
from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/09-operations-autonomy.yaml"

# The autonomy-specific keys the resolver requires (beyond the shared executor
# settings it embeds). If the template stops injecting any of these, the
# evaluator cannot resolve and the deployment is silently broken.
REQUIRED_AUTONOMY_KEYS = frozenset(
    {
        "GBAW_OPERATIONS_AUTONOMY_ENABLED",
        "GBAW_OPERATIONS_MODE",
        "GBAW_OPERATIONS_CAPABILITY_MAXIMUM",
        "GBAW_OPERATIONS_TABLE_NAME",
        "GBAW_OPERATIONS_TENANT_ID",
        "GBAW_OPERATIONS_WORKSPACE_ID",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN",
        "GBAW_OPERATIONS_ENROLLED_LOCATION",
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN",
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE",
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE",
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION",
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_ENVIRONMENT",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH",
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID",
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT",
        # Required by the embedded executor deployment settings resolver.
        "GBAW_OPERATIONS_STATE_MACHINE_ARN",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE",
        "GBAW_OPERATIONS_METRIC_NAMESPACE",
    }
)


@pytest.fixture(scope="module")
def evaluator_env_keys():
    template = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))
    fn = template["Resources"]["EvaluatorFunction"]["Properties"]
    variables = fn["Environment"]["Variables"]
    return set(variables.keys())


def test_template_injects_every_required_autonomy_key(evaluator_env_keys):
    missing = REQUIRED_AUTONOMY_KEYS - evaluator_env_keys
    assert not missing, f"09 template must inject these evaluator env keys: {sorted(missing)}"


def _resolved_env(mode: str) -> dict:
    """A complete env matching what the 09 template injects, parameterized by the
    AutonomyMode-driven lever values."""
    fleet = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
    return {
        "GBAW_OPERATIONS_AUTONOMY_ENABLED": "true" if mode == "operate" else "false",
        "GBAW_OPERATIONS_MODE": "operate" if mode == "operate" else "disabled",
        "GBAW_OPERATIONS_CAPABILITY_MAXIMUM": "operate",
        "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
        "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
        "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": fleet,
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": f"arn:aws:gamelift:us-west-2:000000000000:fleet/{fleet}",
        "GBAW_OPERATIONS_ENROLLED_LOCATION": "us-west-2",
        "GBAW_OPERATIONS_CAPACITY_FLOOR": "0",
        "GBAW_OPERATIONS_CAPACITY_CEILING": "1",
        "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "1",
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-execution"
        ),
        # The embedded executor resolver requires these two; the template binds
        # STATE_MACHINE_ARN to the same E3 workflow and TRUSTED_AUDIENCE to the
        # automation client.
        "GBAW_OPERATIONS_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-execution"
        ),
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "client.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE": "autonomy-profile",
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "app-abc",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "env-abc",
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION": "autonomy-app-abc",
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_ENVIRONMENT": "env-abc",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE": "killswitch-profile",
        "GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT": "2772",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID": "policy.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION": "2026-09-01",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH": "sha256:" + "a" * 64,
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID": "state.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT": "automation.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT": "client.gamelift-capacity-autonomy",
    }


def test_operate_env_resolves_cleanly():
    settings = resolve_autonomy_evaluator_settings(_resolved_env("operate"))
    assert settings.static_deployment_mode == "operate"
    assert settings.autonomy_switch_profile != settings.kill_switch_profile


def test_disabled_env_fails_closed():
    with pytest.raises(ValueError):
        resolve_autonomy_evaluator_settings(_resolved_env("disabled"))
