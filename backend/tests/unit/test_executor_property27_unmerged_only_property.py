#!/usr/bin/env python3
"""Property-based test for no-merge / unmerged-only behavior.

Feature: source-control-connector-executor, Property 27 (design → Correctness Properties).
For every execution the created change proposal is unmerged and the executor invokes no merge,
approve, close, delete, or force-push operation — the write interface exposes none of them, so
the guarantee is **structural** rather than a runtime check (Req 11.1, 11.2). The
:class:`~connector.executor.writer.ExecutorWriter` is asserted to expose only the reused
write/read subset and none of the forbidden operations, and every end-to-end execution is
asserted to drive the provider only through that subset and to leave the proposal unmerged.

Validates: Requirements 11.1, 11.2
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
from connector.executor.writer import ExecutorWriter
from connector.models import ProposedFile
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_WORKFLOW_ROLE = "arn:aws:iam::123456789012:role/ScmWorkflowRole"
_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"

# The write/read subset the executor is permitted to use — and the operations it must NEVER expose.
_ALLOWED_WRITER_OPS = frozenset(
    {
        "branch_exists",
        "latest_commit_sha",
        "create_branch",
        "commit_files",
        "open_change_proposal",
        "find_open_change_proposal",
    }
)
_FORBIDDEN_WRITER_OPS = ("merge", "merge_change_proposal", "approve", "close", "delete", "delete_branch", "force_push")


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


# Feature: source-control-connector-executor, Property 27: The executor creates only an unmerged PR and never merges, approves, closes, deletes, or force-pushes
@settings(max_examples=100)
@given(files=_cfn_files())
def test_property27_unmerged_only_and_no_forbidden_operations(files: tuple[ProposedFile, ...]) -> None:
    """The writer structurally exposes no merge/approve/close/delete/force-push op, and every
    execution leaves the proposal unmerged while driving the provider only through the reused
    write/read subset (Req 11.1, 11.2)."""
    # Structural guarantee: the writer exposes only the reused subset and none of the forbidden ops.
    public_methods = {name for name in vars(ExecutorWriter) if not name.startswith("_")}
    assert public_methods == _ALLOWED_WRITER_OPS
    for forbidden in _FORBIDDEN_WRITER_OPS:
        assert not hasattr(ExecutorWriter, forbidden), f"ExecutorWriter unexpectedly exposes {forbidden!r}"

    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, files)

    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    assert outcome.status == "executed"

    # Exactly one unmerged proposal; the fake records no merge state and the executor surfaces none.
    assert len(provider.pull_requests) == 1
    assert "merged" not in provider.pull_requests[0]
    assert "merged_at" not in provider.pull_requests[0]

    # Every provider operation invoked is within the permitted write/read subset — no forbidden op.
    assert set(provider.call_operations) <= (_ALLOWED_WRITER_OPS | {"get_file", "get_files"})
