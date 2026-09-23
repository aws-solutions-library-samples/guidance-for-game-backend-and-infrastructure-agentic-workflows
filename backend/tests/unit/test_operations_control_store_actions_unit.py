"""Control store CAS uses one atomic TransactWriteItems over granted actions.

The E4 control audit store's IAM policy grants ``dynamodb:PutItem``,
``dynamodb:UpdateItem``, ``dynamodb:GetItem`` (and ``Query`` for the read path).
``TransactWriteItems`` is NOT itself an IAM action: a transaction is authorized
against the underlying per-item actions on the target table. The control CAS
commit is therefore one atomic ``TransactWriteItems`` composed of a conditional
``Update`` (authorized by ``dynamodb:UpdateItem``) for the state advance and a
conditional ``Put`` (authorized by ``dynamodb:PutItem``) for the immutable
outcome record — both already granted, so the transaction never fails with
AccessDenied. Committing the two legs atomically is what guarantees the
``config_version`` can never advance without its outcome audit record.

These tests use a fake DynamoDB client that RAISES if a bare ``update_item`` /
``put_item`` write is used for the commit (which would be a non-atomic
regression) or if ``scan`` is ever called, while proving the transaction is
composed only of the granted underlying Update/Put actions.
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
from operations.control.control_audit_store import (
    ControlCommitOutcome,
    DynamoDbControlAuditStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TABLE = "operations-table"

# The underlying per-item DynamoDB actions the control store's IAM policy grants.
# A TransactWriteItems is authorized against these; there is no separate
# dynamodb:TransactWriteItems permission to grant.
_GRANTED_TRANSACT_ACTIONS = {"Update", "Put"}


def _transaction_cancelled(reason_codes: list[str]) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
            "CancellationReasons": [{"Code": code} for code in reason_codes],
        },
        "TransactWriteItems",
    )


class _TransactDynamo:
    """A fake DynamoDB client whose only write path is atomic TransactWriteItems."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        # Every distinct transact leg operation observed across all commits.
        self.transact_leg_ops: set[str] = set()
        self.transact_calls = 0

    def put_item(self, *, TableName: str, Item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        # Only the pre-decision intent record and one-time state initialization
        # are plain PutItem; the CAS commit itself must go through the transaction.
        key = (Item["PK"]["S"], Item["SK"]["S"])
        cond = kwargs.get("ConditionExpression", "")
        if "attribute_not_exists" in cond and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem")
        self.items[key] = Item
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("control CAS commit must be one atomic TransactWriteItems, not a bare UpdateItem")

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        self.transact_calls += 1
        reasons: list[str] = []
        for entry in TransactItems:
            op = next(iter(entry))
            self.transact_leg_ops.add(op)
            if op == "Update":
                reasons.append(self._eval_update(entry["Update"]))
            elif op == "Put":
                reasons.append(self._eval_put(entry["Put"]))
            else:  # pragma: no cover - defensive
                raise AssertionError(f"unsupported transact leg: {op}")
        if any(code != "None" for code in reasons):
            raise _transaction_cancelled(reasons)
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
        if ":document_json" in values:
            new_item["document_json"] = values[":document_json"]
        self.items[key] = new_item

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("control store must never Scan")


def _store(dynamo: _TransactDynamo) -> DynamoDbControlAuditStore:
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


def _audit(dynamo: _TransactDynamo) -> list[dict[str, Any]]:
    return [item for item in dynamo.items.values() if item.get("record_type", {}).get("S") == "control_audit_record"]


def test_commit_uses_one_transaction_of_granted_actions() -> None:
    dynamo = _TransactDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    outcome = _commit(store)
    assert outcome is ControlCommitOutcome.COMMITTED
    assert store.current_config_version() == 2
    # The commit is exactly one atomic transaction.
    assert dynamo.transact_calls == 1
    # Its legs use only the underlying granted actions (Update + Put); there is
    # no separate dynamodb:TransactWriteItems permission required.
    assert dynamo.transact_leg_ops <= _GRANTED_TRANSACT_ACTIONS
    assert dynamo.transact_leg_ops == _GRANTED_TRANSACT_ACTIONS
    # An immutable outcome audit record exists and never carries a ttl.
    audit = _audit(dynamo)
    assert audit and all("ttl" not in item for item in audit)


def test_stale_version_is_conflict_and_writes_no_audit() -> None:
    dynamo = _TransactDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=3)
    outcome = _commit(store, expected_config_version=1, resulting_config_version=2)
    assert outcome is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 3  # unchanged
    # A conflicting decision must NOT leave a committed outcome audit record.
    assert not _audit(dynamo)


def test_two_concurrent_writers_only_one_wins() -> None:
    dynamo = _TransactDynamo()
    store = _store(dynamo)
    store.initialize_state_if_absent(config_version=1)
    first = _commit(store, record_id="ctl_" + "a" * 26, expected_config_version=1, resulting_config_version=2)
    second = _commit(store, record_id="ctl_" + "b" * 26, expected_config_version=1, resulting_config_version=2)
    assert first is ControlCommitOutcome.COMMITTED
    assert second is ControlCommitOutcome.VERSION_CONFLICT
    assert store.current_config_version() == 2
