"""Durable dispatch audit records on the bundle store (issue #439).

Terminal execution must never claim an unproven predecessor. The bundle store
therefore records a ``dispatch_requested`` state record *before* StartExecution
and a ``dispatched`` record only after a confirmed (or idempotent) start. After
StartExecution uncertainty the runtime retains the ``dispatch_requested``
evidence and never fabricates a ``dispatched`` record. Both records are written
with conditional immutability so a replay cannot rewrite the audit trail, and
``dispatched`` is additionally fenced — in one ``TransactWriteItems`` — on a
matching ``dispatch_requested`` predecessor.
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


class _TransactionCancelled(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("transaction cancelled")
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": code} for code in reasons],
        }


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

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> None:
        reasons: list[str] = []
        staged: list[tuple[tuple[str, str], dict[str, Any]]] = []
        failed = False
        for entry in TransactItems:
            if "ConditionCheck" in entry:
                cc = entry["ConditionCheck"]
                key = (cc["Key"]["PK"]["S"], cc["Key"]["SK"]["S"])
                ok = key in self.items
                if ok and ":name" in cc.get("ExpressionAttributeValues", {}):
                    stored = self.items[key]
                    ok = stored.get("execution_name", {}).get("S") == cc["ExpressionAttributeValues"][":name"]["S"]
                reasons.append("None" if ok else "ConditionalCheckFailed")
                failed = failed or not ok
            elif "Put" in entry:
                put = entry["Put"]
                key = (put["Item"]["PK"]["S"], put["Item"]["SK"]["S"])
                ok = not ("attribute_not_exists" in put.get("ConditionExpression", "") and key in self.items)
                reasons.append("None" if ok else "ConditionalCheckFailed")
                if ok:
                    staged.append((key, put["Item"]))
                else:
                    failed = True
        if failed:
            raise _TransactionCancelled(reasons)
        for key, item in staged:
            self.items[key] = item


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
