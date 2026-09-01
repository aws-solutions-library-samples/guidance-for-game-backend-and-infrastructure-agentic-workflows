"""Identity-boundary and separation-of-duties policy tests for approvals."""

from __future__ import annotations

# Standard library
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

# Third-party packages
import pytest

# Local modules
from operations.approval import ApprovalPolicy, ApprovalPolicyError, ApprovalPolicyReason
from operations.contracts import load_json
from operations.identity import (
    ApprovalIdentityBoundary,
    IdentityBoundaryError,
    IdentityBoundaryReason,
    VerifiedPrincipal,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
NOW = datetime(2026, 8, 13, 20, 5, tzinfo=timezone.utc)


def _operation() -> dict:
    return load_json(FIXTURES / "source-control-prepared-operation.valid.json")


def _principal(**overrides) -> VerifiedPrincipal:
    values = {
        "subject_id": "subject:approver",
        "client_id": "client:operations-web",
        "audience": "client:operations-web",
        "tenant_id": "tenant:example",
        "workspace_id": "workspace:game-platform",
        "expires_at": datetime(2026, 8, 13, 21, 5, tzinfo=timezone.utc),
        "groups": frozenset({"operations-approvers"}),
        "scopes": frozenset(),
    }
    values.update(overrides)
    return VerifiedPrincipal(**values)


def _boundary() -> ApprovalIdentityBoundary:
    return ApprovalIdentityBoundary(
        tenant_id="tenant:example",
        workspace_id="workspace:game-platform",
        requester_client_ids=frozenset({"client:operations-web", "client:chat-runtime"}),
        approver_client_ids=frozenset({"client:operations-web", "client:approval-cli"}),
        trusted_audiences=frozenset({"client:operations-web"}),
    )


def _policy(**overrides) -> ApprovalPolicy:
    values = {
        "policy_id": "policy:source-control",
        "policy_version": "12",
        "approver_groups": frozenset({"operations-approvers"}),
    }
    values.update(overrides)
    return ApprovalPolicy(**values)


def test_verified_principal_is_immutable_and_contract_identity_excludes_claims() -> None:
    principal = _principal(scopes=frozenset({"operations.approve"}))

    with pytest.raises(FrozenInstanceError):
        principal.subject_id = "subject:changed"

    assert principal.contract_identity() == {
        "subject_id": "subject:approver",
        "client_id": "client:operations-web",
        "tenant_id": "tenant:example",
        "workspace_id": "workspace:game-platform",
    }
    assert "audience" not in principal.contract_identity()
    assert "groups" not in principal.contract_identity()
    assert "scopes" not in principal.contract_identity()
    assert "expires_at" not in principal.contract_identity()


def test_identity_boundary_binds_fresh_requester_and_approver_separately() -> None:
    boundary = _boundary()
    requester = _principal(client_id="client:chat-runtime")
    approver = _principal(client_id="client:approval-cli")

    assert boundary.bind_requester(requester, now=NOW)["subject_id"] == "subject:approver"
    assert boundary.bind_approver(approver, now=NOW)["client_id"] == "client:approval-cli"

    with pytest.raises(IdentityBoundaryError) as requester_error:
        boundary.bind_requester(_principal(client_id="client:approval-cli"), now=NOW)
    assert requester_error.value.reason == IdentityBoundaryReason.CLIENT_NOT_TRUSTED

    with pytest.raises(IdentityBoundaryError) as approver_error:
        boundary.bind_approver(_principal(client_id="client:chat-runtime"), now=NOW)
    assert approver_error.value.reason == IdentityBoundaryReason.CLIENT_NOT_TRUSTED


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"expires_at": NOW}, IdentityBoundaryReason.CREDENTIAL_EXPIRED),
        ({"tenant_id": "tenant:other"}, IdentityBoundaryReason.TENANT_MISMATCH),
        ({"workspace_id": "workspace:other"}, IdentityBoundaryReason.WORKSPACE_MISMATCH),
        ({"client_id": "client:untrusted"}, IdentityBoundaryReason.CLIENT_NOT_TRUSTED),
        ({"audience": "client:other"}, IdentityBoundaryReason.AUDIENCE_NOT_TRUSTED),
    ],
)
def test_identity_boundary_rejects_untrusted_approver_context(overrides, reason) -> None:
    with pytest.raises(IdentityBoundaryError) as error:
        _boundary().bind_approver(_principal(**overrides), now=NOW)

    assert error.value.reason == reason


def test_identity_boundary_rechecks_stored_requester_tenant_workspace_and_client() -> None:
    requester = _operation()["requester"]
    _boundary().validate_stored_requester(requester)

    cases = [
        ("tenant_id", "tenant:other", IdentityBoundaryReason.TENANT_MISMATCH),
        ("workspace_id", "workspace:other", IdentityBoundaryReason.WORKSPACE_MISMATCH),
        ("client_id", "client:untrusted", IdentityBoundaryReason.CLIENT_NOT_TRUSTED),
    ]
    for field_name, value, reason in cases:
        changed = {**requester, field_name: value}
        with pytest.raises(IdentityBoundaryError) as error:
            _boundary().validate_stored_requester(changed)
        assert error.value.reason == reason


def test_policy_requires_operations_approval_group_or_scope() -> None:
    operation = _operation()
    _policy().authorize(operation, _principal())
    _policy(
        approver_groups=frozenset(),
        approver_scopes=frozenset({"operations.approve"}),
    ).authorize(operation, _principal(groups=frozenset(), scopes=frozenset({"operations.approve"})))

    for claims in (
        {"groups": frozenset({"admin"}), "scopes": frozenset()},
        {"groups": frozenset(), "scopes": frozenset({"operations.read"})},
    ):
        with pytest.raises(ApprovalPolicyError) as error:
            _policy().authorize(operation, _principal(**claims))
        assert error.value.reason == ApprovalPolicyReason.APPROVER_NOT_AUTHORIZED


def test_policy_denies_self_approval_by_subject_even_through_another_client() -> None:
    operation = _operation()
    requester_subject = operation["requester"]["subject_id"]
    approver = _principal(subject_id=requester_subject, client_id="client:approval-cli")

    with pytest.raises(ApprovalPolicyError) as error:
        _policy().authorize(operation, approver)

    assert error.value.reason == ApprovalPolicyReason.SELF_APPROVAL_RISK_NOT_ELIGIBLE


def test_policy_allows_only_explicit_low_risk_self_approval_action() -> None:
    operation = _operation()
    operation["calculated_risk"] = {"level": "low", "score": 10, "factors": ["review-only-change"]}
    approver = _principal(subject_id=operation["requester"]["subject_id"])

    with pytest.raises(ApprovalPolicyError) as disabled_error:
        _policy().authorize(operation, approver)
    assert disabled_error.value.reason == ApprovalPolicyReason.SELF_APPROVAL_NOT_ALLOWED

    _policy(low_risk_self_approval_actions=frozenset({operation["action"]})).authorize(operation, approver)

    wrong_action = deepcopy(operation)
    wrong_action["action"] = "source-control.other-action"
    with pytest.raises(ApprovalPolicyError) as action_error:
        _policy(low_risk_self_approval_actions=frozenset({operation["action"]})).authorize(wrong_action, approver)
    assert action_error.value.reason == ApprovalPolicyReason.SELF_APPROVAL_NOT_ALLOWED


def test_policy_never_allows_higher_risk_self_approval() -> None:
    operation = _operation()
    approver = _principal(subject_id=operation["requester"]["subject_id"])
    policy = _policy(low_risk_self_approval_actions=frozenset({operation["action"]}))

    for risk_level in ("moderate", "high", "critical"):
        operation["calculated_risk"]["level"] = risk_level
        with pytest.raises(ApprovalPolicyError) as error:
            policy.authorize(operation, approver)
        assert error.value.reason == ApprovalPolicyReason.SELF_APPROVAL_RISK_NOT_ELIGIBLE


def test_policy_version_and_identifier_must_match_prepared_operation() -> None:
    operation = _operation()

    for policy in (_policy(policy_version="13"), _policy(policy_id="policy:other")):
        with pytest.raises(ApprovalPolicyError) as error:
            policy.authorize(operation, _principal())
        assert error.value.reason == ApprovalPolicyReason.POLICY_MISMATCH
