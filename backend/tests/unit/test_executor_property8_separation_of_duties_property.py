#!/usr/bin/env python3
"""Property-based test for separation of duties.

Feature: source-control-connector-executor, Property 8 (design → Correctness Properties).
When separation-of-duties policy is in effect, an approval is accepted **iff** the
``Approver_Identity`` differs from the requester identity (Req 2.6). When the policy is not in
effect, an approval is accepted regardless. The approver identity is always derived from the
#278 identity seam (:class:`DefaultIdentityContract278`) — never from model/tool input.

The generator crosses a requester subject, an approver subject (sometimes equal, sometimes
different), and a ``require_sod`` flag; the test asserts the biconditional for the required case
and unconditional acceptance for the not-required case.

Validates: Requirements 2.6
"""

# Standard library
import json

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DefaultIdentityContract278, DefaultOperationContracts277
from connector.executor.approval import ApprovalRejected, ApprovalService
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.models import DraftedChange, RequesterIdentity, RiskLevel, TargetSelector
from connector.executor.preparation import PreparationService
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"

_SUBJECT = st.from_regex(r"[a-z][a-z0-9]{1,10}", fullmatch=True)


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


def _draft() -> DraftedChange:
    return DraftedChange(
        files=(
            ProposedFile(
                path="infra/main.yaml",
                content=json.dumps({"Resources": {"Res0": {"Type": "AWS::S3::Bucket"}}}),
                iac_format="cloudformation",
            ),
        ),
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )


def _prepare(requester_subject: str) -> tuple[InMemoryOperationStore, str]:
    provider = FakeProvider()
    store = InMemoryOperationStore()
    requester = RequesterIdentity(subject=requester_subject, groups=("infra",))
    result = _prep_service(provider, store).prepare(_draft(), requester=requester)
    assert result.status == "prepared" and result.operation_id
    return store, str(result.operation_id)


# Feature: source-control-connector-executor, Property 8: Separation of duties is enforced when required
@settings(max_examples=100)
@given(requester_subject=_SUBJECT, approver_subject=_SUBJECT, require_sod=st.booleans())
def test_property8_separation_of_duties_enforced_when_required(
    requester_subject: str, approver_subject: str, require_sod: bool
) -> None:
    """When required, approval succeeds iff approver != requester; otherwise always succeeds."""
    store, operation_id = _prepare(requester_subject)
    service = ApprovalService(
        store=store,
        identity=DefaultIdentityContract278(),
        require_separation_of_duties=require_sod,
    )

    same_identity = approver_subject == requester_subject

    if require_sod and same_identity:
        # SoD in effect and approver == requester: the approval is rejected.
        with pytest.raises(ApprovalRejected) as exc:
            service.approve(
                operation_id,
                approval_ctx={"subject": approver_subject, "groups": ["infra"]},
                source="web",
            )
        assert exc.value.reason == "separation_of_duties"
        assert store.get_latest_approval(operation_id) is None
    else:
        # Either SoD is off, or the approver differs from the requester: the approval succeeds.
        approval = service.approve(
            operation_id,
            approval_ctx={"subject": approver_subject, "groups": ["infra"]},
            source="web",
        )
        # The approver identity came from the #278 seam / trusted context.
        assert approval.approver_identity.subject == approver_subject
        assert approval.separation_of_duties_ok == (not same_identity)
        assert store.get_latest_approval(operation_id) is not None
