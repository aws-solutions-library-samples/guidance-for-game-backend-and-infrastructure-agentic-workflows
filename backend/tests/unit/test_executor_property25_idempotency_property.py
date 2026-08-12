#!/usr/bin/env python3
"""Property-based test for control-plane idempotency (SECURITY GATE — non-optional).

Feature: source-control-connector-executor, Property 25 (design → Correctness Properties).
Repeated dispatch of the *same* prepared operation — the exact "an approval transition is
delivered more than once" condition a durable at-least-once stream dispatch can produce — must
never create a second independent execution or a second provider artifact. The guarantee is
enforced by the operation record plus the append-only ledger together with the deterministic
``gbaw/<short-operation-id>`` branch and the executor's reconcile-before-retry: on a repeat
dispatch the executor reconciles the already-present branch/commit/proposal and reuses them
rather than duplicating them.

This exercises the real deterministic write path — ``PreparationService.prepare`` →
``ApprovalService.approve`` → ``Executor.handle`` — against the in-repo default seam adapters,
the in-memory #279 store (``InMemoryOperationStore``), and the reader+writer ``FakeProvider``
double, so "no second provider artifact" is asserted directly against the recorded provider
mutations.

Validates: Requirements 8.7, 14.5
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
    return (
        PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),
        PolicyLayer(name="tenant", enabled=True, max_risk=RiskLevel.HIGH),
    )


def _acquirer(secret_arn: str, *, source: str) -> str:
    """Fake executor-role credential acquirer (returns a non-empty write token)."""
    assert secret_arn == _WRITE_SECRET
    return "write-token"


@st.composite
def _cfn_files(draw: st.DrawFn) -> tuple[ProposedFile, ...]:
    """Generate 1..3 distinct, structurally valid CloudFormation ``ProposedFile``s under infra/."""
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
    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    draft = DraftedChange(
        files=files,
        iac_format="cloudformation",
        target=TargetSelector(repository="org/iac", branch="main"),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    result = _prep_service(provider, store).prepare(draft, requester=requester)
    assert result.status == "prepared" and result.operation_id
    ApprovalService(store=store, identity=DefaultIdentityContract278()).approve(
        result.operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source="web",
    )
    return str(result.operation_id)


# Feature: source-control-connector-executor, Property 25: Control-plane idempotency prevents a second independent execution
@settings(max_examples=100)
@given(files=_cfn_files(), dispatches=st.integers(min_value=2, max_value=5))
def test_property25_repeated_dispatch_creates_no_second_execution(
    files: tuple[ProposedFile, ...], dispatches: int
) -> None:
    """Dispatching the same operation N>=2 times yields at most one branch/commit/proposal.

    The operation record + ledger + deterministic branch cause every repeat dispatch to be
    reconciled against the already-present provider state, so no second independent execution
    and no second provider artifact are ever created (Req 8.7, 14.5)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, files)

    executor = Executor(_deps(provider, store))
    outcomes = [
        executor.handle(ExecutorEvent(operation_id=operation_id), {"caller_identity": _WORKFLOW_ROLE})
        for _ in range(dispatches)
    ]

    # Every dispatch terminates in a non-duplicating success (executed via reconcile reuse).
    assert all(o.status == "executed" for o in outcomes), [o.reason for o in outcomes]

    # No second provider artifact: exactly one branch, at most one commit, exactly one proposal.
    created_for_op = [b for b in provider.created_branches if b["new_branch"].startswith("gbaw/")]
    assert len(created_for_op) == 1
    assert len({b["new_branch"] for b in provider.created_branches}) == len(provider.created_branches)
    assert len(provider.commits) <= 1
    assert len(provider.pull_requests) == 1

    # No second *independent* execution: every dispatch resolves to the same single proposal.
    proposal_refs = {o.proposal_ref for o in outcomes}
    assert proposal_refs == {provider.pull_requests[0]["proposal_id"]}
