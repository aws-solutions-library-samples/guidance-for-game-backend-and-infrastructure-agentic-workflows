"""Safe autonomous terminal-replay path before live reservation verify (#439, E5).

First E5 blocker: after a successful v2 execution settled the in-flight
reservation (generation 1), a *retry* invocation would run the
AutonomyExecutionVerifier — whose LAST precondition confirms live reservation
ownership — before the E3 execution-store idempotent replay inside
``execute_verified`` could return the already-recorded terminal result. The
settled reservation made ``require_reservation`` fail closed, so a genuinely
completed operation reported a spurious precondition failure on retry.

The fix adds a narrow, additive terminal-replay path that runs BEFORE the
verifier / live reservation check:

* validate the immutable v2 operation contract + prepared hash and derive the
  exact ``logical_action_id`` from ``(operation_id, prepared_hash)``;
* load the existing E3 recorded terminal result through a new narrow public
  ``ExecutorService.load_recorded_terminal_result`` method;
* validate the result contract and its exact operation/action ids;
* return it verbatim with NO Describe, NO provider write, NO settle, and without
  running the verifier or the write core.

A missing / nonterminal / malformed / mismatched recorded result never triggers
the shortcut: the invocation continues to the normal verify + write path (or
fails closed there). First attempts (no recorded result) are never bypassed.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.execution import (
    CONTRACT_VERSION,
    OUTCOME_SUCCEEDED,
    execution_intent_hash,
    logical_action_id,
)
from operations.execute import executor_entry
from operations.execute.executor_service import ExecutionInvocation

_OP = "op_" + "e" * 26
_PREPARED_HASH = "sha256:" + "a" * 64
_ACTION = logical_action_id(_OP, _PREPARED_HASH)

_V2_OPERATION = {
    "operation_id": _OP,
    "operation_contract_version": "2.0",
    "phase": "operate",
    "profile": "gamelift.capacity-adjustment/2.0",
    "capability": {"capability_version": "2.0"},
    "prepared_hash": _PREPARED_HASH,
}


def _v2_bundle() -> dict[str, Any]:
    return {
        "policy": {},
        "observation": {},
        "decision": {},
        "operation": dict(_V2_OPERATION),
        "window_state": {},
        "reservation": {"operation_id": _OP, "logical_action_id": _ACTION},
    }


def _recorded_result(*, operation_id: str = _OP, action_id: str = _ACTION) -> dict[str, Any]:
    """A well-formed recorded terminal SUCCEEDED result matching the contract."""
    target = {"desired": 5, "minimum": 1, "maximum": 10}
    intent = {
        "execution_contract_version": CONTRACT_VERSION,
        "operation_id": operation_id,
        "prepared_hash": _PREPARED_HASH,
        "logical_action_id": action_id,
        "provider": "gamelift",
        "action": "update-fleet-capacity",
        "target": {"provider": "gamelift", "fleet_id": "fleet-x", "location": "us-west-2"},
        "parameters": target,
        "expected_current_capacity": {"desired": 2, "minimum": 1, "maximum": 10},
    }
    verification = {
        "execution_contract_version": CONTRACT_VERSION,
        "operation_id": operation_id,
        "logical_action_id": action_id,
        "verified_at": "2026-01-01T00:00:00Z",
        "observed_capacity": target,
        "expected_capacity": target,
        "matches_target": True,
        "attempts_observed": 1,
    }
    return {
        "execution_contract_version": CONTRACT_VERSION,
        "operation_id": operation_id,
        "logical_action_id": action_id,
        "intent_hash": execution_intent_hash(intent),
        "outcome": OUTCOME_SUCCEEDED,
        "provider_write_issued": True,
        "verification": verification,
        "recorded_at": "2026-01-01T00:00:00Z",
    }


class _BundleStore:
    def __init__(self, bundle: dict[str, Any] | None, *, dispatched: bool = True) -> None:
        self._bundle = bundle
        self._dispatched = dispatched
        self.audit_loads: list[str] = []

    def load_bundle(self, operation_id: str) -> dict[str, Any] | None:
        return self._bundle

    def load_dispatch_audit(self, *, operation_id: str, phase: str) -> dict[str, Any] | None:
        self.audit_loads.append(phase)
        if self._dispatched:
            return {"operation_id": operation_id, "phase": phase, "execution_name": operation_id}
        return None


class _RecordingVerifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify(self, *, operation_id: str, evidence: Any) -> Any:
        self.calls.append(operation_id)

        class _Plan:
            logical_action_id = _ACTION
            intent = {"operation_id": operation_id, "parameters": {}, "expected_current_capacity": {}}

        return _Plan()


class _RecordingService:
    """Autonomy service exposing the narrow replay loader + the write core."""

    def __init__(self, recorded: dict[str, Any] | None) -> None:
        self._recorded = recorded
        self.load_calls: list[tuple[str, str]] = []
        self.execute_verified_calls: list[Any] = []

    def load_recorded_terminal_result(self, *, operation_id: str, logical_action_id: str) -> dict[str, Any] | None:
        self.load_calls.append((operation_id, logical_action_id))
        return self._recorded

    def execute_verified(self, invocation: Any, *, plan: Any, lease_holder: str) -> dict[str, Any]:
        self.execute_verified_calls.append(plan)
        return {"outcome": "SUCCEEDED", "provider_write_issued": True}


class _Reservation:
    def __init__(self) -> None:
        self.settled: list[tuple[str, str, str]] = []
        self.required: list[tuple[str, str]] = []

    def require(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
        self.required.append((operation_id, logical_action_id))
        raise RuntimeError("reservation already settled")  # settled after prior success

    def settle(
        self, *, operation_id: str, logical_action_id: str, terminal: str, generation: int | None = None
    ) -> None:
        self.settled.append((operation_id, logical_action_id, terminal))


class _NullMetrics:
    def record(self, *args: Any, **kwargs: Any) -> None:
        pass

    def put_latency_ms(self, *args: Any, **kwargs: Any) -> None:
        pass


def _runtime(service: Any, verifier: Any, reservation: Any, bundle_store: Any) -> Any:
    return executor_entry.AutonomyExecutorRuntime(
        service=_RecordingService(None),  # v1 service, unused here
        autonomy_service=service,
        reload_store=_NoV1Reload(),
        metrics=_NullMetrics(),
        bundle_store=bundle_store,
        autonomy_verifier=verifier,
        reservation=reservation,
    )


class _NoV1Reload:
    def load_for_execution(self, operation_id: str) -> Any:
        return None


@pytest.mark.unit
def test_successful_terminal_retry_replays_without_verifier_or_write() -> None:
    service = _RecordingService(_recorded_result())
    verifier = _RecordingVerifier()
    reservation = _Reservation()
    runtime = _runtime(service, verifier, reservation, _BundleStore(_v2_bundle()))

    result = executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")

    # The recorded terminal result is replayed verbatim.
    assert result["outcome"] == OUTCOME_SUCCEEDED
    assert result["logical_action_id"] == _ACTION
    # The verifier (whose last check is LIVE reservation ownership) is NEVER run.
    assert verifier.calls == []
    # The write core is NEVER entered.
    assert service.execute_verified_calls == []
    # No live-reservation verification and no settle happened on the replay.
    assert reservation.required == []
    assert reservation.settled == []
    # The narrow loader was consulted with the DERIVED action id.
    assert service.load_calls == [(_OP, _ACTION)]


@pytest.mark.unit
def test_first_attempt_without_recorded_result_runs_normal_path() -> None:
    service = _RecordingService(None)  # no recorded result -> first attempt
    verifier = _RecordingVerifier()

    class _OkReservation(_Reservation):
        def require(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
            self.required.append((operation_id, logical_action_id))

    reservation = _OkReservation()
    runtime = _runtime(service, verifier, reservation, _BundleStore(_v2_bundle()))

    result = executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")

    assert result["outcome"] == "SUCCEEDED"
    # First attempt runs the verifier and the write core normally.
    assert verifier.calls == [_OP]
    assert len(service.execute_verified_calls) == 1


@pytest.mark.unit
def test_mismatched_recorded_action_id_does_not_shortcut() -> None:
    # A recorded result whose action id does not match the derived id must not be
    # replayed; the invocation continues the normal path.
    bad = _recorded_result(action_id="act_" + "b" * 64)
    service = _RecordingService(bad)
    verifier = _RecordingVerifier()

    class _OkReservation(_Reservation):
        def require(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
            self.required.append((operation_id, logical_action_id))

    reservation = _OkReservation()
    runtime = _runtime(service, verifier, reservation, _BundleStore(_v2_bundle()))

    result = executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")
    # Mismatched id -> no shortcut -> normal verify + write path ran.
    assert result["outcome"] == "SUCCEEDED"
    assert verifier.calls == [_OP]
    assert len(service.execute_verified_calls) == 1


@pytest.mark.unit
def test_malformed_recorded_result_does_not_shortcut() -> None:
    service = _RecordingService({"not": "a valid execution result"})
    verifier = _RecordingVerifier()

    class _OkReservation(_Reservation):
        def require(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
            self.required.append((operation_id, logical_action_id))

    reservation = _OkReservation()
    runtime = _runtime(service, verifier, reservation, _BundleStore(_v2_bundle()))

    result = executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")
    assert result["outcome"] == "SUCCEEDED"
    assert verifier.calls == [_OP]
    assert len(service.execute_verified_calls) == 1
