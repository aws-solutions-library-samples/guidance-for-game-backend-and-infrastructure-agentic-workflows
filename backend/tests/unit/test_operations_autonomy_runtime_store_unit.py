"""Atomic policy-bound reservation port/store tests for E5 autonomy (#439).

The reservation store is the durable, DynamoDB-shaped port an autonomous runtime
uses to *atomically reserve* one write's budget, cooldown, frequency,
concurrency, and anti-oscillation footprint against the policy-bound rolling
window state, fenced on the exact ``state_revision``. It is the mechanism that
makes the pure evaluator's reading safe under concurrency: a reservation either
advances the window-state revision by exactly one and returns the new snapshot,
or it fails closed.

These tests assert the port contract and the semantics of a reference
in-memory store that models DynamoDB conditional-write fencing:

* a reservation is atomic and policy/revision-fenced (exactly-once advance);
* a stale-revision reservation loses the race and returns ``CONFLICT`` — never
  a false success and never a double count;
* store unavailability fails closed as ``UNAVAILABLE``;
* the reserved snapshot stays contract-valid and hash-bound;
* the store holds no provider-write path or executor credential.
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
    ReservationResult,
    ReservationStore,
    ReservationStoreError,
)
from operations.autonomy_runtime.store import InMemoryReservationStore
from operations.contracts import load_json
from operations.contracts.autonomy import (
    autonomy_window_state_hash,
    validate_autonomy_contract,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"


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
        "operation_id": "op_bbbbbbbbbbbbbbbbbbbbbbbbbb",
        "action_micro_usd": window["action_micro_usd"],
        "now_epoch_seconds": window["as_of_epoch_seconds"] + 1,
        "change_direction": "increase",
    }
    kwargs.update(overrides)
    return ReservationRequest(**kwargs)


def _store() -> InMemoryReservationStore:
    return InMemoryReservationStore(initial_state=_window_state())


# -- Reservation port contract ----------------------------------------------


@pytest.mark.unit
def test_in_memory_store_satisfies_the_reservation_port() -> None:
    assert isinstance(_store(), ReservationStore)


@pytest.mark.unit
def test_successful_reservation_advances_revision_by_exactly_one() -> None:
    store = _store()
    before = _window_state()
    result = store.reserve(_request())
    assert result.outcome is ReservationOutcome.RESERVED
    assert result.window_state is not None
    assert result.window_state["state_revision"] == before["state_revision"] + 1


@pytest.mark.unit
def test_reserved_snapshot_is_contract_valid_and_hash_bound() -> None:
    store = _store()
    result = store.reserve(_request())
    snapshot = result.window_state
    assert snapshot is not None
    validate_autonomy_contract("gamelift-capacity-autonomy-window-state", snapshot)
    assert snapshot["state_hash"] == autonomy_window_state_hash(snapshot)


@pytest.mark.unit
def test_reservation_counts_budget_frequency_and_concurrency() -> None:
    store = _store()
    before = _window_state()
    result = store.reserve(_request())
    snapshot = result.window_state
    assert snapshot is not None
    assert snapshot["window_micro_usd"] == before["window_micro_usd"] + before["action_micro_usd"]
    assert snapshot["writes_in_window"] == before["writes_in_window"] + 1
    assert snapshot["in_flight"] == before["in_flight"] + 1


# -- Fail-closed on conflict -------------------------------------------------


@pytest.mark.unit
def test_stale_revision_reservation_loses_the_race() -> None:
    store = _store()
    # First reservation wins and advances the revision.
    first = store.reserve(_request())
    assert first.outcome is ReservationOutcome.RESERVED
    # A second reservation at the original (now stale) revision must fail closed.
    second = store.reserve(_request())
    assert second.outcome is ReservationOutcome.CONFLICT
    assert second.window_state is None


@pytest.mark.unit
def test_conflict_does_not_double_count() -> None:
    store = _store()
    store.reserve(_request())
    conflicted = store.reserve(_request())
    assert conflicted.outcome is ReservationOutcome.CONFLICT
    # The durable revision advanced exactly once despite two attempts.
    current = store.current(_window_state()["state_id"])
    assert current["state_revision"] == _window_state()["state_revision"] + 1


@pytest.mark.unit
def test_wrong_policy_reservation_fails_closed() -> None:
    store = _store()
    bad_ref = _policy_ref(_policy())
    bad_ref["policy_hash"] = "sha256:" + "0" * 64
    result = store.reserve(_request(policy_ref=bad_ref))
    assert result.outcome is ReservationOutcome.CONFLICT
    assert result.window_state is None


# -- Fail-closed on unavailability ------------------------------------------


class _UnavailableStore(InMemoryReservationStore):
    def _commit(self, *args: Any, **kwargs: Any) -> Any:
        raise ReservationStoreError("simulated store unavailability")


@pytest.mark.unit
def test_store_unavailability_fails_closed() -> None:
    store = _UnavailableStore(initial_state=_window_state())
    result = store.reserve(_request())
    assert result.outcome is ReservationOutcome.UNAVAILABLE
    assert result.window_state is None


@pytest.mark.unit
def test_reservation_request_rejects_negative_cost() -> None:
    with pytest.raises((ValueError, ReservationStoreError)):
        _request(action_micro_usd=-1)


@pytest.mark.unit
def test_reservation_request_rejects_unknown_direction() -> None:
    with pytest.raises((ValueError, ReservationStoreError)):
        _request(change_direction="sideways")


# -- No provider-write / credential surface ---------------------------------


@pytest.mark.unit
def test_store_holds_no_provider_write_or_executor_credential() -> None:
    store = _store()
    for forbidden in ("execute", "dispatch", "invoke", "update_fleet_capacity", "provider_client"):
        assert not hasattr(store, forbidden)
