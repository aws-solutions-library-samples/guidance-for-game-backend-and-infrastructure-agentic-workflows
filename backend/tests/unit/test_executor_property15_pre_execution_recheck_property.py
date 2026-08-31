#!/usr/bin/env python3
"""Property-based test for the pre-execution re-check gate.

Feature: source-control-connector-executor, Property 15 (design → Correctness Properties).
The executor performs a provider write **iff** the capability posture, request-time policy,
current policy version, resource enrollment, and normalized paths/extensions all re-validate at
execution time; if any re-check fails it rejects with no provider write (Req 5.1, 5.2, 5.3, 5.6,
7.7).

The operation is always prepared+approved through a fully-authorizing configuration, so the
stored operation is valid; the *executor's* re-validation inputs are then independently varied
so exactly one re-check dimension is failed (or none). The invariant asserted is the
biconditional: a provider write happens **iff** no re-check was failed, and on a failure the
rejection reason names the failed dimension.

Validates: Requirements 5.1, 5.2, 5.3, 5.6, 7.7
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

# The re-check dimensions this property exercises, each mapped to its rejection reason prefix.
# ``None`` is the all-pass case that must write.
_FAIL_REASONS = {
    "capability": "capability_disabled",
    "request_time": "request_time_denied",
    "policy_version": "policy_version_stale",
    "enrollment": "resource_not_enrolled",
    "effective_authority": "effective_authority_denied",
    "target_paths": "target_authorization_denied",
}
_DIMENSIONS = [None, *sorted(_FAIL_REASONS)]


def _prep_policy() -> AuthorizationPolicy:
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


def _acquirer(secret_arn: str, *, source: str) -> str:
    return "write-token"


def _prep_service(provider: FakeProvider, store: InMemoryOperationStore) -> PreparationService:
    return PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_prep_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        policy_layers=(PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),),
    )


def _deps_for(provider: FakeProvider, store: InMemoryOperationStore, failing: str | None) -> ExecutorDependencies:
    """Build executor deps that fail exactly the ``failing`` re-check dimension (or none)."""
    # Defaults: everything passes.
    capability = CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH)
    policy = _prep_policy()
    authorized_groups: tuple[str, ...] = ("infra",)
    layers = (PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),)
    policy_version_ok = True
    is_enrolled = True

    if failing == "capability":
        capability = CapabilityPosture(enabled=False, capability_maximum=RiskLevel.HIGH)
    elif failing == "request_time":
        # Capability maximum below the operation's (MEDIUM) risk => request-time denial.
        capability = CapabilityPosture(enabled=True, capability_maximum=RiskLevel.LOW)
    elif failing == "policy_version":
        policy_version_ok = False
    elif failing == "enrollment":
        is_enrolled = False
    elif failing == "effective_authority":
        # A disabled applicable layer collapses the effective-authority intersection.
        layers = (PolicyLayer(name="deployment_mode", enabled=False, max_risk=RiskLevel.HIGH),)
    elif failing == "target_paths":
        # A policy that no longer authorizes the operation's target repo.
        policy = AuthorizationPolicy(
            entries=(
                AllowlistEntry(
                    repo="org/other",
                    target_branches=("main",),
                    path_prefixes=("infra/",),
                    extensions=(".yaml",),
                ),
            )
        )

    return ExecutorDependencies(
        store=store,
        contracts=DefaultOperationContracts277(),
        provider=provider,
        policy=policy,
        authorized_groups=authorized_groups,
        capability_posture=capability,
        workflow_role_arn=_WORKFLOW_ROLE,
        write_secret_arn=_WRITE_SECRET,
        policy_layers=layers,
        policy_version_ok=lambda: policy_version_ok,
        is_enrolled=lambda _repo: is_enrolled,
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


# Feature: source-control-connector-executor, Property 15: The executor writes only when every pre-execution re-check passes
@settings(max_examples=100)
@given(failing=st.sampled_from(_DIMENSIONS), name=st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True))
def test_property15_writes_iff_every_recheck_passes(failing: str | None, name: str) -> None:
    """A provider write happens iff no pre-execution re-check fails; a failed dimension rejects
    with the matching reason and no provider write (Req 5.1, 5.2, 5.3, 5.6, 7.7)."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    operation_id = _prepare_and_approve(provider, store, name)

    outcome = Executor(_deps_for(provider, store, failing)).handle(
        ExecutorEvent(operation_id=operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    assert wrote == (failing is None)

    if failing is None:
        assert outcome.status == "executed"
    else:
        assert outcome.status == "rejected"
        assert outcome.reason is not None and outcome.reason.startswith(_FAIL_REASONS[failing])
        assert not provider.created_branches
        assert not provider.commits
        assert not provider.pull_requests
