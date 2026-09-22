"""Control audit store tests (issue #416, E4).

:class:`~operations.control.control_audit_store.DynamoDbControlAuditStore` is the
durable backing store for the admin kill-switch control plane. It:

* reads the current control state (the authoritative ``config_version``);
* writes an immutable *intent* audit record before the decision is attempted;
* commits the decision with a compare-and-set on ``expected_config_version`` —
  the state item advances and an immutable *outcome* audit record is written in
  ONE atomic transaction, so a stale write (the stored version moved on) fails
  the CAS and is reported as a version conflict, never a silent clobber;
* writes control audit records that carry a bound ``record_hash`` and never
  expire (no TTL).

These tests assert the CAS advance, the version-conflict race, the immutable
audit records, and that the store never issues a Scan.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID
from operations.control.control_audit_store import (
    ControlCommitOutcome,
    ControlStoreError,
    DynamoDbControlAuditStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TABLE = "operations-table"


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.scans = 0
        self.put_calls = 0

    def put_item(self, *, TableName: str, Item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.put_calls += 1
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
        staged: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                cond = entry["Put"].get("ConditionExpression", "")
                if "attribute_not_exists" in cond and (key in self.items or key in staged):
                    raise _transaction_canceled()
                staged[key] = item
            elif "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                values = upd.get("ExpressionAttributeValues", {})
                existing = self.items.get(key) or staged.get(key)
                # CAS: current config_version must equal :expected.
                if ":expected" in values:
                    current = existing.get("config_version", {}).get("N") if existing else None
                    if current != values[":expected"]["N"]:
                        raise _transaction_canceled()
                new_item = dict(existing) if existing else {"PK": upd["Key"]["PK"], "SK": upd["Key"]["SK"]}
                if ":new_version" in values:
                    new_item["config_version"] = values[":new_version"]
                staged[key] = new_item
        self.items.update(staged)
        return {}

    def scan(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self.scans += 1
        raise AssertionError("control audit store must never Scan")


def _conditional_error() -> Exception:
    exc = Exception("ConditionalCheckFailed")
    exc.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
    return exc


def _transaction_canceled() -> Exception:
    exc = Exception("TransactionCanceled")
    exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "TransactionCanceledException"},
        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
    }
    return exc


def _store(dynamo: _FakeDynamo) -> DynamoDbControlAuditStore:
    return DynamoDbControlAuditStore(client=dynamo, table_name=_TABLE, clock=lambda: _NOW)


def _desired(enabled: bool = True) -> dict[str, Any]:
    return {
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": enabled, "dispatch": enabled, "execute": enabled}},
    }


def _seed_state(store: DynamoDbControlAuditStore, dynamo: _FakeDynamo, version: int) -> None:
    store.initialize_state_if_absent(config_version=version)


def _actor() -> dict[str, str]:
    return {"subject_id": "admin-1", "client_id": "client-1"}


def _intent_and_outcome(store: DynamoDbControlAuditStore, **overrides: Any) -> Any:
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


# -- CAS commit -------------------------------------------------------------


def test_applied_commit_advances_version_and_writes_audit() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    outcome = _intent_and_outcome(store)
    assert outcome is ControlCommitOutcome.COMMITTED
    assert store.current_config_version() == 2
    # An immutable outcome audit record exists and never carries a ttl.
    audit_items = [
        item
        for (pk, _sk), item in dynamo.items.items()
        if item.get("record_type", {}).get("S") == "control_audit_record"
    ]
    assert audit_items
    for item in audit_items:
        assert "ttl" not in item


def test_stale_expected_version_fails_cas_as_conflict() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=3)
    # The admin believes v1 is current, but v3 is stored: CAS must reject.
    outcome = _intent_and_outcome(store, expected_config_version=1, resulting_config_version=2)
    assert outcome is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 3  # unchanged


def test_two_concurrent_writers_only_one_wins() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    first = _intent_and_outcome(
        store, record_id="ctl_" + "a" * 26, expected_config_version=1, resulting_config_version=2
    )
    second = _intent_and_outcome(
        store, record_id="ctl_" + "b" * 26, expected_config_version=1, resulting_config_version=2
    )
    assert first is ControlCommitOutcome.COMMITTED
    assert second is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 2


def test_records_intent_before_decision() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    store.record_control_intent(record_id="ctl_" + "c" * 26, actor=_actor(), desired=_desired())
    intents = [
        item
        for (pk, _sk), item in dynamo.items.items()
        if item.get("record_type", {}).get("S") == "control_intent_record"
    ]
    assert len(intents) == 1
    assert "ttl" not in intents[0]


def test_provider_unavailable_is_not_a_false_conflict() -> None:
    dynamo = _FakeDynamo()

    def boom(**kwargs: Any) -> dict[str, Any]:
        exc = Exception("Throttled")
        exc.response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}  # type: ignore[attr-defined]
        raise exc

    dynamo.transact_write_items = boom  # type: ignore[assignment]
    store = _store(dynamo)
    store.items = {}  # ensure state read path
    dynamo.items[("OPCONTROL#kill-switch", "STATE#current")] = {
        "PK": {"S": "OPCONTROL#kill-switch"},
        "SK": {"S": "STATE#current"},
        "config_version": {"N": "1"},
    }
    with pytest.raises(ControlStoreError):
        _intent_and_outcome(store)


def test_never_scans() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    _intent_and_outcome(store)
    store.current_config_version()
    assert dynamo.scans == 0
