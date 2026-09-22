"""Control audit store tests (issue #416, E4).

:class:`~operations.control.control_audit_store.DynamoDbControlAuditStore` is the
durable backing store for the admin kill-switch control plane. It:

* reads the current control state (the authoritative ``config_version``);
* writes an immutable *intent* audit record before the decision is attempted;
* commits the decision with a compare-and-set on ``expected_config_version`` —
  the state item advances and an immutable *outcome* audit record are written in
  ONE atomic ``TransactWriteItems`` call, so a stale write (the stored version
  moved on) fails the CAS and is reported as a version conflict, never a silent
  clobber, and the version can never advance without its outcome audit record;
* writes control audit records that carry a bound ``record_hash`` and never
  expire (no TTL).

These tests assert the atomic CAS advance, the version-conflict race, that the
version cannot advance without an audit record, that a transient second-leg
failure never permanently loses the outcome (a retry recovers it), the immutable
audit records, and that the store never issues a Scan.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID
from operations.control.control_audit_store import ControlCommitOutcome, ControlStoreError, DynamoDbControlAuditStore

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TABLE = "operations-table"


class _FakeDynamo:
    """A stateful DynamoDB fake modelling atomic ``TransactWriteItems`` semantics.

    ``transact_write_items`` evaluates every leg's condition first and applies
    NONE of the writes if any condition fails or any hook raises — the same
    all-or-nothing guarantee DynamoDB gives. This lets the tests prove that the
    control state item can never advance without its paired outcome audit record.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.scans = 0
        self.transact_calls = 0
        # Optional hook: raise this on the next transact call to model a
        # transient provider fault, then clear it so a retry can succeed.
        self.fail_transact_once: Exception | None = None

    def put_item(self, *, TableName: str, Item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if "ConditionExpression" in kwargs and "attribute_not_exists" in kwargs["ConditionExpression"]:
            if key in self.items:
                raise _conditional_error()
        self.items[key] = Item
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transact_calls += 1
        if self.fail_transact_once is not None:
            exc = self.fail_transact_once
            self.fail_transact_once = None
            raise exc
        # Phase 1: evaluate every condition without mutating anything.
        reasons: list[str] = []
        for entry in TransactItems:
            if "Update" in entry:
                reasons.append(self._eval_update(entry["Update"]))
            elif "Put" in entry:
                reasons.append(self._eval_put(entry["Put"]))
            else:  # pragma: no cover - defensive
                raise AssertionError(f"unsupported transact leg: {entry!r}")
        if any(reason != "None" for reason in reasons):
            raise _transaction_cancelled(reasons)
        # Phase 2: all conditions passed; apply every write atomically.
        for entry in TransactItems:
            if "Update" in entry:
                self._apply_update(entry["Update"])
            elif "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
        return {}

    def _eval_update(self, upd: dict[str, Any]) -> str:
        key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
        existing = self.items.get(key)
        values = upd.get("ExpressionAttributeValues", {})
        cond = upd.get("ConditionExpression", "")
        if "attribute_exists" in cond and existing is None:
            return "ConditionalCheckFailed"
        if ":expected" in values:
            current = existing.get("config_version", {}).get("N") if existing else None
            if current != values[":expected"]["N"]:
                return "ConditionalCheckFailed"
        return "None"

    def _eval_put(self, put: dict[str, Any]) -> str:
        item = put["Item"]
        key = (item["PK"]["S"], item["SK"]["S"])
        cond = put.get("ConditionExpression", "")
        if "attribute_not_exists" in cond and key in self.items:
            return "ConditionalCheckFailed"
        return "None"

    def _apply_update(self, upd: dict[str, Any]) -> None:
        key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
        existing = self.items.get(key)
        values = upd.get("ExpressionAttributeValues", {})
        new_item = dict(existing) if existing else {"PK": upd["Key"]["PK"], "SK": upd["Key"]["SK"]}
        if ":new_version" in values:
            new_item["config_version"] = values[":new_version"]
        self.items[key] = new_item

    def update_item(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("control CAS must be a single atomic TransactWriteItems, not UpdateItem")

    def scan(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self.scans += 1
        raise AssertionError("control audit store must never Scan")


def _conditional_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition failed"}},
        "PutItem",
    )


def _transaction_cancelled(reason_codes: list[str]) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
            "CancellationReasons": [{"Code": code} for code in reason_codes],
        },
        "TransactWriteItems",
    )


def _throttle_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
        "TransactWriteItems",
    )


def _store(dynamo: _FakeDynamo) -> DynamoDbControlAuditStore:
    return DynamoDbControlAuditStore(client=dynamo, table_name=_TABLE, clock=lambda: _NOW)


def _desired(enabled: bool = True) -> dict[str, Any]:
    return {
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": enabled, "dispatch": enabled, "execute": enabled}},
    }


def _actor() -> dict[str, str]:
    return {"subject_id": "admin-1", "client_id": "client-1"}


def _commit(store: DynamoDbControlAuditStore, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "record_id": "ctl_" + "a" * 26,
        "actor": _actor(),
        "expected_config_version": 1,
        "desired": _desired(),
        "outcome": "applied",
        "resulting_config_version": 2,
    }
    kwargs.update(overrides)
    return store.commit_control_decision(**kwargs)


def _audit_items(dynamo: _FakeDynamo) -> list[dict[str, Any]]:
    return [item for item in dynamo.items.values() if item.get("record_type", {}).get("S") == "control_audit_record"]


# -- current state ----------------------------------------------------------


def test_reads_current_config_version() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=5)
    assert store.current_config_version() == 5


def test_initialize_is_idempotent() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    store.initialize_state_if_absent(config_version=99)  # no-op, already present
    assert store.current_config_version() == 1


# -- atomic CAS commit ------------------------------------------------------


def test_applied_commit_advances_version_and_writes_audit() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    outcome = _commit(store)
    assert outcome is ControlCommitOutcome.COMMITTED
    assert store.current_config_version() == 2
    audit_items = _audit_items(dynamo)
    assert audit_items
    for item in audit_items:
        assert "ttl" not in item


def test_external_appconfig_reconciliation_is_atomic_and_already_published() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=2)
    record_id = "ctl_" + "e" * 26
    outcome = store.reconcile_external_control(
        record_id=record_id,
        actor=_actor(),
        expected_config_version=2,
        desired=_desired(False),
        resulting_config_version=1_800_000_000,
    )
    assert outcome is ControlCommitOutcome.COMMITTED
    assert store.current_config_version() == 1_800_000_000
    assert len(_audit_items(dynamo)) == 1
    assert store.pending_publication(record_id=record_id) is None


def test_commit_is_a_single_atomic_transact_write() -> None:
    """The CAS advance + outcome audit Put commit in ONE TransactWriteItems."""
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    _commit(store)
    # Exactly one transaction carried both the state Update and the audit Put.
    assert dynamo.transact_calls == 1


def test_stale_expected_version_fails_cas_as_conflict() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=3)
    # The admin believes v1 is current, but v3 is stored: CAS must reject.
    outcome = _commit(store, expected_config_version=1, resulting_config_version=2)
    assert outcome is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 3  # unchanged
    # No outcome audit record was written for a rejected CAS.
    assert not _audit_items(dynamo)


def test_two_concurrent_writers_only_one_wins() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    first = _commit(store, record_id="ctl_" + "a" * 26, expected_config_version=1, resulting_config_version=2)
    second = _commit(store, record_id="ctl_" + "b" * 26, expected_config_version=1, resulting_config_version=2)
    assert first is ControlCommitOutcome.COMMITTED
    assert second is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 2


def test_version_cannot_advance_without_its_audit_record() -> None:
    """If the audit Put leg fails its condition, the version must NOT advance.

    A pre-existing outcome record for this ``record_id`` fails the audit Put's
    ``attribute_not_exists(SK)`` condition. Because the state advance and the
    audit Put share ONE transaction, the state item cannot advance while the
    audit leg is rejected — proving atomicity, not a best-effort follow-up write.
    """
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    # Pre-seed the exact outcome audit SK this commit would write.
    record_id = "ctl_" + "d" * 26
    outcome_key = ("OPCONTROL#kill-switch", f"CTLAUDIT#{record_id}")
    dynamo.items[outcome_key] = {
        "PK": {"S": outcome_key[0]},
        "SK": {"S": outcome_key[1]},
        "record_type": {"S": "control_audit_record"},
    }
    version_before = store.current_config_version()
    outcome = _commit(store, record_id=record_id, expected_config_version=1, resulting_config_version=2)
    # The audit SK already existed → treated as an idempotent replay: the
    # decision is already recorded, and the version has NOT double-advanced.
    assert outcome is ControlCommitOutcome.COMMITTED
    assert store.current_config_version() == version_before == 1


def test_retry_after_transient_transact_failure_recovers_outcome() -> None:
    """A transient transaction fault never permanently loses the outcome.

    The first attempt raises a throttle mid-commit; because the write is atomic,
    NEITHER the version advance NOR the audit record is applied. A retry then
    commits both together, so the outcome audit is never permanently lost and the
    version advances exactly once.
    """
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)

    # First attempt: transient throttle inside the transaction, applied to none.
    dynamo.fail_transact_once = _throttle_error()
    with pytest.raises(ControlStoreError):
        _commit(store)
    assert store.current_config_version() == 1  # unchanged: nothing applied
    assert not _audit_items(dynamo)  # no orphaned or missing audit state

    # Retry with the same arguments: now both legs commit atomically.
    outcome = _commit(store)
    assert outcome is ControlCommitOutcome.COMMITTED
    assert store.current_config_version() == 2
    assert len(_audit_items(dynamo)) == 1  # exactly one outcome record


def test_records_intent_before_decision() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    store.record_control_intent(record_id="ctl_" + "c" * 26, actor=_actor(), desired=_desired())
    intents = [
        item for item in dynamo.items.values() if item.get("record_type", {}).get("S") == "control_intent_record"
    ]
    assert len(intents) == 1
    assert "ttl" not in intents[0]


def test_throttle_is_not_a_false_conflict() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    dynamo.fail_transact_once = _throttle_error()
    with pytest.raises(ControlStoreError):
        _commit(store)
    assert store.current_config_version() == 1


def test_client_error_transaction_conflict_is_retryable_not_a_conflict() -> None:
    """A TransactionCanceledException citing a transient reason stays retryable.

    A cancellation whose reasons include a non-conditional code (a transaction
    conflict, throttle, or validation error) must raise a retryable
    ``ControlStoreError``, never masquerade as a version conflict.
    """
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    dynamo.fail_transact_once = _transaction_cancelled(["None", "TransactionConflict"])
    with pytest.raises(ControlStoreError):
        _commit(store)
    assert store.current_config_version() == 1


def test_race_pure_conditional_cancellation_is_a_version_conflict() -> None:
    """A cancellation whose only cited reason is the state CAS is a clean race."""
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    dynamo.fail_transact_once = _transaction_cancelled(["ConditionalCheckFailed", "None"])
    outcome = _commit(store)
    assert outcome is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 1


def test_never_scans() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    _commit(store)
    store.current_config_version()
    assert dynamo.scans == 0


# -- durable pending-publication reconciliation -----------------------------


def _pub_markers(dynamo: _FakeDynamo) -> list[dict[str, Any]]:
    return [
        item for item in dynamo.items.values() if item.get("record_type", {}).get("S") == "control_publication_marker"
    ]


def test_commit_records_a_durable_unconfirmed_publication_marker() -> None:
    """A committed decision durably records intent to publish, unconfirmed."""
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    record_id = "ctl_" + "a" * 26
    _commit(store, record_id=record_id)
    markers = _pub_markers(dynamo)
    assert len(markers) == 1
    assert markers[0]["published"]["BOOL"] is False
    assert markers[0]["config_version"]["N"] == "2"
    # The marker commits in the SAME atomic transaction as the CAS + audit.
    assert dynamo.transact_calls == 1
    # It is queryable and reported as pending.
    pending = store.pending_publication(record_id=record_id)
    assert pending is not None and pending["config_version"] == 2 and pending["published"] is False


def test_confirm_publication_marks_marker_published_and_idempotent() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    record_id = "ctl_" + "a" * 26
    _commit(store, record_id=record_id)
    store.confirm_publication(record_id=record_id)
    assert store.pending_publication(record_id=record_id) is None  # no longer pending
    # Confirming again is a harmless no-op.
    store.confirm_publication(record_id=record_id)
    assert store.pending_publication(record_id=record_id) is None


def test_no_publication_marker_for_an_unknown_record() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    assert store.pending_publication(record_id="ctl_" + "z" * 26) is None
