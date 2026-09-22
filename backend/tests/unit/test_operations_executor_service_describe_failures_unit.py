"""Executor-service describe/verification failure containment tests (#415, E3).

The independent review flagged that the executor invokes the adapter's
``describe_capacity`` in three places *after* the fenced lease is acquired — the
pre-write Describe, the confirming Describe after a clear provider rejection, and
the bounded post-write verification poll — and that each of those calls can raise
``ProviderWriteInconclusive``, ``ProviderWriteRejected``, or a plain ``ValueError``
(malformed/unreadable provider response). If any of those escape, a lease is held
with no terminal record written: an *unexplained execution*, and raw provider
text can escape.

These red-green tests pin the contract: every adapter describe/update failure
class is caught, converted to a bounded typed outcome, and a fenced terminal
result is recorded. No raw provider text ever escapes, and no execution is left
without a recorded outcome.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import capacity_prepared_hash, load_json
from operations.execute.execution_store import ExecutionCommitOutcome, LeaseAcquisition
from operations.execute.executor_service import ExecutionInvocation, ExecutorService, ExecutorServiceError
from operations.execute.gamelift_adapter import ProviderWriteInconclusive, ProviderWriteRejected
from operations.execution_verifier import ExecutionAuthorityContext, ExecutionVerifier
from operations.playbook_definition import EXECUTOR_BINDING_VERSION, EXECUTOR_ID, capacity_playbook_hash

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
_FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/" + _FLEET
_NOW = datetime(2026, 9, 21, 19, 12, 5, tzinfo=timezone.utc)

_CURRENT = {"desired": 10, "minimum": 2, "maximum": 20}
_TARGET = {"desired": 14, "minimum": 2, "maximum": 20}

# A deliberately hostile provider message that must never reach a recorded result.
_LEAKY = "boto3 ClientError: fleet arn:aws:gamelift:us-west-2:9999:fleet/secret denied for principal xyz"


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
    def __init__(self) -> None:
        self.commits: list[dict[str, Any]] = []
        self.commit_outcome = ExecutionCommitOutcome.RECORDED

    def acquire_execution_lease(self, **kwargs: Any) -> LeaseAcquisition:
        return LeaseAcquisition(generation=1, recorded_result=None)

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
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def update_capacity(self, **kwargs: Any) -> None:
        self.update_calls.append(kwargs)
        if isinstance(self.update_effect, BaseException):
            raise self.update_effect


def _service(store: _FakeStore, adapter: _FakeAdapter) -> ExecutorService:
    verifier = ExecutionVerifier(context=_context(), clock=lambda: _NOW)
    return ExecutorService(verifier=verifier, adapter=adapter, store=store, clock=lambda: _NOW, max_verify_polls=3)


def _run(store: _FakeStore, adapter: _FakeAdapter) -> dict[str, Any]:
    prepared = _prepared()
    approval = _approval(prepared)
    svc = _service(store, adapter)
    return svc.execute(
        ExecutionInvocation(operation_id="op_aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        prepared_operation=prepared,
        approval=approval,
        lease_holder="exec-1",
    )


def _no_leak(result: dict[str, Any]) -> None:
    blob = repr(result)
    assert "arn:aws:" not in blob
    assert "boto3" not in blob
    assert "denied for principal" not in blob


# -- Pre-write Describe failures (no write issued yet) --------------------------


def test_prewrite_describe_rejection_records_failure_no_write() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[ProviderWriteRejected("PROVIDER_ERROR")])
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is False
    assert result["failure_reason_code"] in {"PROVIDER_ERROR", "VERIFICATION_FAILED"}
    assert adapter.update_calls == []
    assert store.commits, "a terminal result must be recorded — no unexplained execution"
    _no_leak(result)


def test_prewrite_describe_inconclusive_records_failure_no_write() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[ProviderWriteInconclusive()])
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is False
    assert adapter.update_calls == []
    assert store.commits, "a terminal result must be recorded — no unexplained execution"
    _no_leak(result)


def test_prewrite_describe_malformed_value_error_records_failure_no_write() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[ValueError(_LEAKY)])
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is False
    assert adapter.update_calls == []
    assert store.commits
    _no_leak(result)


# -- Post-write verification-poll failures (a write WAS issued) -----------------


def test_postwrite_poll_inconclusive_requires_human_reconciliation() -> None:
    store = _FakeStore()
    # pre-write shows expected; write succeeds; the verification Describe then
    # times out — the write's effect is unconfirmable → reconciliation required.
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, ProviderWriteInconclusive()])
    result = _run(store, adapter)
    assert result["outcome"] == "HUMAN_RECONCILIATION_REQUIRED"
    assert result["failure_reason_code"] == "RESULT_INCONCLUSIVE"
    assert result["provider_write_issued"] is True
    assert len(adapter.update_calls) == 1  # never blind-retried
    assert store.commits
    _no_leak(result)


def test_postwrite_poll_rejection_requires_human_reconciliation() -> None:
    store = _FakeStore()
    # A write was issued; the verification Describe then fails deterministically.
    # We cannot confirm whether the write landed → reconciliation, not silent pass.
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, ProviderWriteRejected("PROVIDER_ERROR")])
    result = _run(store, adapter)
    assert result["outcome"] == "HUMAN_RECONCILIATION_REQUIRED"
    assert result["failure_reason_code"] == "RESULT_INCONCLUSIVE"
    assert result["provider_write_issued"] is True
    assert len(adapter.update_calls) == 1
    assert store.commits
    _no_leak(result)


# -- Confirming Describe after a clear rejection --------------------------------


def test_confirming_describe_failure_after_rejection_records_failure() -> None:
    store = _FakeStore()
    # pre-write expected; the update is rejected (did not land); the confirming
    # Describe then itself fails. The write was a clear rejection so the world is
    # unchanged → a bounded FAILED, no escape, no unexplained execution.
    adapter = _FakeAdapter(
        describe_sequence=[_CURRENT, ValueError(_LEAKY)],
        update_effect=ProviderWriteRejected("PROVIDER_ERROR"),
    )
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is True
    assert result["failure_reason_code"] in {"PROVIDER_ERROR", "RESULT_INCONCLUSIVE"}
    assert len(adapter.update_calls) == 1
    assert store.commits
    _no_leak(result)


def test_no_raw_provider_exception_escapes_execute() -> None:
    """Any adapter failure class must convert to a recorded outcome, never propagate."""
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[RuntimeError(_LEAKY)])
    # RuntimeError from describe must not escape as a raw provider error.
    result = _run(store, adapter)
    assert result["outcome"] in {"FAILED", "HUMAN_RECONCILIATION_REQUIRED"}
    assert store.commits
    _no_leak(result)
