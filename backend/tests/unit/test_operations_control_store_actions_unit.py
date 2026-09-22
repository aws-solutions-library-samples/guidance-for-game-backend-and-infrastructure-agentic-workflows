"""Control store uses only PutItem+UpdateItem+GetItem, never TransactWriteItems.

The E4 control audit store's IAM policy grants only ``dynamodb:PutItem``,
``dynamodb:UpdateItem``, ``dynamodb:GetItem`` (and ``Query`` for the read path).
The CAS commit MUST therefore be expressed as a conditional ``UpdateItem`` on the
state item plus a separate ``PutItem`` for the immutable outcome record — never a
``TransactWriteItems`` call, which would require an IAM action the deployment
does not grant and would fail at runtime with AccessDenied.

These tests use a fake DynamoDB client that RAISES if ``transact_write_items`` is
ever called, so a regression to a transaction is caught immediately, while still
proving the CAS advance, the version-conflict race, and the never-Scan / never-
transact guarantees.
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
    DynamoDbControlAuditStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TABLE = "operations-table"


def _conditional_error() -> Exception:
    exc = Exception("ConditionalCheckFailed")
    exc.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
    return exc


class _NoTransactDynamo:
    """A fake DynamoDB client that only supports PutItem/UpdateItem/GetItem."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.actions: list[str] = []

    def put_item(self, *, TableName: str, Item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.actions.append("put_item")
        key = (Item["PK"]["S"], Item["SK"]["S"])
        cond = kwargs.get("ConditionExpression", "")
        if "attribute_not_exists" in cond and key in self.items:
            raise _conditional_error()
        self.items[key] = Item
        return {}

    def update_item(self, *, TableName: str, Key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.actions.append("update_item")
        key = (Key["PK"]["S"], Key["SK"]["S"])
        existing = self.items.get(key)
        values = kwargs.get("ExpressionAttributeValues", {})
        cond = kwargs.get("ConditionExpression", "")
        if "attribute_exists" in cond and existing is None:
            raise _conditional_error()
        if ":expected" in values:
            current = existing.get("config_version", {}).get("N") if existing else None
            if current != values[":expected"]["N"]:
                raise _conditional_error()
        new_item = dict(existing) if existing else {"PK": Key["PK"], "SK": Key["SK"]}
        if ":new_version" in values:
            new_item["config_version"] = values[":new_version"]
        self.items[key] = new_item
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        self.actions.append("get_item")
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("control store must not use TransactWriteItems")

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("control store must never Scan")


def _store(dynamo: _NoTransactDynamo) -> DynamoDbControlAuditStore:
    return DynamoDbControlAuditStore(client=dynamo, table_name=_TABLE, clock=lambda: _NOW)


def _desired(enabled: bool = True) -> dict[str, Any]:
    return {
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": enabled, "dispatch": enabled, "execute": enabled}},
    }


def _actor() -> dict[str, str]:
    return {"subject_id": "admin-1", "client_id": "client-1"}


def _commit(store: DynamoDbControlAuditStore, **overrides: Any) -> ControlCommitOutcome:
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


def test_commit_advances_without_transaction() -> None:
    dynamo = _NoTransactDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    outcome = _commit(store)
    assert outcome is ControlCommitOutcome.COMMITTED
    assert store.current_config_version() == 2
    # Only the three permitted underlying actions were ever used.
    assert set(dynamo.actions) <= {"put_item", "update_item", "get_item"}
    assert "update_item" in dynamo.actions  # the CAS advance
    # An immutable outcome audit record exists and never carries a ttl.
    audit = [
        item
        for (_pk, _sk), item in dynamo.items.items()
        if item.get("record_type", {}).get("S") == "control_audit_record"
    ]
    assert audit and all("ttl" not in item for item in audit)


def test_stale_version_is_conflict_and_writes_no_audit() -> None:
    dynamo = _NoTransactDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=3)
    outcome = _commit(store, expected_config_version=1, resulting_config_version=2)
    assert outcome is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 3  # unchanged
    # A conflicting decision must NOT leave a committed outcome audit record.
    audit = [
        item
        for (_pk, _sk), item in dynamo.items.items()
        if item.get("record_type", {}).get("S") == "control_audit_record"
    ]
    assert not audit


def test_two_concurrent_writers_only_one_wins() -> None:
    dynamo = _NoTransactDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    first = _commit(store, record_id="ctl_" + "a" * 26, expected_config_version=1, resulting_config_version=2)
    second = _commit(store, record_id="ctl_" + "b" * 26, expected_config_version=1, resulting_config_version=2)
    assert first is ControlCommitOutcome.COMMITTED
    assert second is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 2
