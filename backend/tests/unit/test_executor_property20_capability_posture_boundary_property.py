#!/usr/bin/env python3
"""Property-based test for capability posture at every write-path entry boundary.

Feature: source-control-connector-executor, Property 20 (design → Correctness Properties).
The capability posture (deployment mode + trusted tenant/workspace configuration) is enforced
at *every* write-path entry boundary — preparation, approval, and execution — so an operation
proceeds past a boundary **iff** the posture is enabled for its tenant/workspace (Req 7.1).

The three boundaries are exercised with a single generated ``enabled`` flag:

1. **Preparation** — ``PreparationService.prepare`` proceeds (stores an operation and returns
   an id) iff the posture is enabled; a disabled posture rejects with ``capability_disabled``
   and stores nothing.
2. **Approval** — the trusted approval surface only advances an operation that passed the
   preparation boundary. Under a disabled posture nothing was prepared, so the same
   ``approve`` call fails closed at the approval boundary (``operation_absent``).
3. **Execution** — with a directly-stored, hash-valid, approved operation (so the posture is
   the deciding gate), ``Executor.handle`` performs a provider write iff the posture is
   enabled; a disabled posture rejects with ``capability_disabled`` and performs no
   ``create_branch`` / ``commit_files`` / ``open_change_proposal``.

The real deterministic write path is exercised against the in-repo default seam adapters, the
in-memory #279 store (``InMemoryOperationStore``, the established store double), the #278
identity double (``DefaultIdentityContract278``), and the reader+writer ``FakeProvider``.

Validates: Requirements 7.1
"""

# Standard library
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import (
    DEFAULT_CONTRACT_VERSION,
    DefaultIdentityContract278,
    DefaultOperationContracts277,
)
from connector.executor.approval import ApprovalRejected, ApprovalService
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.handler import Executor, ExecutorDependencies
from connector.executor.models import (
    ApprovalRecord,
    ApproverIdentity,
    DraftedChange,
    EffectiveAuthority,
    ExecutorEvent,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
    TargetSelector,
)
from connector.executor.preparation import PreparationService
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import DEFAULT_HEAD_SHA, FakeProvider

pytestmark = pytest.mark.unit

_WORKFLOW_ROLE = "arn:aws:iam::123456789012:role/ScmWorkflowRole"
_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"
_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


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
    assert secret_arn == _WRITE_SECRET
    return "write-token"


@st.composite
def _cfn_files(draw: st.DrawFn) -> tuple[ProposedFile, ...]:
    """Generate 1..3 distinct, structurally valid CloudFormation files under infra/."""
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


def _prep_service(
    provider: FakeProvider, store: InMemoryOperationStore, posture: CapabilityPosture
) -> PreparationService:
    return PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=posture,
        policy_layers=_layers(),
    )


def _deps(provider: FakeProvider, store: InMemoryOperationStore, posture: CapabilityPosture) -> ExecutorDependencies:
    return ExecutorDependencies(
        store=store,
        contracts=DefaultOperationContracts277(),
        provider=provider,
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=posture,
        workflow_role_arn=_WORKFLOW_ROLE,
        write_secret_arn=_WRITE_SECRET,
        policy_layers=_layers(),
        credential_acquirer=_acquirer,
        clock=lambda: _T0,
    )


def _stored_operation(files: tuple[ProposedFile, ...]) -> PreparedOperation:
    """Build a stored operation whose canonical_hash is the correct #277 hash of its content."""
    contracts = DefaultOperationContracts277()
    base = PreparedOperation(
        operation_id="op-exec",
        canonical_hash="",
        operation_contract_version=DEFAULT_CONTRACT_VERSION,
        files=files,
        target_repo="org/iac",
        target_branch="main",
        base_revision=DEFAULT_HEAD_SHA,
        effective_authority=EffectiveAuthority(decision="authorized", inputs=(), risk_ceiling=RiskLevel.HIGH),
        risk=RiskLevel.LOW,
        requester_identity=RequesterIdentity(subject="user-1", groups=("infra",)),
        duplicate_content_key="dup",
        created_at=_T0.isoformat(),
    )
    return replace(base, canonical_hash=contracts.canonical_hash(base))


def _approval(op: PreparedOperation) -> ApprovalRecord:
    return ApprovalRecord(
        operation_id=op.operation_id,
        approver_identity=ApproverIdentity(subject="approver-1", groups=("infra",)),
        bound_canonical_hash=op.canonical_hash,
        approved_at=_T0.isoformat(),
        expires_at=(_T0 + timedelta(hours=1)).isoformat(),
        separation_of_duties_ok=True,
    )


# Feature: source-control-connector-executor, Property 20: Capability posture is enforced at every write-path entry boundary
@settings(max_examples=100)
@given(files=_cfn_files(), enabled=st.booleans())
def test_property20_capability_posture_enforced_at_every_boundary(
    files: tuple[ProposedFile, ...], enabled: bool
) -> None:
    """An operation proceeds past preparation, approval, and execution iff the capability
    posture is enabled for its tenant/workspace (Req 7.1)."""
    posture = CapabilityPosture(enabled=enabled, capability_maximum=RiskLevel.HIGH, tenant="t", workspace="w")
    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    approval_ctx = {"subject": "approver-1", "groups": ["infra"]}

    # --- Boundary 1: preparation ----------------------------------------------------------
    prep_store = InMemoryOperationStore()
    prep = _prep_service(FakeProvider(), prep_store, posture)
    draft = DraftedChange(
        files=files,
        iac_format="cloudformation",
        target=TargetSelector(repository="org/iac", branch="main"),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    result = prep.prepare(draft, requester=requester)
    assert (result.status == "prepared") == enabled
    if not enabled:
        assert result.reason == "capability_disabled"
        assert result.operation_id == ""

    # --- Boundary 2: approval (advances only an operation that passed preparation) --------
    approval_service = ApprovalService(store=prep_store, identity=DefaultIdentityContract278())
    if enabled:
        approval = approval_service.approve(result.operation_id, approval_ctx=approval_ctx, source="web")
        assert approval is not None
    else:
        with pytest.raises(ApprovalRejected) as exc:
            approval_service.approve(result.operation_id, approval_ctx=approval_ctx, source="web")
        assert exc.value.reason == "operation_absent"

    # --- Boundary 3: execution (posture is the deciding gate on a valid, approved op) -----
    exec_provider = FakeProvider()
    exec_store = InMemoryOperationStore()
    operation = _stored_operation(files)
    exec_store.insert_operation(operation)
    exec_store.apply_approval_transition(operation.operation_id, _approval(operation))

    outcome = Executor(_deps(exec_provider, exec_store, posture)).handle(
        ExecutorEvent(operation_id=operation.operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(exec_provider.created_branches or exec_provider.commits or exec_provider.pull_requests)
    assert wrote == enabled
    if enabled:
        assert outcome.status == "executed"
    else:
        assert outcome.status == "rejected"
        assert outcome.reason == "capability_disabled"
        assert not exec_provider.created_branches
        assert not exec_provider.commits
        assert not exec_provider.pull_requests
