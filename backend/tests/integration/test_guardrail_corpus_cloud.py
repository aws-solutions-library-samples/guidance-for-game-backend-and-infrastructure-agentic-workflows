"""Run the Guardrail evaluation corpus through ApplyGuardrail (deployed Guardrail required).

Usage (from backend/):
    AWS_REGION=<guardrail region> GBAW_BEDROCK_GUARDRAIL_ID=<id> GBAW_BEDROCK_GUARDRAIL_VERSION=DRAFT \
        uv run pytest tests/integration/test_guardrail_corpus_cloud.py -m cloud

Set the Guardrail ID explicitly: ``./test-cloud.sh`` does not export
``GBAW_BEDROCK_GUARDRAIL_ID``, so every row is skipped there. The Region
defaults to ``us-west-2``, the repository default.

Each row's observed effect is derived only from the ApplyGuardrail response:
``pass`` (no intervention, no detection), ``detect`` (a policy matched with
action NONE), ``anonymize`` (intervened, text returned with masked values), or
``block`` (intervened, text replaced by the configured blocked message). The
row's ``policies`` must all appear in the assessment, so a row passes only if
the expected policy produced the effect. A ``detect`` may come only from a scope
topic, the only policies with output action NONE. Output is limited to row IDs,
effects, and policy names, never the corpus text.
"""

# Standard library
import json
import os
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.cloud]

CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "guardrail_eval_corpus.json"
SCOPE_TOPICS = {"topic:General Programming Help", "topic:Entertainment and Casual Chat"}


def load_corpus() -> dict[str, Any]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


BLOCKED_PREFIXES = tuple(load_corpus()["blocked_prefixes"].values())


def _policies(assessments: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Return (policies that acted, policies that only detected)."""
    acted: set[str] = set()
    detected: set[str] = set()
    for assessment in assessments:
        for topic in assessment.get("topicPolicy", {}).get("topics", []):
            (acted if topic.get("action") == "BLOCKED" else detected).add(f"topic:{topic.get('name')}")
        for item in assessment.get("contentPolicy", {}).get("filters", []):
            (acted if item.get("action") == "BLOCKED" else detected).add(f"content:{item.get('type')}")
        sensitive = assessment.get("sensitiveInformationPolicy", {})
        for item in sensitive.get("piiEntities", []):
            (acted if item.get("action") in {"BLOCKED", "ANONYMIZED"} else detected).add(f"pii:{item.get('type')}")
        for item in sensitive.get("regexes", []):
            (acted if item.get("action") in {"BLOCKED", "ANONYMIZED"} else detected).add(f"regex:{item.get('name')}")
        for item in assessment.get("wordPolicy", {}).get("managedWordLists", []):
            (acted if item.get("action") == "BLOCKED" else detected).add(f"word:{item.get('type')}")
    return acted, detected


def observed_effect(response: dict[str, Any]) -> tuple[str, set[str], set[str]]:
    """Return (effect, policies that acted, policies that only detected)."""
    acted, detected = _policies(response.get("assessments", []))
    if response.get("action") != "GUARDRAIL_INTERVENED":
        return ("detect" if detected else "pass"), acted, detected
    text = "".join(output.get("text", "") for output in response.get("outputs", []))
    effect = "block" if text.startswith(BLOCKED_PREFIXES) else "anonymize"
    return effect, acted, detected


@pytest.fixture(scope="module")
def guardrail():
    guardrail_id = os.environ.get("GBAW_BEDROCK_GUARDRAIL_ID")
    if not guardrail_id:
        pytest.skip("GBAW_BEDROCK_GUARDRAIL_ID is not set")
    # Third-party packages
    import boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
    client = boto3.client("bedrock-runtime", region_name=region)
    return client, guardrail_id, os.environ.get("GBAW_BEDROCK_GUARDRAIL_VERSION", "DRAFT")


@pytest.mark.parametrize("row", load_corpus()["rows"], ids=lambda row: row["id"])
def test_corpus_row_has_expected_effect(guardrail, row):
    client, guardrail_id, version = guardrail
    response = client.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=version,
        source=row["source"],
        content=[{"text": {"text": row["text"]}}],
    )
    effect, acted, detected = observed_effect(response)
    observed = sorted(acted | detected)
    allowed = row.get("effects") or [row["effect"]]
    assert effect in allowed, f"{row['id']}: expected {allowed}, observed {effect} via {observed}"
    missing = set(row["policies"]) - acted - detected
    assert not missing, f"{row['id']}: {effect} via {observed}, but not from {sorted(missing)}"
    if effect == "detect":
        assert detected <= SCOPE_TOPICS, f"{row['id']}: detect-only policies outside the scope topics: {observed}"
