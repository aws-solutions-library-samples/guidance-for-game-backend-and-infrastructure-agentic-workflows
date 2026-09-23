"""Reserve/require/settle lifecycle tests for the unified E5 autonomy store (#439).

The E5 runtime blockers review found three incompatible reservation ports, no
idempotent operation ownership, and no in-flight settlement. This suite locks the
single unified reservation lifecycle the runtime, verifier, and gate all depend
on:

* **Idempotent operation ownership.** Reserving the same ``operation_id`` twice
  (a replay) returns the *same* reservation and never double-counts budget,
  frequency, or concurrency — the durable revision advances exactly once.
* **Ownership confirmation.** ``require`` succeeds only for the exact
  ``(operation_id, logical_action_id)`` that currently, atomically owns the live
  in-flight reservation, and fails closed otherwise.
* **Concurrency guard.** A second *distinct* operation cannot reserve while one
  write is in flight (the window schema bounds ``in_flight`` to 1).
* **Safe in-flight release.** ``settle`` releases the in-flight slot (``in_flight``
  back to 0) on any terminal/failed handoff while *conservatively retaining* the
  consumed budget (``window_micro_usd``) and frequency (``writes_in_window``).
* **Fail closed.** Any store failure or lost/superseded ownership fails closed.

These are contract tests over the in-memory reference store, which models the
DynamoDB conditional-write fencing so the fail-closed contract is provable with
no AWS dependency.
"""

from __future__ import annotations

# Standard library
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime import (
    ReservationOutcome,
    ReservationRequest,
)
from operations.autonomy_runtime.store import InMemoryReservationStore, ReservationStoreError
from operations.contracts import load_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"

_ACTION = "act_" + "a" * 64
_OTHER_ACTION = "act_" + "b" * 64
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


def _store() -> InMemoryReservationStore:
    return InMemoryReservationStore(initial_state=_window_state())


# -- Idempotent operation ownership -----------------------------------------


@pytest.mark.unit
def test_replaying_same_operation_id_does_not_double_count() -> None:
    store = _store()
    before = _window_state()
    first = store.reserve(_request())
    assert first.outcome is ReservationOutcome.RESERVED

    # A replay of the SAME operation_id must be idempotent: the same reservation
    # is returned and the durable revision advanced exactly once.
    replay = store.reserve(_request())
    assert replay.outcome is ReservationOutcome.RESERVED
    current = store.current(before["state_id"])
    assert current["state_revision"] == before["state_revision"] + 1
    assert current["window_micro_usd"] == before["window_micro_usd"] + before["action_micro_usd"]
    assert current["writes_in_window"] == before["writes_in_window"] + 1
    assert current["in_flight"] == 1


# -- require ----------------------------------------------------------------


@pytest.mark.unit
def test_require_confirms_the_owning_operation_and_action() -> None:
    store = _store()
    store.reserve(_request())
    # No raise: the exact (operation_id, logical_action_id) owns the reservation.
    store.require(operation_id=_OP, logical_action_id=_ACTION)


@pytest.mark.unit
def test_require_fails_closed_for_unknown_operation() -> None:
    store = _store()
    store.reserve(_request())
    with pytest.raises(ReservationStoreError):
        store.require(operation_id="op_dddddddddddddddddddddddddd", logical_action_id=_ACTION)


@pytest.mark.unit
def test_require_fails_closed_for_wrong_action() -> None:
    store = _store()
    store.reserve(_request())
    with pytest.raises(ReservationStoreError):
        store.require(operation_id=_OP, logical_action_id=_OTHER_ACTION)


@pytest.mark.unit
def test_require_fails_closed_after_settlement_releases_the_slot() -> None:
    store = _store()
    store.reserve(_request())
    store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    # The in-flight slot is released, so ownership can no longer be required.
    with pytest.raises(ReservationStoreError):
        store.require(operation_id=_OP, logical_action_id=_ACTION)


# -- Concurrency guard ------------------------------------------------------


@pytest.mark.unit
def test_second_operation_cannot_reserve_while_one_is_in_flight() -> None:
    store = _store()
    first = store.reserve(_request())
    assert first.outcome is ReservationOutcome.RESERVED
    # A distinct operation reserving against the advanced revision loses because
    # a write is already in flight (in_flight is bounded to 1).
    advanced = store.current(_window_state()["state_id"])
    second = store.reserve(
        _request(
            operation_id="op_dddddddddddddddddddddddddd",
            logical_action_id=_OTHER_ACTION,
            expected_revision=advanced["state_revision"],
        )
    )
    assert second.outcome is ReservationOutcome.CONFLICT
    assert second.window_state is None


# -- Safe in-flight release (settle) ----------------------------------------


@pytest.mark.unit
def test_settle_releases_in_flight_but_retains_budget_and_frequency() -> None:
    store = _store()
    before = _window_state()
    store.reserve(_request())
    settled = store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="failed")
    assert settled.outcome is ReservationOutcome.RESERVED
    snapshot = settled.window_state
    assert snapshot is not None
    # In-flight released so a later operation may proceed.
    assert snapshot["in_flight"] == 0
    # Budget and frequency are conservatively RETAINED (never rolled back).
    assert snapshot["window_micro_usd"] == before["window_micro_usd"] + before["action_micro_usd"]
    assert snapshot["writes_in_window"] == before["writes_in_window"] + 1


@pytest.mark.unit
def test_settle_advances_revision_and_stays_contract_valid() -> None:
    # Local modules
    from operations.contracts.autonomy import autonomy_window_state_hash, validate_autonomy_contract

    store = _store()
    reserved = store.reserve(_request())
    reserved_revision = reserved.window_state["state_revision"]  # type: ignore[index]
    settled = store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    snapshot = settled.window_state
    assert snapshot is not None
    assert snapshot["state_revision"] == reserved_revision + 1
    validate_autonomy_contract("gamelift-capacity-autonomy-window-state", snapshot)
    assert snapshot["state_hash"] == autonomy_window_state_hash(snapshot)


@pytest.mark.unit
def test_settle_is_idempotent() -> None:
    store = _store()
    store.reserve(_request())
    first = store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    second = store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    assert first.outcome is ReservationOutcome.RESERVED
    assert second.outcome is ReservationOutcome.RESERVED
    # A second settle does not release twice or advance again.
    assert second.window_state["in_flight"] == 0  # type: ignore[index]
    assert first.window_state["state_revision"] == second.window_state["state_revision"]  # type: ignore[index]


@pytest.mark.unit
def test_settle_of_unknown_operation_fails_closed() -> None:
    store = _store()
    store.reserve(_request())
    with pytest.raises(ReservationStoreError):
        store.settle(operation_id="op_dddddddddddddddddddddddddd", logical_action_id=_ACTION, terminal="failed")


# -- After settlement, a new operation may reserve --------------------------


@pytest.mark.unit
def test_new_operation_may_reserve_after_settlement() -> None:
    store = _store()
    store.reserve(_request())
    store.settle(operation_id=_OP, logical_action_id=_ACTION, terminal="succeeded")
    advanced = store.current(_window_state()["state_id"])
    second = store.reserve(
        _request(
            operation_id="op_dddddddddddddddddddddddddd",
            logical_action_id=_OTHER_ACTION,
            expected_revision=advanced["state_revision"],
        )
    )
    assert second.outcome is ReservationOutcome.RESERVED
    assert second.window_state["in_flight"] == 1  # type: ignore[index]
