"""Unit tests for the E0 persistence sinks (issue #412).

A live provider-read measurement exposed that the harness's default persistence
callable was a no-op, which does not meet the durable acceptance boundary of
ADR 0005. These tests cover the two explicit persistence modes that replace it,
using an in-process fake DynamoDB client so no live AWS call is ever made:

* the in-memory deterministic sink is for unit tests only and is explicitly
  **not** acceptable as live ADR evidence;
* the real DynamoDB transactional sink writes exactly one operation-state item
  and one append-only ledger item per sample, in a single ``TransactWriteItems``
  call, with conditional no-replacement semantics, per-sample-unique keys, a TTL
  attribute when configured, and **only synthetic data** — never a fleet id,
  account id, ARN, or provider payload;
* the transactional sink enforces its persistence deadline and fails closed with
  a typed retryable error; and
* a transactional-write failure propagates (the harness counts it as a failed
  sample and denies acceptance).
"""

from __future__ import annotations

# Standard library
import json

# Third-party packages
import pytest

# Local modules
from operations.validation.e0_latency import DeadlineExceededError, ObservationRunner
from operations.validation.e0_persistence import (
    MODE_DYNAMODB_TRANSACT,
    MODE_IN_MEMORY,
    DynamoDbTransactionalSink,
    InMemoryPersistenceSink,
)

pytestmark = pytest.mark.unit

# Public-content-safe synthetic identifiers used only to assert they never leak.
FAKE_FLEET_ID = "fleet-00000000-0000-4000-8000-000000000000"
FAKE_ACCOUNT_ID = "123456789012"
FAKE_ARN = "arn:aws:gamelift:us-west-2:" + FAKE_ACCOUNT_ID + ":fleet/" + FAKE_FLEET_ID
TABLE = "e0-latency-spike-disposable"


class FakeDynamoDbClient:
    """Records ``transact_write_items`` calls; issues no I/O.

    Optionally raises to simulate a transactional failure (e.g. a
    ConditionalCheckFailed or throttling), and can sleep on a fake clock to
    simulate a slow write for deadline tests.
    """

    def __init__(self, *, fail: bool = False, clock=None, write_cost_s: float = 0.0) -> None:
        self.calls: list[dict] = []
        self._fail = fail
        self._clock = clock
        self._write_cost_s = write_cost_s

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        if self._clock is not None and self._write_cost_s:
            self._clock.advance(self._write_cost_s)
        if self._fail:
            raise RuntimeError("synthetic transactional write failure")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class FakeClock:
    """Deterministic monotonic clock advanced explicitly by the test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _record(operation_id: str = "op-unit-0") -> dict:
    return {
        "observation_contract_version": "1.0",
        "phase": "observe",
        "provider": "gamelift",
        "operation_id": operation_id,
        "read_count": 3,
        "ledger_events": [{"sequence": 0, "event_type": "provider_read_completed"}],
    }


# ---------------------------------------------------------------------------
# In-memory sink: unit-test-only, not acceptable for live evidence
# ---------------------------------------------------------------------------


def test_in_memory_sink_is_not_acceptable_for_live_evidence():
    sink = InMemoryPersistenceSink()
    assert sink.mode == MODE_IN_MEMORY
    assert sink.acceptable_for_live_evidence is False


def test_in_memory_sink_canonicalizes_and_records_bytes():
    sink = InMemoryPersistenceSink()
    record = _record()
    sink.persist(record, b"ignored-canonical-bytes")
    assert len(sink.written) == 1
    assert isinstance(sink.written[0], bytes)


# ---------------------------------------------------------------------------
# DynamoDB transactional sink: real, opt-in, acceptable for live evidence
# ---------------------------------------------------------------------------


def test_transactional_sink_is_acceptable_for_live_evidence():
    sink = DynamoDbTransactionalSink(client=FakeDynamoDbClient(), table_name=TABLE, persistence_budget_s=3.0)
    assert sink.mode == MODE_DYNAMODB_TRANSACT
    assert sink.acceptable_for_live_evidence is True


def test_transactional_sink_writes_one_transaction_with_two_items():
    client = FakeDynamoDbClient()
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    sink.persist(_record(), b"canonical")
    assert len(client.calls) == 1
    items = client.calls[0]["TransactItems"]
    assert len(items) == 2
    # Exactly one operation-state put and one append-only ledger put.
    sks = sorted(item["Put"]["Item"]["SK"]["S"] for item in items)
    assert sks == ["LEDGER#0", "STATE#0"]


def test_transactional_sink_uses_conditional_no_replacement():
    client = FakeDynamoDbClient()
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    sink.persist(_record(), b"canonical")
    conditions = [item["Put"]["ConditionExpression"] for item in client.calls[0]["TransactItems"]]
    # No-replacement / append-only: both puts are conditional on absence.
    assert "attribute_not_exists(PK)" in conditions
    assert "attribute_not_exists(SK)" in conditions


def test_transactional_sink_targets_caller_specified_table():
    client = FakeDynamoDbClient()
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    sink.persist(_record(), b"canonical")
    for item in client.calls[0]["TransactItems"]:
        assert item["Put"]["TableName"] == TABLE


def test_transactional_sink_keys_are_per_sample_unique():
    client = FakeDynamoDbClient()
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    sink.persist(_record("op-a"), b"c")
    sink.persist(_record("op-b"), b"c")
    pks = [call["TransactItems"][0]["Put"]["Item"]["PK"]["S"] for call in client.calls]
    assert pks[0] != pks[1]
    assert all(pk.startswith("OP#") for pk in pks)


def test_transactional_sink_writes_only_synthetic_data_no_provider_identifiers():
    client = FakeDynamoDbClient()
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    # A record that (wrongly) carried provider identifiers must still never place
    # them into the persisted items: the sink builds items from synthetic fields.
    tainted = _record("op-clean")
    tainted["leaked_fleet"] = FAKE_FLEET_ID
    tainted["leaked_account"] = FAKE_ACCOUNT_ID
    tainted["leaked_arn"] = FAKE_ARN
    sink.persist(tainted, b"canonical")
    payload = json.dumps(client.calls[0])
    assert FAKE_FLEET_ID not in payload
    assert FAKE_ACCOUNT_ID not in payload
    assert "arn:aws" not in payload
    # Items are marked synthetic.
    for item in client.calls[0]["TransactItems"]:
        assert item["Put"]["Item"]["synthetic"]["BOOL"] is True


def test_transactional_sink_ttl_present_only_when_configured():
    client_no_ttl = FakeDynamoDbClient()
    DynamoDbTransactionalSink(client=client_no_ttl, table_name=TABLE, persistence_budget_s=3.0).persist(_record(), b"c")
    for item in client_no_ttl.calls[0]["TransactItems"]:
        assert "ttl" not in item["Put"]["Item"]

    client_ttl = FakeDynamoDbClient()
    DynamoDbTransactionalSink(
        client=client_ttl,
        table_name=TABLE,
        persistence_budget_s=3.0,
        ttl_seconds=3600,
        epoch_clock=lambda: 1_000_000.0,
    ).persist(_record(), b"c")
    for item in client_ttl.calls[0]["TransactItems"]:
        assert item["Put"]["Item"]["ttl"]["N"] == str(1_000_000 + 3600)


def test_transactional_sink_canonical_serialization_in_measured_path():
    client = FakeDynamoDbClient()
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    sink.persist(_record(), b"canonical")
    # Each persisted item carries the canonical digest of its own payload.
    for item in client.calls[0]["TransactItems"]:
        digest = item["Put"]["Item"]["canonical_sha256"]["S"]
        assert digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# Deadline enforcement and failure propagation
# ---------------------------------------------------------------------------


def test_transactional_sink_fails_closed_on_deadline_overrun():
    clock = FakeClock()
    # The write itself consumes more than the persistence budget on the clock.
    client = FakeDynamoDbClient(clock=clock, write_cost_s=5.0)
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0, clock=clock)
    with pytest.raises(DeadlineExceededError) as exc:
        sink.persist(_record(), b"canonical")
    assert exc.value.phase == "persistence"
    assert exc.value.retryable is True


def test_transactional_sink_propagates_write_failure():
    client = FakeDynamoDbClient(fail=True)
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    with pytest.raises(RuntimeError):
        sink.persist(_record(), b"canonical")


def test_transactional_sink_rejects_invalid_construction():
    with pytest.raises(ValueError):
        DynamoDbTransactionalSink(client=FakeDynamoDbClient(), table_name="", persistence_budget_s=3.0)
    with pytest.raises(ValueError):
        DynamoDbTransactionalSink(client=FakeDynamoDbClient(), table_name=TABLE, persistence_budget_s=0.0)
    with pytest.raises(ValueError):
        DynamoDbTransactionalSink(
            client=FakeDynamoDbClient(), table_name=TABLE, persistence_budget_s=3.0, ttl_seconds=0
        )


# ---------------------------------------------------------------------------
# Runner integration: durable-persistence flag and sink invocation
# ---------------------------------------------------------------------------


def test_runner_without_sink_is_not_durably_persisted():
    runner = ObservationRunner()
    assert runner.durably_persisted is False


def test_runner_with_sink_is_durably_persisted_and_invokes_it():
    client = FakeDynamoDbClient()
    sink = DynamoDbTransactionalSink(client=client, table_name=TABLE, persistence_budget_s=3.0)
    runner = ObservationRunner(sink=sink)
    assert runner.durably_persisted is True
    reads = [lambda i=i: {"ok": i} for i in range(3)]
    runner.run(reads)
    # One transactional write per observation run.
    assert len(client.calls) == 1


def test_runner_rejects_both_sink_and_persist():
    with pytest.raises(ValueError):
        ObservationRunner(sink=InMemoryPersistenceSink(), persist=lambda _b: None)


def test_runner_in_memory_sink_run_is_durably_persisted_flag_true_but_mode_not_acceptable():
    sink = InMemoryPersistenceSink()
    runner = ObservationRunner(sink=sink)
    # The runner flag only reports that *a* hook ran; acceptability is a property
    # of the sink mode, which the harness checks separately.
    assert runner.durably_persisted is True
    assert sink.acceptable_for_live_evidence is False
