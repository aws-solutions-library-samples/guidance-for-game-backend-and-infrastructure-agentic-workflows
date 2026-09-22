"""Unit tests for the E2 approval request handler (issue #414).

The handler is the protocol adapter that routes the E2 approval surface on the
verified API Gateway JWT authorizer context — never on body/query/header
identity. It dispatches:

* ``POST /operations/prepare`` -> prepare orchestrator
* ``POST /operations/{operationId}/approve`` -> ApprovalService.grant
* ``POST /operations/{operationId}/reject`` -> LifecycleDecisionService.reject
* ``POST /operations/{operationId}/cancel`` -> LifecycleDecisionService.cancel
* ``GET  /operations/{operationId}`` -> bounded workspace-scoped E2 evidence

These tests drive the handler with fake services and assert route dispatch,
typed-error -> HTTP status mapping, direct/unattributed identity denial, and
sanitized catch-all behavior. They never touch a live table or provider.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations import approval_handler as _approval_handler_module
from operations.advice import AdviceErrorCode
from operations.approval import ApprovalBoundaryError, ApprovalErrorCode
from operations.approval_handler import ApprovalRequestHandler
from operations.prepare import PreparedDecision, PrepareErrorCode
from operations.prepare_orchestrator import PrepareOrchestratorError, PrepareResult

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)
OPERATION_ID = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
PREPARED_HASH = "sha256:" + "c" * 64


def _claims() -> dict[str, Any]:
    return {
        "sub": "user.requester",
        "client_id": "client.requester",
        "token_use": "access",
        "exp": str(int((NOW + timedelta(hours=1)).timestamp())),
    }


def _event(
    method: str, *, path: str, body: dict | None = None, claims: dict | None = None, path_params: dict | None = None
) -> dict[str, Any]:
    request_context: dict[str, Any] = {"http": {"method": method, "path": path}, "requestId": "apigw-req-1"}
    if claims is not None:
        request_context["authorizer"] = {"jwt": {"claims": claims}}
    event: dict[str, Any] = {"requestContext": request_context, "rawPath": path}
    if body is not None:
        event["body"] = json.dumps(body)
    if path_params is not None:
        event["pathParameters"] = path_params
    return event


class FakeOrchestrator:
    def __init__(self, result: PrepareResult | Exception) -> None:
        self._result = result
        self.calls: list[Any] = []

    def prepare(self, body, requester, *, request_id, correlation_id) -> PrepareResult:
        self.calls.append((body, requester.subject_id, request_id))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeApprovalService:
    def __init__(self, result: dict | Exception) -> None:
        self._result = result
        self.calls: list[Any] = []

    def grant(self, request, context) -> dict[str, Any]:
        self.calls.append((request.operation_id, context.approver.subject_id))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeDecisionService:
    def __init__(self, reject_result=None, cancel_result=None, expire_result=None) -> None:
        self._reject = reject_result
        self._cancel = cancel_result
        # ``expire_result`` models the outcome of the opportunistic lazy-on-access
        # expiry the handler performs before treating an operation as active:
        # ``None`` == not due (no-op), a dict == the transition won, an
        # ApprovalBoundaryError == a race/terminal no-op the handler must swallow.
        self._expire = expire_result
        self.reject_calls: list[Any] = []
        self.cancel_calls: list[Any] = []
        self.expire_calls: list[Any] = []

    def reject(self, request, context) -> dict[str, Any]:
        self.reject_calls.append(request.operation_id)
        if isinstance(self._reject, Exception):
            raise self._reject
        return self._reject

    def cancel(self, request, context) -> dict[str, Any]:
        self.cancel_calls.append(request.operation_id)
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return self._cancel

    def expire_if_due(self, request) -> dict[str, Any] | None:
        self.expire_calls.append(request.operation_id)
        if isinstance(self._expire, Exception):
            raise self._expire
        return self._expire


class FakeEvidenceService:
    def __init__(self, evidence: dict | None) -> None:
        self._evidence = evidence

    def load_evidence(self, *, operation_id, requester) -> dict[str, Any] | None:
        return self._evidence


def _prepared_result(decision=PreparedDecision.APPROVAL_REQUIRED, persisted=True) -> PrepareResult:
    return PrepareResult(
        decision=decision,
        operation={"operation_id": OPERATION_ID, "prepared_hash": PREPARED_HASH, "state": "pending_approval"},
        authorization={"decision": "approval_required"},
        prepared_hash=PREPARED_HASH,
        persisted=persisted,
        replayed=False,
    )


def _handler(**overrides) -> ApprovalRequestHandler:
    defaults: dict[str, Any] = dict(
        orchestrator=FakeOrchestrator(_prepared_result()),
        approval_service=FakeApprovalService({"approval_id": "approval.x", "decision": "granted"}),
        decision_service=FakeDecisionService(
            reject_result={"new_state": "rejected"}, cancel_result={"new_state": "cancelled"}
        ),
        evidence_service=FakeEvidenceService({"operation_id": OPERATION_ID, "state": "pending_approval"}),
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience="operations-api",
    )
    defaults.update(overrides)
    return ApprovalRequestHandler(**defaults)


def test_prepare_route_returns_201_with_operation() -> None:
    handler = _handler()
    resp = handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=_claims()))
    assert resp["statusCode"] == 201
    payload = json.loads(resp["body"])
    assert payload["operation_id"] == OPERATION_ID
    assert payload["decision"] == "approval_required"


def test_prepare_denied_returns_200_with_denied_decision() -> None:
    handler = _handler(orchestrator=FakeOrchestrator(_prepared_result(PreparedDecision.DENIED, persisted=False)))
    resp = handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=_claims()))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["decision"] == "denied"


def test_prepare_conflict_maps_to_409() -> None:
    handler = _handler(orchestrator=FakeOrchestrator(PrepareOrchestratorError("idempotency_conflict", "conflict")))
    resp = handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=_claims()))
    assert resp["statusCode"] == 409


def test_approve_route_dispatches_and_returns_200() -> None:
    handler = _handler()
    resp = handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/approve",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["decision"] == "granted"


def test_approve_self_denied_maps_to_403() -> None:
    handler = _handler(
        approval_service=FakeApprovalService(
            ApprovalBoundaryError(ApprovalErrorCode.AUTHORIZATION_DENIED, "not authorized")
        )
    )
    resp = handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/approve",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    assert resp["statusCode"] == 403


def test_reject_and_cancel_routes_dispatch() -> None:
    handler = _handler()
    reject = handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/reject",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    cancel = handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/cancel",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    assert reject["statusCode"] == 200
    assert cancel["statusCode"] == 200


def test_get_evidence_route_returns_200() -> None:
    handler = _handler()
    resp = handler.handle(
        _event("GET", path=f"/operations/{OPERATION_ID}", claims=_claims(), path_params={"operationId": OPERATION_ID})
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["operation_id"] == OPERATION_ID


def test_get_evidence_not_found_returns_404() -> None:
    handler = _handler(evidence_service=FakeEvidenceService(None))
    resp = handler.handle(
        _event("GET", path=f"/operations/{OPERATION_ID}", claims=_claims(), path_params={"operationId": OPERATION_ID})
    )
    assert resp["statusCode"] == 404


def test_direct_call_without_authorizer_is_denied() -> None:
    handler = _handler()
    resp = handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=None))
    assert resp["statusCode"] == 401


def test_non_access_token_is_denied() -> None:
    claims = _claims()
    claims["token_use"] = "id"
    handler = _handler()
    resp = handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=claims))
    assert resp["statusCode"] == 401


def test_unknown_route_is_rejected() -> None:
    handler = _handler()
    resp = handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/dispatch",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    assert resp["statusCode"] in (400, 404)


# -- E2 metrics emission on failure paths (issue #414) ----------------------


class FakeMetrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)


def test_prepare_conflict_emits_preparation_failure_metric() -> None:
    metrics = FakeMetrics()
    handler = _handler(
        orchestrator=FakeOrchestrator(PrepareOrchestratorError("idempotency_conflict", "conflict")),
        metrics=metrics,
    )
    handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=_claims()))
    assert "preparation.failed" in metrics.events


def test_approve_denied_emits_approval_failure_metric() -> None:
    metrics = FakeMetrics()
    handler = _handler(
        approval_service=FakeApprovalService(ApprovalBoundaryError(ApprovalErrorCode.AUTHORIZATION_DENIED, "denied")),
        metrics=metrics,
    )
    handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/approve",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    assert "approval.failed" in metrics.events
    assert "approval.expired" not in metrics.events


def test_approve_expired_emits_expired_metric() -> None:
    metrics = FakeMetrics()
    handler = _handler(
        approval_service=FakeApprovalService(ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_EXPIRED, "expired")),
        metrics=metrics,
    )
    handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/approve",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    assert "approval.expired" in metrics.events


def test_cancel_state_conflict_emits_cancellation_conflict_metric() -> None:
    metrics = FakeMetrics()
    handler = _handler(
        decision_service=FakeDecisionService(
            reject_result={"new_state": "rejected"},
            cancel_result=ApprovalBoundaryError(ApprovalErrorCode.STATE_CONFLICT, "conflict"),
        ),
        metrics=metrics,
    )
    handler.handle(
        _event(
            "POST",
            path=f"/operations/{OPERATION_ID}/cancel",
            body={},
            claims=_claims(),
            path_params={"operationId": OPERATION_ID},
        )
    )
    assert "cancellation.conflict" in metrics.events


def test_successful_prepare_emits_no_failure_metric() -> None:
    metrics = FakeMetrics()
    handler = _handler(metrics=metrics)
    handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=_claims()))
    assert metrics.events == []


# -- Reserved wire-stable error-code ownership ---------------------------------
#
# ``CURRENT_STATE_MISMATCH`` is intentionally not raised anywhere in the advice
# or prepare boundaries today, but both enums declare it as a stable wire value
# and the prepare route owns its 409 mapping. These tests pin that ownership so
# the member cannot be silently dropped (which would break wire compatibility)
# nor lose its status mapping without a deliberate, test-visible change.


def test_current_state_mismatch_enum_members_share_the_stable_wire_value() -> None:
    assert AdviceErrorCode.CURRENT_STATE_MISMATCH.value == "current_state_mismatch"
    assert PrepareErrorCode.CURRENT_STATE_MISMATCH.value == "current_state_mismatch"


def test_prepare_status_map_owns_current_state_mismatch_as_409() -> None:
    status_map = _approval_handler_module._STATUS_BY_PREPARE_ERROR
    assert status_map[AdviceErrorCode.CURRENT_STATE_MISMATCH.value] == 409
    assert status_map[PrepareErrorCode.CURRENT_STATE_MISMATCH.value] == 409


def test_prepare_handler_maps_current_state_mismatch_error_to_409() -> None:
    handler = _handler(
        orchestrator=FakeOrchestrator(
            PrepareOrchestratorError("current_state_mismatch", "observed state does not match")
        )
    )
    resp = handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=_claims()))
    assert resp["statusCode"] == 409


# -- Lazy-on-access expiry wiring (issue #414 E2) ---------------------------
#
# ``LifecycleDecisionService.expire_if_due`` must run in the real API flow so a
# DUE pending/prepared operation is atomically transitioned to ``expired`` (with
# a system actor + ledger) BEFORE evidence or approve/reject/cancel can treat it
# as active. When the expiry transition wins, the handler emits the bounded
# ``ApprovalExpired`` metric. A not-due operation is an untouched no-op; an
# already-terminal operation (or a lost race) is a safe no-op the handler
# swallows so the underlying route still yields its own bounded response.

_EXPIRE_STATE_CHANGE = {"new_state": "expired", "previous_state": "pending_approval"}


def _op_event(method: str, suffix: str = "") -> dict[str, Any]:
    path = f"/operations/{OPERATION_ID}{suffix}"
    return _event(
        method,
        path=path,
        body=None if method == "GET" else {},
        claims=_claims(),
        path_params={"operationId": OPERATION_ID},
    )


def test_get_evidence_runs_expiry_before_returning_state() -> None:
    decision = FakeDecisionService(expire_result=_EXPIRE_STATE_CHANGE)
    handler = _handler(decision_service=decision)
    resp = handler.handle(_op_event("GET"))
    # Expiry was attempted for exactly this operation before evidence was read.
    assert decision.expire_calls == [OPERATION_ID]
    # The route still returns its evidence body (200 here from the fake).
    assert resp["statusCode"] == 200


def test_approve_runs_expiry_before_grant() -> None:
    decision = FakeDecisionService(expire_result=_EXPIRE_STATE_CHANGE)
    handler = _handler(decision_service=decision)
    handler.handle(_op_event("POST", "/approve"))
    assert decision.expire_calls == [OPERATION_ID]


def test_reject_runs_expiry_before_decision() -> None:
    decision = FakeDecisionService(reject_result={"new_state": "rejected"}, expire_result=None)
    handler = _handler(decision_service=decision)
    handler.handle(_op_event("POST", "/reject"))
    assert decision.expire_calls == [OPERATION_ID]


def test_cancel_runs_expiry_before_decision() -> None:
    decision = FakeDecisionService(cancel_result={"new_state": "cancelled"}, expire_result=None)
    handler = _handler(decision_service=decision)
    handler.handle(_op_event("POST", "/cancel"))
    assert decision.expire_calls == [OPERATION_ID]


def test_expiry_transition_win_emits_approval_expired_metric() -> None:
    metrics = FakeMetrics()
    decision = FakeDecisionService(expire_result=_EXPIRE_STATE_CHANGE)
    handler = _handler(decision_service=decision, metrics=metrics)
    handler.handle(_op_event("GET"))
    assert "approval.expired" in metrics.events


def test_not_due_expiry_emits_no_metric_and_leaves_route_untouched() -> None:
    metrics = FakeMetrics()
    decision = FakeDecisionService(expire_result=None)
    handler = _handler(decision_service=decision, metrics=metrics)
    resp = handler.handle(_op_event("GET"))
    assert decision.expire_calls == [OPERATION_ID]
    assert "approval.expired" not in metrics.events
    assert resp["statusCode"] == 200


def test_terminal_operation_expiry_is_a_swallowed_no_op() -> None:
    # An already-terminal operation makes expire_if_due raise STATE_CONFLICT; the
    # handler must swallow it (expiry is a safe no-op for terminal ops) so the
    # underlying route runs and produces its own bounded response, not a 409 from
    # the opportunistic expiry attempt.
    metrics = FakeMetrics()
    decision = FakeDecisionService(
        cancel_result=ApprovalBoundaryError(ApprovalErrorCode.STATE_CONFLICT, "conflict"),
        expire_result=ApprovalBoundaryError(ApprovalErrorCode.STATE_CONFLICT, "already terminal"),
    )
    handler = _handler(decision_service=decision, metrics=metrics)
    resp = handler.handle(_op_event("POST", "/cancel"))
    # The 409 comes from the cancel decision (the real route), and expiry did not
    # fabricate an ApprovalExpired metric for a terminal no-op.
    assert resp["statusCode"] == 409
    assert "approval.expired" not in metrics.events
    assert decision.cancel_calls == [OPERATION_ID]


def test_expiry_not_found_no_op_lets_route_return_404() -> None:
    # If the operation is gone, the opportunistic expiry OPERATION_NOT_FOUND is
    # swallowed and the evidence route returns its own bounded 404.
    decision = FakeDecisionService(expire_result=ApprovalBoundaryError(ApprovalErrorCode.OPERATION_NOT_FOUND, "gone"))
    handler = _handler(decision_service=decision, evidence_service=FakeEvidenceService(None))
    resp = handler.handle(_op_event("GET"))
    assert resp["statusCode"] == 404


def test_prepare_route_never_runs_expiry() -> None:
    # Prepare creates a new operation; there is nothing to expire and the id is
    # not a path parameter, so the handler must not attempt expiry.
    decision = FakeDecisionService(expire_result=_EXPIRE_STATE_CHANGE)
    handler = _handler(decision_service=decision)
    handler.handle(_event("POST", path="/operations/prepare", body={"x": 1}, claims=_claims()))
    assert decision.expire_calls == []


def test_approve_after_due_returns_bounded_conflict_after_expiry_wins() -> None:
    # When expiry wins the transition to expired, a same-request approve must not
    # grant: the approval service reports APPROVAL_EXPIRED (bounded 409), never a
    # grant. The metric is emitted for the winning expiry transition.
    metrics = FakeMetrics()
    decision = FakeDecisionService(expire_result=_EXPIRE_STATE_CHANGE)
    handler = _handler(
        decision_service=decision,
        approval_service=FakeApprovalService(ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_EXPIRED, "expired")),
        metrics=metrics,
    )
    resp = handler.handle(_op_event("POST", "/approve"))
    assert resp["statusCode"] == 409
    assert "approval.expired" in metrics.events
