#!/usr/bin/env python3
"""Property-based test for workflow-role-only invocation.

Feature: source-control-connector-executor, Property 12 (design → Correctness Properties).
The executor proceeds **iff** the caller is the Durable_Workflow Step Functions role; any other
caller is rejected at gate 1 with ``caller_not_workflow_role`` and no provider write (Req 4.7,
4.8). The real isolation is enforced by IAM (only the workflow role holds
``lambda:InvokeFunction`` on the executor); this is the in-code fail-closed mirror of that
grant.

The generator produces caller identities — sometimes the exact workflow role, sometimes an
arbitrary other principal arn/string — and the invariant asserted is the biconditional between
"caller is the workflow role" and "a provider write occurred".

Validates: Requirements 4.7, 4.8
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


def _prepare_and_approve(provider: FakeProvider, store: InMemoryOperationStore) -> str:
    draft = DraftedChange(
        files=(
            ProposedFile(
                path="infra/main.yaml",
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
    ApprovalService(store=store, identity=DefaultIdentityContract278()).approve(
        result.operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source="web",
    )
    return str(result.operation_id)


_OTHER_CALLERS = st.one_of(
    st.from_regex(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_-]{3,30}", fullmatch=True),
    st.text(min_size=0, max_size=40),
    st.none(),
)


# Feature: source-control-connector-executor, Property 12: Only the workflow role can invoke the executor
@settings(max_examples=100)
@given(use_workflow_role=st.booleans(), other_caller=_OTHER_CALLERS)
def test_property12_only_workflow_role_can_invoke(use_workflow_role: bool, other_caller: object) -> None:
    """The executor proceeds iff the caller is the workflow role; any other caller is rejected
    with ``caller_not_workflow_role`` and no provider write (Req 4.7, 4.8)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store)

    caller = _WORKFLOW_ROLE if use_workflow_role else other_caller
    context = {} if caller is None else {"caller_identity": caller}
    outcome = Executor(_deps(provider, store)).handle(ExecutorEvent(operation_id=operation_id), context)

    # Expected purely from the actual caller value (robust even if a generated string collides).
    is_workflow_role = caller == _WORKFLOW_ROLE
    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    assert wrote == is_workflow_role

    if is_workflow_role:
        assert outcome.status == "executed"
    else:
        assert outcome.status == "rejected"
        assert outcome.reason == "caller_not_workflow_role"
        assert not provider.created_branches
        assert not provider.commits
        assert not provider.pull_requests
