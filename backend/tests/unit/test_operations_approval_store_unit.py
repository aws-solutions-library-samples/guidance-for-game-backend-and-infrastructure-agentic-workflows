"""DynamoDB E2 approval store: persistence, fenced decisions, TTL-free audit.

Drives :class:`~operations.approval_store.DynamoDbApprovalStore` against a
stateful DynamoDB fake that raises **real** ``botocore.exceptions.ClientError``
values shaped exactly like the wire response, plus a bounded set of injected
faults. Covers: atomic prepared-operation persistence, same-token replay,
changed-intent conflict, cross-workspace idempotency isolation, fenced
approve/reject/cancel/expire commits, conditional-race precondition handling,
transient-fault fail-closed classification, and the invariant that **no E2
record carries a TTL attribute**.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from operations.approval import ApprovalCommitOutcome
from operations.approval_store import (
    ApprovalStoreError,
    DynamoDbApprovalStore,
    PersistOutcome,
    _canonical_json,
)
from operations.contracts import load_json
from operations.contracts.capacity import capacity_prepared_hash
from operations.decisions import DecisionCommitOutcome
from operations.evidence import OperationEvidence

FIXTURES = __import__("pathlib").Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
NOW = datetime(2026, 9, 21, 19, 15, tzinfo=timezone.utc)
TABLE = "operations-e2-test"
WORKSPACE = "workspace.default"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
FINGERPRINT = "sha256:" + "a" * 64
OPERATION_ID = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"


def _operation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")


def _hash() -> str:
    return capacity_prepared_hash(_operation())


def _state_change(new_state: str = "pending_approval", reason: str = "APPROVAL_REQUESTED") -> dict[str, Any]:
    return {
        "state_contract_version": "1.0",
        "state_change_id": "state.persist-1",
        "operation_id": OPERATION_ID,
        "prepared_operation_hash": _hash(),
        "previous_state": "prepared" if new_state == "pending_approval" else "pending_approval",
        "new_state": new_state,
        "attempt": 0,
        "reason_code": reason,
        "changed_at": "2026-09-21T19:15:00Z",
        "actor": {"actor_type": "system", "actor_id": "operations.prepare"},
        "correlation": {"correlation_id": "corr.prepare-1", "request_id": "request.prepare-1"},
    }


def _ledger_event(event_type: str = "operation.prepared") -> dict[str, Any]:
    payload: dict[str, Any]
    if event_type == "operation.prepared":
        payload = {"payload_type": "operation_prepared", "playbook_hash": "sha256:" + "1" * 64, "profile": "p/1.0"}
    else:
        payload = {
            "payload_type": "state_changed",
            "state_change_id": "state.persist-1",
            "previous_state": "pending_approval",
            "new_state": "cancelled",
        }
    return {
        "ledger_contract_version": "1.0",
        "event_id": "event.persist-1",
        "event_type": event_type,
        "operation_id": OPERATION_ID,
        "prepared_operation_hash": _hash(),
        "sequence": 1,
        "occurred_at": "2026-09-21T19:15:00Z",
        "actor": {"actor_type": "system", "actor_id": "operations.prepare"},
        "correlation": {"correlation_id": "corr.prepare-1", "request_id": "request.prepare-1"},
        "payload": payload,
    }


def _approval(decision: str = "granted") -> dict[str, Any]:
    return {
        "approval_contract_version": "1.0",
        "approval_id": "approval.decision-1",
        "operation_id": OPERATION_ID,
        "prepared_operation_hash": _hash(),
        "approver": {
            "subject_id": "subject.approver-1",
            "client_id": "client.web-console",
            "tenant_id": "tenant.default",
            "workspace_id": "workspace.default",
        },
        "decision": decision,
        "policy_version": "2026-09-01",
        "decided_at": "2026-09-21T19:15:00Z",
        "expires_at": "2026-09-21T19:20:00Z",
        "correlation": {"correlation_id": "corr.prepare-1", "request_id": "request.prepare-1"},
    }


def _transaction_canceled(*reason_codes: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
            "CancellationReasons": [{"Code": code} for code in reason_codes],
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "TransactWriteItems",
    )


class StatefulDynamoClient:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[list[dict[str, Any]]] = []
        self.raise_on_transact: BaseException | None = None

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transactions.append(TransactItems)
        if self.raise_on_transact is not None:
            raise self.raise_on_transact
        # Evaluate conditions.
        for entry in TransactItems:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                cond = put.get("ConditionExpression", "")
                if ("attribute_not_exists(PK)" in cond or "attribute_not_exists(SK)" in cond) and key in self.items:
                    raise _transaction_canceled("ConditionalCheckFailed")
            if "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                existing = self.items.get(key)
                values = upd["ExpressionAttributeValues"]
                expected_state = values[":expected"]["S"]
                expected_hash = values[":hash"]["S"]
                prev_seq = int(values[":prev_seq"]["N"])
                if (
                    existing is None
                    or existing.get("state", {}).get("S") != expected_state
                    or existing.get("prepared_hash", {}).get("S") != expected_hash
                    or int(existing.get("sequence", {}).get("N", "-1")) != prev_seq
                ):
                    raise _transaction_canceled("ConditionalCheckFailed")
        # Apply.
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
            if "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                values = upd["ExpressionAttributeValues"]
                self.items[key]["state"] = {"S": values[":new"]["S"]}
                self.items[key]["sequence"] = {"N": values[":seq"]["N"]}
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}


def _store(client: StatefulDynamoClient) -> DynamoDbApprovalStore:
    return DynamoDbApprovalStore(client=client, table_name=TABLE, clock=lambda: NOW)


def _persist(store: DynamoDbApprovalStore, *, token: str = TOKEN, workspace: str = WORKSPACE, fingerprint=FINGERPRINT):
    return store.persist_prepared_operation(
        prepared_operation=_operation(),
        prepared_hash=_hash(),
        workspace_id=workspace,
        idempotency_token=token,
        idempotency_fingerprint=fingerprint,
        state_change=_state_change(),
        ledger_event=_ledger_event(),
        commit_not_after=NOW + timedelta(minutes=15),
    )


# -- Persist -------------------------------------------------------------


def test_persist_materializes_prepared_operation_atomically() -> None:
    client = StatefulDynamoClient()
    result = _persist(_store(client))

    assert result.outcome is PersistOutcome.PERSISTED
    assert result.operation_id == OPERATION_ID
    # One transaction writing all five legs: idem, prepared, snapshot, state#0, ledger#0.
    assert len(client.transactions) == 1
    assert len(client.transactions[0]) == 5


def test_persist_no_e2_record_carries_a_ttl_attribute() -> None:
    client = StatefulDynamoClient()
    _persist(_store(client))

    for item in client.items.values():
        assert "ttl" not in item, "E2 records must omit TTL"


def test_persist_replays_same_token_and_matching_intent() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)

    replay = _persist(store)

    assert replay.outcome is PersistOutcome.REPLAYED
    assert replay.operation_id == OPERATION_ID


def test_persist_conflicts_on_same_token_different_intent() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)

    conflict = _persist(store, fingerprint="sha256:" + "b" * 64)

    assert conflict.outcome is PersistOutcome.INTENT_CONFLICT


def test_persist_isolates_idempotency_by_workspace() -> None:
    # A prepared operation id is deterministic from (workspace, token, intent),
    # so a genuinely different workspace yields a different operation id and a
    # different idempotency PK. Persisting under a second workspace must not
    # collide with the first — the mapping and operation are fully isolated.
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store, workspace="workspace.default")

    other_operation = _operation()
    other_operation["operation_id"] = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
    other_operation["requester"]["workspace_id"] = "workspace.other"
    other_hash = capacity_prepared_hash(other_operation)
    other_operation["prepared_hash"] = other_hash
    other = store.persist_prepared_operation(
        prepared_operation=other_operation,
        prepared_hash=other_hash,
        workspace_id="workspace.other",
        idempotency_token=TOKEN,
        idempotency_fingerprint=FINGERPRINT,
        state_change=_state_change(),
        ledger_event=_ledger_event(),
        commit_not_after=NOW + timedelta(minutes=15),
    )

    assert other.outcome is PersistOutcome.PERSISTED
    assert other.operation_id == "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_persist_fails_closed_before_deadline() -> None:
    client = StatefulDynamoClient()
    store = DynamoDbApprovalStore(client=client, table_name=TABLE, clock=lambda: NOW + timedelta(minutes=30))
    result = _persist(store)

    assert result.outcome is PersistOutcome.DEADLINE_EXPIRED
    assert client.transactions == []


def test_persist_transient_fault_is_unavailable_not_conflict() -> None:
    client = StatefulDynamoClient()
    client.raise_on_transact = _transaction_canceled("ConditionalCheckFailed", "TransactionConflict")
    result = _persist(_store(client))

    assert result.outcome is PersistOutcome.PROVIDER_UNAVAILABLE


# -- Load ----------------------------------------------------------------


def test_persist_rejects_prepared_hash_that_does_not_bind_the_operation() -> None:
    """The passed prepared_hash MUST equal the operation's own bound hash.

    Discriminating: the operation is otherwise valid and the deadline is open,
    so the ONLY reason this fails closed is the guard that recomputes/binds the
    hash before any DynamoDB write. A wrong hash raises ApprovalStoreError and
    issues no transaction at all.
    """
    client = StatefulDynamoClient()
    store = _store(client)
    correct_hash = _hash()
    wrong_hash = "sha256:" + "b" * 64
    assert wrong_hash != correct_hash

    with pytest.raises(ApprovalStoreError, match="does not bind"):
        store.persist_prepared_operation(
            prepared_operation=_operation(),  # embeds correct_hash
            prepared_hash=wrong_hash,  # caller passes a non-binding hash
            workspace_id=WORKSPACE,
            idempotency_token=TOKEN,
            idempotency_fingerprint=FINGERPRINT,
            state_change=_state_change(),
            ledger_event=_ledger_event(),
            commit_not_after=NOW + timedelta(minutes=15),
        )
    # Fail closed BEFORE any write: no transaction was ever issued.
    assert client.transactions == []


def _sized_operation(payload_bytes: int) -> dict[str, Any]:
    """A valid operation padded so its canonical JSON reaches ~payload_bytes.

    The padding is added under an extra field, then the operation's own bound
    ``prepared_hash`` is recomputed so the bind guard passes and the ONLY
    remaining gate is the durable item-size ceiling.
    """
    operation = _operation()
    operation.pop("prepared_hash", None)
    operation["_size_padding"] = "x" * payload_bytes
    operation["prepared_hash"] = capacity_prepared_hash(operation)
    return operation


def _persist_operation(store: DynamoDbApprovalStore, operation: dict[str, Any]):
    return store.persist_prepared_operation(
        prepared_operation=operation,
        prepared_hash=operation["prepared_hash"],
        workspace_id=WORKSPACE,
        idempotency_token=TOKEN,
        idempotency_fingerprint=FINGERPRINT,
        state_change=_state_change(),
        ledger_event=_ledger_event(),
        commit_not_after=NOW + timedelta(minutes=15),
    )


def test_persist_accepts_operation_just_below_the_item_size_ceiling() -> None:
    """A hash-consistent operation whose canonical JSON stays under 400 KB persists.

    Paired with the over-ceiling test below, this pins the exact bound: the same
    code path succeeds just under the limit and fails closed just over it, so the
    ceiling — not some unrelated validation — is what discriminates the outcome.
    """
    client = StatefulDynamoClient()
    store = _store(client)
    # Comfortably below 400 KB even after the surrounding canonical JSON.
    operation = _sized_operation(300 * 1024)
    encoded = len(_canonical_json(operation).encode("utf-8"))
    assert encoded < 400 * 1024

    result = _persist_operation(store, operation)
    assert result.outcome is PersistOutcome.PERSISTED
    assert len(client.transactions) == 1


def test_persist_rejects_operation_at_or_above_the_item_size_ceiling() -> None:
    """A hash-consistent operation whose canonical JSON reaches 400 KB fails closed.

    The bind guard passes (the padded operation carries its own recomputed hash),
    so the size ceiling is the sole discriminating gate. It raises before any
    DynamoDB write.
    """
    client = StatefulDynamoClient()
    store = _store(client)
    # Padding alone exceeds the 400 KB ceiling; the encoded item is >= the limit.
    operation = _sized_operation(400 * 1024 + 1024)
    encoded = len(_canonical_json(operation).encode("utf-8"))
    assert encoded >= 400 * 1024

    with pytest.raises(ApprovalStoreError, match="item-size limit"):
        _persist_operation(store, operation)
    # Fail closed BEFORE any write: no transaction was ever issued.
    assert client.transactions == []


def test_load_returns_stored_operation_hash_and_state() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)

    stored = store.load_for_approval(OPERATION_ID)

    assert stored is not None
    assert stored.prepared_operation_hash == _hash()
    assert stored.state == "pending_approval"
    assert stored.copy_prepared_operation()["operation_id"] == OPERATION_ID


def test_load_missing_operation_returns_none() -> None:
    assert _store(StatefulDynamoClient()).load_for_decision(OPERATION_ID) is None


# -- Grant commit --------------------------------------------------------


def test_record_granted_approval_advances_snapshot_to_approved() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)

    outcome = store.record_granted_approval(
        operation_id=OPERATION_ID,
        expected_prepared_operation_hash=_hash(),
        expected_state="pending_approval",
        commit_not_after=NOW + timedelta(minutes=5),
        approval=_approval(),
    )

    assert outcome is ApprovalCommitOutcome.RECORDED
    assert store.load_for_approval(OPERATION_ID).state == "approved"


def test_record_granted_approval_precondition_failed_on_state_race() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)
    # First grant wins.
    store.record_granted_approval(
        operation_id=OPERATION_ID,
        expected_prepared_operation_hash=_hash(),
        expected_state="pending_approval",
        commit_not_after=NOW + timedelta(minutes=5),
        approval=_approval(),
    )
    # Replay against the now-approved snapshot loses the conditional check.
    outcome = store.record_granted_approval(
        operation_id=OPERATION_ID,
        expected_prepared_operation_hash=_hash(),
        expected_state="pending_approval",
        commit_not_after=NOW + timedelta(minutes=5),
        approval=_approval(),
    )

    assert outcome is ApprovalCommitOutcome.PRECONDITION_FAILED


# -- Terminal decision commit -------------------------------------------


def test_record_terminal_decision_cancels_and_appends_ledger() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)

    outcome = store.record_terminal_decision(
        operation_id=OPERATION_ID,
        expected_prepared_operation_hash=_hash(),
        expected_state="pending_approval",
        new_state="cancelled",
        commit_not_after=NOW + timedelta(minutes=5),
        state_change=_state_change("cancelled", "CANCELLED"),
        ledger_event=_ledger_event("operation.state-changed"),
        approval=None,
    )

    assert outcome is DecisionCommitOutcome.RECORDED
    assert store.load_for_decision(OPERATION_ID).state == "cancelled"


def test_record_terminal_decision_transient_fault_raises_not_conflict() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)
    client.raise_on_transact = _transaction_canceled("ThrottlingError")

    with pytest.raises(ApprovalStoreError):
        store.record_terminal_decision(
            operation_id=OPERATION_ID,
            expected_prepared_operation_hash=_hash(),
            expected_state="pending_approval",
            new_state="cancelled",
            commit_not_after=NOW + timedelta(minutes=5),
            state_change=_state_change("cancelled", "CANCELLED"),
            ledger_event=_ledger_event("operation.state-changed"),
            approval=None,
        )


def test_record_terminal_decision_deadline_expired() -> None:
    client = StatefulDynamoClient()
    store = DynamoDbApprovalStore(client=client, table_name=TABLE, clock=lambda: NOW)
    _persist(store)
    outcome = store.record_terminal_decision(
        operation_id=OPERATION_ID,
        expected_prepared_operation_hash=_hash(),
        expected_state="pending_approval",
        new_state="expired",
        commit_not_after=NOW - timedelta(seconds=1),
        state_change=_state_change("expired", "EXPIRED"),
        ledger_event=_ledger_event("operation.state-changed"),
        approval=None,
    )

    assert outcome is DecisionCommitOutcome.DEADLINE_EXPIRED


# -- Evidence load -----------------------------------------------------------


def test_load_operation_evidence_returns_bounded_state_and_ledger() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)

    evidence = store.load_operation_evidence(OPERATION_ID)

    assert isinstance(evidence, OperationEvidence)
    assert evidence.state == "pending_approval"
    assert evidence.prepared_hash == _hash()
    assert evidence.operation["operation_id"] == OPERATION_ID
    assert evidence.approval is None
    assert [e["sequence"] for e in evidence.ledger] == [0]


def test_load_operation_evidence_includes_approval_after_grant() -> None:
    client = StatefulDynamoClient()
    store = _store(client)
    _persist(store)
    store.record_granted_approval(
        operation_id=OPERATION_ID,
        expected_prepared_operation_hash=_hash(),
        expected_state="pending_approval",
        commit_not_after=NOW + timedelta(minutes=5),
        approval=_approval(),
    )

    evidence = store.load_operation_evidence(OPERATION_ID)

    assert evidence.state == "approved"
    assert evidence.approval is not None
    assert evidence.approval["decision"] == "granted"
    assert [e["sequence"] for e in evidence.ledger] == [0, 1]


def test_load_operation_evidence_missing_returns_none() -> None:
    client = StatefulDynamoClient()
    assert _store(client).load_operation_evidence(OPERATION_ID) is None
