"""Runtime regression tests for operation-bound authenticated approvals."""

from __future__ import annotations

# Standard library
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Third-party packages
import pytest

# Local modules
from operations.approval import (
    ApprovalBoundaryError,
    ApprovalCommitOutcome,
    ApprovalErrorCode,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalRequestContext,
    ApprovalService,
    StoredPreparedOperation,
)
from operations.contracts import canonical_sha256, load_json, source_control_branch_name, validate_approval_binding
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
NOW = datetime(2026, 8, 13, 20, 5, tzinfo=timezone.utc)
OPERATION_ID = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
APPROVAL_ID = "approval:01HZY3N6VQ7X8Y9Z0A1B2C3D4E"
REQUEST_ID = "request:approval-boundary"


class FakeApprovalStore:
    def __init__(self, stored: StoredPreparedOperation | None) -> None:
        self.stored = stored
        self.loaded_operation_ids: list[str] = []
        self.commits: list[dict[str, Any]] = []
        self.recorded_approvals: list[dict[str, Any]] = []
        self.commit_outcome: object = ApprovalCommitOutcome.RECORDED
        self.commit_time: datetime | None = None

    def load_for_approval(self, operation_id: str) -> StoredPreparedOperation | None:
        self.loaded_operation_ids.append(operation_id)
        return self.stored

    def record_granted_approval(
        self,
        *,
        operation_id: str,
        expected_prepared_operation_hash: str,
        expected_state: str,
        commit_not_after: datetime,
        approval,
    ) -> object:
        self.commits.append(
            {
                "operation_id": operation_id,
                "expected_prepared_operation_hash": expected_prepared_operation_hash,
                "expected_state": expected_state,
                "commit_not_after": commit_not_after,
                "approval": deepcopy(dict(approval)),
            }
        )
        if self.commit_time is not None and self.commit_time >= commit_not_after:
            return ApprovalCommitOutcome.DEADLINE_EXPIRED
        if self.commit_outcome is not ApprovalCommitOutcome.RECORDED:
            return self.commit_outcome
        if (
            self.stored is None
            or self.stored.prepared_operation_hash != expected_prepared_operation_hash
            or self.stored.state != expected_state
        ):
            return ApprovalCommitOutcome.PRECONDITION_FAILED

        self.recorded_approvals.append(deepcopy(dict(approval)))
        self.stored = StoredPreparedOperation(
            self.stored.copy_prepared_operation(),
            self.stored.prepared_operation_hash,
            "approved",
        )
        return ApprovalCommitOutcome.RECORDED


def _operation() -> dict[str, Any]:
    return load_json(FIXTURES / "source-control-prepared-operation.valid.json")


def _stored(
    operation: dict[str, Any] | None = None,
    *,
    operation_hash: str | None = None,
    state: str = "pending_approval",
) -> StoredPreparedOperation:
    document = operation or _operation()
    return StoredPreparedOperation(
        document,
        operation_hash or canonical_sha256(document),
        state,
    )


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


def _policy(**overrides) -> ApprovalPolicy:
    values = {
        "policy_id": "policy:source-control",
        "policy_version": "12",
        "approver_groups": frozenset({"operations-approvers"}),
    }
    values.update(overrides)
    return ApprovalPolicy(**values)


def _service(
    store: FakeApprovalStore,
    *,
    policy: ApprovalPolicy | None = None,
    now: datetime = NOW,
    clock: Callable[[], datetime] | None = None,
) -> ApprovalService:
    boundary = ApprovalIdentityBoundary(
        tenant_id="tenant:example",
        workspace_id="workspace:game-platform",
        requester_client_ids=frozenset({"client:operations-web", "client:chat-runtime"}),
        approver_client_ids=frozenset({"client:operations-web", "client:approval-cli"}),
        trusted_audiences=frozenset({"client:operations-web"}),
    )
    return ApprovalService(
        identity_boundary=boundary,
        policy=policy or _policy(),
        store=store,
        clock=clock or (lambda: now),
        approval_id_factory=lambda: APPROVAL_ID,
    )


def _context(principal: VerifiedPrincipal | None = None) -> ApprovalRequestContext:
    return ApprovalRequestContext(approver=principal or _principal(), request_id=REQUEST_ID)


def _assert_error(
    service: ApprovalService,
    expected: ApprovalErrorCode,
    *,
    request: ApprovalRequest | None = None,
    context: ApprovalRequestContext | None = None,
) -> ApprovalBoundaryError:
    with pytest.raises(ApprovalBoundaryError) as error:
        service.grant(request or ApprovalRequest(OPERATION_ID), context or _context())
    assert error.value.error_code == expected
    return error.value


def test_runtime_records_authenticated_approver_against_exact_stored_hash() -> None:
    operation = _operation()
    expected_hash = canonical_sha256(operation)
    store = FakeApprovalStore(_stored(operation))

    approval = _service(store).grant(ApprovalRequest(OPERATION_ID), _context())

    validate_approval_binding(approval, operation)
    assert store.loaded_operation_ids == [OPERATION_ID]
    assert approval["operation_id"] == OPERATION_ID
    assert approval["prepared_operation_hash"] == expected_hash
    assert approval["approver"] == _principal().contract_identity()
    assert approval["approver"] != operation["requester"]
    assert approval["decided_at"] == "2026-08-13T20:05:00Z"
    assert approval["expires_at"] == "2026-08-13T20:35:00Z"
    assert approval["correlation"] == {
        "correlation_id": operation["correlation"]["correlation_id"],
        "request_id": REQUEST_ID,
    }
    assert store.commits == [
        {
            "operation_id": OPERATION_ID,
            "expected_prepared_operation_hash": expected_hash,
            "expected_state": "pending_approval",
            "commit_not_after": datetime(2026, 8, 13, 20, 35, tzinfo=timezone.utc),
            "approval": approval,
        }
    ]

    assert store.recorded_approvals == [approval]
    approval["approver"]["subject_id"] = "subject:mutated-after-return"
    assert store.commits[0]["approval"]["approver"]["subject_id"] == "subject:approver"


@pytest.mark.parametrize(
    "injected_field",
    [
        "approver",
        "requester",
        "prepared_operation_hash",
        "tenant_id",
        "workspace_id",
        "policy_version",
        "decision",
    ],
)
def test_action_payload_rejects_identity_content_and_policy_injection(injected_field: str) -> None:
    payload = {"operation_id": OPERATION_ID, injected_field: "untrusted"}

    with pytest.raises(ApprovalBoundaryError) as error:
        ApprovalRequest.from_payload(payload)

    assert error.value.error_code == ApprovalErrorCode.APPROVAL_INVALID


def test_runtime_rejects_expired_or_cross_workspace_approver_before_store_lookup() -> None:
    for principal in (
        _principal(expires_at=NOW),
        _principal(tenant_id="tenant:other"),
        _principal(workspace_id="workspace:other"),
        _principal(client_id="client:untrusted"),
        _principal(audience="client:other"),
    ):
        store = FakeApprovalStore(_stored())
        _assert_error(
            _service(store),
            ApprovalErrorCode.IDENTITY_CONTEXT_INVALID,
            context=_context(principal),
        )
        assert store.loaded_operation_ids == []
        assert store.commits == []


def test_runtime_rejects_untrusted_stored_requester_boundary() -> None:
    for field_name, value in (
        ("tenant_id", "tenant:other"),
        ("workspace_id", "workspace:other"),
        ("client_id", "client:untrusted"),
    ):
        operation = _operation()
        operation["requester"][field_name] = value
        store = FakeApprovalStore(_stored(operation))

        _assert_error(_service(store), ApprovalErrorCode.AUTHORIZATION_DENIED)
        assert store.commits == []


def test_runtime_does_not_grant_operations_authority_to_existing_admin_group() -> None:
    store = FakeApprovalStore(_stored())
    context = _context(_principal(groups=frozenset({"admin"})))

    _assert_error(_service(store), ApprovalErrorCode.AUTHORIZATION_DENIED, context=context)
    assert store.commits == []


def test_runtime_denies_self_approval_by_default_even_with_a_second_client() -> None:
    operation = _operation()
    requester_subject = operation["requester"]["subject_id"]
    store = FakeApprovalStore(_stored(operation))
    context = _context(_principal(subject_id=requester_subject, client_id="client:approval-cli"))

    _assert_error(_service(store), ApprovalErrorCode.AUTHORIZATION_DENIED, context=context)
    assert store.commits == []


def test_runtime_allows_only_explicit_low_risk_self_approval() -> None:
    operation = _operation()
    operation["calculated_risk"] = {"level": "low", "score": 10, "factors": ["review-only-change"]}
    requester_subject = operation["requester"]["subject_id"]
    store = FakeApprovalStore(_stored(operation))
    policy = _policy(low_risk_self_approval_actions=frozenset({operation["action"]}))

    approval = _service(store, policy=policy).grant(
        ApprovalRequest(OPERATION_ID),
        _context(_principal(subject_id=requester_subject)),
    )

    assert approval["approver"]["subject_id"] == requester_subject
    assert len(store.commits) == 1


def test_runtime_rejects_stale_policy_binding() -> None:
    store = FakeApprovalStore(_stored())

    _assert_error(_service(store, policy=_policy(policy_version="13")), ApprovalErrorCode.POLICY_STALE)
    assert store.commits == []


def test_runtime_rejects_missing_changed_or_mismatched_operation() -> None:
    missing_store = FakeApprovalStore(None)
    _assert_error(_service(missing_store), ApprovalErrorCode.OPERATION_NOT_FOUND)

    changed = _operation()
    changed["policy"]["policy_version"] = "13"
    stale_hash = canonical_sha256(_operation())
    changed_store = FakeApprovalStore(_stored(changed, operation_hash=stale_hash))
    _assert_error(_service(changed_store), ApprovalErrorCode.OPERATION_HASH_MISMATCH)

    mismatched = _operation()
    mismatched["operation_id"] = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
    mismatched["target"]["proposal_branch"] = source_control_branch_name(mismatched["operation_id"])
    mismatched_store = FakeApprovalStore(_stored(mismatched))
    _assert_error(_service(mismatched_store), ApprovalErrorCode.OPERATION_HASH_MISMATCH)

    assert missing_store.commits == []
    assert changed_store.commits == []
    assert mismatched_store.commits == []


def test_runtime_rejects_expired_profile_invalid_operation_and_wrong_state() -> None:
    operation = _operation()
    operation["expires_at"] = "2026-08-13T20:05:00Z"
    expired_store = FakeApprovalStore(_stored(operation))
    _assert_error(_service(expired_store), ApprovalErrorCode.APPROVAL_EXPIRED)

    invalid = _operation()
    invalid["parameters"]["files"][0]["path"] = "../outside.yaml"
    invalid_store = FakeApprovalStore(_stored(invalid))
    _assert_error(_service(invalid_store), ApprovalErrorCode.APPROVAL_INVALID)

    approved_store = FakeApprovalStore(_stored(state="approved"))
    _assert_error(_service(approved_store), ApprovalErrorCode.STATE_CONFLICT)

    assert expired_store.commits == []
    assert invalid_store.commits == []
    assert approved_store.commits == []


def test_runtime_limits_approval_expiry_to_operation_expiry() -> None:
    store = FakeApprovalStore(_stored())

    approval = _service(
        store,
        now=datetime(2026, 8, 13, 20, 50, tzinfo=timezone.utc),
    ).grant(ApprovalRequest(OPERATION_ID), _context())

    assert approval["expires_at"] == "2026-08-13T21:00:00Z"


def test_runtime_fails_closed_when_conditional_commit_loses_race() -> None:
    store = FakeApprovalStore(_stored())
    store.commit_outcome = ApprovalCommitOutcome.PRECONDITION_FAILED

    error = _assert_error(_service(store), ApprovalErrorCode.STATE_CONFLICT)

    assert error.retryable is False
    assert store.commits[0]["expected_state"] == "pending_approval"
    assert store.commits[0]["expected_prepared_operation_hash"] == canonical_sha256(_operation())


def test_runtime_rejects_untyped_recorded_store_outcome() -> None:
    store = FakeApprovalStore(_stored())
    store.commit_outcome = "recorded"

    error = _assert_error(_service(store), ApprovalErrorCode.STATE_CONFLICT)

    assert error.retryable is False
    assert store.recorded_approvals == []
    assert store.stored is not None
    assert store.stored.state == "pending_approval"


def test_runtime_rejects_sequential_approval_replay() -> None:
    store = FakeApprovalStore(_stored())
    service = _service(store)

    service.grant(ApprovalRequest(OPERATION_ID), _context())
    error = _assert_error(service, ApprovalErrorCode.STATE_CONFLICT)

    assert error.retryable is False
    assert store.loaded_operation_ids == [OPERATION_ID, OPERATION_ID]
    assert len(store.commits) == 1
    assert len(store.recorded_approvals) == 1


def test_runtime_rechecks_credential_and_operation_expiry_before_commit() -> None:
    credential_expires_at = datetime(2026, 8, 13, 20, 6, tzinfo=timezone.utc)
    credential_clock = iter((NOW, credential_expires_at))
    credential_store = FakeApprovalStore(_stored())
    _assert_error(
        _service(credential_store, clock=lambda: next(credential_clock)),
        ApprovalErrorCode.IDENTITY_CONTEXT_INVALID,
        context=_context(_principal(expires_at=credential_expires_at)),
    )

    operation = _operation()
    operation["expires_at"] = "2026-08-13T20:06:00Z"
    operation_clock = iter((NOW, credential_expires_at))
    operation_store = FakeApprovalStore(_stored(operation))
    _assert_error(
        _service(operation_store, clock=lambda: next(operation_clock)),
        ApprovalErrorCode.APPROVAL_EXPIRED,
    )

    assert credential_store.commits == []
    assert operation_store.commits == []


@pytest.mark.parametrize(
    ("principal_expires_at", "commit_time", "expected_deadline"),
    [
        (
            datetime(2026, 8, 13, 21, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 20, 40, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 20, 35, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 8, 13, 20, 20, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 20, 20, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 20, 20, tzinfo=timezone.utc),
        ),
    ],
)
def test_store_enforces_approval_and_credential_commit_deadlines(
    principal_expires_at: datetime,
    commit_time: datetime,
    expected_deadline: datetime,
) -> None:
    store = FakeApprovalStore(_stored())
    store.commit_time = commit_time

    error = _assert_error(
        _service(store),
        ApprovalErrorCode.APPROVAL_EXPIRED,
        context=_context(_principal(expires_at=principal_expires_at)),
    )

    assert error.retryable is False
    assert store.commits[0]["commit_not_after"] == expected_deadline
    assert store.recorded_approvals == []
    assert store.stored is not None
    assert store.stored.state == "pending_approval"


def test_runtime_preserves_positive_subsecond_validity_interval() -> None:
    decided_at = datetime(2026, 8, 13, 20, 59, 59, 999500, tzinfo=timezone.utc)
    store = FakeApprovalStore(_stored())

    approval = _service(store, now=decided_at).grant(ApprovalRequest(OPERATION_ID), _context())

    parsed_decision = datetime.fromisoformat(approval["decided_at"].replace("Z", "+00:00"))
    parsed_expiry = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    assert parsed_decision == decided_at
    assert parsed_expiry > parsed_decision
