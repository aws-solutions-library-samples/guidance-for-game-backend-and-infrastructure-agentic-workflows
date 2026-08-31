#!/usr/bin/env python3
"""Property-based test for approval-gated write.

Feature: source-control-connector-executor, Property 28 (design → Correctness Properties).
The executor creates a branch and an unmerged PR **iff** a bound Approval_Record exists for the
operation; without one it performs no provider write (Req 11.4). The generator crosses an
``approved`` flag: the operation is always prepared (and stored), but only approved when the
flag is set. The invariant asserted is the biconditional between "an approval exists" and "a
provider write occurred" — an unapproved operation is rejected at the load-and-verify gate with
``approval_absent`` and writes nothing.

Validates: Requirements 11.4
"""

# Standard library
import json

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import (
    DefaultIdentityContract278,
    DefaultOperationContracts277,
)
from connector.executor.approval import ApprovalService
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.handler import Executor, ExecutorDependencies
from connector.executor.models import (
    DraftedChange,
    ExecutorEvent,
    RequesterIdentity,
    RiskLevel,
    TargetSelector,
)
from connector.executor.preparation import PreparationService
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_WORKFLOW_ROLE = "arn:aws:iam::123456789012:role/ScmWorkflowRole"
_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"


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


def _prep_service(provider: FakeProvider, store: InMemoryOperationStore) -> PreparationService:
    return PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        policy_layers=_layers(),
    )


def _deps(provider: FakeProvider, store: InMemoryOperationStore) -> ExecutorDependencies:
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
    )


def _prepare(provider: FakeProvider, store: InMemoryOperationStore, name: str) -> str:
    draft = DraftedChange(
        files=(
            ProposedFile(
                path=f"infra/{name}.yaml",
                content=json.dumps({"Resources": {"Res0": {"Type": "AWS::S3::Bucket"}}}),
                iac_format="cloudformation",
            ),
        ),
        iac_format="cloudformation",
        target=TargetSelector(repository="org/iac", branch="main"),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    result = _prep_service(provider, store).prepare(
        draft, requester=RequesterIdentity(subject="user-1", groups=("infra",))
    )
    assert result.status == "prepared"
    return str(result.operation_id)


# Feature: source-control-connector-executor, Property 28: A branch and PR are created only after an approval exists
@settings(max_examples=100)
@given(approved=st.booleans(), name=st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True))
def test_property28_write_only_after_approval(approved: bool, name: str) -> None:
    """A branch and unmerged PR are created iff a bound approval exists; an unapproved operation
    rejects with ``approval_absent`` and no provider write (Req 11.4)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare(provider, store, name)

    if approved:
        ApprovalService(store=store, identity=DefaultIdentityContract278()).approve(
            operation_id,
            approval_ctx={"subject": "approver-1", "groups": ["infra"]},
            source="web",
        )

    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    assert wrote == approved

    if approved:
        assert outcome.status == "executed"
        assert len(provider.created_branches) == 1
        assert len(provider.pull_requests) == 1
    else:
        assert outcome.status == "rejected"
        assert outcome.reason == "approval_absent"
        assert not provider.created_branches
        assert not provider.pull_requests
