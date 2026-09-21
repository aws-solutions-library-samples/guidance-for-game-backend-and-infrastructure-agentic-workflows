"""Unit tests for the two-phase conditional/idempotent DynamoDB store (#413)."""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.canonical import canonical_sha256
from operations.observation import (
    ObservationBeginOutcome,
    ObservationCompleteOutcome,
    ObservationStatusView,
)
from operations.observation_store import (
    DynamoDbObservationStore,
    ObservationStoreError,
)

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
TABLE = "operations-observations-test"
WORKSPACE = "workspace.default"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
FINGERPRINT = "sha256:" + "a" * 64
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
HOLDER = "request.observe-1"
INTENT = {"phase": "observe", "provider": "gamelift", "target": {"provider": "gamelift", "fleet_id": "fleet-abc"}}


def _observation() -> dict[str, Any]:
    return {
        "observation_id": OPERATION_ID,
        "phase": "observe",
        "provider": "gamelift",
        "results": {"utilization": {}, "capacity": [], "scaling_policies": []},
    }


class TransactionCanceled(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "TransactionCanceledException"}}
        self.cancellation_reasons = [{"Code": "ConditionalCheckFailed"}]
        super().__init__("transaction canceled")


class StatefulDynamoClient:
    """A minimal stateful DynamoDB fake honoring conditional puts and updates."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[list[dict[str, Any]]] = []
        self.gets: list[tuple[str, str]] = []
        self.raise_non_conditional = False

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transactions.append(TransactItems)
        if self.raise_non_conditional:
            raise RuntimeError("throttled")
        # First pass: evaluate every condition against current state.
        for entry in TransactItems:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                cond = put.get("ConditionExpression", "")
                if "attribute_not_exists(PK)" in cond and key in self.items:
                    raise TransactionCanceled()
                if "attribute_not_exists(SK)" in cond and key in self.items:
                    raise TransactionCanceled()
            elif "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                if not self._update_condition_holds(upd, key):
                    raise TransactionCanceled()
        # Second pass: apply.
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
            elif "Update" in entry:
                self._apply_update(entry["Update"])
        return {}

    def _update_condition_holds(self, upd: dict[str, Any], key: tuple[str, str]) -> bool:
        current = self.items.get(key)
        if current is None:
            return False
        values = upd.get("ExpressionAttributeValues", {})
        # Emulate the specific fencing condition used by complete/fail.
        if current.get("state", {}).get("S") != values.get(":observing", {}).get("S"):
            return False
        if int(current.get("sequence", {}).get("N", "-1")) != int(values.get(":zero", {}).get("N", "0")):
            return False
        if int(current.get("generation", {}).get("N", "-1")) != int(values.get(":gen", {}).get("N", "0")):
            return False
        if current.get("lease_holder", {}).get("S") != values.get(":holder", {}).get("S"):
            return False
        if ":now" in values:
            if int(current.get("lease_not_after", {}).get("N", "0")) <= int(values[":now"]["N"]):
                return False
        return True

    def _apply_update(self, upd: dict[str, Any]) -> None:
        key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
        current = dict(self.items[key])
        values = upd.get("ExpressionAttributeValues", {})
        if ":succeeded" in values:
            current["state"] = values[":succeeded"]
            current["sequence"] = values[":one"]
        elif ":failed" in values:
            current["state"] = values[":failed"]
            current["sequence"] = values[":one"]
        self.items[key] = current

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        pk = Key["PK"]["S"]
        sk = Key["SK"]["S"]
        self.gets.append((pk, sk))
        item = self.items.get((pk, sk))
        return {"Item": item} if item is not None else {}


def _store(client: StatefulDynamoClient, clock: datetime = NOW) -> DynamoDbObservationStore:
    return DynamoDbObservationStore(client=client, table_name=TABLE, clock=lambda: clock)


def _begin(
    store: DynamoDbObservationStore, *, deadline: datetime | None = None, operation_id: str = OPERATION_ID
) -> Any:
    return store.begin_observation(
        operation_id=operation_id,
        idempotency_fingerprint=FINGERPRINT,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder=HOLDER,
        commit_not_after=deadline or (NOW + timedelta(minutes=30)),
        lease_not_after=NOW + timedelta(seconds=15),
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
        intent=INTENT,
    )


def _complete(store: DynamoDbObservationStore, observation: dict[str, Any] | None = None) -> Any:
    return store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        commit_not_after=NOW + timedelta(minutes=30),
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
        observation=observation or _observation(),
    )


# --- begin ----------------------------------------------------------------


def test_begin_writes_four_conditional_items() -> None:
    client = StatefulDynamoClient()
    begin = _begin(_store(client))
    assert begin.outcome is ObservationBeginOutcome.CREATED
    items = client.transactions[0]
    assert len(items) == 4
    assert all("ConditionExpression" in entry["Put"] for entry in items)
    ledger = next(e for e in items if e["Put"]["Item"]["SK"]["S"] == "LEDGER#0")
    assert ledger["Put"]["ConditionExpression"] == "attribute_not_exists(SK)"


def test_begin_snapshot_is_observing_with_lease() -> None:
    client = StatefulDynamoClient()
    _begin(_store(client))
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["state"]["S"] == "observing"
    assert snapshot["generation"]["N"] == "1"
    assert snapshot["lease_holder"]["S"] == HOLDER
    assert snapshot["workspace_id"]["S"] == WORKSPACE
    assert "ttl" in snapshot


def test_begin_expired_deadline_fails_closed_without_writing() -> None:
    client = StatefulDynamoClient()
    begin = _begin(_store(client), deadline=NOW - timedelta(seconds=1))
    assert begin.outcome is ObservationBeginOutcome.DEADLINE_EXPIRED
    assert client.transactions == []


def test_begin_non_conditional_error_is_state_conflict() -> None:
    client = StatefulDynamoClient()
    client.raise_non_conditional = True
    begin = _begin(_store(client))
    assert begin.outcome is ObservationBeginOutcome.STATE_CONFLICT


# --- replay / conflict / in-progress --------------------------------------


def test_begin_replays_completed_operation_without_new_read() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    # A second begin with the same token+fingerprint resolves the completed op.
    begin = _begin(store)
    assert begin.outcome is ObservationBeginOutcome.REPLAY_COMPLETED
    assert begin.observation is not None
    assert begin.observation["observation_id"] == OPERATION_ID
    assert begin.observation_hash == canonical_sha256(_observation())


def test_begin_changed_intent_is_idempotency_conflict() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    # Same token, different fingerprint => conflict, never mutates the op.
    conflict = store.begin_observation(
        operation_id="obs_bbbbbbbbbbbbbbbbbbbbbbbbbb",
        idempotency_fingerprint="sha256:" + "b" * 64,
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        lease_holder="request.observe-2",
        commit_not_after=NOW + timedelta(minutes=30),
        lease_not_after=NOW + timedelta(seconds=15),
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
        intent={"phase": "observe", "provider": "gamelift", "target": {"provider": "gamelift", "fleet_id": "fleet-z"}},
    )
    assert conflict.outcome is ObservationBeginOutcome.IDEMPOTENCY_CONFLICT


def test_begin_in_progress_when_not_yet_completed() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    # Retry before completion => in progress, no second operation.
    retry = _begin(store)
    assert retry.outcome is ObservationBeginOutcome.IN_PROGRESS
    assert retry.current_state == "observing"


def test_no_scan_and_all_puts_conditional() -> None:
    client = StatefulDynamoClient()
    _begin(_store(client))
    assert not hasattr(client, "scan")
    for entry in client.transactions[0]:
        assert "Put" in entry
        assert "ConditionExpression" in entry["Put"]


# --- complete -------------------------------------------------------------


def test_complete_records_result_and_advances_state() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    complete = _complete(store)
    assert complete.outcome is ObservationCompleteOutcome.RECORDED
    result = client.items[(f"OP#{OPERATION_ID}", "RESULT#current")]
    assert result["observation_hash"]["S"] == canonical_sha256(_observation())
    stored = json.loads(result["observation_json"]["S"])
    assert stored["observation_id"] == OPERATION_ID
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["state"]["S"] == "succeeded"
    transition = client.items[(f"OP#{OPERATION_ID}", "STATE#1")]
    assert transition["new_state"]["S"] == "succeeded"


def test_complete_is_append_only_on_ledger() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    items = client.transactions[-1]
    ledger = next(e for e in items if "Put" in e and e["Put"]["Item"]["SK"]["S"] == "LEDGER#1")
    assert ledger["Put"]["ConditionExpression"] == "attribute_not_exists(SK)"


def test_complete_rejects_result_above_item_size_limit() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    big = _observation()
    big["padding"] = "x" * (400 * 1024 + 10)
    with pytest.raises(ObservationStoreError):
        _complete(store, big)


def test_complete_expired_deadline_fails_closed() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    complete = store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        commit_not_after=NOW - timedelta(seconds=1),
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
        observation=_observation(),
    )
    assert complete.outcome is ObservationCompleteOutcome.DEADLINE_EXPIRED


def test_complete_under_wrong_holder_is_state_conflict() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    complete = store.complete_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder="request.someone-else",
        commit_not_after=NOW + timedelta(minutes=30),
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
        observation=_observation(),
    )
    assert complete.outcome is ObservationCompleteOutcome.STATE_CONFLICT


# --- fail -----------------------------------------------------------------


def test_fail_records_bounded_failed_transition() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    store.fail_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        reason_code="provider_unavailable",
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
    )
    snapshot = client.items[(f"OP#{OPERATION_ID}", "STATE#current")]
    assert snapshot["state"]["S"] == "failed"
    transition = client.items[(f"OP#{OPERATION_ID}", "STATE#1")]
    assert transition["new_state"]["S"] == "failed"
    assert transition["reason_code"]["S"] == "provider_unavailable"


def test_fail_is_best_effort_and_swallows_errors() -> None:
    client = StatefulDynamoClient()
    client.raise_non_conditional = True
    store = _store(client)
    # No begin: the update condition fails; fail_observation must not raise.
    store.fail_observation(
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE,
        lease_holder=HOLDER,
        reason_code="provider_unavailable",
        ttl_epoch_s=int((NOW + timedelta(minutes=30)).timestamp()),
    )


# --- status ---------------------------------------------------------------


def test_load_status_succeeded_returns_observation() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    _complete(store)
    status = store.load_status(operation_id=OPERATION_ID, workspace_id=WORKSPACE)
    assert status is not None
    assert status.state is ObservationStatusView.SUCCEEDED
    assert status.observation is not None
    assert status.observation_hash == canonical_sha256(_observation())


def test_load_status_observing_has_no_observation() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    status = store.load_status(operation_id=OPERATION_ID, workspace_id=WORKSPACE)
    assert status is not None
    assert status.state is ObservationStatusView.OBSERVING
    assert status.observation is None


def test_load_status_cross_workspace_is_invisible() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _begin(store)
    assert store.load_status(operation_id=OPERATION_ID, workspace_id="workspace.other") is None


def test_load_status_missing_is_none() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    assert store.load_status(operation_id=OPERATION_ID, workspace_id=WORKSPACE) is None


def test_table_name_required() -> None:
    with pytest.raises(ValueError):
        DynamoDbObservationStore(client=StatefulDynamoClient(), table_name="  ")
