#!/usr/bin/env python3
"""Property-based test for verified-operation write shape.

Feature: source-control-connector-executor, Property 13 (design → Correctness Properties).
For any operation that passes every gate, the executor invokes ``create_branch``,
``commit_files``, and ``open_change_proposal`` exactly once each, and the resulting change
proposal is **unmerged** (Req 4.9). The real deterministic write path
(``PreparationService.prepare`` → ``ApprovalService.approve`` → ``Executor.handle``) is
exercised end-to-end against the in-memory #279 store and the reader+writer ``FakeProvider``,
whose recorded ``created_branches`` / ``commits`` / ``pull_requests`` make the three-call write
shape directly assertable.

Validates: Requirements 4.9
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


@st.composite
def _cfn_files(draw: st.DrawFn) -> tuple[ProposedFile, ...]:
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    files: list[ProposedFile] = []
    for index, name in enumerate(names):
        resource_type = draw(st.sampled_from(["AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::SNS::Topic"]))
        content = json.dumps({"Resources": {f"Res{index}": {"Type": resource_type}}})
        files.append(ProposedFile(path=f"infra/{name}.yaml", content=content, iac_format="cloudformation"))
    return tuple(files)


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


def _prepare_and_approve(provider: FakeProvider, store: InMemoryOperationStore, files: tuple[ProposedFile, ...]) -> str:
    draft = DraftedChange(
        files=files,
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
    ApprovalService(store=store, identity=DefaultIdentityContract278()).approve(
        result.operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source="web",
    )
    return str(result.operation_id)


# Feature: source-control-connector-executor, Property 13: A verified operation yields a branch, a commit, and an unmerged PR
@settings(max_examples=100)
@given(files=_cfn_files())
def test_property13_verified_operation_yields_branch_commit_unmerged_pr(files: tuple[ProposedFile, ...]) -> None:
    """A fully-verified operation invokes create_branch, commit_files, and open_change_proposal
    once each, and the resulting proposal is unmerged (Req 4.9)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, files)

    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    assert outcome.status == "executed"

    # Exactly one branch, one commit, one proposal — the three-call write shape.
    assert len(provider.created_branches) == 1
    assert provider.created_branches[0]["new_branch"].startswith("gbaw/")
    assert len(provider.commits) == 1
    assert len(provider.pull_requests) == 1

    # The proposal is unmerged: the fake never records a merge, and the executor's outcome
    # carries only an internal proposal reference (never a merge / PR-URL surfaced to the model).
    proposal = provider.pull_requests[0]
    assert "merged" not in proposal
    assert "merged_at" not in proposal
    assert outcome.proposal_ref == proposal["proposal_id"]

    # The provider was driven through create_branch -> commit_files -> open_change_proposal.
    assert provider.calls_for("create_branch")
    assert provider.calls_for("commit_files")
    assert provider.calls_for("open_change_proposal")
