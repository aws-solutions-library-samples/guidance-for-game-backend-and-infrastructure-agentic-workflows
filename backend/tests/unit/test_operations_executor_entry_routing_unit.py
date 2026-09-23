"""Executor-entry v1/v2 routing tests (#439, E5).

The E5 blockers review found the v2 autonomous path unreachable from the
executor Lambda handler: the handler only ran the v1 human-approval branch and
``_build_runtime`` constructed no autonomy components. This suite locks the
actual routing seam the handler uses:

* a reloaded **v1** ``gamelift.capacity-adjustment/1.0`` operation runs the
  UNCHANGED human-approval path (``service.execute`` with the stored approval);
* a reloaded **v2** ``gamelift.capacity-adjustment/2.0`` autonomous operation is
  routed — from the immutable, server-owned envelope alone — through the
  AutonomyExecutionVerifier + ``execute_verified`` write core via
  ``execute_autonomous``, always settling the in-flight reservation;
* the v1 branch never touches the bundle store, and the v2 branch never touches
  the v1 approval verifier.

The routing decision is made only from the reloaded operation envelope
(``select_execution_path``), never from model output or a request-body field.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute import executor_entry
from operations.execute.executor_service import ExecutionInvocation

_OP = "op_" + "e" * 26
_ACTION = "act_" + "f" * 64

_V1_OPERATION = {
    "operation_id": _OP,
    "operation_contract_version": "1.0",
    "phase": "advise",
    "profile": "gamelift.capacity-adjustment/1.0",
    "capability": {"capability_version": "1.0"},
}
_V2_OPERATION = {
    "operation_id": _OP,
    "operation_contract_version": "2.0",
    "phase": "operate",
    "profile": "gamelift.capacity-adjustment/2.0",
    "capability": {"capability_version": "2.0"},
    "prepared_hash": "sha256:" + "a" * 64,
}


class _V1ReloadStore:
    """Returns a v1 (operation, approval, state) tuple, or None for v2."""

    def __init__(self, tuple_or_none: Any) -> None:
        self._value = tuple_or_none

    def load_for_execution(self, operation_id: str) -> Any:
        return self._value


class _BundleStore:
    def __init__(self, bundle: dict[str, Any] | None, *, dispatched: bool = True) -> None:
        self._bundle = bundle
        self._dispatched = dispatched
        self.loads: list[str] = []
        self.audit_loads: list[str] = []

    def load_bundle(self, operation_id: str) -> dict[str, Any] | None:
        self.loads.append(operation_id)
        return self._bundle

    def load_dispatch_audit(self, *, operation_id: str, phase: str) -> dict[str, Any] | None:
        # The evaluator records ``dispatch_requested`` before StartExecution and a
        # transaction-fenced ``dispatched`` after a confirmed start; the executor
        # requires BOTH matching records before verifying or writing.
        self.audit_loads.append(f"{phase}:{operation_id}")
        if phase in ("dispatch_requested", "dispatched") and self._dispatched:
            return {"operation_id": operation_id, "phase": phase, "execution_name": operation_id}
        return None


class _V1Service:
    def __init__(self) -> None:
        self.execute_calls: list[dict[str, Any]] = []
        self.execute_verified_calls: list[Any] = []

    def execute(self, invocation: Any, *, prepared_operation: Any, approval: Any, lease_holder: str) -> dict[str, Any]:
        self.execute_calls.append({"op": prepared_operation, "approval": approval})
        return {"outcome": "SUCCEEDED", "provider_write_issued": True}

    def execute_verified(self, invocation: Any, *, plan: Any, lease_holder: str) -> dict[str, Any]:
        self.execute_verified_calls.append(plan)
        return {"outcome": "SUCCEEDED", "provider_write_issued": True}


class _Verifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify(self, *, operation_id: str, evidence: Any) -> Any:
        self.calls.append(operation_id)

        class _Plan:
            logical_action_id = _ACTION
            intent = {"operation_id": operation_id}

        return _Plan()


class _Reservation:
    def __init__(self) -> None:
        self.settled: list[tuple[str, str, str]] = []

    def require(self, *, operation_id: str, logical_action_id: str) -> None:
        pass

    def settle(self, *, operation_id: str, logical_action_id: str, terminal: str) -> None:
        self.settled.append((operation_id, logical_action_id, terminal))


def _v2_bundle() -> dict[str, Any]:
    return {
        "policy": {},
        "observation": {},
        "decision": {},
        "operation": dict(_V2_OPERATION),
        "window_state": {},
        "reservation": {"operation_id": _OP, "logical_action_id": _ACTION},
    }


def _runtime(*, v1_reload: Any, bundle: dict[str, Any] | None, service: Any, verifier: Any, reservation: Any) -> Any:
    return executor_entry.AutonomyExecutorRuntime(
        service=service,
        reload_store=_V1ReloadStore(v1_reload),
        metrics=_NullMetrics(),
        bundle_store=_BundleStore(bundle),
        autonomy_verifier=verifier,
        reservation=reservation,
    )


class _NullMetrics:
    def record(self, *args: Any, **kwargs: Any) -> None:
        pass

    def put_latency_ms(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.mark.unit
def test_v1_operation_runs_human_approval_path_and_never_touches_bundle_store() -> None:
    service = _V1Service()
    verifier = _Verifier()
    bundle = _BundleStore(_v2_bundle())
    runtime = executor_entry.AutonomyExecutorRuntime(
        service=service,
        reload_store=_V1ReloadStore((_V1_OPERATION, {"state": "approved"}, "approved")),
        metrics=_NullMetrics(),
        bundle_store=bundle,
        autonomy_verifier=verifier,
        reservation=_Reservation(),
    )

    result = executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")

    assert result["outcome"] == "SUCCEEDED"
    assert len(service.execute_calls) == 1
    # v1 never runs the autonomous verifier or touches the bundle store.
    assert verifier.calls == []
    assert bundle.loads == []
    assert service.execute_verified_calls == []


@pytest.mark.unit
def test_runtime_keeps_v1_and_v2_services_separate() -> None:
    v1_service = _V1Service()
    autonomy_service = _V1Service()
    verifier = _Verifier()
    reservation = _Reservation()
    runtime = executor_entry.AutonomyExecutorRuntime(
        service=v1_service,
        autonomy_service=autonomy_service,
        reload_store=_V1ReloadStore((_V1_OPERATION, {"state": "approved"}, "approved")),
        metrics=_NullMetrics(),
        bundle_store=_BundleStore(_v2_bundle()),
        autonomy_verifier=verifier,
        reservation=reservation,
    )

    executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-v1")
    assert len(v1_service.execute_calls) == 1
    assert autonomy_service.execute_calls == []

    runtime.reload_store = _V1ReloadStore(None)
    executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-v2")
    assert len(autonomy_service.execute_verified_calls) == 1
    assert v1_service.execute_verified_calls == []


@pytest.mark.unit
def test_v2_operation_routes_through_autonomous_verifier_and_execute_verified() -> None:
    service = _V1Service()
    verifier = _Verifier()
    reservation = _Reservation()
    runtime = executor_entry.AutonomyExecutorRuntime(
        service=service,
        reload_store=_V1ReloadStore(None),  # no v1 approval for a v2 op
        metrics=_NullMetrics(),
        bundle_store=_BundleStore(_v2_bundle()),
        autonomy_verifier=verifier,
        reservation=reservation,
    )

    result = executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")

    assert result["outcome"] == "SUCCEEDED"
    # v2 ran the autonomous verifier + execute_verified, and never the v1 approval path.
    assert verifier.calls == [_OP]
    assert len(service.execute_verified_calls) == 1
    assert service.execute_calls == []
    # The in-flight reservation was settled on the terminal handoff, keyed on the
    # action id DERIVED from the reloaded (operation_id, prepared_hash) — never a
    # caller-supplied value.
    # Local modules
    from operations.contracts.execution import logical_action_id

    expected_action = logical_action_id(_OP, _V2_OPERATION["prepared_hash"])
    assert reservation.settled == [(_OP, expected_action, "succeeded")]


@pytest.mark.unit
def test_missing_everywhere_fails_closed() -> None:
    # Local modules
    from operations.execute.executor_service import ExecutorServiceError

    runtime = executor_entry.AutonomyExecutorRuntime(
        service=_V1Service(),
        reload_store=_V1ReloadStore(None),
        metrics=_NullMetrics(),
        bundle_store=_BundleStore(None),
        autonomy_verifier=_Verifier(),
        reservation=_Reservation(),
    )
    with pytest.raises(ExecutorServiceError):
        executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")


@pytest.mark.unit
def test_v1_unapproved_state_fails_closed() -> None:
    # Local modules
    from operations.execute.executor_service import ExecutorServiceError

    runtime = executor_entry.AutonomyExecutorRuntime(
        service=_V1Service(),
        reload_store=_V1ReloadStore((_V1_OPERATION, {"state": "rejected"}, "rejected")),
        metrics=_NullMetrics(),
        bundle_store=_BundleStore(None),
        autonomy_verifier=_Verifier(),
        reservation=_Reservation(),
    )
    with pytest.raises(ExecutorServiceError):
        executor_entry.execute_reloaded(runtime, ExecutionInvocation(operation_id=_OP), lease_holder="exec-1")
