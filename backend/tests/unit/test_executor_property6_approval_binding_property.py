#!/usr/bin/env python3
"""Property-based test for approval binding + expiry.

Feature: source-control-connector-executor, Property 6 (design → Correctness Properties).
Every approval's bound hash equals the stored ``Canonical_Hash`` of the prepared operation and
the :class:`ApprovalRecord` carries an expiry timestamp (Req 2.3, 2.4, 6.2). The approval is
written as a separate conditional transition; the test also confirms the record persisted to
the store binds the same hash and that ``expires_at`` sits exactly ``ttl_seconds`` after
``approved_at``.

The path exercised is the real deterministic write path — ``PreparationService.prepare`` →
``ApprovalService.approve`` — against the in-repo default seams and the in-memory #279 store,
with an injected clock and a generated approval TTL so the expiry offset is asserted exactly.

Validates: Requirements 2.3, 2.4, 6.2
"""

# Standard library
import json
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DefaultIdentityContract278, DefaultOperationContracts277
from connector.executor.approval import ApprovalService
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.models import DraftedChange, RequesterIdentity, RiskLevel, TargetSelector
from connector.executor.preparation import PreparationService
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"
_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _policy() -> AuthorizationPolicy:
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


def _layers() -> tuple[PolicyLayer, ...]:
    return (PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),)


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


@st.composite
def _cfn_draft(draw: st.DrawFn) -> DraftedChange:
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    files = tuple(
        ProposedFile(
            path=f"infra/{name}.yaml",
            content=json.dumps({"Resources": {f"Res{index}": {"Type": "AWS::S3::Bucket"}}}),
            iac_format="cloudformation",
        )
        for index, name in enumerate(names)
    )
    return DraftedChange(
        files=files,
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )


# Feature: source-control-connector-executor, Property 6: Approval binds to the stored canonical hash and carries an expiry
@settings(max_examples=100)
@given(draft=_cfn_draft(), ttl_seconds=st.integers(min_value=1, max_value=86400))
def test_property6_approval_binds_hash_and_carries_expiry(draft: DraftedChange, ttl_seconds: int) -> None:
    """The approval binds the stored canonical hash and carries an expiry ttl after approval."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    result = _prep_service(provider, store).prepare(
        draft, requester=RequesterIdentity(subject="user-1", groups=("infra",))
    )
    assert result.status == "prepared" and result.operation_id

    operation = store.get_operation(result.operation_id)
    assert operation is not None

    approval = ApprovalService(
        store=store,
        identity=DefaultIdentityContract278(),
        approval_ttl_seconds=ttl_seconds,
        clock=lambda: _T0,
    ).approve(
        result.operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source="web",
    )

    # The approval binds exactly the stored operation's canonical hash.
    assert approval.bound_canonical_hash == operation.canonical_hash
    # It carries an expiry timestamp, exactly ttl_seconds after the approval time.
    approved_at = datetime.fromisoformat(approval.approved_at)
    expires_at = datetime.fromisoformat(approval.expires_at)
    assert expires_at == approved_at + timedelta(seconds=ttl_seconds)
    assert expires_at > approved_at

    # The persisted transition record binds the same stored hash.
    latest = store.get_latest_approval(result.operation_id)
    assert latest is not None and latest.bound_canonical_hash == operation.canonical_hash
