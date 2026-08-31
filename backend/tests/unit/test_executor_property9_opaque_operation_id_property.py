#!/usr/bin/env python3
"""Property-based test for opaque-operation-id-only input.

Feature: source-control-connector-executor, Property 9 (design → Correctness Properties).
Only the opaque ``Operation_ID`` drives the executor's behavior: any additional / free-form
field carried alongside the invocation has **no** effect on whether or how a provider write
occurs (Req 4.2, 4.3). The :class:`ExecutorEvent` is structurally the operation id alone, so
the "extra field" surface exercised here is arbitrary noise injected into the invocation
context beyond the caller-identity key.

Two independent runs are prepared+approved for the **same** forced ``operation_id`` over the
**same** file set (one clean, one with generated free-form context noise). The invariant is
that the two runs are indistinguishable: identical status, identical derived branch, and an
identical branch/commit/proposal write shape — the noise changed nothing.

Validates: Requirements 4.2, 4.3
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
_FIXED_OP_ID = "op-fixed-identity"
_RESERVED_CONTEXT_KEYS = frozenset({"caller_identity", "invoked_role_arn"})


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
        new_operation_id=lambda: _FIXED_OP_ID,
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


def _prepare_approve_execute(files: tuple[ProposedFile, ...], context: dict) -> tuple[str, FakeProvider]:
    """Prepare+approve a fixed-id operation over ``files`` and execute with ``context``.

    Returns the terminal status and the provider double (whose recorded branches/commits/PRs
    capture the exact write shape).
    """
    provider = FakeProvider()
    store = InMemoryOperationStore()
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
    outcome = Executor(_deps(provider, store)).handle(ExecutorEvent(operation_id=result.operation_id), context)
    return outcome.status, provider


# Feature: source-control-connector-executor, Property 9: The executor consumes only the opaque operation id
@settings(max_examples=100)
@given(
    files=_cfn_files(),
    noise=st.dictionaries(
        keys=st.text(min_size=1, max_size=12).filter(lambda k: k not in _RESERVED_CONTEXT_KEYS),
        values=st.one_of(st.text(max_size=20), st.integers(), st.booleans()),
        max_size=5,
    ),
)
def test_property9_extra_fields_have_no_effect(files: tuple[ProposedFile, ...], noise: dict) -> None:
    """A clean invocation and one carrying arbitrary free-form context noise produce an
    identical outcome and identical branch/commit/proposal write shape — only the operation id
    drives behavior (Req 4.2, 4.3)."""
    clean_status, clean_provider = _prepare_approve_execute(files, {"caller_identity": _WORKFLOW_ROLE})
    noisy_status, noisy_provider = _prepare_approve_execute(files, {"caller_identity": _WORKFLOW_ROLE, **noise})

    # Same terminal decision.
    assert clean_status == noisy_status == "executed"

    # Same derived branch (a pure function of the shared, forced operation id).
    clean_branches = [b["new_branch"] for b in clean_provider.created_branches]
    noisy_branches = [b["new_branch"] for b in noisy_provider.created_branches]
    assert clean_branches == noisy_branches

    # Same write shape: one branch, one commit, one proposal in each.
    assert len(clean_provider.created_branches) == len(noisy_provider.created_branches) == 1
    assert len(clean_provider.commits) == len(noisy_provider.commits) == 1
    assert len(clean_provider.pull_requests) == len(noisy_provider.pull_requests) == 1
