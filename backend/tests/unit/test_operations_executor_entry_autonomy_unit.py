"""Executor-entry v2 autonomous routing + settlement tests (#439, E5).

The E5 blockers review found the v2 path unreachable from the executor entry.
This suite locks the additive orchestration that reloads a v2 bundle, runs the
AutonomyExecutionVerifier, feeds its VerifiedExecutionPlan into the existing
ExecutorService write core via execute_verified, and — crucially — SETTLES the
in-flight reservation on every terminal/failed handoff so the concurrency slot
is always released.

The stable logical_action_id is computed once by the entry from the reloaded,
hash-bound operation and threaded through so the settle in the ``finally`` always
targets the exact reservation, whether the write core succeeds, fails, or raises,
and whether verification itself raises.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.executor_entry import execute_autonomous
from operations.execute.executor_service import ExecutionInvocation

_OP = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
_ACTION = "act_" + "a" * 64


class _FakePlan:
    logical_action_id = _ACTION

    def __init__(self) -> None:
        self.intent = {"operation_id": _OP}


class _FakeVerifier:
    def __init__(self, plan: Any = None, error: Exception | None = None) -> None:
        self._plan = plan or _FakePlan()
        self._error = error
        self.calls: list[str] = []

    def verify(self, *, operation_id: str, evidence: Any) -> Any:
        self.calls.append(operation_id)
        if self._error is not None:
            raise self._error
        return self._plan


class _FakeService:
    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self._result = result or {"outcome": "SUCCEEDED", "provider_write_issued": True}
        self._error = error

    def execute_verified(self, invocation: Any, *, plan: Any, lease_holder: str) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._result


class _FakeReservation:
    def __init__(self) -> None:
        self.settled: list[tuple[str, str, str]] = []

    def require(self, *, operation_id: str, logical_action_id: str) -> None:
        pass

    def settle(self, *, operation_id: str, logical_action_id: str, terminal: str) -> Any:
        self.settled.append((operation_id, logical_action_id, terminal))


def _bundle() -> dict[str, Any]:
    return {
        "policy": {},
        "observation": {},
        "decision": {},
        "operation": {"operation_id": _OP},
        "window_state": {},
    }


def _call(verifier: Any, service: Any, reservation: Any) -> dict[str, Any]:
    return execute_autonomous(
        ExecutionInvocation(operation_id=_OP),
        bundle=_bundle(),
        logical_action_id=_ACTION,
        verifier=verifier,
        service=service,
        reservation=reservation,
        lease_holder="exec-1",
    )


@pytest.mark.unit
def test_autonomous_path_verifies_and_executes_then_settles() -> None:
    verifier = _FakeVerifier()
    service = _FakeService(result={"outcome": "SUCCEEDED", "provider_write_issued": True})
    reservation = _FakeReservation()
    result = _call(verifier, service, reservation)
    assert result["outcome"] == "SUCCEEDED"
    assert verifier.calls == [_OP]
    assert reservation.settled == [(_OP, _ACTION, "succeeded")]


@pytest.mark.unit
def test_settles_in_flight_even_when_write_core_raises() -> None:
    verifier = _FakeVerifier()
    service = _FakeService(error=RuntimeError("write core blew up"))
    reservation = _FakeReservation()
    with pytest.raises(RuntimeError):
        _call(verifier, service, reservation)
    assert reservation.settled == [(_OP, _ACTION, "failed")]


@pytest.mark.unit
def test_failed_outcome_settles_as_failed() -> None:
    verifier = _FakeVerifier()
    service = _FakeService(result={"outcome": "FAILED", "provider_write_issued": False})
    reservation = _FakeReservation()
    result = _call(verifier, service, reservation)
    assert result["outcome"] == "FAILED"
    assert reservation.settled == [(_OP, _ACTION, "failed")]


@pytest.mark.unit
def test_verifier_failure_settles_and_propagates() -> None:
    verifier = _FakeVerifier(error=RuntimeError("binding invalid"))
    service = _FakeService()
    reservation = _FakeReservation()
    with pytest.raises(RuntimeError):
        _call(verifier, service, reservation)
    assert reservation.settled == [(_OP, _ACTION, "failed")]
