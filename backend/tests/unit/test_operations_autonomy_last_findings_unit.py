"""Final semantic-review reproductions for E5 bounded autonomy (issue #439).

These are the direct, failing-first reproductions for the four last review
findings, closed by this change:

1. **Reclaim has no production owner lookup / caller; a crash wedges state.**
   The reservation window item now carries an ``active_operation_id`` index,
   written in the SAME reserve transaction and removed in settle/reclaim, and
   both stores expose ``sweep_state(state_id, now)`` that reclaims an expired
   owner by that index (no ``Scan``). The production window loader calls it
   before ``current()`` so a next evaluation reclaims an expired owner.
2. **Dispatch audit is best-effort / dispatch has no evidence requirement.**
   ``dispatch_requested`` is mandatory before StartExecution and ``dispatched``
   is mandatory after a confirmed/idempotent start; either audit failure
   refuses and releases the reservation. The executor v2 reload requires the
   exact ``dispatched`` audit record before it verifies or writes.
3. **Verifier expiry runs before Describe; the final hook checks no time.**
   The immediate pre-write hook reloads the bundle and re-runs the
   ``AutonomyExecutionVerifier`` at the CURRENT clock (after E4's second check,
   before the write) and compares the action id, so a clock that crosses the
   decision/window deadline DURING Describe fails closed with no write.
4. **Policy loader checks hash but not configured id/version.** ``load`` now
   requires the stored ``policy_id`` and ``policy_version`` equal the configured
   arguments in addition to the hash.

No AWS calls, no infrastructure, no provider writes.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import load_json

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


def _request(**overrides: Any) -> Any:
    # Local modules
    from operations.autonomy_runtime.store import ReservationRequest

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


# ===========================================================================
# Finding 1 — sweep_state(state_id, now): owner index + production caller
# ===========================================================================


class _ConditionalFailure(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        }


class _FakeDynamo:
    """Fake modeling conditional transact/get with an active_operation_id index."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

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
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {"Item": item} if item is not None else {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        staged: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for entry in kwargs["TransactItems"]:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                pk, sk = item["PK"]["S"], item["SK"]["S"]
                if put.get("ConditionExpression") == "attribute_not_exists(SK)" and (pk, sk) in self.items:
                    raise _ConditionalFailure()
                staged.append(((pk, sk), item))
            elif "Update" in entry:
                update = entry["Update"]
                key = update["Key"]
                pk, sk = key["PK"]["S"], key["SK"]["S"]
                current = self.items.get((pk, sk))
                cond = update.get("ConditionExpression", "")
                values = update.get("ExpressionAttributeValues", {})
                expr = update["UpdateExpression"]
                if "state_revision = :expected" in cond:
                    if current is None or int(current["state_revision"]["N"]) != int(values[":expected"]["N"]):
                        raise _ConditionalFailure()
                    if "in_flight = :zero" in cond and int(current["in_flight"]["N"]) != 0:
                        raise _ConditionalFailure()
                    if "in_flight = :one" in cond and int(current["in_flight"]["N"]) != 1:
                        raise _ConditionalFailure()
                    new_item = dict(current)
                    new_item["document"] = values[":doc"]
                    new_item["state_revision"] = values[":next"]
                    new_item["in_flight"] = values[":one"] if "= :one" in expr else values[":zero"]
                    if ":owner" in values and "active_operation_id = :owner" in expr:
                        new_item["active_operation_id"] = values[":owner"]
                    if "REMOVE active_operation_id" in expr:
                        new_item.pop("active_operation_id", None)
                    staged.append(((pk, sk), new_item))
                elif "settled = :false" in cond:
                    if current is None or current.get("settled", {}).get("BOOL") is not False:
                        raise _ConditionalFailure()
                    if ":gen" in values and int(current.get("generation", {}).get("N", "-1")) != int(
                        values[":gen"]["N"]
                    ):
                        raise _ConditionalFailure()
                    new_item = dict(current)
                    new_item["settled"] = values[":true"]
                    if ":terminal" in values:
                        new_item["terminal"] = values[":terminal"]
                    if ":nextgen" in values:
                        new_item["generation"] = values[":nextgen"]
                    staged.append(((pk, sk), new_item))
        for key, item in staged:
            self.items[key] = item
        return {}


def _ddb_store(fake: _FakeDynamo) -> Any:
    # Local modules
    from operations.autonomy_runtime.store import DynamoDbReservationStore

    return DynamoDbReservationStore(client=fake, table_name="ops-table")


@pytest.mark.unit
def test_reserve_writes_active_owner_index_on_window_item() -> None:
    """The reserve transaction records the active owner on the window item."""
    fake = _FakeDynamo()
    fake.seed_window()
    now = _window_state()["as_of_epoch_seconds"] + 1
    _ddb_store(fake).reserve(_request(lease_not_after=now + 30, generation=1))
    win = fake.items[(f"AUTZ#{_window_state()['state_id']}", "AUTZWINDOW")]
    assert win.get("active_operation_id", {}).get("S") == _OP


@pytest.mark.unit
def test_settle_removes_active_owner_index() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    now = _window_state()["as_of_epoch_seconds"] + 1
    store = _ddb_store(fake)
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    win = fake.items[(f"AUTZ#{_window_state()['state_id']}", "AUTZWINDOW")]
    # Precondition: reserve wrote the owner index.
    assert win.get("active_operation_id", {}).get("S") == _OP
    store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=1, terminal="succeeded")
    win = fake.items[(f"AUTZ#{_window_state()['state_id']}", "AUTZWINDOW")]
    assert "active_operation_id" not in win


@pytest.mark.unit
def test_sweep_state_reclaims_expired_owner_by_index_without_scan() -> None:
    """``sweep_state(state_id, now)`` reclaims the expired owner via the index."""
    fake = _FakeDynamo()
    fake.seed_window()
    now = _window_state()["as_of_epoch_seconds"] + 1
    store = _ddb_store(fake)
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    state_id = _window_state()["state_id"]
    # Live lease: never reclaimed.
    assert store.sweep_state(state_id=state_id, now_epoch_seconds=now + 10) is False
    # Expired lease: reclaimed by the owner index (no operation_id argument).
    assert store.sweep_state(state_id=state_id, now_epoch_seconds=now + 31) is True
    win = fake.items[(f"AUTZ#{state_id}", "AUTZWINDOW")]
    assert int(win["in_flight"]["N"]) == 0
    assert "active_operation_id" not in win


@pytest.mark.unit
def test_sweep_state_with_no_owner_is_a_noop() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    assert store.sweep_state(state_id=_window_state()["state_id"], now_epoch_seconds=10_000_000_000) is False


@pytest.mark.unit
def test_in_memory_sweep_state_reclaims_expired_owner() -> None:
    # Local modules
    from operations.autonomy_runtime.store import InMemoryReservationStore

    store = InMemoryReservationStore(initial_state=_window_state())
    now = _window_state()["as_of_epoch_seconds"] + 1
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    state_id = _window_state()["state_id"]
    assert store.sweep_state(state_id=state_id, now_epoch_seconds=now + 10) is False
    assert store.sweep_state(state_id=state_id, now_epoch_seconds=now + 31) is True
    assert store.current(state_id)["in_flight"] == 0


@pytest.mark.unit
def test_production_window_loader_sweeps_before_current() -> None:
    """The production window loader reclaims an expired owner before reading."""
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import DynamoDbWindowStateLoader

    calls: list[str] = []

    class _RecordingStore:
        def sweep_state(self, *, state_id: str, now_epoch_seconds: int) -> bool:
            calls.append(f"sweep:{state_id}")
            return True

        def current(self, state_id: str) -> dict[str, Any]:
            calls.append(f"current:{state_id}")
            return _window_state()

    loader = DynamoDbWindowStateLoader(_RecordingStore())
    loader.load(_window_state()["state_id"])
    # sweep_state must be called BEFORE current so an expired owner is reclaimed
    # and the read reflects the released slot.
    assert calls == [f"sweep:{_window_state()['state_id']}", f"current:{_window_state()['state_id']}"]


# ===========================================================================
# Finding 4 — policy loader requires configured id/version, not only hash
# ===========================================================================


class _PolicyFakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, *, TableName: str, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        self.items[(Item["PK"]["S"], Item["SK"]["S"])] = Item

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item is not None else {}


@pytest.mark.unit
def test_policy_loader_refuses_record_whose_id_does_not_match_configured() -> None:
    """A record stored under the configured PK but carrying a different policy_id
    (hash still self-consistent) must fail closed on id mismatch."""
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import AutonomyPolicyLoaderError, DynamoDbAutonomyPolicyLoader
    from operations.contracts.autonomy import autonomy_policy_hash

    policy = _policy()
    tampered = dict(policy)
    tampered["policy_id"] = "policy.evil-shadow"
    # Re-bind the hash so the document is internally self-consistent and passes
    # the frozen contract; only the configured-id check can catch it.
    tampered["policy_hash"] = autonomy_policy_hash(tampered)

    fake = _PolicyFakeDynamo()
    # Store the tampered doc under the CONFIGURED id/version PK (as a drifted or
    # mis-seeded record would appear).
    canonical = json.dumps(tampered, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    fake.items[(f"AUTZPOLICY#{policy['policy_id']}#{policy['policy_version']}", "AUTZPOLICY")] = {
        "PK": {"S": f"AUTZPOLICY#{policy['policy_id']}#{policy['policy_version']}"},
        "SK": {"S": "AUTZPOLICY"},
        "policy": {"S": canonical},
    }
    loader = DynamoDbAutonomyPolicyLoader(client=fake, table_name="ops-06")
    with pytest.raises(AutonomyPolicyLoaderError):
        loader.load(
            policy_id=policy["policy_id"],
            policy_version=policy["policy_version"],
            policy_hash=tampered["policy_hash"],
        )


@pytest.mark.unit
def test_policy_loader_refuses_record_whose_version_does_not_match_configured() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import AutonomyPolicyLoaderError, DynamoDbAutonomyPolicyLoader
    from operations.contracts.autonomy import autonomy_policy_hash

    policy = _policy()
    tampered = dict(policy)
    tampered["policy_version"] = "2099-01-01"
    tampered["policy_hash"] = autonomy_policy_hash(tampered)

    fake = _PolicyFakeDynamo()
    canonical = json.dumps(tampered, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    fake.items[(f"AUTZPOLICY#{policy['policy_id']}#{policy['policy_version']}", "AUTZPOLICY")] = {
        "PK": {"S": f"AUTZPOLICY#{policy['policy_id']}#{policy['policy_version']}"},
        "SK": {"S": "AUTZPOLICY"},
        "policy": {"S": canonical},
    }
    loader = DynamoDbAutonomyPolicyLoader(client=fake, table_name="ops-06")
    with pytest.raises(AutonomyPolicyLoaderError):
        loader.load(
            policy_id=policy["policy_id"],
            policy_version=policy["policy_version"],
            policy_hash=tampered["policy_hash"],
        )
