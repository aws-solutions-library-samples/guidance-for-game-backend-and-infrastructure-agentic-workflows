"""API Gateway Lambda handler for the E2 approval surface (issue #414).

This is the protocol adapter for the E2 prepare/approval lifecycle. Like the E1
observation handler it trusts **only** the API Gateway JWT authorizer context
(``requestContext.authorizer.jwt.claims``) that API Gateway populates after
cryptographically verifying the Cognito access token. Identity is never read
from the request body, query string, or a custom header, so a direct or
unattributed invocation is rejected before any store read or write.

Routes dispatched on the verified caller:

* ``POST /operations/prepare`` — parse the untrusted capacity proposal (plus the
  bound ``observation_id``) and delegate to the prepare orchestrator.
* ``POST /operations/{operationId}/approve`` — a direct JWT-authenticated
  approver grants; the operation id is taken from the trusted path, never the
  body.
* ``POST /operations/{operationId}/reject`` — an authorized approver denies.
* ``POST /operations/{operationId}/cancel`` — the requester or an approver
  cancels a pre-dispatch operation.
* ``GET /operations/{operationId}`` — return bounded, workspace-scoped E2
  evidence (preview/state/approval/ledger + identifier-only handoff), or 404.

Every typed boundary error maps to its HTTP status; any unexpected exception is
caught and returned as a sanitized generic 500. Responses are serialized with
the project's single canonical (RFC 8785) serializer so a replay is byte-stable.
This module holds no executor credential and performs no provider write.
"""

from __future__ import annotations

# Standard library
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

# Local modules
from operations.approval import (
    ApprovalBoundaryError,
    ApprovalErrorCode,
    ApprovalRequest,
    ApprovalRequestContext,
)
from operations.claims import parse_group_claim, parse_scope_claim
from operations.contracts import CONTRACT_VERSION
from operations.contracts.canonical import CanonicalizationError, canonicalize
from operations.decisions import DecisionRequest, DecisionRequestContext
from operations.identity import VerifiedPrincipal
from operations.prepare import PreparedDecision
from operations.prepare_orchestrator import PrepareOrchestratorError, PrepareResult

# HTTP status for each typed approval boundary error. Everything else is 500.
_STATUS_BY_APPROVAL_ERROR = {
    ApprovalErrorCode.IDENTITY_CONTEXT_INVALID: 401,
    ApprovalErrorCode.AUTHORIZATION_DENIED: 403,
    ApprovalErrorCode.APPROVAL_INVALID: 400,
    ApprovalErrorCode.APPROVAL_EXPIRED: 409,
    ApprovalErrorCode.OPERATION_NOT_FOUND: 404,
    ApprovalErrorCode.OPERATION_HASH_MISMATCH: 409,
    ApprovalErrorCode.POLICY_STALE: 409,
    ApprovalErrorCode.STATE_CONFLICT: 409,
}

# HTTP status for each prepare-orchestrator error code (safe string codes).
_STATUS_BY_PREPARE_ERROR = {
    "contract_invalid": 400,
    "identity_context_invalid": 401,
    "authorization_denied": 403,
    "current_state_unavailable": 409,
    "current_state_stale": 409,
    "current_state_mismatch": 409,
    "target_not_enrolled": 409,
    "advice_stale": 409,
    "idempotency_conflict": 409,
    "preparation_expired": 409,
    "contract_output_invalid": 500,
    "provider_unavailable": 503,
}

_PREPARE_ROUTE = "/operations/prepare"


class PrepareOrchestratorPort(Protocol):
    def prepare(
        self, body: object, requester: VerifiedPrincipal, *, request_id: str, correlation_id: str
    ) -> PrepareResult: ...


class ApprovalServicePort(Protocol):
    def grant(self, request: ApprovalRequest, context: ApprovalRequestContext) -> dict[str, Any]: ...


class DecisionServicePort(Protocol):
    def reject(self, request: DecisionRequest, context: DecisionRequestContext) -> dict[str, Any]: ...
    def cancel(self, request: DecisionRequest, context: DecisionRequestContext) -> dict[str, Any]: ...
    def expire_if_due(self, request: DecisionRequest) -> dict[str, Any] | None: ...


class EvidenceServicePort(Protocol):
    def load_evidence(self, *, operation_id: str, requester: VerifiedPrincipal) -> dict[str, Any] | None: ...


class ApprovalMetricsPort(Protocol):
    """Bounded, identifier-free E2 metrics sink (see e2_metrics)."""

    def record(self, event: str) -> None: ...


class _NullApprovalMetrics:
    def record(self, event: str) -> None:  # noqa: D401 - no-op default sink
        return None


class HandlerConfigError(RuntimeError):
    """The handler was constructed without a required trusted binding."""


class ApprovalRequestHandler:
    """Adapter binding API Gateway JWT authorizer context to the E2 services."""

    def __init__(
        self,
        *,
        orchestrator: PrepareOrchestratorPort,
        approval_service: ApprovalServicePort,
        decision_service: DecisionServicePort,
        evidence_service: EvidenceServicePort,
        tenant_id: str,
        workspace_id: str,
        trusted_audience: str,
        metrics: ApprovalMetricsPort | None = None,
    ) -> None:
        if not tenant_id or not workspace_id or not trusted_audience:
            raise HandlerConfigError("tenant_id, workspace_id, and trusted_audience are required")
        self._metrics = metrics or _NullApprovalMetrics()
        self._orchestrator = orchestrator
        self._approval_service = approval_service
        self._decision_service = decision_service
        self._evidence_service = evidence_service
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._trusted_audience = trusted_audience

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one API Gateway HTTP API (payload v2) invocation."""
        stage = ["dispatch"]
        try:
            return self._dispatch(event, stage)
        except ApprovalBoundaryError as exc:
            self._emit_failure_metric(stage[0], exc.error_code)
            return _approval_error_response(exc)
        except PrepareOrchestratorError as exc:
            self._emit_prepare_failure_metric(stage[0])
            return _prepare_error_response(exc)
        except Exception as exc:  # noqa: BLE001 - never leak an internal detail
            self._emit_prepare_failure_metric(stage[0])
            _log_unexpected_exception(stage[0], _method(event), exc)
            return _error_response(500, "INTERNAL_ERROR", "operation request failed", False)

    def _dispatch(self, event: Mapping[str, Any], stage: list[str]) -> dict[str, Any]:
        method = _method(event)
        route = _route(event)
        stage[0] = "identity"
        principal = self._verified_principal(event)

        if method == "POST" and route == _PREPARE_ROUTE:
            stage[0] = "prepare"
            return self._handle_prepare(event, principal)
        if method == "POST" and route.endswith("/approve"):
            stage[0] = "approve"
            self._expire_if_due(event)
            return self._handle_approve(event, principal)
        if method == "POST" and route.endswith("/reject"):
            stage[0] = "reject"
            self._expire_if_due(event)
            return self._handle_reject(event, principal)
        if method == "POST" and route.endswith("/cancel"):
            stage[0] = "cancel"
            self._expire_if_due(event)
            return self._handle_cancel(event, principal)
        if method == "GET":
            stage[0] = "evidence"
            self._expire_if_due(event)
            return self._handle_evidence(event, principal)
        raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "unsupported route")

    # -- Route handlers ---------------------------------------------------

    def _handle_prepare(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        body = self._parse_json_body(event)
        request_id = _request_id(event)
        result = self._orchestrator.prepare(body, principal, request_id=request_id, correlation_id=request_id)
        status = 201 if result.decision is PreparedDecision.APPROVAL_REQUIRED and result.persisted else 200
        return _json_response(status, _prepare_body(result))

    def _handle_approve(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        operation_id = _operation_id(event)
        request = ApprovalRequest.from_payload({"operation_id": operation_id})
        context = ApprovalRequestContext(approver=principal, request_id=_request_id(event))
        approval = self._approval_service.grant(request, context)
        return _json_response(200, approval)

    def _handle_reject(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        request = self._decision_request(event)
        context = DecisionRequestContext(principal=principal, request_id=_request_id(event))
        return _json_response(200, self._decision_service.reject(request, context))

    def _handle_cancel(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        request = self._decision_request(event)
        context = DecisionRequestContext(principal=principal, request_id=_request_id(event))
        return _json_response(200, self._decision_service.cancel(request, context))

    def _handle_evidence(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        operation_id = _operation_id(event)
        evidence = self._evidence_service.load_evidence(operation_id=operation_id, requester=principal)
        if evidence is None:
            raise ApprovalBoundaryError(ApprovalErrorCode.OPERATION_NOT_FOUND, "operation is unavailable")
        return _json_response(200, evidence)

    def _expire_if_due(self, event: Mapping[str, Any]) -> None:
        """Lazily transition a DUE operation to ``expired`` before it is treated
        as active by evidence or a terminal decision (issue #414).

        Expiry is driven by the trusted system clock inside the decision service,
        needs no caller credential, and is a safe no-op when the operation is not
        yet due (``None``), is already terminal, or loses a fenced race to another
        writer (a bounded :class:`ApprovalBoundaryError` we deliberately swallow).
        The subsequent route then produces the correct post-expiry response — GET
        surfaces the ``expired`` state, and approve/reject/cancel observe the
        terminal state and conflict. When the transition WINS we emit the bounded
        ``ApprovalExpired`` metric; a system-actor ledger entry is written by the
        decision service's atomic commit. Any unexpected error is swallowed here
        so opportunistic expiry never breaks the underlying request; a genuine
        integrity failure will resurface deterministically on the real route.
        """
        try:
            request = DecisionRequest.from_payload({"operation_id": _operation_id(event)})
        except ApprovalBoundaryError:
            # An invalid/absent operation id is handled by the real route.
            return
        try:
            outcome = self._decision_service.expire_if_due(request)
        except ApprovalBoundaryError:
            # Not eligible (already terminal), a lost race, or otherwise not
            # expirable right now: a safe no-op. The real route responds.
            return
        except Exception:  # noqa: BLE001 - opportunistic expiry must never break a request
            return
        if outcome is not None:
            # The expiry transition won: record the bounded ApprovalExpired metric.
            try:
                self._metrics.record("approval.expired")
            except Exception:  # noqa: BLE001 - metrics must never break a request
                pass

    def _decision_request(self, event: Mapping[str, Any]) -> DecisionRequest:
        operation_id = _operation_id(event)
        body = self._parse_json_body(event, allow_empty=True)
        payload: dict[str, Any] = {"operation_id": operation_id}
        if isinstance(body, dict) and "expected_prepared_hash" in body:
            payload["expected_prepared_hash"] = body["expected_prepared_hash"]
        return DecisionRequest.from_payload(payload)

    def _parse_json_body(self, event: Mapping[str, Any], *, allow_empty: bool = False) -> object:
        raw_body = event.get("body")
        if event.get("isBase64Encoded"):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "request body is invalid")
        if raw_body is None or (isinstance(raw_body, str) and not raw_body.strip()):
            if allow_empty:
                return {}
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "request body is invalid")
        if not isinstance(raw_body, str):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "request body is invalid")
        try:
            return json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "request body is invalid") from exc

    def _verified_principal(self, event: Mapping[str, Any]) -> VerifiedPrincipal:
        claims = _authorizer_claims(event)
        if claims is None:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.IDENTITY_CONTEXT_INVALID, "request is not attributed to a verified caller"
            )
        subject = claims.get("sub")
        client_id = claims.get("client_id")
        token_use = claims.get("token_use")
        expires_at = _expiry(claims.get("exp"))
        if (
            not isinstance(subject, str)
            or not isinstance(client_id, str)
            or token_use != "access"
            or expires_at is None
        ):
            raise ApprovalBoundaryError(
                ApprovalErrorCode.IDENTITY_CONTEXT_INVALID, "verified caller context is incomplete"
            )
        try:
            return VerifiedPrincipal(
                subject_id=subject,
                client_id=client_id,
                audience=self._trusted_audience,
                tenant_id=self._tenant_id,
                workspace_id=self._workspace_id,
                expires_at=expires_at,
                groups=parse_group_claim(claims.get("cognito:groups")),
                scopes=parse_scope_claim(claims.get("scope")),
            )
        except ValueError as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.IDENTITY_CONTEXT_INVALID, "verified caller context is invalid"
            ) from exc

    def _emit_failure_metric(self, stage: str, error_code: ApprovalErrorCode) -> None:
        """Emit the bounded E2 failure metric for one typed approval error."""
        try:
            if stage == "prepare":
                self._metrics.record("preparation.failed")
            elif stage == "cancel" and error_code == ApprovalErrorCode.STATE_CONFLICT:
                self._metrics.record("cancellation.conflict")
            elif stage in ("approve", "reject", "cancel"):
                if error_code == ApprovalErrorCode.APPROVAL_EXPIRED:
                    self._metrics.record("approval.expired")
                else:
                    self._metrics.record("approval.failed")
        except Exception:  # noqa: BLE001 - metrics must never break a request
            pass

    def _emit_prepare_failure_metric(self, stage: str) -> None:
        try:
            if stage == "prepare":
                self._metrics.record("preparation.failed")
            elif stage in ("approve", "reject", "cancel"):
                self._metrics.record("approval.failed")
        except Exception:  # noqa: BLE001 - metrics must never break a request
            pass


# -- Response bodies ---------------------------------------------------------


def _prepare_body(result: PrepareResult) -> dict[str, Any]:
    return {
        "operation_contract_version": CONTRACT_VERSION,
        "operation_id": result.operation["operation_id"],
        "decision": result.decision.value,
        "prepared_hash": result.prepared_hash,
        "persisted": result.persisted,
        "replayed": result.replayed,
    }


def _method(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    method = http.get("method") if isinstance(http, Mapping) else None
    if isinstance(method, str) and method:
        return method.upper()
    return ""


def _route(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    path = http.get("path") if isinstance(http, Mapping) else None
    if isinstance(path, str) and path:
        return path
    raw = event.get("rawPath")
    return raw if isinstance(raw, str) else ""


def _operation_id(event: Mapping[str, Any]) -> str:
    params = event.get("pathParameters")
    operation_id = params.get("operationId") if isinstance(params, Mapping) else None
    if not isinstance(operation_id, str):
        raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "operation id is invalid")
    return operation_id


def _authorizer_claims(event: Mapping[str, Any]) -> dict[str, Any] | None:
    request_context = event.get("requestContext")
    if not isinstance(request_context, Mapping):
        return None
    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, Mapping):
        return None
    jwt = authorizer.get("jwt")
    if not isinstance(jwt, Mapping):
        return None
    claims = jwt.get("claims")
    if not isinstance(claims, Mapping):
        return None
    return dict(claims)


def _request_id(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext")
    candidate = request_context.get("requestId") if isinstance(request_context, Mapping) else None
    if isinstance(candidate, str) and candidate:
        safe = "".join(ch for ch in candidate if ch.isalnum() or ch in "._:-")
        if len(safe) >= 3:
            return f"apigw.{safe}"[:128]
    return "apigw.operations-request"


def _expiry(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return None


def _json_response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    try:
        serialized = canonicalize(dict(body)).decode("utf-8")
    except CanonicalizationError:
        logging.getLogger(__name__).error("approval response body was not canonicalizable", extra={"status": status})
        return _error_response(500, "INTERNAL_ERROR", "operation request failed", False)
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": serialized}


def _error_response(status: int, error_code: str, safe_message: str, retryable: bool) -> dict[str, Any]:
    body = {
        "error_contract_version": CONTRACT_VERSION,
        "error_code": error_code,
        "safe_message": safe_message,
        "retryable": retryable,
    }
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": canonicalize(body).decode("utf-8"),
    }


def _approval_error_response(error: ApprovalBoundaryError) -> dict[str, Any]:
    status = _STATUS_BY_APPROVAL_ERROR.get(error.error_code, 500)
    return _error_response(status, error.error_code.value, error.safe_message, error.retryable)


def _prepare_error_response(error: PrepareOrchestratorError) -> dict[str, Any]:
    status = _STATUS_BY_PREPARE_ERROR.get(error.error_code, 500)
    return _error_response(status, error.error_code.upper(), error.safe_message, error.retryable)


# -- Safe diagnostic logging at the handler's unexpected catch-all -----------
#
# Mirrors the E1 observation handler: emit a single BOUNDED, sanitized record
# naming only a fixed event name, the allowlisted dispatch stage, the allowlisted
# HTTP method, the exception TYPE, and the bounded AWS error / cancellation-reason
# codes. It never logs the exception message, str(exc), the event/body, an
# idempotency token, an operation/request id, a path parameter, an ARN, an
# account, provider data, or stack locals, and never attaches a traceback.
_MAX_CODE_LEN = 128
_MAX_REASON_CODES = 16
_DIAG_EVENT = "approval_handler_exception"
_DIAG_FIELD_SEP = " "
_SAFE_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_STAGE_ALLOWLIST = frozenset({"dispatch", "identity", "prepare", "approve", "reject", "cancel", "evidence"})
_METHOD_ALLOWLIST = frozenset({"POST", "GET"})
_LOGGER = logging.getLogger(__name__)


def _safe_token(value: str) -> str:
    return "".join(ch if ch in _SAFE_TOKEN_CHARS else "." for ch in value)


def _allowlisted_stage(stage: str) -> str:
    return stage if stage in _STAGE_ALLOWLIST else "unknown"


def _allowlisted_method(method: str) -> str:
    return method if method in _METHOD_ALLOWLIST else "OTHER"


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    code = response.get("Error", {}).get("Code")
    if not isinstance(code, str) or not code:
        return None
    return code[:_MAX_CODE_LEN]


def _bounded_reason_codes(exc: Exception) -> list[str]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return []
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return []
    codes = {r.get("Code") for r in reasons if isinstance(r, dict)}
    bounded = sorted(code[:_MAX_CODE_LEN] for code in codes if isinstance(code, str) and code and code != "None")
    return bounded[:_MAX_REASON_CODES]


def _log_unexpected_exception(stage: str, method: str, exc: Exception) -> None:
    error_code = _aws_error_code(exc)
    reason_codes = _bounded_reason_codes(exc)
    fields = [
        _DIAG_EVENT,
        "stage=" + _allowlisted_stage(stage),
        "http_method=" + _allowlisted_method(method),
        "exception_type=" + _safe_token(type(exc).__name__),
        "aws_error_code=" + (_safe_token(error_code) if error_code else "none"),
        "cancellation_reason_codes="
        + (",".join(_safe_token(code) for code in reason_codes) if reason_codes else "none"),
    ]
    _LOGGER.warning(_DIAG_FIELD_SEP.join(fields))
