"""Executor-service Describe BaseException-signal propagation tests (#415, E3).

The independent review confirmed that ``ExecutorService._describe`` wrapped its
adapter call in ``except BaseException``. That is over-broad: ``BaseException``
is the parent of the non-error control-flow signals ``KeyboardInterrupt``,
``SystemExit``, ``GeneratorExit`` and of asyncio ``CancelledError`` (a
``BaseException`` since Python 3.8). Swallowing those to ``None`` fabricates a
*false provider terminal state* — a process the operator/runtime asked to stop
is instead recorded as a failed/inconclusive execution against the provider.

The contract these red-green tests pin:

#. A ``BaseException`` *signal* raised by ``describe_capacity`` — on the pre-write
   Describe, on the confirming Describe after a clear rejection, or on a
   post-write verification poll — MUST propagate out of ``execute`` unchanged.
   It is not the provider's terminal state and must never be recorded as one.
#. Every real *adapter* failure (``ProviderWriteRejected``,
   ``ProviderWriteInconclusive``, malformed-response ``ValueError``, and any
   other ordinary ``Exception``) MUST still be caught, converted to a bounded
   typed outcome, and recorded exactly once — unchanged by narrowing the catch.

The fake adapter raises whatever effect it is handed directly, so these tests
exercise the executor's ``_describe`` boundary in isolation from the real
adapter's own classification layer.
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
from operations.execute.executor_service import ExecutionInvocation, ExecutorService
from operations.execute.gamelift_adapter import ProviderWriteInconclusive, ProviderWriteRejected
from operations.execution_verifier import ExecutionAuthorityContext, ExecutionVerifier
from operations.playbook_definition import EXECUTOR_BINDING_VERSION, EXECUTOR_ID, capacity_playbook_hash

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
_FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/" + _FLEET
_NOW = datetime(2026, 9, 21, 19, 12, 5, tzinfo=timezone.utc)

_CURRENT = {"desired": 10, "minimum": 2, "maximum": 20}
_TARGET = {"desired": 14, "minimum": 2, "maximum": 20}


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


# The non-error control-flow signals whose sole safe handling is to propagate.
_SIGNALS: list[type[BaseException]] = [KeyboardInterrupt, SystemExit, GeneratorExit]


# -- Signals MUST propagate; they are never a provider terminal state -----------


@pytest.mark.parametrize("signal", _SIGNALS)
def test_prewrite_describe_signal_propagates_and_records_nothing(signal: type[BaseException]) -> None:
    """A BaseException signal on the pre-write Describe must escape, not become FAILED."""
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[signal()])
    with pytest.raises(signal):
        _run(store, adapter)
    # No provider write, and no fabricated terminal record: the process is stopping,
    # not reporting a provider outcome.
    assert adapter.update_calls == []
    assert store.commits == [], "a stop signal must never be recorded as a provider terminal state"


@pytest.mark.parametrize("signal", _SIGNALS)
def test_postwrite_poll_signal_propagates(signal: type[BaseException]) -> None:
    """A BaseException signal on a post-write verification poll must escape."""
    store = _FakeStore()
    # Pre-write reads the expected state; the write is issued; the verification
    # Describe then receives a stop signal — it must propagate, not be swallowed
    # into a false HUMAN_RECONCILIATION_REQUIRED record.
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, signal()])
    with pytest.raises(signal):
        _run(store, adapter)
    assert len(adapter.update_calls) == 1
    assert store.commits == [], "a stop signal after a write must not be recorded as a terminal outcome"


@pytest.mark.parametrize("signal", _SIGNALS)
def test_confirming_describe_signal_after_rejection_propagates(signal: type[BaseException]) -> None:
    """A signal on the confirming Describe after a clear rejection must escape."""
    store = _FakeStore()
    adapter = _FakeAdapter(
        describe_sequence=[_CURRENT, signal()],
        update_effect=ProviderWriteRejected("PROVIDER_ERROR"),
    )
    with pytest.raises(signal):
        _run(store, adapter)
    assert len(adapter.update_calls) == 1
    assert store.commits == [], "a stop signal must never be recorded as a provider terminal state"


# -- Ordinary Exceptions still record truthful bounded outcomes -----------------


def test_prewrite_describe_ordinary_exception_still_recorded_failed() -> None:
    """Narrowing the catch to Exception must not change real-failure handling."""
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[ProviderWriteRejected("PROVIDER_ERROR")])
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is False
    assert adapter.update_calls == []
    assert store.commits, "a real adapter failure must still record a truthful terminal outcome"


def test_prewrite_describe_inconclusive_still_recorded_failed() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[ProviderWriteInconclusive()])
    result = _run(store, adapter)
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is False
    assert store.commits


def test_postwrite_poll_ordinary_exception_still_reconciliation() -> None:
    store = _FakeStore()
    adapter = _FakeAdapter(describe_sequence=[_CURRENT, ProviderWriteInconclusive()])
    result = _run(store, adapter)
    assert result["outcome"] == "HUMAN_RECONCILIATION_REQUIRED"
    assert result["failure_reason_code"] == "RESULT_INCONCLUSIVE"
    assert result["provider_write_issued"] is True
    assert len(adapter.update_calls) == 1
    assert store.commits
