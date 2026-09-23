"""``record_dispatched`` is a fenced TransactWriteItems, not an independent Put (#439).

The second E5 blocker: ``dispatched`` was an independent conditional ``Put`` and
the executor consulted only that single record. A ``dispatched`` marker could
therefore exist (or be forged) without a matching ``dispatch_requested``
predecessor, and the executor would still run.

This suite locks the fix on the store side:

* ``record_dispatched`` issues ONE ``TransactWriteItems`` containing
  (a) a ``ConditionCheck`` that the exact ``dispatch_requested`` item already
  exists for this operation id and execution name, and
  (b) a conditional immutable ``Put`` of the ``dispatched`` record.
* When no matching ``dispatch_requested`` exists the transaction is cancelled
  and the store fails closed — no ``dispatched`` record is written.
* An identical re-record is idempotent; a differing execution name fails closed.

The store never falls back to an independent ``put_item`` for ``dispatched``.
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
_OTHER_NAME = "op_" + "f" * 26


class _Cancelled(Exception):
    """A TransactWriteItems cancellation with a conditional-check reason."""

    def __init__(self, codes: list[str]) -> None:
        super().__init__("transaction cancelled")
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": code} for code in codes],
        }


class _ConditionalPut(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _TransactFakeDynamo:
    """Fake supporting get_item, put_item, and transact_write_items.

    Implements the exact TransactWriteItems semantics the store relies on:
    every item's condition (ConditionCheck existence and Put attribute_not_exists)
    is evaluated together; if any fails the whole transaction is cancelled with a
    ConditionalCheckFailed reason for that item and NOTHING is written.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transacts: list[list[dict[str, Any]]] = []
        self.put_calls: list[dict[str, Any]] = []

    def put_item(self, *, TableName: str, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        self.put_calls.append(Item)
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and key in self.items:
            raise _ConditionalPut()
        self.items[key] = Item

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item is not None else {}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> None:
        self.transacts.append(TransactItems)
        reasons: list[str] = []
        staged: list[tuple[tuple[str, str], dict[str, Any]]] = []
        failed = False
        for entry in TransactItems:
            if "ConditionCheck" in entry:
                cc = entry["ConditionCheck"]
                key = (cc["Key"]["PK"]["S"], cc["Key"]["SK"]["S"])
                exists = key in self.items
                needs_exists = "attribute_exists" in cc.get("ConditionExpression", "")
                # Optional equality guard on execution_name.
                ok = exists if needs_exists else True
                if ok and needs_exists:
                    values = cc.get("ExpressionAttributeValues", {})
                    if ":name" in values:
                        stored = self.items[key]
                        ok = stored.get("execution_name", {}).get("S") == values[":name"]["S"]
                reasons.append("None" if ok else "ConditionalCheckFailed")
                if not ok:
                    failed = True
            elif "Put" in entry:
                put = entry["Put"]
                key = (put["Item"]["PK"]["S"], put["Item"]["SK"]["S"])
                cond = put.get("ConditionExpression", "")
                ok = not ("attribute_not_exists" in cond and key in self.items)
                reasons.append("None" if ok else "ConditionalCheckFailed")
                if not ok:
                    failed = True
                else:
                    staged.append((key, put["Item"]))
            else:  # pragma: no cover - unexpected shape
                reasons.append("None")
        if failed:
            raise _Cancelled(reasons)
        for key, item in staged:
            self.items[key] = item


def _requested_item(store: DynamoDbAutonomyBundleStore, client: _TransactFakeDynamo, execution_name: str) -> None:
    store.record_dispatch_requested(operation_id=_OP, execution_name=execution_name)


@pytest.mark.unit
def test_dispatched_uses_transaction_conditioned_on_requested() -> None:
    client = _TransactFakeDynamo()
    store = DynamoDbAutonomyBundleStore(client=client, table_name="ops-06")

    store.record_dispatch_requested(operation_id=_OP, execution_name=_NAME)
    store.record_dispatched(operation_id=_OP, execution_name=_NAME)

    # ``dispatched`` went through a single TransactWriteItems, never an
    # independent conditional Put.
    assert len(client.transacts) == 1
    entry_kinds = {next(iter(e)) for e in client.transacts[0]}
    assert entry_kinds == {"ConditionCheck", "Put"}
    dispatched = store.load_dispatch_audit(operation_id=_OP, phase="dispatched")
    assert dispatched is not None and dispatched["execution_name"] == _NAME


@pytest.mark.unit
def test_dispatched_without_requested_fails_closed_and_writes_nothing() -> None:
    client = _TransactFakeDynamo()
    store = DynamoDbAutonomyBundleStore(client=client, table_name="ops-06")

    # No ``dispatch_requested`` predecessor: the transaction's ConditionCheck
    # fails and no ``dispatched`` record is written.
    with pytest.raises(AutonomyBundleStoreError):
        store.record_dispatched(operation_id=_OP, execution_name=_NAME)
    assert store.load_dispatch_audit(operation_id=_OP, phase="dispatched") is None


@pytest.mark.unit
def test_dispatched_requires_matching_execution_name() -> None:
    client = _TransactFakeDynamo()
    store = DynamoDbAutonomyBundleStore(client=client, table_name="ops-06")

    store.record_dispatch_requested(operation_id=_OP, execution_name=_NAME)
    # A ``dispatched`` for a DIFFERENT execution name than the recorded request
    # fails closed rather than crossing the two records.
    with pytest.raises(AutonomyBundleStoreError):
        store.record_dispatched(operation_id=_OP, execution_name=_OTHER_NAME)
    assert store.load_dispatch_audit(operation_id=_OP, phase="dispatched") is None


@pytest.mark.unit
def test_dispatched_is_immutable_idempotent() -> None:
    client = _TransactFakeDynamo()
    store = DynamoDbAutonomyBundleStore(client=client, table_name="ops-06")

    store.record_dispatch_requested(operation_id=_OP, execution_name=_NAME)
    store.record_dispatched(operation_id=_OP, execution_name=_NAME)
    # An identical re-record is idempotent (no raise, no overwrite).
    store.record_dispatched(operation_id=_OP, execution_name=_NAME)
    dispatched = store.load_dispatch_audit(operation_id=_OP, phase="dispatched")
    assert dispatched is not None and dispatched["execution_name"] == _NAME
