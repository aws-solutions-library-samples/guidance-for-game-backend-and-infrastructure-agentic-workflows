"""Bridge tests unifying the E5 switch and reservation ports (#439).

The runtime blockers review found two switch interfaces
(``AutonomySwitchGate.require_enabled`` vs the verifier's
``AutonomySwitchPort.require_autonomy``) and three incompatible reservation
ports (the store's ``reserve``/``require``/``settle``, the verifier's
``require_reservation``, and the gate's ``reserve(**kwargs) -> bool``). This
suite locks the single set of bridge adapters that let ONE canonical
``AutonomySwitchGate`` and ONE canonical ``ReservationStore`` satisfy every
consumer, so there is exactly one switch source and one reservation source of
truth:

* :class:`SwitchAutonomyPort` adapts the canonical switch gate onto the
  verifier's ``require_autonomy()`` (deny -> ``AutonomySwitchDenied``).
* :class:`StoreReservationVerifierPort` adapts the canonical store onto the
  verifier's ``require_reservation(operation_id, logical_action_id)``.
* :class:`StoreReservationGatePort` adapts the canonical store onto the gate's
  ``reserve(**kwargs) -> bool`` from trusted, server-owned inputs only.

Every deny path fails closed.
"""

from __future__ import annotations

# Standard library
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_execution_verifier import AutonomySwitchDenied
from operations.autonomy_runtime.bridges import (
    StoreReservationGatePort,
    StoreReservationVerifierPort,
    SwitchAutonomyPort,
)
from operations.autonomy_runtime.store import InMemoryReservationStore, ReservationRequest
from operations.autonomy_switch import AutonomySwitchUnavailable
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


def _reserved_store() -> InMemoryReservationStore:
    store = InMemoryReservationStore(initial_state=_window_state())
    window = _window_state()
    store.reserve(
        ReservationRequest(
            policy_ref=_policy_ref(_policy()),
            state_id=window["state_id"],
            expected_revision=window["state_revision"],
            operation_id=_OP,
            logical_action_id=_ACTION,
            action_micro_usd=window["action_micro_usd"],
            now_epoch_seconds=window["as_of_epoch_seconds"] + 1,
            change_direction="increase",
        )
    )
    return store


# -- Switch bridge ----------------------------------------------------------


class _EnabledSwitch:
    def require_enabled(self) -> str:
        return "ok"


class _DisabledSwitch:
    def require_enabled(self) -> None:
        raise AutonomySwitchUnavailable("disabled")


@pytest.mark.unit
def test_switch_bridge_satisfies_the_verifier_port() -> None:
    port = SwitchAutonomyPort(_EnabledSwitch())
    assert callable(port.require_autonomy)
    port.require_autonomy()  # no raise when enabled


@pytest.mark.unit
def test_switch_bridge_fails_closed_on_denial() -> None:
    port = SwitchAutonomyPort(_DisabledSwitch())
    with pytest.raises(AutonomySwitchDenied):
        port.require_autonomy()


# -- Reservation verifier bridge --------------------------------------------


@pytest.mark.unit
def test_reservation_verifier_bridge_confirms_ownership() -> None:
    port = StoreReservationVerifierPort(_reserved_store())
    assert callable(port.require_reservation)
    port.require_reservation(operation_id=_OP, logical_action_id=_ACTION)


@pytest.mark.unit
def test_reservation_verifier_bridge_fails_closed_when_not_owned() -> None:
    port = StoreReservationVerifierPort(_reserved_store())
    with pytest.raises(Exception):
        port.require_reservation(operation_id="op_zzzzzzzzzzzzzzzzzzzzzzzzzz", logical_action_id=_ACTION)


# -- Reservation gate bridge ------------------------------------------------


@pytest.mark.unit
def test_reservation_gate_bridge_reserves_from_trusted_inputs() -> None:
    store = InMemoryReservationStore(initial_state=_window_state())
    port = StoreReservationGatePort(store, operation_id=_OP, logical_action_id=_ACTION)
    reserved = port.reserve(
        policy=_policy(),
        observation={},
        requested={"desired": 14, "minimum": 2, "maximum": 20},
        window_state=_window_state(),
        now_epoch_seconds=_window_state()["as_of_epoch_seconds"] + 1,
        decision={"change": {"direction": "increase"}, "action_micro_usd": _window_state()["action_micro_usd"]},
    )
    assert reserved is True
    assert store.current(_window_state()["state_id"])["in_flight"] == 1


@pytest.mark.unit
def test_reservation_gate_bridge_returns_false_on_conflict() -> None:
    store = _reserved_store()  # already in flight at advanced revision
    port = StoreReservationGatePort(
        store, operation_id="op_dddddddddddddddddddddddddd", logical_action_id="act_" + "d" * 64
    )
    reserved = port.reserve(
        policy=_policy(),
        observation={},
        requested={"desired": 14, "minimum": 2, "maximum": 20},
        window_state=_window_state(),  # stale revision
        now_epoch_seconds=_window_state()["as_of_epoch_seconds"] + 2,
        decision={"change": {"direction": "increase"}, "action_micro_usd": _window_state()["action_micro_usd"]},
    )
    assert reserved is False
