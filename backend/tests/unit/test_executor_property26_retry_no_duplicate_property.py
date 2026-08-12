#!/usr/bin/env python3
"""Property-based test for retry non-duplication + reconcile-first (SECURITY GATE — non-optional).

Feature: source-control-connector-executor, Property 26 (design → Correctness Properties).
For any transient failure during execution, the executor reconciles an ambiguous provider
outcome before retrying so the resulting provider state contains at most one branch, one
committed file set, and one open proposal for the operation — a retry never duplicates
operation/branch/commit/proposal state (Requirement 14.8).

The generator crosses the failing mutating operation across
``{create_branch, commit_files, open_change_proposal}``. Each is programmed via
``FakeProvider.apply_then_fail`` to apply its effect and *then* raise a transient error — the
exact "effect landed, then the provider raised" ambiguity the reused baseline
reconcile-before-retry (``_idempotent_mutate`` / ``_retry_mutating`` /
``find_open_change_proposal``) must handle without duplicating state. The real deterministic
write path (``PreparationService.prepare`` → ``ApprovalService.approve`` → ``Executor.handle``)
is exercised end-to-end against the in-memory #279 store and the reader+writer ``FakeProvider``.
``connector.service.time.sleep`` is neutralized so no retry backoff actually waits.

Validates: Requirements 10.1, 10.3, 10.4, 14.8
"""

# Standard library
import json
from unittest import mock

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
from connector.provider import ProviderTransientError
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_WORKFLOW_ROLE = "arn:aws:iam::123456789012:role/ScmWorkflowRole"
_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"
_MUTATING_OPS = ("create_branch", "commit_files", "open_change_proposal")


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


# Feature: source-control-connector-executor, Property 26: Retries never duplicate provider state and ambiguous outcomes are reconciled first
@settings(max_examples=100)
@given(files=_cfn_files(), failing_op=st.sampled_from(_MUTATING_OPS))
def test_property26_retry_never_duplicates_provider_state(files: tuple[ProposedFile, ...], failing_op: str) -> None:
    """A mutating op whose effect landed then raised a transient error is reconciled, not
    repeated: the provider ends with at most one branch, one commit, and one open proposal for
    the operation (Req 10.1, 10.3, 10.4, 14.8)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, files)

    # The chosen mutating op applies its effect and THEN raises a transient error, modeling the
    # ambiguous "effect landed, provider raised" outcome reconcile-before-retry must resolve.
    provider.apply_then_fail(failing_op, ProviderTransientError("ambiguous transient failure"), times=1)

    with mock.patch("connector.service.time.sleep", return_value=None):
        outcome = Executor(_deps(provider, store)).handle(
            ExecutorEvent(operation_id=operation_id),
            {"caller_identity": _WORKFLOW_ROLE},
        )

    # The effect landed and was reconciled, so the outcome is a non-duplicating success.
    assert outcome.status == "executed", outcome.reason

    # No duplication: at most one branch, one commit, one open proposal for the operation.
    created = [b for b in provider.created_branches if b["new_branch"].startswith("gbaw/")]
    assert len(created) <= 1
    assert len({b["new_branch"] for b in provider.created_branches}) == len(provider.created_branches)
    assert len(provider.commits) <= 1
    assert len(provider.pull_requests) == 1
    assert outcome.proposal_ref == provider.pull_requests[0]["proposal_id"]
