"""Executor-entry settle-hardening tests (#439, E5).

The Final #439 hardening pass threads the reserve ``generation`` into the
executor's settle and require, and requires that a *failed* settle is surfaced as
a metrics-safe signal WITHOUT being swallowed as success and WITHOUT any write
retry. These tests lock:

* the generation is threaded into ``settle`` when the reservation port accepts it;
* a settle that reports a non-released outcome (CONFLICT / UNAVAILABLE) triggers
  the ``on_settle_failure`` metrics callback but never masks the handoff result;
* a settle that raises is likewise surfaced, never retried, and never masks the
  handoff outcome/exception.
"""

from __future__ import annotations

# Standard library
from enum import Enum
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.executor_entry import execute_autonomous
from operations.execute.executor_service import ExecutionInvocation

_OP = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
_ACTION = "act_" + "a" * 64


class _Outcome(str, Enum):
    RESERVED = "reserved"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class _Result:
    def __init__(self, outcome: _Outcome) -> None:
        self.outcome = outcome


class _FakePlan:
    logical_action_id = _ACTION

    def __init__(self) -> None:
        self.intent = {"operation_id": _OP}


class _FakeVerifier:
    def verify(self, *, operation_id: str, evidence: Any) -> Any:
        return _FakePlan()


class _FakeService:
    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self._result = result or {"outcome": "SUCCEEDED", "provider_write_issued": True}
        self._error = error

    def execute_verified(self, invocation: Any, *, plan: Any, lease_holder: str) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._result


class _GenReservation:
    """A reservation that records the generation and returns a chosen outcome."""

    def __init__(self, outcome: _Outcome = _Outcome.RESERVED, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcome = outcome
        self._raise = raise_exc

    def settle(self, *, operation_id: str, logical_action_id: str, terminal: str, generation: int | None = None) -> Any:
        self.calls.append(
            {
                "operation_id": operation_id,
                "logical_action_id": logical_action_id,
                "terminal": terminal,
                "generation": generation,
            }
        )
        if self._raise is not None:
            raise self._raise
        return _Result(self._outcome)


def _bundle() -> dict[str, Any]:
    return {"policy": {}, "observation": {}, "decision": {}, "operation": {"operation_id": _OP}, "window_state": {}}


def _call(
    reservation: Any, *, generation: int | None, on_settle_failure: Any = None, service: Any = None
) -> dict[str, Any]:
    return execute_autonomous(
        ExecutionInvocation(operation_id=_OP),
        bundle=_bundle(),
        logical_action_id=_ACTION,
        verifier=_FakeVerifier(),
        service=service or _FakeService(),
        reservation=reservation,
        lease_holder="exec-1",
        generation=generation,
        on_settle_failure=on_settle_failure,
    )


@pytest.mark.unit
def test_generation_is_threaded_into_settle() -> None:
    reservation = _GenReservation()
    _call(reservation, generation=1)
    assert reservation.calls == [
        {"operation_id": _OP, "logical_action_id": _ACTION, "terminal": "succeeded", "generation": 1}
    ]


@pytest.mark.unit
def test_settle_conflict_surfaces_metric_without_masking_success() -> None:
    reservation = _GenReservation(outcome=_Outcome.CONFLICT)
    flags: list[str] = []
    result = _call(reservation, generation=1, on_settle_failure=lambda: flags.append("settle_failed"))
    # The handoff result is NOT masked by the settle conflict.
    assert result["outcome"] == "SUCCEEDED"
    # The failed settle is surfaced as a metrics-safe signal.
    assert flags == ["settle_failed"]


@pytest.mark.unit
def test_settle_unavailable_surfaces_metric() -> None:
    reservation = _GenReservation(outcome=_Outcome.UNAVAILABLE)
    flags: list[str] = []
    _call(reservation, generation=1, on_settle_failure=lambda: flags.append("x"))
    assert flags == ["x"]


@pytest.mark.unit
def test_settle_exception_is_surfaced_not_retried_and_does_not_mask() -> None:
    reservation = _GenReservation(raise_exc=RuntimeError("settle blew up"))
    flags: list[str] = []
    result = _call(reservation, generation=1, on_settle_failure=lambda: flags.append("x"))
    # A settle exception never masks the handoff outcome and is not retried.
    assert result["outcome"] == "SUCCEEDED"
    assert len(reservation.calls) == 1  # no retry
    assert flags == ["x"]


@pytest.mark.unit
def test_released_settle_does_not_trip_the_failure_metric() -> None:
    reservation = _GenReservation(outcome=_Outcome.RESERVED)
    flags: list[str] = []
    _call(reservation, generation=1, on_settle_failure=lambda: flags.append("x"))
    assert flags == []


@pytest.mark.unit
def test_write_core_failure_still_settles_as_failed_and_surfaces_conflict() -> None:
    reservation = _GenReservation(outcome=_Outcome.CONFLICT)
    flags: list[str] = []
    with pytest.raises(RuntimeError):
        _call(
            reservation,
            generation=1,
            on_settle_failure=lambda: flags.append("x"),
            service=_FakeService(error=RuntimeError("write core blew up")),
        )
    assert reservation.calls[0]["terminal"] == "failed"
    assert flags == ["x"]
