#!/usr/bin/env python3
"""Property-based test for base-revision re-verification.

Feature: source-control-connector-executor, Property 16 (design → Correctness Properties).
Immediately before writing, the executor re-reads the provider head for the operation's target
and performs the write **iff** the head still matches the stored ``base_revision``; a moved /
stale head rejects with ``stale_base_revision`` and no provider write (Req 5.4). This reuses the
baseline stale-head check via the reader+writer ``FakeProvider``.

The generator crosses a ``stale`` flag: when set, the provider's target-branch head is advanced
(to a fresh, distinct SHA) after preparation captured the base revision, modeling a push that
landed between drafting and execution. The invariant asserted is the biconditional — a write
happens **iff** the head was not advanced.

Validates: Requirements 5.4
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
_REPO = "org/iac"
_BRANCH = "main"


def _policy() -> AuthorizationPolicy:
    return AuthorizationPolicy(
        entries=(
            AllowlistEntry(
                repo=_REPO,
                target_branches=(_BRANCH,),
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


def _prepare_and_approve(provider: FakeProvider, store: InMemoryOperationStore, name: str) -> str:
    draft = DraftedChange(
        files=(
            ProposedFile(
                path=f"infra/{name}.yaml",
                content=json.dumps({"Resources": {"Res0": {"Type": "AWS::S3::Bucket"}}}),
                iac_format="cloudformation",
            ),
        ),
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    result = _prep_service(provider, store).prepare(
        draft, requester=RequesterIdentity(subject="user-1", groups=("infra",))
    )
    assert result.status == "prepared"
    ApprovalService(store=store, identity=DefaultIdentityContract278()).approve(
        result.operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source="web",
    )
    return str(result.operation_id)


# Feature: source-control-connector-executor, Property 16: The base revision is re-verified before writing and a stale revision rejects
@settings(max_examples=100)
@given(stale=st.booleans(), name=st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True))
def test_property16_base_revision_reverified_before_write(stale: bool, name: str) -> None:
    """The executor writes iff the provider head still matches the stored base revision; a
    head advanced after preparation rejects with ``stale_base_revision`` and no write (Req 5.4)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, name)

    if stale:
        # A push landed on the target branch between drafting and execution.
        provider.advance_head(_REPO, _BRANCH)

    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    assert wrote == (not stale)

    if stale:
        assert outcome.status == "rejected"
        assert outcome.reason == "stale_base_revision"
        assert not provider.created_branches
        assert not provider.commits
        assert not provider.pull_requests
    else:
        assert outcome.status == "executed"
