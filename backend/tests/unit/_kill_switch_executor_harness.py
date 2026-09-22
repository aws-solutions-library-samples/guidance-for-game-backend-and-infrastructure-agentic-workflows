"""Shared harness: run the REAL ExecutorService against a REAL kill-switch gate.

This helper builds a genuine :class:`ExecutorService` with a permissive
verifier/adapter/store and the caller's REAL
:class:`~operations.control.kill_switch_gate.KillSwitchGate` (backed by the real
:class:`AppConfigExtensionClient` over a scripted localhost opener). It returns
the bounded outcome (``"denied"`` when a phase is denied, otherwise the service
result outcome) and the number of provider writes the adapter issued, so a test
can prove that a dynamic flip of the switch between the executor's entry read and
its pre-write read blocks ``UpdateFleetCapacity``.
"""

from __future__ import annotations

# Standard library
from datetime import datetime
from typing import Any


def run_executor_with_gate(*, gate: Any, now: datetime) -> tuple[str, int]:
    # Local modules
    from operations.execute.executor_service import (
        ExecutionInvocation,
        ExecutorService,
        ExecutorServiceError,
    )
    from operations.execution_verifier import VerifiedExecutionPlan

    operation_id = "op_" + "a" * 26
    plan = VerifiedExecutionPlan(
        intent={
            "operation_id": operation_id,
            "parameters": {"desired": 2, "minimum": 0, "maximum": 2},
            "expected_current_capacity": {"desired": 1, "minimum": 0, "maximum": 2},
        },
        logical_action_id="act_" + "a" * 64,
        fleet_arn="arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-1",
        fleet_id="fleet-1",
        location="us-west-2",
        max_writes=1,
    )

    class _Verifier:
        def verify(self, *, prepared_operation: Any, approval: Any) -> Any:
            return plan

    class _Adapter:
        def __init__(self) -> None:
            self.writes = 0

        def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]:
            return {"desired": 1, "minimum": 0, "maximum": 2}

        def update_capacity(self, **kwargs: Any) -> None:
            self.writes += 1

    class _Store:
        def acquire_execution_lease(self, **kwargs: Any) -> Any:
            # Local modules
            from operations.execute.execution_store import LeaseAcquisition

            return LeaseAcquisition(generation=1, recorded_result=None)

        def record_execution_result(self, **kwargs: Any) -> Any:
            # Local modules
            from operations.execute.execution_store import ExecutionCommitOutcome

            return ExecutionCommitOutcome.RECORDED

    adapter = _Adapter()
    service = ExecutorService(
        verifier=_Verifier(),
        adapter=adapter,
        store=_Store(),
        clock=lambda: now,
        kill_switch_gate=gate,
    )
    invocation = ExecutionInvocation(operation_id=operation_id)
    try:
        result = service.execute(
            invocation,
            prepared_operation={"operation_id": operation_id},
            approval={"decision": "granted"},
            lease_holder="h",
        )
    except ExecutorServiceError:
        return "denied", adapter.writes
    return str(result.get("outcome", "")), adapter.writes
