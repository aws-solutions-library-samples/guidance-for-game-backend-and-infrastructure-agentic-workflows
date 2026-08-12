#!/usr/bin/env python3
"""Property-based test for expired-approval rejection (SECURITY GATE — non-optional).

Feature: source-control-connector-executor, Property 17 (design → Correctness Properties).
An approval whose expiry has passed at execution time must be rejected by the executor with no
``create_branch`` / ``commit_files`` / ``open_change_proposal`` provider write (Requirement
14.4). Conversely, an approval still within its validity window at execution time proceeds to a
provider write. The generator crosses a validity window (``ttl_seconds``) against an execution
offset (``exec_offset_seconds``) so approvals with expiries both before and after execution
time are exercised.

The store record is built directly through the in-memory #279 store and the default #277
contract adapter (so hash/version gates pass) and the reader+writer ``FakeProvider`` records
whether any mutation occurred. The single invariant asserted is the biconditional: a provider
write happens **iff** the approval has not expired at execution time.

Validates: Requirements 5.5, 14.4
"""

# Standard library
from dataclasses import replace
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION, DefaultOperationContracts277
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.handler import Executor, ExecutorDependencies
from connector.executor.models import (
    ApprovalRecord,
    ApproverIdentity,
    EffectiveAuthority,
    ExecutorEvent,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
)
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import DEFAULT_HEAD_SHA, FakeProvider

pytestmark = pytest.mark.unit

_WORKFLOW_ROLE = "arn:aws:iam::123456789012:role/ScmWorkflowRole"
_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"
_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _policy() -> AuthorizationPolicy:
    return AuthorizationPolicy(
        entries=(
            AllowlistEntry(
                repo="org/iac",
                target_branches=("main",),
                path_prefixes=("infra/",),
                extensions=(".yaml",),
            ),
        )
    )


def _layers() -> tuple[PolicyLayer, ...]:
    return (PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),)


def _acquirer(secret_arn: str, *, source: str) -> str:
    return "write-token"


def _deps(provider: FakeProvider, store: InMemoryOperationStore, *, exec_now: datetime) -> ExecutorDependencies:
    return ExecutorDependencies(
        store=store,
        contracts=DefaultOperationContracts277(),
        provider=provider,
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        workflow_role_arn=_WORKFLOW_ROLE,
        write_secret_arn=_WRITE_SECRET,
        policy_layers=_layers(),
        credential_acquirer=_acquirer,
        clock=lambda: exec_now,
    )


def _stored_operation() -> PreparedOperation:
    contracts = DefaultOperationContracts277()
    base = PreparedOperation(
        operation_id="op-1",
        canonical_hash="",
        operation_contract_version=DEFAULT_CONTRACT_VERSION,
        files=(ProposedFile(path="infra/main.yaml", content="body", iac_format="cloudformation"),),
        target_repo="org/iac",
        target_branch="main",
        base_revision=DEFAULT_HEAD_SHA,
        effective_authority=EffectiveAuthority(decision="authorized", inputs=(), risk_ceiling=RiskLevel.HIGH),
        risk=RiskLevel.LOW,
        requester_identity=RequesterIdentity(subject="user-1", groups=("infra",)),
        duplicate_content_key="dup",
        created_at=_T0.isoformat(),
    )
    return replace(base, canonical_hash=contracts.canonical_hash(base))


# Feature: source-control-connector-executor, Property 17: An expired approval never executes
@settings(max_examples=100)
@given(
    ttl_seconds=st.integers(min_value=1, max_value=3600),
    exec_offset_seconds=st.integers(min_value=0, max_value=7200),
)
def test_property17_expired_approval_never_executes(ttl_seconds: int, exec_offset_seconds: int) -> None:
    """A provider write happens iff the approval has NOT expired at execution time; an expired
    approval is rejected with no provider write (Req 5.5, 14.4)."""
    operation = _stored_operation()
    store = InMemoryOperationStore()
    store.insert_operation(operation)

    # Approved at T0 with the given TTL; execution happens exec_offset seconds after T0.
    expires_at = _T0 + timedelta(seconds=ttl_seconds)
    store.apply_approval_transition(
        operation.operation_id,
        ApprovalRecord(
            operation_id=operation.operation_id,
            approver_identity=ApproverIdentity(subject="approver-1", groups=("infra",)),
            bound_canonical_hash=operation.canonical_hash,
            approved_at=_T0.isoformat(),
            expires_at=expires_at.isoformat(),
            separation_of_duties_ok=True,
        ),
    )

    exec_now = _T0 + timedelta(seconds=exec_offset_seconds)
    expired = exec_now >= expires_at

    provider = FakeProvider()
    outcome = Executor(_deps(provider, store, exec_now=exec_now)).handle(
        ExecutorEvent(operation_id=operation.operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    assert wrote == (not expired)

    if expired:
        assert outcome.status == "rejected"
        assert outcome.reason == "approval_expired"
        assert not provider.created_branches
        assert not provider.commits
        assert not provider.pull_requests
    else:
        assert outcome.status == "executed"
        assert len(provider.pull_requests) == 1
