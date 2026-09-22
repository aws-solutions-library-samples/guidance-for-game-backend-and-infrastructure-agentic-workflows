"""DynamoDB E3 execution store tests (#415, E3 execute).

The execution store is the durable, atomic backing store for one logical
capacity update. These tests assert its safety invariants against a fake
DynamoDB client that records transactions:

* **Lease + generation fencing** — acquiring the execution lease returns a
  fencing generation; a commit from a superseded generation fails closed as a
  precondition failure.
* **At most one logical update** — a recorded terminal result is idempotently
  replayed on a second acquire keyed by the same logical_action_id; the write
  is never duplicated.
* **TTL only on the lease** — the transient lease item carries a numeric ``ttl``;
  the audit records (intent, result, verification, state change, ledger) carry
  no ``ttl`` at all.
* **Safe ClientError classification** — only a pure ConditionalCheckFailed is a
  precondition/idempotency race; a throttle/transient fault is raised as a
  retryable store error, never silently swallowed.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.execution_store import (
    DynamoDbExecutionStore,
    ExecutionCommitOutcome,
    ExecutionStoreError,
    LeaseAcquisition,
)

_NOW = datetime(2026, 9, 21, 19, 12, 0, tzinfo=timezone.utc)
_LEASE_NOT_AFTER = _NOW + timedelta(seconds=30)
_OP = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_ACTION = "act_" + "a" * 64


def _result() -> dict[str, Any]:
    return {
        "execution_contract_version": "1.0",
        "operation_id": _OP,
        "logical_action_id": _ACTION,
        "intent_hash": "sha256:" + "1" * 64,
        "outcome": "SUCCEEDED",
        "provider_write_issued": True,
        "verification": {
            "execution_contract_version": "1.0",
            "operation_id": _OP,
            "logical_action_id": _ACTION,
            "verified_at": "2026-09-21T19:12:10Z",
            "observed_capacity": {"desired": 14, "minimum": 2, "maximum": 20},
            "expected_capacity": {"desired": 14, "minimum": 2, "maximum": 20},
            "matches_target": True,
            "attempts_observed": 1,
        },
        "recorded_at": "2026-09-21T19:12:11Z",
    }


class _FakeDynamo:
    """A minimal fake modeling conditional put/transact with one stored table."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[list[dict[str, Any]]] = []
        self.fail_next: Exception | None = None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        pk = key["PK"]["S"]
        sk = key["SK"]["S"]
        item = self.items.get((pk, sk))
        return {"Item": item} if item is not None else {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        staged: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for entry in kwargs["TransactItems"]:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                pk = item["PK"]["S"]
                sk = item["SK"]["S"]
                if put.get("ConditionExpression") == "attribute_not_exists(SK)" and (pk, sk) in self.items:
                    raise _conditional_failure()
                staged.append(((pk, sk), item))
        self.transactions.append(kwargs["TransactItems"])
        for key, item in staged:
            self.items[key] = item
        return {}


def _conditional_failure():
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )


def _throttle():
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems")


def _store(fake: _FakeDynamo) -> DynamoDbExecutionStore:
    return DynamoDbExecutionStore(client=fake, table_name="ops-table")


def test_acquire_lease_returns_generation_and_no_prior_result() -> None:
    fake = _FakeDynamo()
    acquisition = _store(fake).acquire_execution_lease(
        operation_id=_OP,
        logical_action_id=_ACTION,
        lease_holder="exec-1",
        lease_not_after=_LEASE_NOT_AFTER,
    )
    assert isinstance(acquisition, LeaseAcquisition)
    assert acquisition.generation >= 1
    assert acquisition.recorded_result is None


def test_only_lease_item_carries_ttl() -> None:
    fake = _FakeDynamo()
    _store(fake).acquire_execution_lease(
        operation_id=_OP,
        logical_action_id=_ACTION,
        lease_holder="exec-1",
        lease_not_after=_LEASE_NOT_AFTER,
    )
    lease_items = [item for (_, sk), item in fake.items.items() if "LEASE" in sk]
    assert lease_items, "a lease item must be written"
    assert all("ttl" in item for item in lease_items)


def test_recorded_result_is_idempotently_replayed() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    acquisition = store.acquire_execution_lease(
        operation_id=_OP, logical_action_id=_ACTION, lease_holder="exec-1", lease_not_after=_LEASE_NOT_AFTER
    )
    outcome = store.record_execution_result(
        operation_id=_OP,
        logical_action_id=_ACTION,
        generation=acquisition.generation,
        expected_state="dispatched",
        new_state="succeeded",
        result=_result(),
    )
    assert outcome is ExecutionCommitOutcome.RECORDED

    # No audit record carries a ttl.
    audit_items = [item for (_, sk), item in fake.items.items() if "LEASE" not in sk]
    assert audit_items
    assert all("ttl" not in item for item in audit_items)

    # A second acquire keyed by the same logical_action_id replays the result.
    replay = store.acquire_execution_lease(
        operation_id=_OP, logical_action_id=_ACTION, lease_holder="exec-2", lease_not_after=_LEASE_NOT_AFTER
    )
    assert replay.recorded_result is not None
    assert replay.recorded_result["outcome"] == "SUCCEEDED"


def test_superseded_generation_commit_fails_closed() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    acquisition = store.acquire_execution_lease(
        operation_id=_OP, logical_action_id=_ACTION, lease_holder="exec-1", lease_not_after=_LEASE_NOT_AFTER
    )
    # Simulate a fencing precondition failure on commit.
    fake.fail_next = _conditional_failure()
    outcome = store.record_execution_result(
        operation_id=_OP,
        logical_action_id=_ACTION,
        generation=acquisition.generation,
        expected_state="dispatched",
        new_state="succeeded",
        result=_result(),
    )
    assert outcome is ExecutionCommitOutcome.PRECONDITION_FAILED


def test_throttle_is_raised_not_swallowed() -> None:
    fake = _FakeDynamo()
    store = _store(fake)
    acquisition = store.acquire_execution_lease(
        operation_id=_OP, logical_action_id=_ACTION, lease_holder="exec-1", lease_not_after=_LEASE_NOT_AFTER
    )
    fake.fail_next = _throttle()
    with pytest.raises(ExecutionStoreError):
        store.record_execution_result(
            operation_id=_OP,
            logical_action_id=_ACTION,
            generation=acquisition.generation,
            expected_state="dispatched",
            new_state="succeeded",
            result=_result(),
        )
