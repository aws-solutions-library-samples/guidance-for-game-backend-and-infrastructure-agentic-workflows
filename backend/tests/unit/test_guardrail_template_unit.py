"""Contract tests for the Bedrock Guardrail topic policy (04-bedrock-guardrails.yaml).

Scope topics keep users on-topic, so they are enforced on input. On output the
topic classifier mislabels the assistant's own in-scope infrastructure answers
(kubectl, GameLift Server SDK steps, CloudFormation comments) and truncated
generated templates, so scope topics are detect-only there. Every other
protection must stay enforced on output.
"""

# Standard library
from pathlib import Path

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[3] / "infrastructure" / "cloudformation" / "04-bedrock-guardrails.yaml"
SCOPE_TOPICS = {"General Programming Help", "Entertainment and Casual Chat"}


def _guardrail_props() -> dict:
    template = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))
    return template["Resources"]["GameAgentGuardrail"]["Properties"]


def _topics() -> dict:
    return {t["Name"]: t for t in _guardrail_props()["TopicPolicyConfig"]["TopicsConfig"]}


@pytest.mark.parametrize("name", sorted(SCOPE_TOPICS))
def test_scope_topics_block_input_and_only_detect_output(name):
    topic = _topics()[name]
    assert topic["Type"] == "DENY"
    assert topic["InputEnabled"] is True
    assert topic["InputAction"] == "BLOCK"
    assert topic["OutputEnabled"] is True
    assert topic["OutputAction"] == "NONE"


def test_non_scope_topics_still_block_output():
    for name, topic in _topics().items():
        if name in SCOPE_TOPICS:
            continue
        # Absent actions default to BLOCK on both input and output.
        assert topic.get("InputAction", "BLOCK") == "BLOCK", name
        assert topic.get("OutputAction", "BLOCK") == "BLOCK", name
        assert topic.get("OutputEnabled", True) is True, name


def test_other_output_protections_remain_enforced():
    props = _guardrail_props()
    filters = {f["Type"]: f for f in props["ContentPolicyConfig"]["FiltersConfig"]}
    for kind in ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"):
        assert filters[kind]["OutputStrength"] == "HIGH", kind
    assert filters["PROMPT_ATTACK"]["InputStrength"] == "HIGH"
    pii = {e["Type"]: e["Action"] for e in props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    for kind in ("AWS_ACCESS_KEY", "AWS_SECRET_KEY", "PASSWORD", "CREDIT_DEBIT_CARD_NUMBER"):
        assert pii[kind] == "BLOCK", kind
    assert props["WordPolicyConfig"]["ManagedWordListsConfig"] == [{"Type": "PROFANITY"}]
