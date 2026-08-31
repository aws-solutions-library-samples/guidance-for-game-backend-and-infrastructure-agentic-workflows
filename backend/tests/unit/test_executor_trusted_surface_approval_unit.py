#!/usr/bin/env python3
"""Unit tests for trusted-surface-only approval (task 5.5).

These example tests lock in two guarantees of the approval surface
(:class:`connector.executor.approval.ApprovalService`):

1. **Trusted-surface-only** — an approval is accepted only from a trusted web/API surface; a
   chat- or model-originated source is rejected outright (Req 2.2).
2. **Identity from the #278 seam** — the approver identity is derived from the trusted approval
   context through the :class:`IdentityContract278` seam, never from request or model arguments.
   The ``approve`` signature carries no approver parameter a model could populate, and a
   seam that ignores its context proves the surface reads identity only from the seam.

Validates: Requirements 2.2, 2.7
"""

# Standard library
import json

# Third-party packages
import pytest

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DefaultIdentityContract278, DefaultOperationContracts277
from connector.executor.approval import TRUSTED_APPROVAL_SOURCES, ApprovalRejected, ApprovalService
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.models import (
    ApproverIdentity,
    DraftedChange,
    RequesterIdentity,
    RiskLevel,
    TargetSelector,
)
from connector.executor.preparation import PreparationService
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"


class _FixedIdentity278:
    """A #278 identity double that returns a fixed approver, ignoring its context entirely.

    Used to prove the approval surface derives the approver from the seam alone: whatever the
    caller passes as ``approval_ctx`` (which could carry model-influenced data), the recorded
    approver is exactly what the trusted identity provider returns.
    """

    def __init__(self, subject: str) -> None:
        self._subject = subject

    def requester_identity(self, request_ctx: object) -> RequesterIdentity:  # pragma: no cover - unused here
        return RequesterIdentity(subject=self._subject, groups=("infra",))

    def approver_identity(self, approval_ctx: object) -> ApproverIdentity:
        return ApproverIdentity(subject=self._subject, groups=("infra",))


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


def _prepare(store: InMemoryOperationStore, *, requester_subject: str = "requester-1") -> str:
    provider = FakeProvider()
    service = PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        policy_layers=_layers(),
    )
    draft = DraftedChange(
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
    result = service.prepare(draft, requester=RequesterIdentity(subject=requester_subject, groups=("infra",)))
    assert result.status == "prepared" and result.operation_id
    return str(result.operation_id)


@pytest.mark.parametrize("source", ["chat", "model", "orchestrator", "specialist", "", "WEB", "tool"])
def test_untrusted_sources_are_rejected(source: str) -> None:
    """Any source outside the trusted web/API set is rejected as ``untrusted_source``."""
    store = InMemoryOperationStore()
    operation_id = _prepare(store)
    service = ApprovalService(store=store, identity=DefaultIdentityContract278())

    with pytest.raises(ApprovalRejected) as exc:
        service.approve(
            operation_id,
            approval_ctx={"subject": "approver-1", "groups": ["infra"]},
            source=source,
        )
    assert exc.value.reason == "untrusted_source"
    # No approval transition was recorded on the rejected path.
    assert store.get_latest_approval(operation_id) is None


@pytest.mark.parametrize("source", sorted(TRUSTED_APPROVAL_SOURCES))
def test_trusted_sources_are_accepted(source: str) -> None:
    """The trusted web/API sources are accepted and record an approval transition."""
    store = InMemoryOperationStore()
    operation_id = _prepare(store)
    service = ApprovalService(store=store, identity=DefaultIdentityContract278())

    approval = service.approve(
        operation_id,
        approval_ctx={"subject": "approver-1", "groups": ["infra"]},
        source=source,
    )
    assert approval.approver_identity.subject == "approver-1"
    assert store.get_latest_approval(operation_id) is not None


def test_approver_identity_is_read_from_seam_not_context_payload() -> None:
    """The approver is whatever the #278 seam returns, regardless of the approval-ctx payload.

    The context here carries a spoofed ``subject`` that a model might try to inject; the fixed
    identity seam returns a different, trusted subject, and that trusted subject is what the
    recorded approval binds — proving identity comes from the seam, not from the arguments.
    """
    store = InMemoryOperationStore()
    operation_id = _prepare(store, requester_subject="requester-1")
    trusted_service = ApprovalService(store=store, identity=_FixedIdentity278("trusted-approver"))

    approval = trusted_service.approve(
        operation_id,
        approval_ctx={"subject": "spoofed-approver", "groups": ["admin"]},
        source="web",
    )

    # The recorded approver is the seam-provided identity, not the spoofed context subject.
    assert approval.approver_identity.subject == "trusted-approver"
    assert approval.approver_identity.subject != "spoofed-approver"
    assert store.get_latest_approval(operation_id).approver_identity.subject == "trusted-approver"
