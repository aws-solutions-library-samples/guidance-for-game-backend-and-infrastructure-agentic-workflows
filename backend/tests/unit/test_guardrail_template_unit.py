"""Contract tests for the Bedrock Guardrail policy (04-bedrock-guardrails.yaml).

Scope topics keep users on-topic, so they are enforced on input. On output the
topic classifier mislabels the assistant's own in-scope infrastructure answers
(kubectl, GameLift Server SDK steps, CloudFormation comments) and truncated
generated templates, so the two scope topics are detect-only there. Personal
Advice, the harmful-content filters, PII, custom regexes, and the managed
profanity list stay enforced on input and output. Prompt-attack detection
applies to input only (Bedrock evaluates it on user input).

Every policy block is pinned to an expected literal, so any weakening, not just
a revert of the scope-topic change, fails here.
"""

# Standard library
import json
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[3] / "infrastructure" / "cloudformation" / "04-bedrock-guardrails.yaml"
CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "guardrail_eval_corpus.json"
SCOPE_TOPICS = {"General Programming Help", "Entertainment and Casual Chat"}

EXPECTED_TOPIC_ACTIONS = {
    # name: (input enabled, input action, output enabled, output action); None = service default (BLOCK, enabled)
    "Personal Advice": (None, None, None, None),
    "General Programming Help": (True, "BLOCK", True, "NONE"),
    "Entertainment and Casual Chat": (True, "BLOCK", True, "NONE"),
}
EXPECTED_CONTENT_FILTERS = [
    {"Type": "HATE", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
    {"Type": "INSULTS", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
    {"Type": "SEXUAL", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
    {"Type": "VIOLENCE", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
    {"Type": "MISCONDUCT", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
    {"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"},
]
EXPECTED_PII_ENTITIES = [
    {"Type": "EMAIL", "Action": "ANONYMIZE"},
    {"Type": "PHONE", "Action": "ANONYMIZE"},
    {"Type": "CREDIT_DEBIT_CARD_NUMBER", "Action": "BLOCK"},
    {"Type": "US_SOCIAL_SECURITY_NUMBER", "Action": "BLOCK"},
    {"Type": "IP_ADDRESS", "Action": "ANONYMIZE"},
    {"Type": "AWS_ACCESS_KEY", "Action": "BLOCK"},
    {"Type": "AWS_SECRET_KEY", "Action": "BLOCK"},
    {"Type": "PASSWORD", "Action": "BLOCK"},
    {"Type": "USERNAME", "Action": "ANONYMIZE"},
]
EXPECTED_REGEXES = [
    {
        "Name": "AWS Account ID",
        "Description": "Block AWS account IDs from being exposed",
        "Pattern": r"\b\d{12}\b",
        "Action": "ANONYMIZE",
    },
    {
        "Name": "Internal Hostnames",
        "Description": "Block internal AWS hostnames",
        "Pattern": r"\b[a-z0-9-]+\.internal\b",
        "Action": "ANONYMIZE",
    },
    {
        "Name": "Kubernetes Secrets",
        "Description": "Block potential Kubernetes secret references",
        "Pattern": r"kubectl\s+get\s+secret|base64\s+-d",
        "Action": "BLOCK",
    },
]


def _guardrail_props() -> dict[str, Any]:
    template = load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))
    return template["Resources"]["GameAgentGuardrail"]["Properties"]


def _topics() -> dict[str, dict[str, Any]]:
    return {t["Name"]: t for t in _guardrail_props()["TopicPolicyConfig"]["TopicsConfig"]}


def test_topic_set_and_actions_are_pinned():
    topics = _topics()
    assert set(topics) == set(EXPECTED_TOPIC_ACTIONS)
    for name, (in_enabled, in_action, out_enabled, out_action) in EXPECTED_TOPIC_ACTIONS.items():
        topic = topics[name]
        assert topic["Type"] == "DENY", name
        assert topic.get("InputEnabled") == in_enabled, name
        assert topic.get("InputAction") == in_action, name
        assert topic.get("OutputEnabled") == out_enabled, name
        assert topic.get("OutputAction") == out_action, name


def test_content_filters_are_pinned():
    props = _guardrail_props()["ContentPolicyConfig"]["FiltersConfig"]
    assert props == EXPECTED_CONTENT_FILTERS


def test_sensitive_information_policy_is_pinned():
    props = _guardrail_props()["SensitiveInformationPolicyConfig"]
    assert props["PiiEntitiesConfig"] == EXPECTED_PII_ENTITIES
    assert props["RegexesConfig"] == EXPECTED_REGEXES
    assert set(props) == {"PiiEntitiesConfig", "RegexesConfig"}


def test_word_policy_is_pinned():
    assert _guardrail_props()["WordPolicyConfig"] == {"ManagedWordListsConfig": [{"Type": "PROFANITY"}]}


def _walk(node: Any, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")
    else:
        yield path, node


def test_no_protection_is_disabled_except_scope_topic_output_and_prompt_attack_output():
    """Belt and braces: no NONE action or false flag anywhere except the two reviewed exceptions."""
    props = _guardrail_props()
    allowed = set()
    for index, topic in enumerate(props["TopicPolicyConfig"]["TopicsConfig"]):
        if topic["Name"] in SCOPE_TOPICS:
            allowed.add(f"TopicPolicyConfig.TopicsConfig[{index}].OutputAction")
    for index, item in enumerate(props["ContentPolicyConfig"]["FiltersConfig"]):
        if item["Type"] == "PROMPT_ATTACK":
            allowed.add(f"ContentPolicyConfig.FiltersConfig[{index}].OutputStrength")
    for path, value in _walk(props):
        if value == "NONE" or value is False:
            assert path in allowed, f"{path} disables a protection"


def test_blocked_messages_are_present():
    props = _guardrail_props()
    assert props["BlockedInputMessaging"].startswith("I can only help with AWS GameLift")
    assert props["BlockedOutputsMessaging"].startswith("I cannot provide that information")


# ---------------------------------------------------------------------------
# Evaluation corpus (run against a deployed Guardrail by
# tests/integration/test_guardrail_corpus_cloud.py)
# ---------------------------------------------------------------------------

_EFFECTS = {"pass", "detect", "anonymize", "block"}


def _corpus() -> dict[str, Any]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def test_corpus_rows_are_well_formed():
    corpus = _corpus()
    assert corpus["version"]
    ids = [row["id"] for row in corpus["rows"]]
    assert len(ids) == len(set(ids))
    for row in corpus["rows"]:
        assert row["source"] in {"INPUT", "OUTPUT"}, row["id"]
        effects = row.get("effects") or [row["effect"]]
        assert set(effects) <= _EFFECTS and effects, row["id"]
        assert row["text"].strip(), row["id"]


def test_corpus_covers_the_review_matrix():
    rows = _corpus()["rows"]
    covered = {(row["source"], row["category"]) for row in rows}
    required = {
        ("INPUT", "in_scope"),
        ("INPUT", "off_topic"),
        ("INPUT", "personal_advice"),
        ("INPUT", "prompt_attack"),
        ("INPUT", "harmful"),
        ("INPUT", "credential"),
        ("OUTPUT", "valid_infrastructure"),
        ("OUTPUT", "off_topic"),
        ("OUTPUT", "personal_advice"),
        ("OUTPUT", "harmful"),
        ("OUTPUT", "credential"),
        ("OUTPUT", "regex_account_id"),
        ("OUTPUT", "regex_internal_hostname"),
        ("OUTPUT", "regex_k8s_secret"),
    }
    assert required <= covered, sorted(required - covered)
    # Off-topic output is detect-only; valid infrastructure output is never blocked.
    for row in rows:
        effects = set(row.get("effects") or [row["effect"]])
        if row["source"] == "OUTPUT" and row["category"] in {"valid_infrastructure", "off_topic"}:
            assert "block" not in effects, row["id"]


def test_corpus_uses_only_synthetic_identifiers():
    text = CORPUS.read_text(encoding="utf-8")
    assert "123456789012" in text  # the synthetic account ID
    for value in ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"):
        assert value in text  # AWS documentation example credentials only
