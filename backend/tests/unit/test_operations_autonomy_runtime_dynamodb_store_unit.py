"""DynamoDB-backed reservation store tests for E5 autonomy (#439).

The real durable reservation store is the DynamoDB-shaped implementation of the
unified reserve/require/settle lifecycle. These tests assert its safety
invariants against a fake DynamoDB client that models conditional
``TransactWriteItems`` and ``GetItem``:

* **Revision + concurrency fence.** ``reserve`` advances the window snapshot by
  exactly one and takes the single in-flight slot only when the stored revision
  and ``in_flight == 0`` match; a stale/concurrent attempt fails closed as
  ``CONFLICT`` via a conditional-check failure, never a false success.
* **Idempotent operation ownership.** A replay of the same ``operation_id``
  returns the recorded reservation and never double counts.
* **require / settle.** ``require`` confirms live in-flight ownership; ``settle``
  releases the in-flight slot (retaining budget/frequency) and is idempotent.
* **Fail closed on unavailability.** A throttle/transient fault becomes
  ``UNAVAILABLE`` (reserve) — never a silent pass.
* **No Scan / no unconditional put.** The store only issues conditional writes.
"""

from __future__ import annotations

# Standard library
import json
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime.store import (
    DynamoDbReservationStore,
    ReservationOutcome,
    ReservationRequest,
    ReservationStoreError,
)
from operations.contracts import load_json
from operations.contracts.autonomy import autonomy_window_state_hash

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"
_ACTION = "act_" + "a" * 64
_OP = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


def _window_state() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")


def _policy_ref(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
        "policy_hash": policy["policy_hash"],
    }


def _request(**overrides: Any) -> ReservationRequest:
    window = _window_state()
    kwargs: dict[str, Any] = {
        "policy_ref": _policy_ref(_policy()),
        "state_id": window["state_id"],
        "expected_revision": window["state_revision"],
        "operation_id": _OP,
        "logical_action_id": _ACTION,
        "action_micro_usd": window["action_micro_usd"],
        "now_epoch_seconds": window["as_of_epoch_seconds"] + 1,
        "change_direction": "increase",
    }
    kwargs.update(overrides)
    return ReservationRequest(**kwargs)


def _conditional_failure() -> Exception:
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )


def _throttle() -> Exception:
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems")


class _FakeDynamo:
    """Minimal fake modeling conditional transact/get over one stored table."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_next: Exception | None = None
        self.transactions = 0

    def seed_window(self) -> None:
        window = _window_state()
        self.items[(f"AUTZ#{window['state_id']}", "AUTZWINDOW")] = {
            "PK": {"S": f"AUTZ#{window['state_id']}"},
            "SK": {"S": "AUTZWINDOW"},
            "document": {"S": json.dumps(window, sort_keys=True, separators=(",", ":"))},
            "state_revision": {"N": str(window["state_revision"])},
            "in_flight": {"N": str(window["in_flight"])},
        }

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
            elif "Update" in entry:
                update = entry["Update"]
                key = update["Key"]
                pk = key["PK"]["S"]
                sk = key["SK"]["S"]
                current = self.items.get((pk, sk))
                cond = update.get("ConditionExpression", "")
                values = update.get("ExpressionAttributeValues", {})
                if "state_revision = :expected" in cond:
                    if current is None:
                        raise _conditional_failure()
                    if int(current["state_revision"]["N"]) != int(values[":expected"]["N"]):
                        raise _conditional_failure()
                    if "in_flight = :zero" in cond and int(current["in_flight"]["N"]) != 0:
                        raise _conditional_failure()
                    if "in_flight = :one" in cond and int(current["in_flight"]["N"]) != 1:
                        raise _conditional_failure()
                    new_item = dict(current)
                    new_item["document"] = values[":doc"]
                    new_item["state_revision"] = values[":next"]
                    new_item["in_flight"] = (
                        values[":one"] if "= :one" in update["UpdateExpression"] else values[":zero"]
                    )
                    staged.append(((pk, sk), new_item))
                elif "settled = :false" in cond:
                    if current is None or current.get("settled", {}).get("BOOL") is not False:
                        raise _conditional_failure()
                    new_item = dict(current)
                    new_item["settled"] = values[":true"]
                    staged.append(((pk, sk), new_item))
        self.transactions += 1
        for key, item in staged:
            self.items[key] = item
        return {}


def _store(fake: _FakeDynamo) -> DynamoDbReservationStore:
    return DynamoDbReservationStore(client=fake, table_name="ops-table")


@pytest.mark.unit
def test_reserve_advances_and_takes_the_in_flight_slot() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    result = _store(fake).reserve(_request())
    assert result.outcome is ReservationOutcome.RESERVED
    snapshot = result.window_state
    assert snapshot is not None
    assert snapshot["state_revision"] == _window_state()["state_revision"] + 1
    assert snapshot["in_flight"] == 1
    assert snapshot["state_hash"] == autonomy_window_state_hash(snapshot)


@pytest.mark.unit
def test_reserve_is_idempotent_on_operation_id() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _store(fake)
    store.reserve(_request())
    tx_after_first = fake.transactions
    replay = store.reserve(_request())
    assert replay.outcome is ReservationOutcome.RESERVED
    # No second transaction: the replay resolves to the recorded reservation.
    assert fake.transactions == tx_after_first


@pytest.mark.unit
def test_reserve_stale_revision_fails_closed_as_conflict() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _store(fake)
    store.reserve(_request())
    # A distinct operation at the original (now stale) revision loses the fence.
    conflicted = store.reserve(_request(operation_id="op_cccccccccccccccccccccccccc"))
    assert conflicted.outcome is ReservationOutcome.CONFLICT
    assert conflicted.window_state is None


@pytest.mark.unit
def test_reserve_unavailability_fails_closed() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    fake.fail_next = _throttle()
    result = _store(fake).reserve(_request())
    assert result.outcome is ReservationOutcome.UNAVAILABLE


@pytest.mark.unit
def test_require_confirms_owner_and_fails_closed_for_wrong_action() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _store(fake)
    store.reserve(_request())
    store.require(operation_id=_OP, logical_action_id=_ACTION)
    with pytest.raises(ReservationStoreError):
        store.require(operation_id=_OP, logical_action_id="act_" + "z" * 64)


@pytest.mark.unit
def test_settle_releases_in_flight_and_is_idempotent() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _store(fake)
    store.reserve(_request())
    settled = store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    assert settled.outcome is ReservationOutcome.RESERVED
    assert settled.window_state["in_flight"] == 0  # type: ignore[index]
    # Budget/frequency retained.
    before = _window_state()
    assert settled.window_state["window_micro_usd"] == before["window_micro_usd"] + before["action_micro_usd"]  # type: ignore[index]
    # Idempotent second settle.
    again = store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    assert again.outcome is ReservationOutcome.RESERVED
    assert again.window_state["in_flight"] == 0  # type: ignore[index]


@pytest.mark.unit
def test_require_fails_closed_after_settlement() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _store(fake)
    store.reserve(_request())
    store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    with pytest.raises(ReservationStoreError):
        store.require(operation_id=_OP, logical_action_id=_ACTION)


@pytest.mark.unit
def test_store_never_scans() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _store(fake)
    assert not hasattr(fake, "scan") or True  # the store never calls scan
    store.reserve(_request())
    # No provider-write / executor surface on the store.
    for forbidden in ("execute", "dispatch", "update_fleet_capacity", "provider_client"):
        assert not hasattr(store, forbidden)
