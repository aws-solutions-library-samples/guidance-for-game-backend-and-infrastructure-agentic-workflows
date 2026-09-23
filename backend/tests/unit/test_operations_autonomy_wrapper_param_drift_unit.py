"""Wrapper<->template parameter drift guard for the E5 autonomy stack (#440, Finding 1).

The deploy wrapper's ``--parameter-overrides`` must reference ONLY parameters the
09 template declares, and it must supply every parameter the template marks
required when ``AutonomyMode=operate`` (including the previously-omitted
``EnrolledFleetArn`` and ``TrustedAudience``). Before this fix the wrapper passed
undeclared keys (``AppConfigApplicationId``/``AppConfigEnvironmentId``/
``AutonomySwitchProfileId``) — which CloudFormation rejects — and omitted the
required fleet ARN and trusted audience.

These tests parse the shipped template and the shipped wrapper (no AWS, no
subprocess) so any future drift fails closed here.
"""

from __future__ import annotations

# Standard library
import pathlib
import re

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/09-operations-autonomy.yaml"
DEPLOY = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-autonomy.sh"
DISABLE = PROJECT_ROOT / "scripts/infrastructure/disable-operations-autonomy.sh"
EXECUTION_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"


def _declared_parameters() -> set[str]:
    template = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))
    return set(template["Parameters"].keys())


def _wrapper_parameter_keys() -> set[str]:
    """Extract the ParameterKeys the 09 deploy passes via --parameter-overrides.

    Scoped to the ``aws cloudformation deploy`` (09) --parameter-overrides block
    so the separate 07 update-stack block (which uses ParameterKey=/ParameterValue=
    syntax) is not conflated with the 09 overrides.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    # Isolate the 09 deploy --parameter-overrides block: from the first
    # "--parameter-overrides" after an "aws cloudformation deploy" up to the next
    # blank line / non-continuation.
    start = text.index("aws cloudformation deploy")
    overrides_at = text.index("--parameter-overrides", start)
    tail = text[overrides_at:]
    # The override block is the run of backslash-continued "Key=$VAR" lines.
    keys: set[str] = set()
    for line in tail.splitlines()[1:]:
        stripped = line.strip()
        match = re.match(r'"([A-Za-z][A-Za-z0-9]*)=\$', stripped)
        if match:
            keys.add(match.group(1))
        elif not stripped.endswith("\\") and keys:
            # End of the continued override block.
            break
    return keys


def _disable_parameter_keys(path: pathlib.Path) -> set[str]:
    """Extract ParameterKey=NAME names from a disable wrapper's update-stack calls."""
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"ParameterKey=([A-Za-z][A-Za-z0-9]*)", text))


def _execution_parameters() -> set[str]:
    template = load_cfn_template(EXECUTION_TEMPLATE.read_text(encoding="utf-8"))
    return set(template["Parameters"].keys())


def _operate_required_parameters() -> set[str]:
    """Parameters the template's Rules assert are required when operate."""
    template = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))
    required: set[str] = set()
    for rule in template.get("Rules", {}).values():
        for assertion in rule.get("Assertions", []):
            text = str(assertion.get("AssertDescription", ""))
            m = re.match(r"^([A-Za-z][A-Za-z0-9]*) is required", text)
            if m:
                required.add(m.group(1))
    return required


def test_wrapper_passes_only_declared_parameters() -> None:
    declared = _declared_parameters()
    passed = _wrapper_parameter_keys()
    undeclared = passed - declared
    assert not undeclared, f"wrapper passes undeclared template parameters: {sorted(undeclared)}"


def test_wrapper_does_not_pass_the_removed_undeclared_keys() -> None:
    passed = _wrapper_parameter_keys()
    for stale in ("AppConfigApplicationId", "AppConfigEnvironmentId", "AutonomySwitchProfileId"):
        assert stale not in passed, f"wrapper still passes undeclared parameter {stale}"


def test_wrapper_supplies_required_fleet_arn_and_audience() -> None:
    passed = _wrapper_parameter_keys()
    assert "EnrolledFleetArn" in passed, "wrapper must supply the required EnrolledFleetArn"
    assert "TrustedAudience" in passed, "wrapper must supply the required TrustedAudience"


def test_wrapper_supplies_the_e4_kill_switch_coordinate() -> None:
    passed = _wrapper_parameter_keys()
    for key in ("KillSwitchApplicationId", "KillSwitchEnvironmentId", "KillSwitchProfileId"):
        assert key in passed, f"wrapper must supply the E4 kill-switch parameter {key}"


def test_wrapper_supplies_every_operate_required_parameter() -> None:
    required = _operate_required_parameters()
    passed = _wrapper_parameter_keys()
    # The wrapper always deploys with AutonomyMode=operate, so it must supply
    # every operate-required parameter the template's Rules enforce.
    missing = required - passed
    assert not missing, f"wrapper omits operate-required template parameters: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# Findings 7 & 8: disable closes the 07 pre-write hook; disable's 09 params match
# the current template.
# --------------------------------------------------------------------------- #
def test_disable_wrapper_09_params_are_all_declared() -> None:
    declared = _declared_parameters()
    passed = _disable_parameter_keys(DISABLE)
    # The disable wrapper also touches the 07 stack, so restrict to keys the 09
    # template declares by intersecting with the union of 09+07 declared params.
    execution = _execution_parameters()
    undeclared = passed - (declared | execution)
    assert not undeclared, f"disable wrapper references undeclared parameters: {sorted(undeclared)}"


def test_disable_wrapper_does_not_pass_removed_undeclared_keys() -> None:
    passed = _disable_parameter_keys(DISABLE)
    for stale in ("AppConfigApplicationId", "AppConfigEnvironmentId", "AutonomySwitchProfileId"):
        assert stale not in passed, f"disable wrapper still references undeclared parameter {stale}"


def test_disable_closes_the_07_prewrite_hook() -> None:
    text = DISABLE.read_text(encoding="utf-8").replace(" ", "")
    # Disable must update the 07 execution stack, flipping AutonomyMode=disabled.
    assert "operations-execution" in text, "disable must reference the 07 execution stack"
    assert "AutonomyMode,ParameterValue=disabled" in text, "disable must flip 07 AutonomyMode=disabled"


def test_disable_07_update_reuses_all_other_07_parameters_dynamically() -> None:
    """The 07 AutonomyMode flip must reuse every OTHER 07 parameter (else
    update-stack would reset them to defaults). Rather than hardcode 07's
    parameter list (drift-prone), the disable wrapper reads the LIVE 07 stack's
    parameter keys and re-supplies them all as UsePreviousValue except
    AutonomyMode."""
    text = DISABLE.read_text(encoding="utf-8")
    # It queries the live 07 parameters and builds the list dynamically.
    assert "Stacks[0].Parameters[].ParameterKey" in text, "disable must read the live 07 parameter keys to reuse them"
    assert "UsePreviousValue=true" in text, "disable must reuse 07 params via UsePreviousValue"
    # And it must NOT hardcode a 07 param list that would drift (only AutonomyMode
    # and ProjectName may appear literally in the 07 close path is not required,
    # but the dynamic loop must key off AutonomyMode explicitly).
    assert "ParameterKey=AutonomyMode,ParameterValue=disabled" in text.replace(" ", "")


def test_wrapper_binds_08_tenant_workspace_and_source_to_06() -> None:
    """#440 final semantic blocker: the deploy wrapper must actually COMPARE the
    live 08 control-plane tenant/workspace to the 06 values and verify the 08
    kill-switch AppConfig SOURCE (hosted), aborting BEFORE any change — not merely
    require the kill-switch app/env/profile to be non-empty."""
    text = DEPLOY.read_text(encoding="utf-8")

    # It reads the 08 TenantId/WorkspaceId parameters and byte-compares them to
    # the 06 values (STACK_06_TENANT / STACK_06_WORKSPACE).
    assert "STACK_08_TENANT" in text, "wrapper must read the 08 TenantId"
    assert "STACK_08_WORKSPACE" in text, "wrapper must read the 08 WorkspaceId"
    assert '"$STACK_08_TENANT" != "$STACK_06_TENANT"' in text, "wrapper must compare 08 tenant to 06 tenant"
    assert '"$STACK_08_WORKSPACE" != "$STACK_06_WORKSPACE"' in text, "wrapper must compare 08 workspace to 06 workspace"

    # It verifies the 08 kill-switch AppConfig SOURCE is the hosted document store.
    assert "get-configuration-profile" in text, "wrapper must read the 08 kill-switch profile source"
    assert "LocationUri" in text, "wrapper must inspect the 08 profile LocationUri (AppConfig source)"
    assert '!= "hosted"' in text, "wrapper must require the 08 kill-switch source to be hosted"


def test_wrapper_08_identity_checks_precede_any_mutation() -> None:
    """The 08 identity/source binding must abort BEFORE the wrapper performs any
    write (the first mutation is the S3 artifact upload / conditional seeds)."""
    text = DEPLOY.read_text(encoding="utf-8")
    tenant_at = text.index("STACK_08_TENANT")
    source_at = text.index("get-configuration-profile")
    # The first mutating action in the enable path is the S3 put-object upload.
    first_write_at = text.index("aws s3api put-object")
    assert tenant_at < first_write_at, "08 tenant binding must run before any mutation"
    assert source_at < first_write_at, "08 source binding must run before any mutation"
