"""Handler lease/generation threading tests (#439, E5 hardening).

The Final #439 hardening pass has the runtime handler derive a bounded reservation
``lease_not_after`` — no later than the decision expiry — and a fresh
``generation`` of 1, so a crash between reserve and settle leaves a bounded,
reclaimable lease rather than a wedged in-flight slot. These tests lock that the
handler threads both onto the ``ReservationRequest`` and that the derived lease
never exceeds the prepared decision's own expiry.
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
from operations.autonomy_runtime.handler import AutonomyRuntimeHandler, RuntimeDispatchOutcome
from operations.autonomy_runtime.store import InMemoryReservationStore, ReservationRequest, ReservationResult
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
    def persist_bundle(self, **kwargs: Any) -> None:
        pass


class _CapturingReservation:
    """Capture the ReservationRequest the handler builds, then delegate."""

    def __init__(self, inner: InMemoryReservationStore) -> None:
        self._inner = inner
        self.requests: list[ReservationRequest] = []

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        self.requests.append(request)
        return self._inner.reserve(request)

    def settle(self, **kwargs: Any) -> ReservationResult:
        return self._inner.settle(**kwargs)

    def current(self, state_id: str) -> dict[str, Any]:
        return self._inner.current(state_id)


class _PermitGate:
    def require_pre_dispatch(self) -> None:
        pass


class _RecordingStartExecution:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def __call__(self, payload: dict[str, str]) -> None:
        self.calls.append(payload)


def _handler(reservation: Any) -> AutonomyRuntimeHandler:
    return AutonomyRuntimeHandler(
        service=AutonomyRuntimeService(),
        reservation_store=reservation,
        bundle_store=_FakeBundleStore(),
        gate=_PermitGate(),
        start_execution=_RecordingStartExecution(),
    )


@pytest.mark.unit
def test_handler_threads_bounded_lease_and_generation() -> None:
    reservation = _CapturingReservation(InMemoryReservationStore(initial_state=_window_state()))
    result = _handler(reservation).dispatch(_inputs())
    assert result.outcome is RuntimeDispatchOutcome.DISPATCHED
    assert len(reservation.requests) == 1
    request = reservation.requests[0]
    now_epoch = int(_EVALUATED_AT.timestamp())
    # A bounded lease strictly after now.
    assert request.lease_not_after is not None
    assert request.lease_not_after > now_epoch
    # A fresh generation.
    assert request.generation == 1


@pytest.mark.unit
def test_lease_never_exceeds_the_decision_expiry() -> None:
    reservation = _CapturingReservation(InMemoryReservationStore(initial_state=_window_state()))
    handler = _handler(reservation)
    prepared = AutonomyRuntimeService().prepare(_inputs())
    decision_expiry = datetime.fromisoformat(
        prepared.operation["decision_expires_at"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    decision_epoch = int(decision_expiry.timestamp())

    handler.dispatch(_inputs())
    request = reservation.requests[0]
    assert request.lease_not_after is not None
    # The derived lease is never later than the decision's own expiry.
    assert request.lease_not_after <= decision_epoch
