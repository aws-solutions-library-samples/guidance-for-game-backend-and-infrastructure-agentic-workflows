"""Executor-service additive v2 wiring tests (#439, E5).

The E5 autonomous path reuses the existing ExecutorService write core — the sole
GameLift writer — additively, without changing v1 behavior:

* **Immediate pre-write hook.** An optional ``pre_write_hook`` runs AFTER the
  existing E4 second kill-switch/durable check and IMMEDIATELY BEFORE
  UpdateFleetCapacity. When it raises, the write is refused, no
  UpdateFleetCapacity is issued, and a bounded FAILED is recorded. When absent
  (v1 default), behavior is byte-for-byte unchanged.
* **execute_verified.** A pre-verified :class:`VerifiedExecutionPlan` (produced by
  the AutonomyExecutionVerifier) is fed straight into the same write core,
  bypassing the v1 approval verifier but running the identical
  Describe-before-write / write-once / verify / atomic-record pipeline.

These tests use the same fakes as the v1 executor-service suite and assert the
hook fires at exactly the right point and fails closed.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.execution_store import ExecutionCommitOutcome, LeaseAcquisition
from operations.execute.executor_service import ExecutionInvocation, ExecutorService
from operations.execution_verifier import VerifiedExecutionPlan

_NOW = datetime(2026, 9, 21, 19, 12, 5, tzinfo=timezone.utc)
_FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/" + _FLEET
_OP = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_ACTION = "act_" + "a" * 64
_CURRENT = {"desired": 10, "minimum": 2, "maximum": 20}
_TARGET = {"desired": 14, "minimum": 2, "maximum": 20}


class _FakeStore:
    def __init__(self, recorded_result: dict[str, Any] | None = None) -> None:
        self.recorded_result = recorded_result
        self.commits: list[dict[str, Any]] = []
        self.commit_outcome = ExecutionCommitOutcome.RECORDED

    def acquire_execution_lease(self, **kwargs: Any) -> LeaseAcquisition:
        return LeaseAcquisition(generation=1, recorded_result=self.recorded_result)

    def record_execution_result(self, **kwargs: Any) -> ExecutionCommitOutcome:
        self.commits.append(kwargs)
        return self.commit_outcome


class _FakeAdapter:
    def __init__(self, describe_sequence: list[Any]) -> None:
        self._describe = list(describe_sequence)
        self.update_calls: list[dict[str, Any]] = []

    def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]:
        return self._describe.pop(0)

    def update_capacity(self, **kwargs: Any) -> None:
        self.update_calls.append(kwargs)


def _plan() -> VerifiedExecutionPlan:
    intent = {
        "execution_contract_version": "1.0",
        "operation_id": _OP,
        "prepared_hash": "sha256:" + "a" * 64,
        "logical_action_id": _ACTION,
        "provider": "gamelift",
        "action": "update-fleet-capacity",
        "target": {"provider": "gamelift", "fleet_id": _FLEET, "location": "us-west-2"},
        "parameters": dict(_TARGET),
        "expected_current_capacity": dict(_CURRENT),
    }
    return VerifiedExecutionPlan(
        intent=intent,
        logical_action_id=_ACTION,
        fleet_arn=_ARN,
        fleet_id=_FLEET,
        location="us-west-2",
        max_writes=1,
    )


def _service(store: _FakeStore, adapter: _FakeAdapter, *, pre_write_hook: Any = None) -> ExecutorService:
    # No verifier is exercised on the execute_verified path; a sentinel object is
    # sufficient since the plan is supplied directly.
    return ExecutorService(
        verifier=object(),
        adapter=adapter,
        store=store,
        clock=lambda: _NOW,
        max_verify_polls=3,
        pre_write_hook=pre_write_hook,
    )


@pytest.mark.unit
def test_execute_verified_writes_once_and_succeeds() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, _TARGET])
    svc = _service(store, adapter)
    result = svc.execute_verified(ExecutionInvocation(operation_id=_OP), plan=_plan(), lease_holder="exec-1")
    assert result["outcome"] == "SUCCEEDED"
    assert len(adapter.update_calls) == 1


@pytest.mark.unit
def test_pre_write_hook_runs_immediately_before_write_and_blocks_on_failure() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[_CURRENT])  # pre-write describe only; no write expected
    calls: list[str] = []

    def hook(plan: VerifiedExecutionPlan) -> None:
        calls.append(plan.logical_action_id)
        raise RuntimeError("autonomy switch or reservation lost")

    svc = _service(store, adapter, pre_write_hook=hook)
    result = svc.execute_verified(ExecutionInvocation(operation_id=_OP), plan=_plan(), lease_holder="exec-1")
    # The hook fired with the plan, and blocked the write: no UpdateFleetCapacity.
    assert calls == [_ACTION]
    assert adapter.update_calls == []
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is False


@pytest.mark.unit
def test_pre_write_hook_passes_allows_write() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, _TARGET])
    fired: list[str] = []

    def hook(plan: VerifiedExecutionPlan) -> None:
        fired.append(plan.logical_action_id)

    svc = _service(store, adapter, pre_write_hook=hook)
    result = svc.execute_verified(ExecutionInvocation(operation_id=_OP), plan=_plan(), lease_holder="exec-1")
    assert fired == [_ACTION]
    assert result["outcome"] == "SUCCEEDED"
    assert len(adapter.update_calls) == 1


@pytest.mark.unit
def test_execute_verified_rejects_operation_identity_mismatch() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[])
    svc = _service(store, adapter)
    # Local modules
    from operations.execute.executor_service import ExecutorServiceError

    with pytest.raises(ExecutorServiceError):
        svc.execute_verified(
            ExecutionInvocation(operation_id="op_bbbbbbbbbbbbbbbbbbbbbbbbbb"),
            plan=_plan(),
            lease_holder="exec-1",
        )
    assert adapter.update_calls == []
