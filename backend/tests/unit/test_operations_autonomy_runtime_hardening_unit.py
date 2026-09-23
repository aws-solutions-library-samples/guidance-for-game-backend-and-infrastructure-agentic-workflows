"""Reservation hardening tests for E5 autonomy (#439, track A).

The Final #439 hardening pass addresses four defects the runtime blockers review
found in the previously concrete reservation lifecycle:

1. **No lease deadline / generation; a crash wedges ``in_flight`` forever.**
   Reservation records now carry a bounded ``lease_not_after`` deadline and a
   monotonic ``generation``. A *distinct* operation may reclaim only an
   **expired** lease (via the sweeper), which bumps the generation so a stale
   holder that returns is fenced. ``require`` / ``settle`` demand the exact
   generation.
2. **Concurrent same-operation transaction loser returns CONFLICT instead of
   converging.** On a conditional failure that is a same-operation replay
   (matching action/state/generation), ``reserve`` re-reads and converges to
   the recorded reservation rather than reporting a false conflict.
3. **Settlement conditional failure fabricates success.** ``settle`` now
   re-reads both the reservation record and the window state on a conditional
   failure and returns success only when the slot is *truly* released and the
   record is settled; otherwise it fails closed (CONFLICT / UNAVAILABLE).
4. **Terminal reason / audit absent.** ``settle`` persists the terminal reason
   on the reservation record and appends a bounded immutable ``autonomy_settled``
   audit record in the same transaction; ``reserve`` appends
   ``autonomy_reserved``.

These are contract tests over the in-memory reference store (which models the
DynamoDB conditional-write fencing) and the DynamoDB store over a fake client.
No AWS calls, no infrastructure, no provider writes.
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
    InMemoryReservationStore,
    ReservationOutcome,
    ReservationRequest,
    ReservationStoreError,
)
from operations.contracts import load_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"

_ACTION = "act_" + "a" * 64
_OTHER_ACTION = "act_" + "b" * 64
_OP = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
_OTHER_OP = "op_dddddddddddddddddddddddddd"


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
    now = window["as_of_epoch_seconds"] + 1
    kwargs: dict[str, Any] = {
        "policy_ref": _policy_ref(_policy()),
        "state_id": window["state_id"],
        "expected_revision": window["state_revision"],
        "operation_id": _OP,
        "logical_action_id": _ACTION,
        "action_micro_usd": window["action_micro_usd"],
        "now_epoch_seconds": now,
        "change_direction": "increase",
        "lease_not_after": now + 30,
        "generation": 1,
    }
    kwargs.update(overrides)
    return ReservationRequest(**kwargs)


def _store() -> InMemoryReservationStore:
    return InMemoryReservationStore(initial_state=_window_state())


# -- Request validation: lease + generation ---------------------------------


@pytest.mark.unit
def test_request_requires_bounded_lease_after_now() -> None:
    window = _window_state()
    now = window["as_of_epoch_seconds"] + 1
    with pytest.raises(ValueError):
        _request(lease_not_after=now)  # not strictly after now
    with pytest.raises(ValueError):
        _request(lease_not_after=now - 1)


@pytest.mark.unit
def test_request_requires_positive_generation() -> None:
    with pytest.raises(ValueError):
        _request(generation=0)
    with pytest.raises(ValueError):
        _request(generation=-1)
    with pytest.raises(ValueError):
        _request(generation=True)  # bool is not an int here


# -- Records carry lease + generation ---------------------------------------


@pytest.mark.unit
def test_reserved_record_carries_lease_and_generation() -> None:
    store = _store()
    now = _window_state()["as_of_epoch_seconds"] + 1
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    record = store.reservation_record(_OP)
    assert record is not None
    assert record["lease_not_after"] == now + 30
    assert record["generation"] == 1
    assert record["settled"] is False
    assert record["terminal"] is None


# -- require / settle demand the exact generation ---------------------------


@pytest.mark.unit
def test_require_fails_closed_on_generation_mismatch() -> None:
    store = _store()
    store.reserve(_request(generation=1))
    store.require(operation_id=_OP, logical_action_id=_ACTION, generation=1)
    with pytest.raises(ReservationStoreError):
        store.require(operation_id=_OP, logical_action_id=_ACTION, generation=2)


@pytest.mark.unit
def test_settle_fails_closed_on_generation_mismatch() -> None:
    store = _store()
    store.reserve(_request(generation=1))
    with pytest.raises(ReservationStoreError):
        store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=2, terminal="succeeded")


@pytest.mark.unit
def test_settle_persists_terminal_reason() -> None:
    store = _store()
    store.reserve(_request(generation=1))
    store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=1, terminal="failed")
    record = store.reservation_record(_OP)
    assert record is not None
    assert record["settled"] is True
    assert record["terminal"] == "failed"


# -- Expired-lease reclaim via the sweeper ----------------------------------


@pytest.mark.unit
def test_sweeper_reclaims_only_an_expired_lease() -> None:
    store = _store()
    now = _window_state()["as_of_epoch_seconds"] + 1
    store.reserve(_request(lease_not_after=now + 30, generation=1))

    # Before expiry: the sweeper must not reclaim a live lease.
    swept = store.sweep_expired(state_id=_window_state()["state_id"], now_epoch_seconds=now + 10)
    assert swept is False
    assert store.current(_window_state()["state_id"])["in_flight"] == 1

    # After expiry: the sweeper reclaims the slot, advancing the revision and
    # bumping the generation so a stale holder is fenced.
    swept = store.sweep_expired(state_id=_window_state()["state_id"], now_epoch_seconds=now + 31)
    assert swept is True
    reclaimed = store.current(_window_state()["state_id"])
    assert reclaimed["in_flight"] == 0


@pytest.mark.unit
def test_stale_holder_is_fenced_after_reclaim() -> None:
    store = _store()
    now = _window_state()["as_of_epoch_seconds"] + 1
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    store.sweep_expired(state_id=_window_state()["state_id"], now_epoch_seconds=now + 31)

    # The stale holder returns with its OLD generation and must be fenced from
    # both require and settle (the slot it thinks it owns was reclaimed).
    with pytest.raises(ReservationStoreError):
        store.require(operation_id=_OP, logical_action_id=_ACTION, generation=1)
    with pytest.raises(ReservationStoreError):
        store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=1, terminal="succeeded")


@pytest.mark.unit
def test_distinct_operation_reserves_after_reclaim() -> None:
    store = _store()
    now = _window_state()["as_of_epoch_seconds"] + 1
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    store.sweep_expired(state_id=_window_state()["state_id"], now_epoch_seconds=now + 31)

    advanced = store.current(_window_state()["state_id"])
    second = store.reserve(
        _request(
            operation_id=_OTHER_OP,
            logical_action_id=_OTHER_ACTION,
            expected_revision=advanced["state_revision"],
            now_epoch_seconds=now + 32,
            lease_not_after=now + 62,
            generation=2,
        )
    )
    assert second.outcome is ReservationOutcome.RESERVED
    assert second.window_state["in_flight"] == 1  # type: ignore[index]


@pytest.mark.unit
def test_a_live_lease_blocks_a_distinct_operation() -> None:
    store = _store()
    now = _window_state()["as_of_epoch_seconds"] + 1
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    advanced = store.current(_window_state()["state_id"])
    # A distinct operation cannot reserve while a LIVE (unexpired) lease holds
    # the slot: it must fail closed rather than reclaim.
    second = store.reserve(
        _request(
            operation_id=_OTHER_OP,
            logical_action_id=_OTHER_ACTION,
            expected_revision=advanced["state_revision"],
            now_epoch_seconds=now + 5,
            lease_not_after=now + 35,
            generation=2,
        )
    )
    assert second.outcome is ReservationOutcome.CONFLICT


# -- DynamoDB store: same fixtures via fake client --------------------------


class _FakeDynamo:
    """Fake modeling conditional transact/get over one stored table.

    ``hide_next_get`` lets a test model the reserve pre-read *missing* the
    reservation record (so the transaction path is exercised) while the record
    actually exists — the concurrent same-operation replay race.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_next: Exception | None = None
        self.transactions = 0
        self.hide_next_get: tuple[str, str] | None = None

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
        pk, sk = key["PK"]["S"], key["SK"]["S"]
        if self.hide_next_get == (pk, sk):
            self.hide_next_get = None
            return {}
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
                pk, sk = item["PK"]["S"], item["SK"]["S"]
                if put.get("ConditionExpression") == "attribute_not_exists(SK)" and (pk, sk) in self.items:
                    raise _conditional_failure()
                staged.append(((pk, sk), item))
            elif "Update" in entry:
                update = entry["Update"]
                key = update["Key"]
                pk, sk = key["PK"]["S"], key["SK"]["S"]
                current = self.items.get((pk, sk))
                cond = update.get("ConditionExpression", "")
                values = update.get("ExpressionAttributeValues", {})
                if "state_revision = :expected" in cond:
                    if current is None or int(current["state_revision"]["N"]) != int(values[":expected"]["N"]):
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
                    if ":gen" in values and int(current.get("generation", {}).get("N", "-1")) != int(
                        values[":gen"]["N"]
                    ):
                        raise _conditional_failure()
                    new_item = dict(current)
                    new_item["settled"] = values[":true"]
                    if ":terminal" in values:
                        new_item["terminal"] = values[":terminal"]
                    staged.append(((pk, sk), new_item))
        self.transactions += 1
        for key, item in staged:
            self.items[key] = item
        return {}


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


def _ddb_store(fake: _FakeDynamo) -> DynamoDbReservationStore:
    return DynamoDbReservationStore(client=fake, table_name="ops-table")


@pytest.mark.unit
def test_ddb_reserve_persists_lease_generation_and_audit() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    now = _window_state()["as_of_epoch_seconds"] + 1
    _ddb_store(fake).reserve(_request(lease_not_after=now + 30, generation=1))
    # A reservation record with lease + generation.
    record = fake.items.get((f"AUTZRSV#{_OP}", "AUTZRSV"))
    assert record is not None
    assert int(record["generation"]["N"]) == 1
    assert int(record["lease_not_after"]["N"]) == now + 30
    # A bounded immutable autonomy_reserved audit record.
    audit_keys = [k for k in fake.items if k[0].startswith("AUTZAUDIT")]
    assert any("autonomy_reserved" in fake.items[k].get("event", {}).get("S", "") for k in audit_keys)


@pytest.mark.unit
def test_ddb_concurrent_same_operation_converges_not_conflicts() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    req = _request(generation=1)
    # Model a concurrent same-operation replay where the winner has already
    # written the reservation record, but this loser's pre-read MISSES it
    # (hidden) so the transaction path runs and loses the attribute_not_exists
    # race. The window fence still passes at the loser's expected revision. The
    # store must re-read and CONVERGE to the winner's reservation (RESERVED),
    # not fabricate a false conflict.
    fake.items[(f"AUTZRSV#{_OP}", "AUTZRSV")] = {
        "PK": {"S": f"AUTZRSV#{_OP}"},
        "SK": {"S": "AUTZRSV"},
        "operation_id": {"S": _OP},
        "logical_action_id": {"S": _ACTION},
        "state_id": {"S": _window_state()["state_id"]},
        "settled": {"BOOL": False},
        "generation": {"N": "1"},
        "lease_not_after": {"N": "0"},
        "terminal": {"NULL": True},
    }
    fake.hide_next_get = (f"AUTZRSV#{_OP}", "AUTZRSV")
    fake.fail_next = _conditional_failure()
    replay = store.reserve(req)
    assert replay.outcome is ReservationOutcome.RESERVED


@pytest.mark.unit
def test_ddb_lost_reserve_with_mismatched_generation_conflicts() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    # A lost transaction whose surviving record is bound to a DIFFERENT generation
    # must NOT converge — it fails closed as a conflict.
    fake.items[(f"AUTZRSV#{_OP}", "AUTZRSV")] = {
        "PK": {"S": f"AUTZRSV#{_OP}"},
        "SK": {"S": "AUTZRSV"},
        "operation_id": {"S": _OP},
        "logical_action_id": {"S": _ACTION},
        "state_id": {"S": _window_state()["state_id"]},
        "settled": {"BOOL": False},
        "generation": {"N": "9"},
        "lease_not_after": {"N": "0"},
        "terminal": {"NULL": True},
    }
    fake.hide_next_get = (f"AUTZRSV#{_OP}", "AUTZRSV")
    fake.fail_next = _conditional_failure()
    replay = store.reserve(_request(generation=1))
    assert replay.outcome is ReservationOutcome.CONFLICT


@pytest.mark.unit
def test_ddb_concurrent_conflict_from_other_op_still_conflicts() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    store.reserve(_request(generation=1))
    # A DISTINCT operation whose transaction loses the revision/in_flight fence
    # must still fail closed as a conflict (no false convergence).
    advanced = store.current(_window_state()["state_id"])
    conflicted = store.reserve(
        _request(
            operation_id=_OTHER_OP,
            logical_action_id=_OTHER_ACTION,
            expected_revision=advanced["state_revision"],
            generation=2,
        )
    )
    assert conflicted.outcome is ReservationOutcome.CONFLICT


@pytest.mark.unit
def test_ddb_settle_conditional_failure_only_success_if_truly_settled() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    store.reserve(_request(generation=1))
    # Genuine concurrent settle: the record is flipped to settled and the slot
    # released out-of-band, then our transaction hits a conditional failure.
    rec_key = (f"AUTZRSV#{_OP}", "AUTZRSV")
    win_key = (f"AUTZ#{_window_state()['state_id']}", "AUTZWINDOW")
    fake.items[rec_key]["settled"] = {"BOOL": True}
    fake.items[win_key]["in_flight"] = {"N": "0"}
    fake.fail_next = _conditional_failure()
    settled = store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=1, terminal="succeeded")
    assert settled.outcome is ReservationOutcome.RESERVED


@pytest.mark.unit
def test_ddb_settle_conditional_failure_not_settled_fails_closed() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    store.reserve(_request(generation=1))
    # The record is NOT settled and the slot is still held, but our transaction
    # hits a conditional failure: the store must fail closed (never a false
    # success), returning CONFLICT rather than RESERVED.
    fake.fail_next = _conditional_failure()
    settled = store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=1, terminal="succeeded")
    assert settled.outcome in (ReservationOutcome.CONFLICT, ReservationOutcome.UNAVAILABLE)


@pytest.mark.unit
def test_ddb_settle_unavailability_fails_closed() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    store.reserve(_request(generation=1))
    fake.fail_next = _throttle()
    settled = store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=1, terminal="succeeded")
    assert settled.outcome is ReservationOutcome.UNAVAILABLE


@pytest.mark.unit
def test_ddb_settle_appends_audit_and_terminal() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    store.reserve(_request(generation=1))
    store.settle(operation_id=_OP, logical_action_id=_ACTION, generation=1, terminal="failed")
    rec = fake.items[(f"AUTZRSV#{_OP}", "AUTZRSV")]
    assert rec["settled"]["BOOL"] is True
    assert rec["terminal"]["S"] == "failed"
    audit_keys = [k for k in fake.items if k[0].startswith("AUTZAUDIT")]
    assert any("autonomy_settled" in fake.items[k].get("event", {}).get("S", "") for k in audit_keys)


@pytest.mark.unit
def test_ddb_sweeper_reclaims_expired_lease() -> None:
    fake = _FakeDynamo()
    fake.seed_window()
    store = _ddb_store(fake)
    now = _window_state()["as_of_epoch_seconds"] + 1
    store.reserve(_request(lease_not_after=now + 30, generation=1))
    # Not expired yet.
    assert store.sweep_expired(operation_id=_OP, now_epoch_seconds=now + 10) is False
    # Expired: reclaimed, slot released.
    assert store.sweep_expired(operation_id=_OP, now_epoch_seconds=now + 31) is True
    win_key = (f"AUTZ#{_window_state()['state_id']}", "AUTZWINDOW")
    assert int(fake.items[win_key]["in_flight"]["N"]) == 0
