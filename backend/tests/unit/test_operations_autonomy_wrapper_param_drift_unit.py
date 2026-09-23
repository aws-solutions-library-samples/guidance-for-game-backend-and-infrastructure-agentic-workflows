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


def _declared_parameters() -> set[str]:
    template = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))
    return set(template["Parameters"].keys())


def _wrapper_parameter_keys() -> set[str]:
    """Extract the ParameterKeys the wrapper passes via --parameter-overrides."""
    text = DEPLOY.read_text(encoding="utf-8")
    # Each override line looks like:  "SomeKey=$SOME_VAR" \
    keys: set[str] = set()
    for match in re.finditer(r'"([A-Za-z][A-Za-z0-9]*)=\$', text):
        keys.add(match.group(1))
    return keys


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
