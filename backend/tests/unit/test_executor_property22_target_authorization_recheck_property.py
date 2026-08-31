#!/usr/bin/env python3
"""Property-based test for independent target-authorization re-check.

Feature: source-control-connector-executor, Property 22 (design → Correctness Properties).
The executor re-evaluates ``Target_Authorization`` (repository, branch, normalized path,
extension, group) at execution time and writes **iff** it passes, independent of the
preparation-time decision (Req 7.5, 7.6). The operation is always prepared+approved through a
fully-authorizing policy (so the prep-time target decision passed), and the *executor's* target
policy / authorized groups are then independently varied so a single target dimension is denied
(or none). The invariant asserted is the biconditional between the executor-side target
authorization and whether a provider write occurred.

Validates: Requirements 7.5, 7.6
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

# Target dimensions this property denies at execution time. ``None`` is the allowed case.
_TARGET_DIMENSIONS = [None, "repository", "branch", "path", "extension", "group"]


def _prep_policy() -> AuthorizationPolicy:
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


def _acquirer(secret_arn: str, *, source: str) -> str:
    return "write-token"


def _layers() -> tuple[PolicyLayer, ...]:
    return (PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),)


def _prep_service(provider: FakeProvider, store: InMemoryOperationStore) -> PreparationService:
    return PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_prep_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        policy_layers=_layers(),
    )


def _exec_policy_and_groups(dimension: str | None) -> tuple[AuthorizationPolicy, tuple[str, ...]]:
    """Return an executor-side (policy, authorized_groups) that denies exactly ``dimension``."""
    # The fully-authorizing baseline (identical to prep).
    repo, branches, prefixes, exts = _REPO, (_BRANCH,), ("infra/",), (".yaml",)
    authorized_groups: tuple[str, ...] = ("infra",)

    if dimension == "repository":
        repo = "org/other"
    elif dimension == "branch":
        branches = ("release",)
    elif dimension == "path":
        prefixes = ("src/",)
    elif dimension == "extension":
        exts = (".json",)
    elif dimension == "group":
        authorized_groups = ("platform-admins",)

    policy = AuthorizationPolicy(
        entries=(AllowlistEntry(repo=repo, target_branches=branches, path_prefixes=prefixes, extensions=exts),)
    )
    return policy, authorized_groups


def _deps(provider: FakeProvider, store: InMemoryOperationStore, dimension: str | None) -> ExecutorDependencies:
    policy, authorized_groups = _exec_policy_and_groups(dimension)
    return ExecutorDependencies(
        store=store,
        contracts=DefaultOperationContracts277(),
        provider=provider,
        policy=policy,
        authorized_groups=authorized_groups,
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


# Feature: source-control-connector-executor, Property 22: The executor re-checks target authorization independently
@settings(max_examples=100)
@given(dimension=st.sampled_from(_TARGET_DIMENSIONS), name=st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True))
def test_property22_executor_rechecks_target_authorization(dimension: str | None, name: str) -> None:
    """The executor writes iff its own target-authorization re-check passes, independent of the
    prep-time decision; any denied target dimension rejects with no write (Req 7.5, 7.6)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, name)

    outcome = Executor(_deps(provider, store, dimension)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    assert wrote == (dimension is None)

    if dimension is None:
        assert outcome.status == "executed"
    else:
        assert outcome.status == "rejected"
        assert outcome.reason is not None and outcome.reason.startswith("target_authorization_denied")
        assert not provider.created_branches
        assert not provider.commits
        assert not provider.pull_requests
