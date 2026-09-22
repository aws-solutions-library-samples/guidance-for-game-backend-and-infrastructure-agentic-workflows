"""Executor kill-switch enforcement tests (issue #416, E4).

The E3 executor is the last line before a provider write, so it checks the
kill-switch TWICE: once on entry (before any verify/Describe/write) and again
immediately before ``UpdateFleetCapacity`` (after the pre-write Describe, so a
switch flipped during the Describe still blocks the write). If either check
denies the execute phase the executor performs NO ``UpdateFleetCapacity`` and
raises a bounded executor failure — the world is left untouched.

The gate is optional on the service so the existing E3 unit suite (no gate)
stays unchanged; when present it is enforced exactly at these two points.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _GateStub:
    """A kill-switch gate stub that permits or denies the execute phase.

    ``deny_after`` lets a test permit the entry check but deny the pre-write
    check (simulating a switch flipped during the pre-write Describe).
    """

    def __init__(self, *, permit: bool = True, deny_after: int | None = None) -> None:
        self._permit = permit
        self._deny_after = deny_after
        self.calls = 0

    def require_phase(self, phase: str) -> Any:
        self.calls += 1
        from operations.control.kill_switch_gate import PhaseDenied

        if not self._permit:
            raise PhaseDenied(phase, "disabled")
        if self._deny_after is not None and self.calls > self._deny_after:
            raise PhaseDenied(phase, "flipped mid-flight")
        return object()


def _build_service(gate: Any) -> Any:
    """Build an ExecutorService with a permissive verifier/adapter/store + gate."""
    from operations.execute.executor_service import ExecutionInvocation, ExecutorService

    # Minimal stubs: the verifier returns a plan; the adapter records writes.
    plan = _plan()

    class _Verifier:
        def verify(self, *, prepared_operation: Any, approval: Any) -> Any:
            return plan

    class _Adapter:
        def __init__(self) -> None:
            self.writes = 0
            self.describes = 0

        def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]:
            self.describes += 1
            return {"desired": 1, "minimum": 0, "maximum": 2}

        def update_capacity(self, *, fleet_id: str, location: str, desired: int, minimum: int, maximum: int) -> None:
            self.writes += 1

    class _Store:
        def acquire_execution_lease(self, **kwargs: Any) -> Any:
            from operations.execute.execution_store import LeaseAcquisition

            return LeaseAcquisition(generation=1, recorded_result=None)

        def record_execution_result(self, **kwargs: Any) -> Any:
            from operations.execute.execution_store import ExecutionCommitOutcome

            return ExecutionCommitOutcome.RECORDED

    adapter = _Adapter()
    service = ExecutorService(
        verifier=_Verifier(),
        adapter=adapter,
        store=_Store(),
        clock=lambda: _NOW,
        kill_switch_gate=gate,
    )
    return service, adapter, ExecutionInvocation(operation_id="op_" + "a" * 26)


def _plan() -> Any:
    from operations.execution_verifier import VerifiedExecutionPlan

    intent = {
        "operation_id": "op_" + "a" * 26,
        "parameters": {"desired": 2, "minimum": 0, "maximum": 2},
        "expected_current_capacity": {"desired": 1, "minimum": 0, "maximum": 2},
    }
    return VerifiedExecutionPlan(
        intent=intent,
        logical_action_id="act_" + "a" * 64,
        fleet_arn="arn:aws:gamelift:us-west-2:111122223333:fleet/fleet-1",
        fleet_id="fleet-1",
        location="us-west-2",
        max_writes=1,
    )


def _prepared_and_approval() -> tuple[dict[str, Any], dict[str, Any]]:
    return ({"operation_id": "op_" + "a" * 26}, {"decision": "granted"})


def test_entry_gate_denial_blocks_write() -> None:
    from operations.execute.executor_service import ExecutorServiceError

    gate = _GateStub(permit=False)
    service, adapter, invocation = _build_service(gate)
    prepared, approval = _prepared_and_approval()
    with pytest.raises(ExecutorServiceError):
        service.execute(invocation, prepared_operation=prepared, approval=approval, lease_holder="h")
    assert adapter.writes == 0
    assert gate.calls == 1  # denied at entry, before any Describe


def test_pre_write_gate_denial_blocks_write_after_describe() -> None:
    from operations.execute.executor_service import ExecutorServiceError

    # Permit entry (call 1), deny the pre-write check (call 2).
    gate = _GateStub(permit=True, deny_after=1)
    service, adapter, invocation = _build_service(gate)
    prepared, approval = _prepared_and_approval()
    with pytest.raises(ExecutorServiceError):
        service.execute(invocation, prepared_operation=prepared, approval=approval, lease_holder="h")
    assert adapter.writes == 0
    assert gate.calls == 2  # entry permitted, pre-write denied
    assert adapter.describes >= 1  # the pre-write Describe happened before the second check


def test_both_gates_permit_allows_write() -> None:
    gate = _GateStub(permit=True)
    service, adapter, invocation = _build_service(gate)
    prepared, approval = _prepared_and_approval()
    service.execute(invocation, prepared_operation=prepared, approval=approval, lease_holder="h")
    assert adapter.writes == 1
    assert gate.calls == 2  # entry + pre-write


def test_no_gate_preserves_existing_behavior() -> None:
    service, adapter, invocation = _build_service(gate=None)
    prepared, approval = _prepared_and_approval()
    service.execute(invocation, prepared_operation=prepared, approval=approval, lease_holder="h")
    assert adapter.writes == 1
