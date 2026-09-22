"""Runtime tests for reject/cancel/expiry approval-lifecycle decisions (#414).

These drive the E2 approval domain beyond the grant path: an authorized
approver may *reject* a pending operation, the requester (or an authorized
principal) may *cancel* a not-yet-terminal operation, and a due operation may be
*expired*. Every decision binds the exact stored ``prepared_hash``, carries no
executable content, uses a ``VerifiedPrincipal`` only, and commits through a
fenced, conditional, atomic store transaction that fails closed under races.
"""

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
    ApprovalErrorCode,
    ApprovalPolicy,
    DecisionCommitOutcome,
    DecisionRequest,
    DecisionRequestContext,
    LifecycleDecisionService,
    StoredPreparedOperation,
)
from operations.contracts import load_json
from operations.contracts.capacity import capacity_prepared_hash
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
NOW = datetime(2026, 9, 21, 19, 15, tzinfo=timezone.utc)
OPERATION_ID = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
REQUEST_ID = "request.decision-1"


class FakeDecisionStore:
    def __init__(self, stored: StoredPreparedOperation | None) -> None:
        self.stored = stored
        self.loaded_operation_ids: list[str] = []
        self.commits: list[dict[str, Any]] = []
        self.commit_outcome: object = DecisionCommitOutcome.RECORDED

    def load_for_decision(self, operation_id: str) -> StoredPreparedOperation | None:
        self.loaded_operation_ids.append(operation_id)
        return self.stored

    def record_terminal_decision(
        self,
        *,
        operation_id: str,
        expected_prepared_operation_hash: str,
        expected_state: str,
        new_state: str,
        commit_not_after: datetime,
        state_change: Any,
        ledger_event: Any,
        approval: Any | None,
    ) -> object:
        self.commits.append(
            {
                "operation_id": operation_id,
                "expected_prepared_operation_hash": expected_prepared_operation_hash,
                "expected_state": expected_state,
                "new_state": new_state,
                "commit_not_after": commit_not_after,
                "state_change": deepcopy(dict(state_change)),
                "ledger_event": deepcopy(dict(ledger_event)),
                "approval": deepcopy(dict(approval)) if approval is not None else None,
            }
        )
        if self.commit_outcome is not DecisionCommitOutcome.RECORDED:
            return self.commit_outcome
        if (
            self.stored is None
            or self.stored.prepared_operation_hash != expected_prepared_operation_hash
            or self.stored.state != expected_state
        ):
            return DecisionCommitOutcome.PRECONDITION_FAILED
        self.stored = StoredPreparedOperation(
            self.stored.copy_prepared_operation(),
            self.stored.prepared_operation_hash,
            new_state,
        )
        return DecisionCommitOutcome.RECORDED


def _operation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")


def _stored(
    operation: dict[str, Any] | None = None,
    *,
    state: str = "pending_approval",
) -> StoredPreparedOperation:
    document = operation or _operation()
    return StoredPreparedOperation(document, capacity_prepared_hash(document), state)


def _principal(**overrides) -> VerifiedPrincipal:
    values = {
        "subject_id": "subject.approver-1",
        "client_id": "client.web-console",
        "audience": "client.web-console",
        "tenant_id": "tenant.default",
        "workspace_id": "workspace.default",
        "expires_at": datetime(2026, 9, 21, 20, 15, tzinfo=timezone.utc),
        "groups": frozenset({"operations-approvers"}),
        "scopes": frozenset(),
    }
    values.update(overrides)
    return VerifiedPrincipal(**values)


def _requester_principal(**overrides) -> VerifiedPrincipal:
    operation = _operation()
    values = {
        "subject_id": operation["requester"]["subject_id"],
        "client_id": operation["requester"]["client_id"],
        "audience": "client.web-console",
        "tenant_id": "tenant.default",
        "workspace_id": "workspace.default",
        "expires_at": datetime(2026, 9, 21, 20, 15, tzinfo=timezone.utc),
        "groups": frozenset(),
        "scopes": frozenset(),
    }
    values.update(overrides)
    return VerifiedPrincipal(**values)


def _policy(**overrides) -> ApprovalPolicy:
    values = {
        "policy_id": "policy.capacity-default",
        "policy_version": "2026-09-01",
        "approver_groups": frozenset({"operations-approvers"}),
    }
    values.update(overrides)
    return ApprovalPolicy(**values)


def _boundary() -> ApprovalIdentityBoundary:
    return ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        requester_client_ids=frozenset({"client.web-console", "client.chat-runtime"}),
        approver_client_ids=frozenset({"client.web-console", "client.approval-cli"}),
        trusted_audiences=frozenset({"client.web-console"}),
    )


def _service(
    store: FakeDecisionStore,
    *,
    policy: ApprovalPolicy | None = None,
    now: datetime = NOW,
    clock: Callable[[], datetime] | None = None,
) -> LifecycleDecisionService:
    return LifecycleDecisionService(
        identity_boundary=_boundary(),
        policy=policy or _policy(),
        store=store,
        clock=clock or (lambda: now),
        decision_id_factory=lambda: "approval.01HZY3N6VQ7X8Y9Z0A1B2C3D4E",
        event_id_factory=lambda: "event.01HZY3N6VQ7X8Y9Z0A1B2C3D4E",
        state_change_id_factory=lambda: "state.01HZY3N6VQ7X8Y9Z0A1B2C3D4E",
    )


def _reject_context(principal: VerifiedPrincipal | None = None) -> DecisionRequestContext:
    return DecisionRequestContext(principal=principal or _principal(), request_id=REQUEST_ID)


def _assert_error(
    call: Callable[[], Any],
    expected: ApprovalErrorCode,
) -> ApprovalBoundaryError:
    with pytest.raises(ApprovalBoundaryError) as error:
        call()
    assert error.value.error_code == expected
    return error.value


# -- Reject --------------------------------------------------------------


def test_reject_records_denied_decision_and_transitions_to_rejected() -> None:
    operation = _operation()
    store = FakeDecisionStore(_stored(operation))
    service = _service(store)

    result = service.reject(DecisionRequest(OPERATION_ID), _reject_context())

    assert result["decision"] == "denied"
    assert result["operation_id"] == OPERATION_ID
    assert result["prepared_operation_hash"] == capacity_prepared_hash(operation)
    assert store.commits[0]["expected_state"] == "pending_approval"
    assert store.commits[0]["new_state"] == "rejected"
    assert store.commits[0]["state_change"]["reason_code"] == "APPROVAL_DENIED"
    assert store.stored is not None and store.stored.state == "rejected"


def test_reject_binds_exact_hash_and_fails_closed_on_race() -> None:
    store = FakeDecisionStore(_stored())
    store.commit_outcome = DecisionCommitOutcome.PRECONDITION_FAILED
    service = _service(store)

    error = _assert_error(
        lambda: service.reject(DecisionRequest(OPERATION_ID), _reject_context()),
        ApprovalErrorCode.STATE_CONFLICT,
    )
    assert error.retryable is False
    assert store.commits[0]["expected_prepared_operation_hash"] == capacity_prepared_hash(_operation())


def test_reject_rejects_unauthorized_principal_before_commit() -> None:
    store = FakeDecisionStore(_stored())
    service = _service(store)

    _assert_error(
        lambda: service.reject(
            DecisionRequest(OPERATION_ID),
            _reject_context(_principal(groups=frozenset({"admin"}))),
        ),
        ApprovalErrorCode.AUTHORIZATION_DENIED,
    )
    assert store.commits == []


def test_reject_denies_requester_self_rejection_by_default() -> None:
    store = FakeDecisionStore(_stored())
    service = _service(store)

    _assert_error(
        lambda: service.reject(DecisionRequest(OPERATION_ID), _reject_context(_requester_principal())),
        ApprovalErrorCode.AUTHORIZATION_DENIED,
    )
    assert store.commits == []


def test_reject_rejects_operation_not_pending() -> None:
    store = FakeDecisionStore(_stored(state="approved"))
    service = _service(store)

    _assert_error(
        lambda: service.reject(DecisionRequest(OPERATION_ID), _reject_context()),
        ApprovalErrorCode.STATE_CONFLICT,
    )
    assert store.commits == []


# -- Cancel --------------------------------------------------------------


def test_cancel_lets_requester_cancel_their_pending_operation() -> None:
    operation = _operation()
    store = FakeDecisionStore(_stored(operation))
    service = _service(store)

    result = service.cancel(DecisionRequest(OPERATION_ID), _reject_context(_requester_principal()))

    assert result["new_state"] == "cancelled"
    assert store.commits[0]["new_state"] == "cancelled"
    assert store.commits[0]["state_change"]["reason_code"] == "CANCELLED"
    assert store.commits[0]["approval"] is None
    assert store.stored is not None and store.stored.state == "cancelled"


@pytest.mark.parametrize("state", ["prepared", "pending_approval", "approved"])
def test_cancel_allowed_from_every_non_terminal_pre_dispatch_state(state: str) -> None:
    store = FakeDecisionStore(_stored(state=state))
    service = _service(store)

    result = service.cancel(DecisionRequest(OPERATION_ID), _reject_context(_requester_principal()))

    assert result["new_state"] == "cancelled"
    assert store.commits[0]["expected_state"] == state


def test_cancel_denies_unrelated_non_approver_principal() -> None:
    store = FakeDecisionStore(_stored())
    service = _service(store)
    stranger = _principal(subject_id="subject.stranger-1", groups=frozenset())

    _assert_error(
        lambda: service.cancel(DecisionRequest(OPERATION_ID), _reject_context(stranger)),
        ApprovalErrorCode.AUTHORIZATION_DENIED,
    )
    assert store.commits == []


def test_cancel_rejects_terminal_operation() -> None:
    store = FakeDecisionStore(_stored(state="succeeded"))
    service = _service(store)

    _assert_error(
        lambda: service.cancel(DecisionRequest(OPERATION_ID), _reject_context(_requester_principal())),
        ApprovalErrorCode.STATE_CONFLICT,
    )
    assert store.commits == []


# -- Expiry --------------------------------------------------------------


def test_expire_transitions_due_pending_operation_to_expired() -> None:
    operation = _operation()
    store = FakeDecisionStore(_stored(operation))
    after_expiry = datetime(2026, 9, 21, 19, 40, tzinfo=timezone.utc)
    service = _service(store, now=after_expiry)

    result = service.expire_if_due(DecisionRequest(OPERATION_ID))

    assert result["new_state"] == "expired"
    assert store.commits[0]["new_state"] == "expired"
    assert store.commits[0]["state_change"]["reason_code"] == "EXPIRED"
    assert store.commits[0]["state_change"]["actor"]["actor_type"] == "system"


def test_expire_is_a_noop_before_the_operation_deadline() -> None:
    store = FakeDecisionStore(_stored())
    service = _service(store, now=NOW)

    result = service.expire_if_due(DecisionRequest(OPERATION_ID))

    assert result is None
    assert store.commits == []


def test_expire_fails_closed_on_race() -> None:
    store = FakeDecisionStore(_stored())
    store.commit_outcome = DecisionCommitOutcome.PRECONDITION_FAILED
    after_expiry = datetime(2026, 9, 21, 19, 40, tzinfo=timezone.utc)
    service = _service(store, now=after_expiry)

    error = _assert_error(
        lambda: service.expire_if_due(DecisionRequest(OPERATION_ID)),
        ApprovalErrorCode.STATE_CONFLICT,
    )
    assert error.retryable is False


# -- Action payload safety ------------------------------------------------


@pytest.mark.parametrize(
    "injected_field",
    ["principal", "approver", "prepared_operation_hash", "decision", "new_state", "actor"],
)
def test_decision_payload_rejects_injected_content(injected_field: str) -> None:
    payload = {"operation_id": OPERATION_ID, injected_field: "untrusted"}

    error = _assert_error(
        lambda: DecisionRequest.from_payload(payload),
        ApprovalErrorCode.APPROVAL_INVALID,
    )
    assert error.error_code == ApprovalErrorCode.APPROVAL_INVALID


def test_decision_binds_expected_prepared_hash_and_conflicts_on_mismatch() -> None:
    store = FakeDecisionStore(_stored())
    service = _service(store)

    _assert_error(
        lambda: service.reject(
            DecisionRequest(OPERATION_ID, expected_prepared_hash="sha256:" + "0" * 64),
            _reject_context(),
        ),
        ApprovalErrorCode.OPERATION_HASH_MISMATCH,
    )
    assert store.commits == []
