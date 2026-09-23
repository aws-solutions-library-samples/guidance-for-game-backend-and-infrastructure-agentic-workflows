"""Combined-checkout contract tests binding the 07 execution stack's injected
environment to the REAL core settings resolver (issue #415).

The E3 dispatcher and executor Lambdas both bootstrap from
:func:`operations.settings.resolve_executor_deployment_settings`. This suite
asserts, as data, that the 07 CloudFormation template injects EXACTLY the frozen
environment contract that resolver reads — no more (the retired
``GBAW_OPERATIONS_EXECUTION_MODE`` must be gone) and no less (every required
binding must be present, and the enrolled fleet ARN must be a constructed exact
ARN, never taken from the operation).

It also asserts the cross-stack CMK / access-log wiring the alignment introduced:
the E3 dispatcher, executor, and dispatch-API access log groups are CMK-encrypted
and allowlisted in the 06 key policy by exact ARN, the access log group has a
vended-log delivery resource policy, and the E3 stateful log resources use
``RetainExceptOnCreate``.

These tests never call AWS and need no sibling worktree: they parse the
repository-owned templates and read the core settings module from this checkout.
"""

from __future__ import annotations

# Standard library
import os
import pathlib

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template
from operations.settings import resolve_executor_deployment_settings

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
EXEC_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"
OBSERVE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"

# The exact set of environment keys the core resolver reads (required + the
# optional ones the stack always injects). The stack must inject each of these
# and must NOT inject the retired execution-mode key.
REQUIRED_ENV_KEYS = frozenset(
    {
        "GBAW_OPERATIONS_MODE",
        "GBAW_OPERATIONS_TABLE_NAME",
        "GBAW_OPERATIONS_METRIC_NAMESPACE",
        "GBAW_OPERATIONS_TENANT_ID",
        "GBAW_OPERATIONS_WORKSPACE_ID",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE",
        "GBAW_OPERATIONS_STATE_MACHINE_ARN",
        "GBAW_OPERATIONS_CAPABILITY_MAXIMUM",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN",
        "GBAW_OPERATIONS_ENROLLED_LOCATION",
        "GBAW_OPERATIONS_APPROVER_GROUP",
        "GBAW_OPERATIONS_CAPACITY_FLOOR",
        "GBAW_OPERATIONS_CAPACITY_CEILING",
        "GBAW_OPERATIONS_CAPACITY_MAX_STEP",
    }
)

RETIRED_ENV_KEY = "GBAW_OPERATIONS_EXECUTION_MODE"


@pytest.fixture(scope="module")
def exec_template():
    assert EXEC_TEMPLATE.exists(), f"missing template: {EXEC_TEMPLATE}"
    return load_cfn_template(EXEC_TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def observe_template():
    assert OBSERVE_TEMPLATE.exists(), f"missing template: {OBSERVE_TEMPLATE}"
    return load_cfn_template(OBSERVE_TEMPLATE.read_text(encoding="utf-8"))


def _functions(template):
    return {n: b for n, b in template["Resources"].items() if b.get("Type") == "AWS::Lambda::Function"}


def _find_function(template, needle):
    for name, body in _functions(template).items():
        if needle.lower() in name.lower():
            return body
    raise AssertionError(f"no Lambda function matching {needle!r}")


def _env(template, needle):
    return _find_function(template, needle)["Properties"]["Environment"]["Variables"]


# --------------------------------------------------------------------------- #
# Both Lambdas inject exactly the core contract; the retired key is gone.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("needle", ["executor", "dispatch"])
def test_lambda_env_matches_core_contract(exec_template, needle):
    env = _env(exec_template, needle)
    missing = REQUIRED_ENV_KEYS - set(env)
    assert not missing, f"{needle} Lambda missing required env keys: {sorted(missing)}"
    assert RETIRED_ENV_KEY not in env, f"{needle} Lambda must not inject the retired {RETIRED_ENV_KEY}"


@pytest.mark.parametrize("needle", ["executor", "dispatch"])
def test_mode_is_fed_the_execution_mode_kill_switch(exec_template, needle):
    env = _env(exec_template, needle)
    assert "ExecutionMode" in str(env["GBAW_OPERATIONS_MODE"]), "GBAW_OPERATIONS_MODE must key off ExecutionMode"


@pytest.mark.parametrize("needle", ["executor", "dispatch"])
def test_capability_maximum_is_remediate(exec_template, needle):
    env = _env(exec_template, needle)
    value = env["GBAW_OPERATIONS_CAPABILITY_MAXIMUM"]
    # v1 default is 'remediate'. The E5 (issue #440) ADDITIVE autonomy wiring
    # overlays the EXECUTOR's capability maximum with
    # !If[AutonomyOperate, operate, remediate] (flattened by the loader to a
    # list), so the v1/disabled branch stays 'remediate' and only an explicit
    # AutonomyMode=operate raises it. The dispatcher is unchanged (literal).
    if isinstance(value, list):
        assert needle == "executor", "only the executor carries the additive autonomy overlay"
        assert value[-2:] == [
            "operate",
            "remediate",
        ], f"executor capability maximum must be !If[AutonomyOperate, operate, remediate], got {value}"
    else:
        assert str(value) == "remediate"


@pytest.mark.parametrize("needle", ["executor", "dispatch"])
def test_approver_group_is_admin(exec_template, needle):
    env = _env(exec_template, needle)
    assert str(env["GBAW_OPERATIONS_APPROVER_GROUP"]) == "admin"


@pytest.mark.parametrize("needle", ["executor", "dispatch"])
def test_capacity_bounds_are_safe_defaults(exec_template, needle):
    env = _env(exec_template, needle)
    assert str(env["GBAW_OPERATIONS_CAPACITY_FLOOR"]) == "0"
    assert str(env["GBAW_OPERATIONS_CAPACITY_CEILING"]) == "1"
    assert str(env["GBAW_OPERATIONS_CAPACITY_MAX_STEP"]) == "1"


@pytest.mark.parametrize("needle", ["executor", "dispatch"])
def test_enrolled_fleet_arn_is_constructed_exactly(exec_template, needle):
    """The enrolled fleet ARN must be a constructed exact fleet ARN (never taken
    from the operation payload)."""
    env = _env(exec_template, needle)
    arn = str(env["GBAW_OPERATIONS_ENROLLED_FLEET_ARN"])
    assert "gamelift" in arn and "fleet/" in arn and "EnrolledFleetId" in arn


# --------------------------------------------------------------------------- #
# The validated new parameters exist with fail-closed patterns.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("param", ["TenantId", "WorkspaceId", "TrustedAudience", "EnrolledLocation"])
def test_new_parameters_present(exec_template, param):
    assert param in exec_template["Parameters"], f"missing parameter {param}"


@pytest.mark.parametrize("param", ["TenantId", "WorkspaceId", "EnrolledLocation"])
def test_identity_parameters_are_pattern_validated(exec_template, param):
    assert "AllowedPattern" in exec_template["Parameters"][param], f"{param} must be pattern-validated"


def test_trusted_audience_defaults_to_client_id(exec_template):
    """When TrustedAudience is empty the injected value falls back to the JWT
    audience (CognitoClientId) via a condition."""
    assert "HasExplicitTrustedAudience" in exec_template.get("Conditions", {})


# --------------------------------------------------------------------------- #
# The injected contract actually resolves through the REAL core resolver.
# --------------------------------------------------------------------------- #
def test_injected_contract_resolves_through_core(monkeypatch):
    """A concrete instantiation of the stack's env keys resolves and enables
    execution through the real resolver — the CFN contract and code agree."""
    for key in [k for k in os.environ if k.startswith("GBAW_OPERATIONS_")]:
        monkeypatch.delenv(key, raising=False)
    fleet_id = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
    injected = {
        "GBAW_OPERATIONS_MODE": "remediate",
        "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
        "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
        "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "client-abc",
        "GBAW_OPERATIONS_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:123456789012:stateMachine:game-agent-operations-execution"
        ),
        "GBAW_OPERATIONS_CAPABILITY_MAXIMUM": "remediate",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": fleet_id,
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": f"arn:aws:gamelift:us-west-2:123456789012:fleet/{fleet_id}",
        "GBAW_OPERATIONS_ENROLLED_LOCATION": "us-west-2",
        "GBAW_OPERATIONS_APPROVER_GROUP": "admin",
        "GBAW_OPERATIONS_CAPACITY_FLOOR": "0",
        "GBAW_OPERATIONS_CAPACITY_CEILING": "1",
        "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "1",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    settings = resolve_executor_deployment_settings()
    assert settings.execute_enabled
    assert settings.capability_maximum == "remediate"
    assert settings.admin_group == "admin"
    assert settings.enrolled_fleet_arn.startswith("arn:aws:gamelift:")
    assert settings.observation.operations.capacity_ceiling == 1


def test_disabled_mode_fails_closed_through_core(monkeypatch):
    for key in [k for k in os.environ if k.startswith("GBAW_OPERATIONS_")]:
        monkeypatch.delenv(key, raising=False)
    fleet_id = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
    for key, value in {
        "GBAW_OPERATIONS_MODE": "disabled",
        "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
        "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
        "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "client-abc",
        "GBAW_OPERATIONS_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:123456789012:stateMachine:game-agent-operations-execution"
        ),
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": fleet_id,
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": f"arn:aws:gamelift:us-west-2:123456789012:fleet/{fleet_id}",
        "GBAW_OPERATIONS_ENROLLED_LOCATION": "us-west-2",
    }.items():
        monkeypatch.setenv(key, value)
    settings = resolve_executor_deployment_settings()
    assert not settings.execute_enabled, "disabled mode must fail closed (no provider write)"


# --------------------------------------------------------------------------- #
# Cross-stack CMK / access-log wiring introduced by the alignment.
# --------------------------------------------------------------------------- #
def _log_groups(template):
    return {n: b for n, b in template["Resources"].items() if b.get("Type") == "AWS::Logs::LogGroup"}


def _log_group_by_suffix(template, suffix):
    for name, body in _log_groups(template).items():
        if str(body["Properties"].get("LogGroupName", "")).endswith(suffix):
            return name, body
    raise AssertionError(f"no log group ending in {suffix!r}")


def test_e3_lambda_and_access_log_groups_are_cmk_encrypted(exec_template):
    for suffix in (
        "operations-dispatch",
        "operations-executor",
        "operations-dispatch-access",
    ):
        _name, body = _log_group_by_suffix(exec_template, suffix)
        assert "KmsKeyId" in body["Properties"], f"{suffix} log group must be CMK-encrypted"


def test_e3_stateful_log_resources_retain_except_on_create(exec_template):
    for suffix in (
        "operations-dispatch",
        "operations-executor",
        "operations-execution",
        "operations-dispatch-access",
    ):
        _name, body = _log_group_by_suffix(exec_template, suffix)
        assert body.get("DeletionPolicy") == "RetainExceptOnCreate", f"{suffix} must use RetainExceptOnCreate"


def test_dispatch_access_log_group_has_delivery_resource_policy(exec_template):
    policies = {n: b for n, b in exec_template["Resources"].items() if b.get("Type") == "AWS::Logs::ResourcePolicy"}
    assert policies, "the dispatch access log group needs a vended-log delivery resource policy"
    text = EXEC_TEMPLATE.read_text(encoding="utf-8")
    assert "delivery.logs.amazonaws.com" in text
    assert "operations-dispatch-access" in text


def test_06_cmk_allowlist_covers_the_three_e3_log_groups(observe_template):
    """The 06 CMK key policy must allowlist the three exact E3 log-group ARNs by
    encryption context while preserving the E1/E2 groups."""
    text = OBSERVE_TEMPLATE.read_text(encoding="utf-8")
    for group in (
        "operations-dispatch",
        "operations-executor",
        "operations-dispatch-access",
    ):
        assert group in text, f"06 CMK allowlist must include the E3 {group} log group"
    # E1/E2 groups preserved.
    assert "operations-observe" in text
    assert "operations-access" in text
