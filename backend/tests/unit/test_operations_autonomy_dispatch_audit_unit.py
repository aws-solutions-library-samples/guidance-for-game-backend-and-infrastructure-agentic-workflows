"""Durable dispatch audit records on the bundle store (issue #439).

Terminal execution must never claim an unproven predecessor. The bundle store
therefore records a ``dispatch_requested`` state record *before* StartExecution
and a ``dispatched`` record only after a confirmed (or idempotent) start. After
StartExecution uncertainty the runtime retains the ``dispatch_requested``
evidence and never fabricates a ``dispatched`` record. Both records are written
with conditional immutability so a replay cannot rewrite the audit trail.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime.store import AutonomyBundleStoreError, DynamoDbAutonomyBundleStore

_OP = "op_" + "e" * 26
_NAME = "op_" + "e" * 26


class _ConditionalFailure(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, *, TableName: str, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and key in self.items:
            raise _ConditionalFailure()
        self.items[key] = Item

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item is not None else {}


@pytest.mark.unit
def test_dispatch_requested_then_dispatched_are_persisted() -> None:
    client = _FakeDynamo()
    store = DynamoDbAutonomyBundleStore(client=client, table_name="ops-06")

    store.record_dispatch_requested(operation_id=_OP, execution_name=_NAME)
    store.record_dispatched(operation_id=_OP, execution_name=_NAME)

    requested = store.load_dispatch_audit(operation_id=_OP, phase="dispatch_requested")
    dispatched = store.load_dispatch_audit(operation_id=_OP, phase="dispatched")
    assert requested is not None and requested["execution_name"] == _NAME
    assert dispatched is not None and dispatched["execution_name"] == _NAME


@pytest.mark.unit
def test_dispatch_requested_is_immutable_idempotent() -> None:
    client = _FakeDynamo()
    store = DynamoDbAutonomyBundleStore(client=client, table_name="ops-06")

    store.record_dispatch_requested(operation_id=_OP, execution_name=_NAME)
    # An identical re-record is idempotent (no raise).
    store.record_dispatch_requested(operation_id=_OP, execution_name=_NAME)
    # A DIFFERING re-record must fail closed rather than rewrite the audit trail.
    with pytest.raises(AutonomyBundleStoreError):
        store.record_dispatch_requested(operation_id=_OP, execution_name="op_" + "f" * 26)


@pytest.mark.unit
def test_missing_audit_record_loads_as_none() -> None:
    client = _FakeDynamo()
    store = DynamoDbAutonomyBundleStore(client=client, table_name="ops-06")
    assert store.load_dispatch_audit(operation_id=_OP, phase="dispatched") is None
