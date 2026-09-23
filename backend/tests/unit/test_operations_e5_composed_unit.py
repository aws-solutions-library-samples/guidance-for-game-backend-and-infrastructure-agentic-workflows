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


# --------------------------------------------------------------------------- #
# Finding 10: docs / cost / retention must agree with the template and model.
# --------------------------------------------------------------------------- #
RUNBOOK = PROJECT_ROOT / "docs/OPERATIONS_E5_AUTONOMY.md"
COST_MODEL = PROJECT_ROOT / "docs/operations-e5-cost-model.json"
COST_NOTES = PROJECT_ROOT / "docs/operations-e5-cost-notes.md"


def test_runbook_cost_figures_match_the_cost_model() -> None:
    """The runbook's provisioned-disabled / enabled-idle figures must equal the
    machine-checked cost model scenarios."""
    # Standard library
    import json as _json

    model = _json.loads(COST_MODEL.read_text(encoding="utf-8"))
    runbook = RUNBOOK.read_text(encoding="utf-8")
    disabled = model["scenarios"]["provisioned_disabled"]["monthly_total_usd"]
    idle = model["scenarios"]["enabled_idle"]["monthly_total_usd"]
    assert (
        f"${disabled:.2f}/mo" in runbook or f"${disabled}" in runbook
    ), f"runbook must cite the provisioned-disabled cost ${disabled}"
    assert f"${idle:.2f}/mo" in runbook or f"${idle}" in runbook, f"runbook must cite the enabled-idle cost ${idle}"


def test_runbook_deploy_example_includes_required_bindings() -> None:
    """Finding 10: the runbook's enable example must include the now-required
    fleet ARN and trusted audience env vars (which the wrapper enforces)."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "GBAW_OPERATIONS_ENROLLED_FLEET_ARN" in runbook
    assert "GBAW_OPERATIONS_TRUSTED_AUDIENCE" in runbook


def test_cost_notes_alarm_count_matches_model(template: dict) -> None:
    """Finding 10: the cost notes and model must both reflect the template's
    actual alarm count."""
    # Standard library
    import json as _json

    model = _json.loads(COST_MODEL.read_text(encoding="utf-8"))
    alarms = [name for name, body in template["Resources"].items() if body.get("Type") == "AWS::CloudWatch::Alarm"]
    assert len(model["alarms"]["e5_owned"]) == len(alarms)
    notes = COST_NOTES.read_text(encoding="utf-8")
    # The notes must not claim an out-of-date "two"/"three" alarm count.
    assert "two E5-owned alarms" not in notes
    assert "three E5-owned alarms" not in notes


def test_09_defines_no_state_machine_of_its_own(template: dict) -> None:
    """Finding 8: E5 must reuse the EXACT 07 STANDARD workflow, never define its
    own (even a syntax-only) state machine. The evaluator only StartExecutions
    the 07 ARN."""
    own = [n for n, b in template["Resources"].items() if b.get("Type") == "AWS::StepFunctions::StateMachine"]
    assert not own, f"09 must not define its own state machine, found: {own}"
