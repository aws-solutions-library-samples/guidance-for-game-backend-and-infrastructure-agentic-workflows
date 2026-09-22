"""Contract tests for the ADDITIVE E3 (#415) extension of the accepted 06
observation/advise stack.

E3 extends 06 ONLY for the safe-mode ``remediate`` vocabulary and the necessary
cross-stack outputs that the separate 07 execution stack consumes. It must NOT
grant the 06 handler any execution authority: no ``states:`` action, no
``lambda:InvokeFunction``, no GameLift write. E2 (advise) stays intact.

These tests never call AWS; they parse the 06 template as data.
"""

# Standard library
import pathlib

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"


@pytest.fixture(scope="module")
def template():
    assert TEMPLATE.exists(), f"missing template: {TEMPLATE}"
    return load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))


def _iter_action_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _iter_action_strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_action_strings(value)


def _all_actions(template):
    actions = []
    for name, body in template["Resources"].items():
        if body.get("Type") not in ("AWS::IAM::Role", "AWS::IAM::Policy"):
            continue
        props = body.get("Properties", {})
        docs = [inline.get("PolicyDocument", {}) for inline in props.get("Policies", []) or []]
        if "PolicyDocument" in props:
            docs.append(props["PolicyDocument"])
        for doc in docs:
            for statement in doc.get("Statement", []) or []:
                for key in ("Action", "NotAction"):
                    if key in statement:
                        actions.extend(_iter_action_strings(statement[key]))
    return actions


# --------------------------------------------------------------------------- #
# Safe-mode vocabulary grows to include remediate (E1 observe + E2 advise kept)
# --------------------------------------------------------------------------- #
def test_operations_mode_vocabulary_includes_remediate(template):
    mode = template["Parameters"]["OperationsMode"]
    assert mode["Default"] == "disabled", "default must remain fail-closed disabled"
    assert set(mode["AllowedValues"]) == {"disabled", "observe", "advise", "remediate"}


def test_advise_and_observe_are_retained(template):
    mode = template["Parameters"]["OperationsMode"]
    assert "observe" in mode["AllowedValues"]
    assert "advise" in mode["AllowedValues"]


# --------------------------------------------------------------------------- #
# Necessary cross-stack output: the operations CMK ARN 07 consumes
# --------------------------------------------------------------------------- #
def test_kms_key_arn_is_exported_for_cross_stack_reuse(template):
    outputs = template.get("Outputs", {})
    kms_outputs = [name for name, body in outputs.items() if "kms" in name.lower() and "Export" in body]
    assert kms_outputs, "06 must export the operations CMK ARN for the 07 execution stack to reuse"


def test_table_name_export_is_retained(template):
    outputs = template.get("Outputs", {})
    assert any(
        "TableName" in body.get("Export", {}).get("Name", "")
        for body in outputs.values()
        if isinstance(body.get("Export"), dict)
    ) or any(
        "table" in name.lower() and "Export" in body for name, body in outputs.items()
    ), "the operations table name export must be retained for cross-stack reuse"


# --------------------------------------------------------------------------- #
# The 06 extension grants NO execution authority
# --------------------------------------------------------------------------- #
def test_06_grants_no_states_or_execute_surface(template):
    for action in _all_actions(template):
        assert not action.lower().startswith("states:"), f"06 must hold no states: action, got {action}"
        assert action != "lambda:InvokeFunction", "06 must not invoke the executor"


def test_06_gamelift_stays_read_only(template):
    gamelift = {a for a in _all_actions(template) if a.lower().startswith("gamelift:")}
    for a in gamelift:
        assert a.startswith("gamelift:Describe"), f"06 GameLift must stay read-only, got {a}"
