"""Harness-level tests for the E0 persistence mode wiring (issue #412).

These tests exercise the measurement document assembly and CLI argument parsing
for the persistence mode that replaced the no-op default sink. They use in-process
fakes only; no live AWS call is made. They verify:

* the evidence document states the *actual* persistence mode;
* synchronous acceptance is denied unless the run used the **real transactional**
  mode AND the sample is clean;
* a clean run under the in-memory (test-only) mode is still denied; and
* the CLI validates ``--persistence-table`` / ``--persistence-mode`` and builds
  the correct sink.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.validation.e0_harness import _build_sink, _measure, _parse_args, _RunConfig
from operations.validation.e0_latency import DEFAULT_BUDGET
from operations.validation.e0_persistence import (
    MODE_DYNAMODB_TRANSACT,
    MODE_IN_MEMORY,
    DynamoDbTransactionalSink,
    InMemoryPersistenceSink,
)

pytestmark = pytest.mark.unit

FAKE_FLEET_ID = "fleet-00000000-0000-4000-8000-000000000000"
TABLE = "e0-latency-spike-disposable"


class FakeGameLiftClient:
    def describe_fleet_utilization(self, **kwargs):
        return {"FleetUtilization": [{"ActiveGameSessionCount": 0}]}

    def describe_fleet_capacity(self, **kwargs):
        return {"FleetCapacity": [{"InstanceCounts": {"ACTIVE": 1}}]}

    def describe_scaling_policies(self, **kwargs):
        return {"ScalingPolicies": []}


class FakeDynamoDbClient:
    def __init__(self):
        self.calls = []

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _config(**overrides) -> _RunConfig:
    base = dict(
        region="us-west-2",
        samples=5,
        concurrency=1,
        retry_mode="adaptive",
        max_attempts=3,
    )
    base.update(overrides)
    return _RunConfig(**base)


# ---------------------------------------------------------------------------
# Evidence: actual persistence mode + acceptance gating
# ---------------------------------------------------------------------------


def test_document_states_real_transactional_mode_and_accepts_clean_run():
    sink = DynamoDbTransactionalSink(
        client=FakeDynamoDbClient(), table_name=TABLE, persistence_budget_s=DEFAULT_BUDGET.persistence_s
    )
    doc = _measure(FakeGameLiftClient(), FAKE_FLEET_ID, DEFAULT_BUDGET, _config(), sink=sink)
    assert doc["assumptions"]["persistence_mode"] == MODE_DYNAMODB_TRANSACT
    assert doc["evaluation"]["persistence_mode"] == MODE_DYNAMODB_TRANSACT
    assert doc["evaluation"]["persistence_acceptable"] is True
    assert doc["evaluation"]["clean_run"] is True
    assert doc["evaluation"]["synchronous_accepted"] is True


def test_in_memory_mode_clean_run_is_denied():
    sink = InMemoryPersistenceSink()
    doc = _measure(FakeGameLiftClient(), FAKE_FLEET_ID, DEFAULT_BUDGET, _config(), sink=sink)
    assert doc["assumptions"]["persistence_mode"] == MODE_IN_MEMORY
    assert doc["evaluation"]["persistence_acceptable"] is False
    # Clean run under a non-acceptable mode: still denied.
    assert doc["evaluation"]["clean_run"] is True
    assert doc["evaluation"]["synchronous_accepted"] is False


def test_no_sink_defaults_to_in_memory_and_is_denied():
    doc = _measure(FakeGameLiftClient(), FAKE_FLEET_ID, DEFAULT_BUDGET, _config(), sink=None)
    assert doc["assumptions"]["persistence_mode"] == MODE_IN_MEMORY
    assert doc["evaluation"]["persistence_acceptable"] is False
    assert doc["evaluation"]["synchronous_accepted"] is False


def test_real_mode_with_a_failure_is_denied():
    class Flaky(FakeGameLiftClient):
        def describe_fleet_utilization(self, **kwargs):
            raise RuntimeError("synthetic provider failure")

    sink = DynamoDbTransactionalSink(
        client=FakeDynamoDbClient(), table_name=TABLE, persistence_budget_s=DEFAULT_BUDGET.persistence_s
    )
    doc = _measure(Flaky(), FAKE_FLEET_ID, DEFAULT_BUDGET, _config(samples=4), sink=sink)
    assert doc["evaluation"]["persistence_acceptable"] is True
    assert doc["evaluation"]["clean_run"] is False
    assert doc["evaluation"]["synchronous_accepted"] is False


# ---------------------------------------------------------------------------
# CLI validation and sink construction
# ---------------------------------------------------------------------------


def test_cli_parses_persistence_flags():
    args = _parse_args(
        [
            "--fleet-id",
            FAKE_FLEET_ID,
            "--persistence-table",
            TABLE,
            "--persistence-mode",
            "dynamodb-transactional",
            "--ttl-seconds",
            "3600",
        ]
    )
    assert args.persistence_table == TABLE
    assert args.persistence_mode == "dynamodb-transactional"
    assert args.ttl_seconds == 3600


def test_cli_defaults_persistence_mode_to_in_memory():
    args = _parse_args(["--fleet-id", FAKE_FLEET_ID])
    assert args.persistence_mode == "in-memory"
    assert args.persistence_table is None


def test_build_sink_transactional_requires_table():
    args = _parse_args(["--fleet-id", FAKE_FLEET_ID, "--persistence-mode", "dynamodb-transactional"])
    with pytest.raises(SystemExit):
        _build_sink(args, DEFAULT_BUDGET, dynamodb_client=FakeDynamoDbClient())


def test_build_sink_in_memory_returns_in_memory_sink():
    args = _parse_args(["--fleet-id", FAKE_FLEET_ID])
    sink = _build_sink(args, DEFAULT_BUDGET, dynamodb_client=FakeDynamoDbClient())
    assert isinstance(sink, InMemoryPersistenceSink)


def test_build_sink_transactional_builds_transactional_sink():
    args = _parse_args(
        ["--fleet-id", FAKE_FLEET_ID, "--persistence-mode", "dynamodb-transactional", "--persistence-table", TABLE]
    )
    client = FakeDynamoDbClient()
    sink = _build_sink(args, DEFAULT_BUDGET, dynamodb_client=client)
    assert isinstance(sink, DynamoDbTransactionalSink)
    assert sink.table_name == TABLE
    assert sink.persistence_budget_s == DEFAULT_BUDGET.persistence_s
