#!/usr/bin/env python3
"""Import-sanity / wiring unit tests for the executor core write path.

These focused example tests exercise the end-to-end deterministic write path built for the
executor spec — ``PreparationService.prepare`` → ``ApprovalService.approve`` →
``Executor.handle`` — against the in-repo default seam adapters, the in-memory #279 store, and
the ``FakeProvider`` double. They lock in the module wiring and a few fail-closed gates:

- a fully-authorized, approved operation executes and yields a branch/commit/unmerged proposal
  (with only an internal ``proposal_ref`` on the outcome, never a PR URL),
- an invocation from a caller other than the workflow role is rejected with no provider write,
- an expired approval is rejected with no provider write, and
- the approval surface refuses a chat/model-originated (untrusted) source.

The universal property tests (Properties 1-28) are separate later tasks and are intentionally
NOT written here.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import (
    DefaultIdentityContract278,
    DefaultOperationContracts277,
)
from connector.executor.approval import ApprovalRejected, ApprovalService
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
_VALID_CFN = "Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n"


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
    return (
        PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),
        PolicyLayer(name="tenant", enabled=True, max_risk=RiskLevel.HIGH),
    )


def _draft() -> DraftedChange:
    return DraftedChange(
        files=(ProposedFile(path="infra/main.yaml", content=_VALID_CFN, iac_format="cloudformation"),),
        iac_format="cloudformation",
        target=TargetSelector(repository="org/iac", branch="main"),
        intent="add a bucket",
        title="Add bucket",
        description="Adds an S3 bucket.",
    )


def _acquirer(secret_arn: str, *, source: str) -> str:
    """Fake executor-role credential acquirer (returns a non-empty token)."""
    assert secret_arn == _WRITE_SECRET
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


def _deps(provider: FakeProvider, store: InMemoryOperationStore, *, clock=None) -> ExecutorDependencies:
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
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
        **kwargs,
    )


def _prepare_and_approve(provider: FakeProvider, store: InMemoryOperationStore) -> str:
    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    result = _prep_service(provider, store).prepare(_draft(), requester=requester)
    assert result.status == "prepared" and result.operation_id
    approval_service = ApprovalService(store=store, identity=DefaultIdentityContract278())
    approval_service.approve(
        result.operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source="web",
    )
    return result.operation_id


def test_full_prepare_approve_execute_happy_path() -> None:
    """An authorized, approved operation executes into a branch/commit/unmerged proposal."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store)

    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    assert outcome.status == "executed"
    # An internal provider reference is carried, never a PR URL.
    assert outcome.proposal_ref is not None
    # A branch, a commit, and exactly one (unmerged) proposal were created on the provider.
    assert provider.created_branches and provider.created_branches[0]["new_branch"].startswith("gbaw/")
    assert len(provider.commits) == 1
    assert len(provider.pull_requests) == 1
    # The terminal ledger entry records the executed outcome.
    assert any(e.outcome == "executed" for e in store.list_ledger(operation_id))


def test_caller_not_workflow_role_is_rejected_without_write() -> None:
    """Gate 1: any caller other than the workflow role is rejected with no provider write."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store)

    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": "arn:aws:iam::123456789012:role/SomeOtherRole"},
    )

    assert outcome.status == "rejected" and outcome.reason == "caller_not_workflow_role"
    assert not provider.created_branches and not provider.pull_requests


def test_expired_approval_is_rejected_without_write() -> None:
    """Gate 4: an approval whose expiry has passed never executes."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store)

    # A clock far in the future makes the (default 1h TTL) approval expired at execution time.
    future = lambda: datetime.now(timezone.utc) + timedelta(days=1)  # noqa: E731
    outcome = Executor(_deps(provider, store, clock=future)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    assert outcome.status == "rejected" and outcome.reason == "approval_expired"
    assert not provider.created_branches and not provider.pull_requests


def test_absent_operation_is_rejected() -> None:
    """Gate 2: the executor rejects an unknown operation id with no provider write."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id="does-not-exist"),
        {"caller_identity": _WORKFLOW_ROLE},
    )
    assert outcome.status == "rejected" and outcome.reason == "operation_absent"
    assert not provider.created_branches


def test_approval_surface_rejects_untrusted_source() -> None:
    """The approval surface accepts only trusted web/API input, never chat/model output."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    result = _prep_service(provider, store).prepare(_draft(), requester=requester)
    service = ApprovalService(store=store, identity=DefaultIdentityContract278())
    with pytest.raises(ApprovalRejected) as exc:
        service.approve(
            result.operation_id,
            approval_ctx={"subject": "approver-1", "groups": ["infra"]},
            source="chat",
        )
    assert exc.value.reason == "untrusted_source"
