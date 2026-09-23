"""Bridge adapters unifying the E5 switch and reservation ports (#439).

The bounded-autonomy runtime, the composite :mod:`operations.autonomy_gate`, and
the :mod:`operations.autonomy_execution_verifier` were each written against their
own port shapes. This module collapses those onto a single source of truth so
there is exactly ONE separate-autonomy switch implementation
(:class:`~operations.autonomy_switch.AutonomySwitchGate`) and ONE durable
reservation implementation (:class:`~operations.autonomy_runtime.store.ReservationStore`).

The adapters are thin, hold no state beyond their wrapped source, perform no
provider write, and fail closed:

* :class:`SwitchAutonomyPort` adapts the canonical switch gate onto the
  verifier's ``require_autonomy()``. Any denial (a disabled/absent/unavailable
  switch, surfaced by the gate as :class:`AutonomySwitchUnavailable`) is
  re-raised as the verifier's single :class:`AutonomySwitchDenied` deny signal.
* :class:`StoreReservationVerifierPort` adapts the canonical reservation store
  onto the verifier's ``require_reservation(operation_id, logical_action_id)``,
  confirming live in-flight ownership immediately before the write.
* :class:`StoreReservationGatePort` adapts the canonical reservation store onto
  the composite gate's ``reserve(**kwargs) -> bool``. It builds the strict,
  identifier-and-integer-only :class:`ReservationRequest` from trusted,
  server-owned inputs only (never from model or request-body content) and
  returns ``True`` only on a granted reservation.
"""

from __future__ import annotations

# Standard library
from typing import Any, Protocol

# Local modules
from operations.autonomy_execution_verifier import AutonomySwitchDenied
from operations.autonomy_runtime.store import ReservationOutcome, ReservationRequest, ReservationStore


class _SwitchSource(Protocol):
    def require_enabled(self) -> Any: ...


class SwitchAutonomyPort:
    """Adapt the canonical separate-autonomy switch onto the verifier's port."""

    __slots__ = ("_switch",)

    def __init__(self, switch: _SwitchSource) -> None:
        self._switch = switch

    def require_autonomy(self) -> None:
        """Permit only when the fresh separate switch enables autonomy, else deny."""
        try:
            self._switch.require_enabled()
        except AutonomySwitchDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - any switch denial/unavailability fails closed
            raise AutonomySwitchDenied("autonomy switch does not enable autonomous execution") from exc


class StoreReservationVerifierPort:
    """Adapt the canonical reservation store onto the verifier's require port."""

    __slots__ = ("_store",)

    def __init__(self, store: ReservationStore) -> None:
        self._store = store

    def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None:
        """Confirm the caller owns the live in-flight reservation; fail closed otherwise."""
        self._store.require(operation_id=operation_id, logical_action_id=logical_action_id)


def _direction(decision: dict[str, Any]) -> str:
    change = decision.get("change")
    if isinstance(change, dict):
        direction = change.get("direction")
        if direction in ("none", "increase", "decrease"):
            return str(direction)
    return "none"


def _action_micro_usd(decision: dict[str, Any], window_state: dict[str, Any]) -> int:
    candidate = decision.get("action_micro_usd")
    if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
        return candidate
    # Fall back to the window's own bound action cost (server-owned).
    return int(window_state["action_micro_usd"])


class StoreReservationGatePort:
    """Adapt the canonical reservation store onto the composite gate's reserve port.

    The gate calls ``reserve(**kwargs)`` with server-owned, hash-bound trusted
    inputs. This adapter distills them into the strict identifier-and-integer
    :class:`ReservationRequest` and returns ``True`` only when the store grants
    the reservation. A conflict or unavailability returns ``False`` so the gate
    fails closed; a structural error propagates so the gate treats it as
    "reservation unavailable".
    """

    __slots__ = ("_store", "_operation_id", "_logical_action_id")

    def __init__(self, store: ReservationStore, *, operation_id: str, logical_action_id: str) -> None:
        self._store = store
        self._operation_id = operation_id
        self._logical_action_id = logical_action_id

    def reserve(self, **kwargs: Any) -> bool:
        policy = kwargs["policy"]
        window_state = kwargs["window_state"]
        decision = kwargs["decision"]
        now_epoch_seconds = int(kwargs["now_epoch_seconds"])
        request = ReservationRequest(
            policy_ref={
                "policy_id": policy["policy_id"],
                "policy_version": policy["policy_version"],
                "policy_hash": policy["policy_hash"],
            },
            state_id=window_state["state_id"],
            expected_revision=int(window_state["state_revision"]),
            operation_id=self._operation_id,
            logical_action_id=self._logical_action_id,
            action_micro_usd=_action_micro_usd(decision, window_state),
            now_epoch_seconds=now_epoch_seconds,
            change_direction=_direction(decision),
        )
        result = self._store.reserve(request)
        return result.outcome is ReservationOutcome.RESERVED
