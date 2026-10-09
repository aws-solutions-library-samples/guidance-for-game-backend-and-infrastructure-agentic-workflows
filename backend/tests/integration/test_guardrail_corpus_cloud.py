"""Run the Guardrail evaluation corpus through ApplyGuardrail (deployed Guardrail required).

Usage (from backend/):
    GBAW_BEDROCK_GUARDRAIL_ID=<id> GBAW_BEDROCK_GUARDRAIL_VERSION=DRAFT \
        uv run pytest tests/integration/test_guardrail_corpus_cloud.py -m cloud

Each row's observed effect is derived only from the ApplyGuardrail response:
``pass`` (no intervention, no detection), ``detect`` (a policy matched with
action NONE), ``anonymize`` (intervened, text returned with masked values), or
``block`` (intervened, text replaced by the configured blocked message).
Output is limited to row IDs, effects, and policy names, never the corpus text.
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
BLOCKED_PREFIXES = ("I can only help with AWS GameLift", "I cannot provide that information")


def load_corpus() -> dict[str, Any]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


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


def observed_effect(response: dict[str, Any]) -> tuple[str, list[str]]:
    acted, detected = _policies(response.get("assessments", []))
    if response.get("action") != "GUARDRAIL_INTERVENED":
        return ("detect" if detected else "pass"), sorted(acted | detected)
    text = "".join(output.get("text", "") for output in response.get("outputs", []))
    effect = "block" if text.startswith(BLOCKED_PREFIXES) else "anonymize"
    return effect, sorted(acted | detected)


@pytest.fixture(scope="module")
def guardrail():
    guardrail_id = os.environ.get("GBAW_BEDROCK_GUARDRAIL_ID")
    if not guardrail_id:
        pytest.skip("GBAW_BEDROCK_GUARDRAIL_ID is not set")
    # Third-party packages
    import boto3

    client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
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
    effect, policies = observed_effect(response)
    allowed = row.get("effects") or [row["effect"]]
    assert effect in allowed, f"{row['id']}: expected {allowed}, observed {effect} via {policies}"
