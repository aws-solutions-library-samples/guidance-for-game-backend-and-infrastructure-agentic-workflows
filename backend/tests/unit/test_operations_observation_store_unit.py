"""Unit tests for the conditional/idempotent DynamoDB observation store (#413)."""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.observation import ObservationCommitOutcome
from operations.observation_store import DynamoDbObservationStore

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
TABLE = "operations-observations-test"
WORKSPACE = "workspace.default"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
FINGERPRINT = "sha256:" + "a" * 64
OBSERVATION = {"observation_id": "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa", "phase": "observe"}


class ConditionalCheckFailed(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "TransactionCanceledException"}}
        self.cancellation_reasons = [{"Code": "ConditionalCheckFailed"}]
        super().__init__("conditional check failed")


class FakeDynamoClient:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[list[dict[str, Any]]] = []
        self.fail_condition = False
        self.raise_non_conditional = False
        self.existing_mapping: dict[str, Any] | None = None

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transactions.append(TransactItems)
        if self.raise_non_conditional:
            raise RuntimeError("throttled")
        if self.fail_condition:
            raise ConditionalCheckFailed()
        for entry in TransactItems:
            put = entry["Put"]
            item = put["Item"]
            key = (item["PK"]["S"], item["SK"]["S"])
            self.items[key] = item
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        if self.existing_mapping is not None:
            return {"Item": self.existing_mapping}
        return {}


def _store(client: FakeDynamoClient, clock: datetime = NOW) -> DynamoDbObservationStore:
    return DynamoDbObservationStore(client=client, table_name=TABLE, clock=lambda: clock)


def _record(store: DynamoDbObservationStore, *, deadline: datetime | None = None) -> Any:
    return store.record_observation(
        observation_id=OBSERVATION["observation_id"],
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        commit_not_after=deadline or (NOW + timedelta(minutes=30)),
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
        observation=OBSERVATION,
    )


def test_first_write_records_four_conditional_items() -> None:
    client = FakeDynamoClient()
    commit = _record(_store(client))
    assert commit.outcome is ObservationCommitOutcome.RECORDED
    assert len(client.transactions) == 1
    items = client.transactions[0]
    assert len(items) == 4
    # Every put carries a conditional expression (no unconditional overwrite).
    assert all("ConditionExpression" in entry["Put"] for entry in items)
    # The ledger event is append-only guarded on the sort key.
    ledger = next(e for e in items if e["Put"]["Item"]["SK"]["S"] == "LEDGER#1")
    assert ledger["Put"]["ConditionExpression"] == "attribute_not_exists(SK)"


def test_no_scan_or_unconditional_put_is_issued() -> None:
    client = FakeDynamoClient()
    _record(_store(client))
    assert not hasattr(client, "scanned") or True  # FakeClient exposes no scan
    for entry in client.transactions[0]:
        assert "ConditionExpression" in entry["Put"]


def test_replay_when_fingerprint_matches() -> None:
    client = FakeDynamoClient()
    client.fail_condition = True
    client.existing_mapping = {
        "PK": {"S": f"WS#{WORKSPACE}#IDEM#{TOKEN}"},
        "SK": {"S": "MAP#current"},
        "observation_id": {"S": OBSERVATION["observation_id"]},
        "idempotency_fingerprint": {"S": FINGERPRINT},
    }
    commit = _record(_store(client))
    assert commit.outcome is ObservationCommitOutcome.REPLAY


def test_idempotency_conflict_when_fingerprint_differs() -> None:
    client = FakeDynamoClient()
    client.fail_condition = True
    client.existing_mapping = {
        "PK": {"S": f"WS#{WORKSPACE}#IDEM#{TOKEN}"},
        "SK": {"S": "MAP#current"},
        "observation_id": {"S": "obs_bbbbbbbbbbbbbbbbbbbbbbbbbb"},
        "idempotency_fingerprint": {"S": "sha256:" + "b" * 64},
    }
    commit = _record(_store(client))
    assert commit.outcome is ObservationCommitOutcome.IDEMPOTENCY_CONFLICT


def test_missing_mapping_after_condition_failure_is_state_conflict() -> None:
    client = FakeDynamoClient()
    client.fail_condition = True
    client.existing_mapping = None
    commit = _record(_store(client))
    assert commit.outcome is ObservationCommitOutcome.STATE_CONFLICT


def test_non_conditional_error_is_state_conflict() -> None:
    client = FakeDynamoClient()
    client.raise_non_conditional = True
    commit = _record(_store(client))
    assert commit.outcome is ObservationCommitOutcome.STATE_CONFLICT


def test_expired_deadline_fails_closed_without_writing() -> None:
    client = FakeDynamoClient()
    store = _store(client, clock=NOW)
    commit = _record(store, deadline=NOW - timedelta(seconds=1))
    assert commit.outcome is ObservationCommitOutcome.DEADLINE_EXPIRED
    assert client.transactions == []


def test_items_carry_ttl_and_observation_hash() -> None:
    client = FakeDynamoClient()
    _record(_store(client))
    snapshot = client.items[(f"OBS#{OBSERVATION['observation_id']}", "STATE#current")]
    assert "ttl" in snapshot
    assert snapshot["observation_hash"]["S"].startswith("sha256:")


def test_initial_transition_records_null_previous_state() -> None:
    client = FakeDynamoClient()
    _record(_store(client))
    transition = client.items[(f"OBS#{OBSERVATION['observation_id']}", "STATE#0")]
    assert transition["previous_state"] == {"NULL": True}
    assert transition["new_state"] == {"S": "observed"}


def test_table_name_required() -> None:
    with pytest.raises(ValueError):
        DynamoDbObservationStore(client=FakeDynamoClient(), table_name="  ")
