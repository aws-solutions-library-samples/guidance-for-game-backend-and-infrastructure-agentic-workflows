"""Identifier-only E5 runtime handler tests (#439).

The runtime blockers review found the dispatch path forgeable and unwired. This
suite locks the single identifier-only runtime handler that ties the deterministic
prepare, durable persistence, the atomic reservation, and the composite
pre-dispatch gate together and hands the durable Step Functions dispatcher an
``operation_id``-only envelope:

1. deterministically PREPARE the v2 decision + prepared operation from trusted,
   server-owned inputs only (no model/request identity/policy/limits/executor);
2. PERSIST the policy, canonical observation, decision, operation, bound
   pre-reservation window state, and reservation reference as immutable inert
   evidence BEFORE any reservation is taken — we never reserve before the
   evidence exists;
3. atomically RESERVE the write's footprint against the policy-bound window;
4. check the composite pre-dispatch gate — static + separate AppConfig switch +
   E4 cached kill-switch + E4 durable intent — BEFORE StartExecution;
5. hand off an identifier-only envelope and call StartExecution with
   ``operation_id`` alone.

Every failure/denial fails closed: a denied decision persists nothing and never
reserves; a persist failure never reserves; a reservation conflict never reaches
the gate or StartExecution; a gate denial releases the in-flight slot and never
calls StartExecution. No provider client is ever touched (the handler holds no
provider-write credential).
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime import AutonomyRuntimeInputs, AutonomyRuntimeService
from operations.autonomy_runtime.handler import (
    AutonomyRuntimeHandler,
    RuntimeDispatchOutcome,
)
from operations.autonomy_runtime.store import InMemoryReservationStore, ReservationOutcome, ReservationResult
from operations.contracts import load_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"
_EVALUATED_AT = datetime(2026, 9, 21, 19, 12, 52, tzinfo=timezone.utc)
_AUTHORITY = {
    "deployment_mode": "operate",
    "tenant_policy": "operate",
    "workspace_policy": "operate",
    "principal_authority": "operate",
    "capability_maximum": "operate",
    "operation_risk_policy": "operate",
}
_PRINCIPAL = {
    "source_type": "automation",
    "subject_id": "subject.autonomy-agent",
    "client_id": "client.autonomy-runtime",
}
_CORRELATION = {"correlation_id": "corr.autonomy-1", "request_id": "request.autonomy-1"}


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


def _observation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-autonomy-observation.valid.json")


def _window_state() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")


def _requested() -> dict[str, int]:
    return load_json(FIXTURES / "gamelift-capacity-autonomous-operation.valid.json")["parameters"]["requested"]


def _inputs() -> AutonomyRuntimeInputs:
    return AutonomyRuntimeInputs(
        policy=_policy(),
        observation=_observation(),
        authority_inputs=_AUTHORITY,
        automation_principal=_PRINCIPAL,
        window_state=_window_state(),
        requested=_requested(),
        correlation=_CORRELATION,
        evaluated_at=_EVALUATED_AT,
    )


class _FakeBundleStore:
    def __init__(self, *, error: Exception | None = None, log: list[str] | None = None) -> None:
        self.persisted: list[dict[str, Any]] = []
        self._error = error
        self._log = log

    def persist_bundle(self, **kwargs: Any) -> None:
        if self._log is not None:
            self._log.append("persist")
        if self._error is not None:
            raise self._error
        self.persisted.append(kwargs)


class _RecordingReservationStore:
    """Wrap the in-memory store to record the call order without monkeypatching."""

    def __init__(self, inner: InMemoryReservationStore, log: list[str]) -> None:
        self._inner = inner
        self._log = log

    def reserve(self, request: Any) -> ReservationResult:
        self._log.append("reserve")
        return self._inner.reserve(request)

    def settle(self, **kwargs: Any) -> ReservationResult:
        return self._inner.settle(**kwargs)

    def current(self, state_id: str) -> dict[str, Any]:
        return self._inner.current(state_id)


class _PermitGate:
    def __init__(self) -> None:
        self.pre_dispatch_calls = 0

    def require_pre_dispatch(self) -> None:
        self.pre_dispatch_calls += 1


class _DenyGate:
    def require_pre_dispatch(self) -> None:
        # Local modules
        from operations.autonomy_gate import AutonomyGateDenied

        raise AutonomyGateDenied("dispatch", "denied for test")


class _RecordingStartExecution:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def __call__(self, payload: dict[str, str]) -> None:
        self.calls.append(payload)


def _handler(*, store: Any, gate: Any, start_execution: Any, reservation: Any) -> AutonomyRuntimeHandler:
    return AutonomyRuntimeHandler(
        service=AutonomyRuntimeService(),
        reservation_store=reservation,
        bundle_store=store,
        gate=gate,
        start_execution=start_execution,
    )


@pytest.mark.unit
def test_happy_path_prepares_persists_reserves_gates_and_starts_execution() -> None:
    store = _FakeBundleStore()
    gate = _PermitGate()
    start = _RecordingStartExecution()
    reservation = InMemoryReservationStore(initial_state=_window_state())
    handler = _handler(store=store, gate=gate, start_execution=start, reservation=reservation)

    result = handler.dispatch(_inputs())

    assert result.outcome is RuntimeDispatchOutcome.DISPATCHED
    # Prepared + persisted the full bundle.
    assert len(store.persisted) == 1
    persisted = store.persisted[0]
    for key in ("policy", "observation", "decision", "operation", "window_state", "reservation"):
        assert key in persisted
    # Reserved (in-flight taken).
    assert reservation.current(_window_state()["state_id"])["in_flight"] == 1
    # Gate consulted before StartExecution.
    assert gate.pre_dispatch_calls == 1
    # StartExecution invoked with operation_id ONLY.
    assert len(start.calls) == 1
    assert set(start.calls[0]) == {"operation_id"}
    assert start.calls[0]["operation_id"] == result.operation_id


@pytest.mark.unit
def test_evidence_is_persisted_before_the_reservation_is_taken() -> None:
    """Never reserve before evidence exists: persist strictly precedes reserve."""
    order: list[str] = []
    inner = InMemoryReservationStore(initial_state=_window_state())
    reservation = _RecordingReservationStore(inner, order)
    store = _FakeBundleStore(log=order)
    handler = _handler(
        store=store,
        gate=_PermitGate(),
        start_execution=_RecordingStartExecution(),
        reservation=reservation,
    )

    handler.dispatch(_inputs())

    assert order == ["persist", "reserve"]


@pytest.mark.unit
def test_persist_failure_fails_closed_and_never_reserves() -> None:
    store = _FakeBundleStore(error=RuntimeError("bundle store unavailable"))
    gate = _PermitGate()
    start = _RecordingStartExecution()
    reservation = InMemoryReservationStore(initial_state=_window_state())
    handler = _handler(store=store, gate=gate, start_execution=start, reservation=reservation)

    result = handler.dispatch(_inputs())

    assert result.outcome is RuntimeDispatchOutcome.REFUSED
    assert result.reason == "persist_failed"
    assert start.calls == []
    # A persist failure never advances the window state / takes the in-flight slot.
    assert reservation.current(_window_state()["state_id"])["in_flight"] == 0
    assert gate.pre_dispatch_calls == 0


@pytest.mark.unit
def test_gate_denial_fails_closed_without_starting_execution() -> None:
    store = _FakeBundleStore()
    start = _RecordingStartExecution()
    reservation = InMemoryReservationStore(initial_state=_window_state())
    handler = _handler(store=store, gate=_DenyGate(), start_execution=start, reservation=reservation)

    result = handler.dispatch(_inputs())

    assert result.outcome is RuntimeDispatchOutcome.REFUSED
    # No StartExecution on a gate denial.
    assert start.calls == []
    # The bundle was already persisted (evidence precedes reserve/gate), and the
    # gate denial released the in-flight slot.
    assert len(store.persisted) == 1
    assert reservation.current(_window_state()["state_id"])["in_flight"] == 0


@pytest.mark.unit
def test_reservation_conflict_fails_closed_without_starting_execution() -> None:
    store = _FakeBundleStore()
    gate = _PermitGate()
    start = _RecordingStartExecution()

    class _ConflictStore:
        def reserve(self, request: Any) -> ReservationResult:
            return ReservationResult(ReservationOutcome.CONFLICT)

    handler = _handler(store=store, gate=gate, start_execution=start, reservation=_ConflictStore())
    result = handler.dispatch(_inputs())

    assert result.outcome is RuntimeDispatchOutcome.REFUSED
    assert start.calls == []
    # Evidence was persisted FIRST (inert), then the reservation conflicted; the
    # gate is never reached and nothing is dispatched.
    assert len(store.persisted) == 1
    assert gate.pre_dispatch_calls == 0


@pytest.mark.unit
def test_denied_decision_never_persists_reserves_or_dispatches() -> None:
    store = _FakeBundleStore()
    gate = _PermitGate()
    start = _RecordingStartExecution()
    reservation = InMemoryReservationStore(initial_state=_window_state())
    handler = _handler(store=store, gate=gate, start_execution=start, reservation=reservation)

    # Force a denied decision by requesting a change beyond policy bounds.
    denied_inputs = AutonomyRuntimeInputs(
        policy=_policy(),
        observation=_observation(),
        authority_inputs=_AUTHORITY,
        automation_principal=_PRINCIPAL,
        window_state=_window_state(),
        requested={"desired": 100000, "minimum": 0, "maximum": 100000},
        correlation=_CORRELATION,
        evaluated_at=_EVALUATED_AT,
    )
    result = handler.dispatch(denied_inputs)

    assert result.outcome is RuntimeDispatchOutcome.REFUSED
    assert start.calls == []
    # A denied decision persists no evidence and never takes the in-flight slot.
    assert store.persisted == []
    assert reservation.current(_window_state()["state_id"])["in_flight"] == 0


@pytest.mark.unit
def test_handler_holds_no_provider_write_surface() -> None:
    store = _FakeBundleStore()
    handler = _handler(
        store=store,
        gate=_PermitGate(),
        start_execution=_RecordingStartExecution(),
        reservation=InMemoryReservationStore(initial_state=_window_state()),
    )
    for forbidden in ("update_capacity", "update_fleet_capacity", "provider_client", "execute", "boto3"):
        assert not hasattr(handler, forbidden)
