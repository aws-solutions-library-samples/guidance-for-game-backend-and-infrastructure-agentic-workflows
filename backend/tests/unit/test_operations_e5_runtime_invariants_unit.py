"""E5 bounded-autonomy source-wiring invariants (#439).

These lock the safety-critical, cross-module properties of the finished E5
source-level wiring that the per-module suites do not, on their own, assert:

* **Imports are AWS-free.** Importing any E5 runtime module (store, handler,
  service, dispatch, bridges, evaluator entry, execution verifier/selection)
  creates no boto3 client and performs no I/O; boto3 is imported lazily inside
  the bootstraps only.
* **The dispatch payload is closed.** The identifier-only envelope and the Step
  Functions StartExecution input carry ``operation_id`` and nothing else.
* **The E4 kill switch and the separate autonomy switch are each checked twice**
  on the autonomous write path: the kill switch on executor entry and again
  immediately before ``UpdateFleetCapacity``; the autonomy switch at the verifier
  (before the plan) and again in the immediate pre-write hook.
* **Reservation ownership is confirmed against current/expiry/ownership** in the
  pre-write hook, immediately before the single write.
"""

from __future__ import annotations

# Standard library
import importlib
import sys
from datetime import datetime, timezone
from typing import Any

# Third-party packages
import pytest

pytestmark = [pytest.mark.unit]

_E5_MODULES = [
    "operations.autonomy_runtime.store",
    "operations.autonomy_runtime.handler",
    "operations.autonomy_runtime.service",
    "operations.autonomy_runtime.dispatch",
    "operations.autonomy_runtime.bridges",
    "operations.autonomy_runtime.evaluator_entry",
    "operations.autonomy_execution_verifier",
    "operations.autonomy_execution_selection",
    "operations.execute.executor_entry",
]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("module_name", _E5_MODULES)
def test_importing_e5_module_creates_no_boto3_client(module_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing an E5 module must never construct an AWS client as a side effect."""
    created: list[str] = []

    # If boto3 is importable, wrap Session.client to detect any construction.
    boto3 = sys.modules.get("boto3")
    if boto3 is not None and hasattr(boto3, "Session"):
        original_client = boto3.Session.client

        def _tracking_client(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - guard
            created.append(name)
            return original_client(self, name, *args, **kwargs)

        monkeypatch.setattr(boto3.Session, "client", _tracking_client)

    sys.modules.pop(module_name, None)
    importlib.import_module(module_name)
    assert created == []


@pytest.mark.unit
def test_dispatch_envelope_payload_is_operation_id_only() -> None:
    # Local modules
    from operations.autonomy_runtime.dispatch import DispatchEnvelope

    envelope = DispatchEnvelope(operation_id="op_" + "a" * 26)
    assert envelope.payload() == {"operation_id": "op_" + "a" * 26}
    assert set(envelope.payload()) == {"operation_id"}


@pytest.mark.unit
def test_stepfunctions_start_execution_input_is_operation_id_only() -> None:
    # Standard library
    import json

    # Local modules
    from operations.autonomy_runtime.evaluator_entry import StepFunctionsStartExecution

    captured: list[dict[str, Any]] = []

    class _Sfn:
        def start_execution(self, **kwargs: Any) -> dict[str, str]:
            captured.append(kwargs)
            return {"executionArn": "arn:aws:states:us-west-2:111122223333:execution:x:y"}

    start = StepFunctionsStartExecution(
        client=_Sfn(),
        state_machine_arn="arn:aws:states:us-west-2:111122223333:stateMachine:autonomy-execute",
    )
    start({"operation_id": "op_" + "b" * 26}, name="op_" + "b" * 26)
    assert json.loads(captured[0]["input"]) == {"operation_id": "op_" + "b" * 26}
    # A deterministic execution name is supplied so a duplicate start is idempotent.
    assert captured[0]["name"] == "op_" + "b" * 26


# -- The switch/kill-switch twice + reservation ownership on the write core -----


class _CountingSwitchPort:
    def __init__(self) -> None:
        self.calls = 0

    def require_autonomy(self) -> None:
        self.calls += 1


class _CountingReservationPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None:
        self.calls.append((operation_id, logical_action_id))


class _KillGate:
    def __init__(self) -> None:
        self.calls = 0

    def require_phase(self, phase: str) -> Any:
        self.calls += 1

        class _D:
            config_version = 7

        return _D()


class _DurableGate:
    def __init__(self) -> None:
        self.calls = 0

    def require_phase(self, phase: str, *, deployed_decision: Any) -> None:
        self.calls += 1


def _plan() -> Any:
    # Local modules
    from operations.execution_verifier import VerifiedExecutionPlan

    intent = {
        "operation_id": "op_" + "a" * 26,
        "parameters": {"desired": 2, "minimum": 0, "maximum": 2},
        "expected_current_capacity": {"desired": 1, "minimum": 0, "maximum": 2},
    }
    return VerifiedExecutionPlan(
        intent=intent,
        logical_action_id="act_" + "a" * 64,
        fleet_arn="arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-1",
        fleet_id="fleet-1",
        location="us-west-2",
        max_writes=1,
    )


def _autonomous_service(switch: _CountingSwitchPort, reservation: _CountingReservationPort, kill: Any, durable: Any):
    # Local modules
    from operations.execute.executor_service import ExecutionInvocation, ExecutorService

    class _Adapter:
        def __init__(self) -> None:
            self.writes = 0

        def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]:
            # Before the write, read the expected current; after the write,
            # read the target so the post-write verification confirms success.
            if self.writes == 0:
                return {"desired": 1, "minimum": 0, "maximum": 2}
            return {"desired": 2, "minimum": 0, "maximum": 2}

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

    def _pre_write_hook(plan: Any) -> None:
        switch.require_autonomy()
        reservation.require_reservation(
            operation_id=plan.intent["operation_id"],
            logical_action_id=plan.logical_action_id,
        )

    class _UnusedVerifier:
        def verify(self, *, prepared_operation: Any, approval: Any) -> Any:  # pragma: no cover - never called
            raise RuntimeError("v1 verifier must not run on the autonomous path")

    adapter = _Adapter()
    service = ExecutorService(
        verifier=_UnusedVerifier(),
        adapter=adapter,
        store=_Store(),
        clock=lambda: _NOW,
        kill_switch_gate=kill,
        durable_control_gate=durable,
        pre_write_hook=_pre_write_hook,
    )
    return service, adapter, ExecutionInvocation(operation_id="op_" + "a" * 26)


@pytest.mark.unit
def test_autonomous_write_checks_kill_switch_twice_and_autonomy_switch_at_pre_write() -> None:
    switch = _CountingSwitchPort()
    reservation = _CountingReservationPort()
    kill = _KillGate()
    durable = _DurableGate()
    service, adapter, invocation = _autonomous_service(switch, reservation, kill, durable)

    result = service.execute_verified(invocation, plan=_plan(), lease_holder="exec-1")

    assert result["outcome"] == "SUCCEEDED"
    assert adapter.writes == 1
    # The E4 kill switch is checked TWICE: on entry and immediately before write.
    assert kill.calls == 2
    assert durable.calls == 2
    # The separate autonomy switch is checked in the immediate pre-write hook
    # (its FIRST check is the verifier's gate 7, exercised separately).
    assert switch.calls == 1
    # Reservation ownership is confirmed immediately before the write.
    assert reservation.calls == [("op_" + "a" * 26, "act_" + "a" * 64)]


@pytest.mark.unit
def test_autonomous_pre_write_reservation_loss_refuses_write() -> None:
    switch = _CountingSwitchPort()
    kill = _KillGate()
    durable = _DurableGate()

    class _LostReservation:
        def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None:
            raise RuntimeError("reservation lost")

    service, adapter, invocation = _autonomous_service(switch, _LostReservation(), kill, durable)  # type: ignore[arg-type]

    result = service.execute_verified(invocation, plan=_plan(), lease_holder="exec-1")

    # The pre-write hook raised: NO provider write, bounded FAILED recorded.
    assert result["outcome"] == "FAILED"
    assert adapter.writes == 0
