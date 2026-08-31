#!/usr/bin/env python3
"""Property-based test for load-and-reject-absent.

Feature: source-control-connector-executor, Property 10 (design → Correctness Properties).
The executor loads the :class:`PreparedOperation` from the store by ``Operation_ID`` before any
write; when no such operation exists it rejects with ``operation_absent`` and performs no
provider write (Req 4.4).

The generator crosses a presence flag: when ``present`` is true a real operation is
prepared+approved and its id is executed (loading it succeeds and it writes); when false a
freshly generated id that was never stored is executed against an empty store. The invariant is
the biconditional — a provider write happens **iff** the operation was loadable.

Validates: Requirements 4.4
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


# Feature: source-control-connector-executor, Property 10: The executor loads the operation and rejects when it is absent
@settings(max_examples=100)
@given(
    present=st.booleans(),
    absent_id=st.from_regex(r"[a-z0-9]{6,24}", fullmatch=True),
    name=st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
)
def test_property10_loads_or_rejects_absent(present: bool, absent_id: str, name: str) -> None:
    """A provider write occurs iff the operation is loadable by id; an absent operation rejects
    with ``operation_absent`` and no provider write (Req 4.4)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()

    if present:
        operation_id = _prepare_and_approve(provider, store, name)
    else:
        # A generated id that was never inserted into the (empty) store.
        operation_id = f"missing-{absent_id}"

    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    assert wrote == present

    if present:
        assert outcome.status == "executed"
    else:
        assert outcome.status == "rejected"
        assert outcome.reason == "operation_absent"
        assert not provider.created_branches
        assert not provider.commits
        assert not provider.pull_requests
