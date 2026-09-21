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

SCHEMA_PATH = Path(__file__).parents[3] / "docs" / "evidence" / "e0-latency-evidence.schema.json"
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
