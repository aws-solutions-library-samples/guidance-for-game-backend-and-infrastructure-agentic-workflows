"""The E0 harness output must validate against the public-safe evidence schema.

Guards issue #412's "public-safe, reproducible evidence" requirement: the
document the harness emits conforms to the published schema, and the schema
forbids any field that could carry an account id, fleet id, ARN, or payload
(``additionalProperties: false`` throughout).
"""

from __future__ import annotations

# Standard library
import json
from pathlib import Path

# Third-party packages
import pytest
from jsonschema import Draft202012Validator

# Local modules
from operations.validation.e0_harness import _measure, _RunConfig
from operations.validation.e0_latency import DEFAULT_BUDGET

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).parents[3]
SCHEMA_PATH = PROJECT_ROOT / "docs" / "evidence" / "e0-latency-evidence.schema.json"
PUBLISHED_EVIDENCE_PATH = PROJECT_ROOT / "docs" / "evidence" / "e0-latency-2026-09-21-dynamodb.json"
ADR_PATH = PROJECT_ROOT / "docs" / "adr" / "0005-persist-operations-and-recover-workflows.md"
ADR_INDEX_PATH = PROJECT_ROOT / "docs" / "adr" / "README.md"
FAKE_FLEET_ID = "fleet-00000000-0000-4000-8000-000000000000"


class FakeGameLiftClient:
    def describe_fleet_utilization(self, **kwargs):
        return {"FleetUtilization": []}

    def describe_fleet_capacity(self, **kwargs):
        return {"FleetCapacity": []}

    def describe_scaling_policies(self, **kwargs):
        return {"ScalingPolicies": []}


def test_schema_file_is_valid_draft_2020_12():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_harness_document_validates_against_evidence_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    doc = _measure(
        FakeGameLiftClient(),
        FAKE_FLEET_ID,
        DEFAULT_BUDGET,
        _RunConfig(
            region="us-west-2",
            samples=5,
            concurrency=1,
            retry_mode="adaptive",
            max_attempts=3,
        ),
    )
    errors = sorted(Draft202012Validator(schema).iter_errors(doc), key=str)
    assert errors == [], f"evidence document failed schema: {errors}"


def test_transactional_mode_document_validates_and_states_mode():
    # Local modules
    from operations.validation.e0_persistence import (
        MODE_DYNAMODB_TRANSACT,
        DynamoDbTransactionalSink,
    )

    class FakeDynamoDbClient:
        def transact_write_items(self, **kwargs):
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    sink = DynamoDbTransactionalSink(
        client=FakeDynamoDbClient(),
        table_name="e0-latency-spike-disposable",
        persistence_budget_s=DEFAULT_BUDGET.persistence_s,
    )
    doc = _measure(
        FakeGameLiftClient(),
        FAKE_FLEET_ID,
        DEFAULT_BUDGET,
        _RunConfig(
            region="us-west-2",
            samples=5,
            concurrency=1,
            retry_mode="adaptive",
            max_attempts=3,
        ),
        sink=sink,
    )
    errors = sorted(Draft202012Validator(schema).iter_errors(doc), key=str)
    assert errors == [], f"transactional evidence document failed schema: {errors}"
    assert doc["assumptions"]["persistence_mode"] == MODE_DYNAMODB_TRANSACT
    assert doc["evaluation"]["persistence_acceptable"] is True


def test_published_evidence_validates_and_satisfies_acceptance_rule():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    evidence = json.loads(PUBLISHED_EVIDENCE_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(evidence), key=str)

    assert errors == [], f"published evidence failed schema: {errors}"

    results = evidence["results_ms"]
    evaluation = evidence["evaluation"]
    budget = evidence["budget_ms"]
    clean_run = (
        results["successes"] == results["sample_size"]
        and results["failures"] == 0
        and results["timeouts"] == 0
        and results["partial_denials"] == 0
    )
    acceptance_ceiling_ms = budget["gateway_integration_timeout"] - budget["cancellation_margin"]

    assert evidence["assumptions"]["persistence_mode"] == "dynamodb-transactional"
    assert evaluation["persistence_acceptable"] is True
    assert evaluation["clean_run"] is clean_run is True
    assert evaluation["acceptance_ceiling_ms"] == acceptance_ceiling_ms
    assert evaluation["p99_ms"] <= acceptance_ceiling_ms
    assert evaluation["synchronous_accepted"] is True


def test_published_adr_records_accepted_state_without_stale_pending_claims():
    adr = ADR_PATH.read_text(encoding="utf-8")
    adr_index = ADR_INDEX_PATH.read_text(encoding="utf-8")

    assert "- **Status:** Accepted" in adr
    assert "| [ADR 0005](0005-persist-operations-and-recover-workflows.md) | Accepted |" in adr_index
    for stale_claim in (
        "live measurement PENDING",
        "not yet a live measured percentile",
        "This record stays **Proposed** until that evidence exists",
    ):
        assert stale_claim not in adr
