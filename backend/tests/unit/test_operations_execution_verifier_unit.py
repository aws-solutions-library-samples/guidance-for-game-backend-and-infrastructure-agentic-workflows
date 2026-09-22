"""Precondition re-verification tests for the E3 execution verifier (#415).

The executor never trusts the dispatcher, the workflow, or the intent it is
handed. Before any provider write it independently reloads and re-verifies EVERY
server-owned precondition against the stored prepared operation, the stored
granted approval, and the code-owned deployment context. These tests assert each
precondition is a discriminating gate: flipping exactly one input to an invalid
value fails closed with a bounded, public-safe reason code and never proceeds.
"""

from __future__ import annotations

# Standard library
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import build_execution_intent, capacity_prepared_hash, load_json
from operations.execution_verifier import (
    ExecutionAuthorityContext,
    ExecutionVerificationError,
    ExecutionVerificationReason,
    ExecutionVerifier,
    VerifiedExecutionPlan,
)
from operations.playbook_definition import (
    EXECUTOR_BINDING_VERSION,
    EXECUTOR_ID,
    capacity_playbook_hash,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"

_FLEET_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"


def _prepared() -> dict[str, Any]:
    prepared = load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")
    # Bind the operation to the real, code-owned playbook hash the deployment
    # verifies against, then re-seal prepared_hash so the document is coherent.
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
        "correlation": {
            "correlation_id": prepared["correlation"]["correlation_id"],
            "request_id": "request.approve-1",
        },
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
        enrolled_fleet_id="fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789",
        enrolled_fleet_arn=_FLEET_ARN,
        enrolled_location="us-west-2",
    )


def _clock(t: datetime):
    def _c() -> datetime:
        return t

    return _c


_NOW = datetime(2026, 9, 21, 19, 12, 5, tzinfo=timezone.utc)


def _verifier() -> ExecutionVerifier:
    return ExecutionVerifier(context=_context(), clock=_clock(_NOW))


def test_valid_operation_and_approval_verify() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    plan = _verifier().verify(prepared_operation=prepared, approval=approval)
    assert isinstance(plan, VerifiedExecutionPlan)
    # The verified plan carries the deterministic intent and fleet ARN binding.
    assert plan.intent == build_execution_intent(prepared)
    assert plan.fleet_arn == _FLEET_ARN
    assert plan.logical_action_id == plan.intent["logical_action_id"]
    # Hard server-owned bounds: at most one logical update.
    assert plan.max_writes == 1


def _expect_reason(prepared: dict[str, Any], approval: dict[str, Any], reason: ExecutionVerificationReason) -> None:
    with pytest.raises(ExecutionVerificationError) as exc:
        _verifier().verify(prepared_operation=prepared, approval=approval)
    assert exc.value.reason is reason
    # The safe message never leaks the raw values.
    assert _FLEET_ARN not in str(exc.value)


def test_prepared_hash_tamper_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    prepared = deepcopy(prepared)
    prepared["parameters"]["requested"]["desired"] += 1  # breaks change + prepared_hash
    _expect_reason(prepared, approval, ExecutionVerificationReason.OPERATION_INTEGRITY_INVALID)


def test_approval_hash_mismatch_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    approval = deepcopy(approval)
    approval["prepared_operation_hash"] = "sha256:" + "0" * 64
    _expect_reason(prepared, approval, ExecutionVerificationReason.APPROVAL_NOT_BOUND)


def test_expired_approval_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    approval = deepcopy(approval)
    approval["expires_at"] = "2026-09-21T19:12:04Z"  # before _NOW
    _expect_reason(prepared, approval, ExecutionVerificationReason.APPROVAL_EXPIRED)


def test_non_granted_approval_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    approval = deepcopy(approval)
    approval["decision"] = "denied"
    _expect_reason(prepared, approval, ExecutionVerificationReason.APPROVAL_NOT_BOUND)


def test_expired_operation_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    late = ExecutionVerifier(context=_context(), clock=_clock(datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)))
    with pytest.raises(ExecutionVerificationError) as exc:
        late.verify(prepared_operation=prepared, approval=approval)
    assert exc.value.reason is ExecutionVerificationReason.OPERATION_EXPIRED


def test_policy_version_mismatch_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    approval = deepcopy(approval)
    approval["policy_version"] = "999"
    _expect_reason(prepared, approval, ExecutionVerificationReason.APPROVAL_NOT_BOUND)


def test_playbook_hash_mismatch_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    ctx = _context()
    tampered = replace(ctx, expected_playbook_hash="sha256:" + "0" * 64)
    with pytest.raises(ExecutionVerificationError) as exc:
        ExecutionVerifier(context=tampered, clock=_clock(_NOW)).verify(prepared_operation=prepared, approval=approval)
    assert exc.value.reason is ExecutionVerificationReason.PLAYBOOK_HASH_MISMATCH


def test_executor_binding_mismatch_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    ctx = _context()
    other = replace(ctx, expected_executor_id="executor.other")
    with pytest.raises(ExecutionVerificationError) as exc:
        ExecutionVerifier(context=other, clock=_clock(_NOW)).verify(prepared_operation=prepared, approval=approval)
    assert exc.value.reason is ExecutionVerificationReason.EXECUTOR_BINDING_MISMATCH


def test_insufficient_deployment_authority_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    ctx = _context()
    advise_only = replace(ctx, deployment_mode="advise")
    with pytest.raises(ExecutionVerificationError) as exc:
        ExecutionVerifier(context=advise_only, clock=_clock(_NOW)).verify(
            prepared_operation=prepared, approval=approval
        )
    assert exc.value.reason is ExecutionVerificationReason.INSUFFICIENT_EXECUTION_AUTHORITY


def test_tenant_mismatch_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    ctx = _context()
    other = replace(ctx, tenant_id="tenant.other")
    with pytest.raises(ExecutionVerificationError) as exc:
        ExecutionVerifier(context=other, clock=_clock(_NOW)).verify(prepared_operation=prepared, approval=approval)
    assert exc.value.reason is ExecutionVerificationReason.TENANT_WORKSPACE_MISMATCH


def test_fleet_binding_mismatch_is_rejected() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    ctx = _context()
    other = replace(ctx, enrolled_fleet_id="fleet-deadbeef")
    with pytest.raises(ExecutionVerificationError) as exc:
        ExecutionVerifier(context=other, clock=_clock(_NOW)).verify(prepared_operation=prepared, approval=approval)
    assert exc.value.reason is ExecutionVerificationReason.FLEET_BINDING_MISMATCH


def test_required_execution_authority_must_be_remediate() -> None:
    prepared = _prepared()
    approval = _approval(prepared)
    prepared = deepcopy(prepared)
    prepared["required_execution_authority"] = "advise"  # also breaks prepared_hash
    # Integrity check catches this first (hash no longer binds), fail closed.
    with pytest.raises(ExecutionVerificationError):
        _verifier().verify(prepared_operation=prepared, approval=approval)
