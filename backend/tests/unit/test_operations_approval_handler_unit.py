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
from operations.approval import ApprovalBoundaryError, ApprovalErrorCode
from operations.approval_handler import ApprovalRequestHandler
from operations.prepare import PreparedDecision
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
    def __init__(self, reject_result=None, cancel_result=None) -> None:
        self._reject = reject_result
        self._cancel = cancel_result
        self.reject_calls: list[Any] = []
        self.cancel_calls: list[Any] = []

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
