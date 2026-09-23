"""Composed E5 tests spanning the 09 template and the #439 runtime (#440, Finding 11).

These tests cross the seam that unit tests on either side alone cannot: they take
the ACTUAL artifacts the 09 template ships (the injected evaluator env, the
scheduled rule's closed-event Input) and drive them through the REAL #439 runtime
resolvers/validators, so template<->runtime drift fails closed here.
"""

from __future__ import annotations

# Standard library
import json
import pathlib
import re

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/09-operations-autonomy.yaml"


@pytest.fixture(scope="module")
def template() -> dict:
    return load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))


def _rule_input_sub(template: dict) -> tuple[str, dict]:
    """Return the (format_string, variable_map) of the rule's !Sub Input."""
    rule = template["Resources"]["EvaluationRule"]["Properties"]
    sub = rule["Targets"][0]["Input"]
    # The loader flattens !Sub [tpl, vars] to a list [tpl, {vars}].
    assert isinstance(sub, list) and len(sub) == 2, f"rule Input must be a !Sub list, got {sub!r}"
    fmt, variables = sub
    return fmt, variables


def test_scheduled_rule_input_is_the_closed_439_event_the_runtime_accepts(template: dict) -> None:
    """The rendered scheduled event must satisfy the runtime's closed-event
    contract (exactly observation_operation_id + desired/minimum/maximum, all
    non-negative integers)."""
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import AutonomyModuleRuntime

    fmt, _variables = _rule_input_sub(template)
    # Render with concrete server-owned values (as CloudFormation would).
    rendered = fmt
    for key, value in {
        "OpId": "op_" + "a" * 26,
        "Desired": "1",
        "Minimum": "0",
        "Maximum": "1",
    }.items():
        rendered = rendered.replace("${" + key + "}", value)
    event = json.loads(rendered)
    # The exact closed contract the runtime enforces.
    op_id, requested = AutonomyModuleRuntime._read_closed_event(event)
    assert op_id == "op_" + "a" * 26
    assert requested == {"desired": 1, "minimum": 0, "maximum": 1}


def test_scheduled_rule_input_rejects_a_synthetic_event(template: dict) -> None:
    """Sanity: the OLD synthetic {"source": ...} payload would be rejected by the
    runtime, proving Finding 6's payload really had to change."""
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import AutonomyModuleRuntime

    with pytest.raises(ValueError):
        AutonomyModuleRuntime._read_closed_event({"source": "autonomy-evaluation"})


def test_template_env_matches_runtime_required_keys(template: dict) -> None:
    """Every autonomy env key the runtime resolver requires is injected by the
    template's evaluator function (a live cross-check, not a hardcoded list)."""
    # Local modules
    from operations.autonomy_runtime import evaluator_entry

    injected = set(template["Resources"]["EvaluatorFunction"]["Properties"]["Environment"]["Variables"].keys())
    # The resolver reads these module-level required keys; all must be injected.
    required = {
        v
        for name, v in vars(evaluator_entry).items()
        if name.endswith("_KEY") and isinstance(v, str) and v.startswith("GBAW_OPERATIONS_")
    }
    missing = required - injected
    assert not missing, f"template must inject every runtime-required autonomy env key: {sorted(missing)}"
