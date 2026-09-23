"""Executor service behavior tests (#415, E3 execute).

The executor service is the behavioral core that ties the precondition verifier,
the normalized GameLift adapter, and the fenced execution store together. These
tests assert the exact safety behaviors the milestone requires:

* **Describe-before-write.** DescribeFleetCapacity must equal the hash-bound
  expected state before any write; if it already equals the target, record a
  reconciled success with NO Update.
* **Exactly-once write.** Otherwise issue UpdateFleetCapacity exactly once, then
  poll bounded post-action verification and record SUCCEEDED.
* **Idempotent replay.** A recorded terminal result short-circuits: no verify,
  no Describe, no write.
* **Drift fails closed.** If the observed current state differs from BOTH the
  expected state and the target, no write is issued and the result is FAILED
  (STATE_DRIFT).
* **Inconclusive → reconcile, never blind retry.** On a write timeout, the
  service Describes again; if the fleet now matches the target it records
  SUCCEEDED (the lost-response write landed); if it cannot conclusively confirm,
  it records HUMAN_RECONCILIATION_REQUIRED and never blind-retries.
* **Verification failure.** If the post-write Describe never reaches the target
  within the bounded polls, the result is FAILED (VERIFICATION_FAILED).
"""

from __future__ import annotations

# Standard library
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import capacity_prepared_hash, load_json
from operations.execute.execution_store import ExecutionCommitOutcome, LeaseAcquisition
from operations.execute.executor_service import ExecutionInvocation, ExecutorService
from operations.execute.gamelift_adapter import ProviderWriteInconclusive, ProviderWriteRejected
from operations.execution_verifier import ExecutionAuthorityContext, ExecutionVerifier
from operations.playbook_definition import EXECUTOR_BINDING_VERSION, EXECUTOR_ID, capacity_playbook_hash

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
_FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/" + _FLEET
_NOW = datetime(2026, 9, 21, 19, 12, 5, tzinfo=timezone.utc)


def _prepared() -> dict[str, Any]:
    prepared = load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")
    prepared["playbook"]["playbook_hash"] = capacity_playbook_hash()
    prepared["prepared_hash"] = capacity_prepared_hash(prepared)
    return prepared


def _approval(prepared: dict[str, Any]) -> dict[str, Any]:
    now = datetime(2026, 9, 21, 19, 12, 0, tzinfo=timezone.utc)
    return {
        "approval_contract_version": "1.0",
        "approval_id": "approval:11111111111111111111111111111111",
        "operation_id": prepared["operation_id"],
        "prepared_operation_hash": capacity_prepared_hash(prepared),
        "approver": {
            "subject_id": "subject.admin-1",
            "client_id": "client.web-console",
            "tenant_id": "tenant.default",
            "workspace_id": "workspace.default",
        },
        "decision": "granted",
        "policy_version": prepared["policy"]["policy_version"],
        "decided_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "correlation": {"correlation_id": prepared["correlation"]["correlation_id"], "request_id": "request.approve-1"},
    }


def _context() -> ExecutionAuthorityContext:
    return ExecutionAuthorityContext(
        deployment_mode="remediate",
        capability_maximum="remediate",
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expected_playbook_hash=capacity_playbook_hash(),
        expected_executor_id=EXECUTOR_ID,
        expected_executor_binding_version=EXECUTOR_BINDING_VERSION,
        enrolled_fleet_id=_FLEET,
        enrolled_fleet_arn=_ARN,
        enrolled_location="us-west-2",
    )


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
    def __init__(self, describe_sequence: list[Any], update_effect: Any = None) -> None:
        self._describe = list(describe_sequence)
        self.update_calls: list[dict[str, Any]] = []
        self.update_effect = update_effect

    def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]:
        effect = self._describe.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def update_capacity(self, **kwargs: Any) -> Any:
        self.update_calls.append(kwargs)
        if isinstance(self.update_effect, Exception):
            raise self.update_effect
        return self.update_effect


def _service(store: _FakeStore, adapter: _FakeAdapter) -> ExecutorService:
    verifier = ExecutionVerifier(context=_context(), clock=lambda: _NOW)
    return ExecutorService(verifier=verifier, adapter=adapter, store=store, clock=lambda: _NOW, max_verify_polls=3)


def _invocation() -> ExecutionInvocation:
    return ExecutionInvocation(operation_id="op_aaaaaaaaaaaaaaaaaaaaaaaaaa")


def _run(store: _FakeStore, adapter: _FakeAdapter) -> dict[str, Any]:
    prepared = _prepared()
    approval = _approval(prepared)
    svc = _service(store, adapter)
    return svc.execute(_invocation(), prepared_operation=prepared, approval=approval, lease_holder="exec-1")


# desired change: current desired=10 -> target desired=14
_CURRENT = {"desired": 10, "minimum": 2, "maximum": 20}
_TARGET = {"desired": 14, "minimum": 2, "maximum": 20}


def test_reconciled_when_already_at_target_no_write() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[_TARGET])  # already at target
    result = _run(store, adapter)
    assert result["outcome"] == "RECONCILED"
    assert result["provider_write_issued"] is False
    assert adapter.update_calls == []


def test_exactly_once_write_then_succeeded() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, _TARGET])  # pre-write, post-write verify
    result = _run(store, adapter)
    assert result["outcome"] == "SUCCEEDED"
    assert result["provider_write_issued"] is True
    assert len(adapter.update_calls) == 1
    assert store.commits and store.commits[0]["new_state"] == "succeeded"


def test_recorded_result_short_circuits() -> None:
    prior = {"outcome": "SUCCEEDED", "operation_id": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"}
    store = _FakeStore(recorded_result=prior)
    adapter = _FakeAdapter(describe_sequence=[])  # never called
    result = _run(store, adapter)
    assert result is prior or result["outcome"] == "SUCCEEDED"
    assert adapter.update_calls == []
    assert store.commits == []


def test_state_drift_fails_closed_without_write() -> None:
    store = _FakeStore()
    drift = {"desired": 7, "minimum": 2, "maximum": 20}  # neither expected nor target
    adapter = _FakeAdapter(describe_sequence=[drift])
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["failure_reason_code"] == "STATE_DRIFT"
    assert adapter.update_calls == []


def test_inconclusive_write_then_describe_confirms_success() -> None:
    store = _FakeStore()
    # pre-write shows expected; write times out; post-timeout Describe shows target.
    adapter = _FakeAdapter(
        describe_sequence=[_CURRENT, _TARGET],
        update_effect=ProviderWriteInconclusive(),
    )
    result = _run(store, adapter)
    assert result["outcome"] == "SUCCEEDED"
    assert result["provider_write_issued"] is True
    assert len(adapter.update_calls) == 1  # never blind-retried


def test_inconclusive_write_unconfirmed_requires_human_reconciliation() -> None:
    store = _FakeStore()
    # pre-write expected; write times out; post-timeout Describe still shows the
    # pre-write value, so the outcome cannot be conclusively confirmed.
    adapter = _FakeAdapter(
        describe_sequence=[_CURRENT, _CURRENT, _CURRENT, _CURRENT],
        update_effect=ProviderWriteInconclusive(),
    )
    result = _run(store, adapter)
    assert result["outcome"] == "HUMAN_RECONCILIATION_REQUIRED"
    assert result["failure_reason_code"] == "RESULT_INCONCLUSIVE"
    assert len(adapter.update_calls) == 1  # exactly one attempt, no blind retry


def test_provider_rejection_fails_without_state_change() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(
        describe_sequence=[_CURRENT, _CURRENT],  # pre-write expected; post shows unchanged
        update_effect=ProviderWriteRejected("PROVIDER_ERROR"),
    )
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["failure_reason_code"] in {"PROVIDER_ERROR", "VERIFICATION_FAILED"}
    assert len(adapter.update_calls) == 1


def test_verification_failure_when_target_never_reached() -> None:
    store = _FakeStore()
    # write succeeds but every post-write poll shows the fleet still at current.
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, _CURRENT, _CURRENT, _CURRENT])
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["failure_reason_code"] == "VERIFICATION_FAILED"
    assert len(adapter.update_calls) == 1


def test_successful_write_receipt_is_committed_with_the_terminal_result() -> None:
    """The E3 store receives the exact bounded service request id, not a raw response."""
    store = _FakeStore()
    adapter = _FakeAdapter(
        describe_sequence=[_CURRENT, _TARGET],
        update_effect="request-4f9c2b37-1a4c-4e8e-9d2c-2f6c76c6b470",
    )

    result = _run(store, adapter)

    assert result["outcome"] == "SUCCEEDED"
    assert store.commits[0]["provider_request_id"] == "request-4f9c2b37-1a4c-4e8e-9d2c-2f6c76c6b470"
