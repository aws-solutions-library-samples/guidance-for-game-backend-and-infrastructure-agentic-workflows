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
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

# Local modules
from operations.approval import (
    ApprovalBoundaryError,
    ApprovalErrorCode,
    ApprovalRequest,
    ApprovalRequestContext,
)
from operations.approval_store import ApprovalStoreError
from operations.claims import parse_group_claim, parse_scope_claim
from operations.contracts import CONTRACT_VERSION
from operations.contracts.canonical import CanonicalizationError, canonicalize, load_json
from operations.decisions import DecisionRequest, DecisionRequestContext
from operations.identity import VerifiedPrincipal
from operations.observation import ObservationStoreReadError
from operations.prepare import PreparedDecision
from operations.prepare_orchestrator import PrepareOrchestratorError, PrepareResult

# Transient store faults and the typed E1 read error are retryable service-side
# conditions, mapped to a typed retryable 503 rather than a non-retryable 500.
_RETRYABLE_STORE_FAULTS = (ApprovalStoreError, ObservationStoreReadError)

# Prepare-orchestrator error codes that are service-side failures (not caller
# errors). Only these publish the PreparationFailures metric.
_SERVICE_SIDE_PREPARE_ERRORS = frozenset(
    {"contract_output_invalid", "provider_unavailable", "preparation_expired", "internal_error"}
)

# Approval error codes that are service-side failures (not caller errors). Only
# these publish the ApprovalFailures metric. An integrity/hash mismatch counts;
# authorization denial, invalid input, not-found, policy-stale, state conflict,
# and expiry do not.
_SERVICE_SIDE_APPROVAL_ERRORS = frozenset({ApprovalErrorCode.OPERATION_HASH_MISMATCH})

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

_PREPARE_ROUTE_KEY = "POST /operations/prepare"
_APPROVE_ROUTE_KEY = "POST /operations/{operationId}/approve"
_REJECT_ROUTE_KEY = "POST /operations/{operationId}/reject"
_CANCEL_ROUTE_KEY = "POST /operations/{operationId}/cancel"
_EVIDENCE_ROUTE_KEY = "GET /operations/{operationId}"

# Every E2 request body is a few short fields; bound it before parsing.
_MAX_BODY_BYTES = 4096


class PrepareOrchestratorPort(Protocol):
    def prepare(
        self,
        body: object,
        requester: VerifiedPrincipal,
        *,
        request_id: str,
        correlation_id: str,
        principal_authority: str,
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


class StoreRequestDeadlinePort(Protocol):
    """Durable store port that binds a per-request deadline for one request.

    The adapter binds a monotonic request deadline for the duration of a single
    request so every store read is guarded and every write deadline is capped by
    it. A store with no such binding (a pure in-memory fake) is simply not wired,
    and the handler enforces no store-side deadline.
    """

    def request_deadline(self, deadline: datetime) -> Any: ...


class _NullApprovalMetrics:
    def record(self, event: str) -> None:  # noqa: D401 - no-op default sink
        return None


class HandlerConfigError(RuntimeError):
    """The handler was constructed without a required trusted binding."""


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


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
        requester_groups: frozenset[str],
        metrics: ApprovalMetricsPort | None = None,
        store: StoreRequestDeadlinePort | None = None,
        request_deadline_s: float | None = None,
        min_publish_remaining_s: float = 0.0,
        clock: Callable[[], datetime] = _system_clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not tenant_id or not workspace_id or not trusted_audience:
            raise HandlerConfigError("tenant_id, workspace_id, and trusted_audience are required")
        if not requester_groups:
            raise HandlerConfigError("requester_groups must list at least one group")
        if request_deadline_s is not None and request_deadline_s <= 0:
            raise HandlerConfigError("request_deadline_s must be a positive number of seconds")
        if min_publish_remaining_s < 0:
            raise HandlerConfigError("min_publish_remaining_s must be non-negative")
        self._metrics = metrics or _NullApprovalMetrics()
        self._orchestrator = orchestrator
        self._approval_service = approval_service
        self._decision_service = decision_service
        self._evidence_service = evidence_service
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._trusted_audience = trusted_audience
        self._requester_groups = requester_groups
        # One monotonic per-request deadline derived from the operations budget
        # bounds the whole request. It is threaded into the durable store (reads
        # guarded, writes capped at ``min(request deadline, commit_not_after)``)
        # and gates a metric publish that would not fit, so a slow sequence of
        # store calls returns a typed retryable error before the Lambda timeout.
        self._store = store
        self._request_deadline_s = request_deadline_s
        self._min_publish_remaining_s = min_publish_remaining_s
        self._clock = clock
        self._monotonic = monotonic
        self._request_start = monotonic()

    def _principal_authority(self, principal: VerifiedPrincipal) -> str:
        """Derive requester authority from the verified access-token groups.

        A caller whose verified ``cognito:groups`` intersect the server-owned
        requester allowlist derives ``advise`` authority; a caller with no
        intersecting group derives ``disabled``. The groups come only from the
        authorizer-verified claims, never from request data. Prepare, cancel,
        and evidence require ``advise``; approve and reject keep the separate
        approver-group gate in the approval policy.
        """
        return "advise" if (principal.groups & self._requester_groups) else "disabled"

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one API Gateway HTTP API (payload v2) invocation."""
        stage = ["dispatch"]
        # Take one monotonic request-entry clock and derive a wall-clock request
        # deadline from it. The deadline is bound to the durable store for the
        # whole request (guarding reads and capping writes), and the monotonic
        # start gates a metric publish that would not fit the remaining budget.
        self._request_start = self._monotonic()
        store_scope = self._store_request_deadline()
        try:
            with store_scope:
                return self._dispatch(event, stage)
        except ApprovalBoundaryError as exc:
            self._emit_failure_metric(stage[0], exc.error_code)
            return _approval_error_response(exc)
        except PrepareOrchestratorError as exc:
            self._emit_prepare_failure_metric(stage[0], exc.error_code)
            return _prepare_error_response(exc)
        except _RETRYABLE_STORE_FAULTS as exc:  # noqa: BLE001 - transient store fault
            # A transient store fault (throttle, transaction conflict, capacity
            # limit), the per-request deadline elapsing on a read, or a typed E1
            # read error is retryable, not an internal bug: surface it as a typed
            # retryable 503 and count it as a service-side failure.
            self._emit_service_fault_metric(stage[0])
            _log_unexpected_exception(stage[0], _method(event), exc)
            return _error_response(503, "PROVIDER_UNAVAILABLE", "operation request is temporarily unavailable", True)
        except Exception as exc:  # noqa: BLE001 - never leak an internal detail
            self._emit_service_fault_metric(stage[0])
            _log_unexpected_exception(stage[0], _method(event), exc)
            return _error_response(500, "INTERNAL_ERROR", "operation request failed", False)

    def _store_request_deadline(self) -> Any:
        """Bind the per-request store deadline, or a no-op when unavailable.

        The deadline is a wall-clock instant ``now + request_deadline_s`` so the
        store (which fences on ``commit_not_after`` datetimes) can cap writes and
        guard reads against it. When no store or budget is configured — a unit
        test driving fake services — the scope is a no-op.
        """
        if self._store is None or self._request_deadline_s is None:
            return nullcontext()
        deadline = self._clock().astimezone(timezone.utc) + timedelta(seconds=self._request_deadline_s)
        return self._store.request_deadline(deadline)

    def _publish_fits(self) -> bool:
        """Whether enough request budget remains to publish a metric now.

        A metric publish is a synchronous call with its own cost; when fewer than
        ``min_publish_remaining_s`` of the request budget remain, it is skipped so
        it cannot push the request past the Lambda timeout. With no budget
        configured, a publish always fits.
        """
        if self._request_deadline_s is None:
            return True
        elapsed = self._monotonic() - self._request_start
        remaining = self._request_deadline_s - elapsed
        return remaining >= self._min_publish_remaining_s

    def _record_metric(self, event: str) -> None:
        """Publish one bounded E2 metric, skipping it when the budget is spent."""
        if not self._publish_fits():
            return
        try:
            self._metrics.record(event)
        except Exception:  # noqa: BLE001 - metrics must never break a request
            pass

    def _dispatch(self, event: Mapping[str, Any], stage: list[str]) -> dict[str, Any]:
        method = _method(event)
        route = _route(event)
        route_key = _route_key(event)
        stage[0] = "identity"
        principal = self._verified_principal(event)
        requester_authority = self._principal_authority(principal)

        if method == "POST" and route_key == _PREPARE_ROUTE_KEY:
            stage[0] = "prepare"
            self._require_requester_authority(requester_authority)
            return self._handle_prepare(event, principal, requester_authority)
        if method == "POST" and route_key == _APPROVE_ROUTE_KEY:
            stage[0] = "approve"
            self._expire_if_due(event)
            return self._handle_approve(event, principal)
        if method == "POST" and route_key == _REJECT_ROUTE_KEY:
            stage[0] = "reject"
            self._expire_if_due(event)
            return self._handle_reject(event, principal)
        if method == "POST" and route_key == _CANCEL_ROUTE_KEY:
            stage[0] = "cancel"
            self._require_requester_authority(requester_authority)
            self._expire_if_due(event)
            return self._handle_cancel(event, principal)
        if method == "GET" and route_key == _EVIDENCE_ROUTE_KEY:
            stage[0] = "evidence"
            self._require_requester_authority(requester_authority)
            self._expire_if_due(event)
            return self._handle_evidence(event, principal)
        raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "unsupported route")

    def _require_requester_authority(self, requester_authority: str) -> None:
        """Deny a caller with no allowed requester group before any store read."""
        if requester_authority != "advise":
            raise ApprovalBoundaryError(
                ApprovalErrorCode.AUTHORIZATION_DENIED, "caller is not permitted to operate on prepared operations"
            )

    # -- Route handlers ---------------------------------------------------

    def _handle_prepare(
        self, event: Mapping[str, Any], principal: VerifiedPrincipal, requester_authority: str
    ) -> dict[str, Any]:
        body = self._parse_json_body(event)
        request_id = _request_id(event)
        result = self._orchestrator.prepare(
            body,
            principal,
            request_id=request_id,
            correlation_id=request_id,
            principal_authority=requester_authority,
        )
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
            self._record_metric("approval.expired")

    def _decision_request(self, event: Mapping[str, Any]) -> DecisionRequest:
        operation_id = _operation_id(event)
        body = self._parse_json_body(event, allow_empty=True)
        # Build the decision request from the trusted path id plus only the one
        # optional body field the decision contract allows; any other key is an
        # injection attempt and fails closed.
        if not isinstance(body, dict):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "request body is invalid")
        if not set(body).issubset({"expected_prepared_hash"}):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "request body is invalid")
        payload: dict[str, Any] = {"operation_id": operation_id}
        if "expected_prepared_hash" in body:
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
        if len(raw_body.encode("utf-8")) > _MAX_BODY_BYTES:
            # Bound the raw body before parsing so a hostile payload cannot force
            # unbounded work; every E2 request body is a few short fields.
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "request body is invalid")
        try:
            # Strict I-JSON parse: reject duplicate names (last-wins ambiguity),
            # non-finite numbers, and other non-canonical values.
            return load_json(raw_body)
        except (CanonicalizationError, ValueError, TypeError) as exc:
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
        """Emit a bounded E2 failure metric only for a service-side approval error.

        Caller errors (authorization denial, self-approval denial, invalid input,
        not-found, policy-stale, a decision against an already-terminal state) and
        expiry publish no failure metric, so a shakedown's expected 4xx outcomes
        never page. A cancel that loses a fenced conditional race still records the
        informational CancellationConflicts metric.
        """
        try:
            if stage == "cancel" and error_code == ApprovalErrorCode.STATE_CONFLICT:
                self._record_metric("cancellation.conflict")
                return
            if error_code not in _SERVICE_SIDE_APPROVAL_ERRORS:
                return
            if stage == "prepare":
                self._record_metric("preparation.failed")
            elif stage in ("approve", "reject", "cancel"):
                self._record_metric("approval.failed")
        except Exception:  # noqa: BLE001 - metrics must never break a request
            pass

    def _emit_prepare_failure_metric(self, stage: str, error_code: str) -> None:
        """Emit PreparationFailures only for a service-side prepare error."""
        if error_code not in _SERVICE_SIDE_PREPARE_ERRORS:
            return
        if stage == "prepare":
            self._record_metric("preparation.failed")
        elif stage in ("approve", "reject", "cancel"):
            self._record_metric("approval.failed")

    def _emit_service_fault_metric(self, stage: str) -> None:
        """Emit the service-side failure metric for a transient fault or bug."""
        if stage == "prepare":
            self._record_metric("preparation.failed")
        elif stage in ("approve", "reject", "cancel"):
            self._record_metric("approval.failed")


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


def _route_key(event: Mapping[str, Any]) -> str:
    """Return the API Gateway HTTP API ``routeKey`` for this invocation.

    Dispatch is by the gateway's own ``routeKey`` (for example
    ``"POST /operations/prepare"``) so a request that reached the integration on
    an unexpected path is routed to the not-found path rather than mistaken for
    a known route. When the gateway did not supply ``routeKey`` (an invocation
    shaped only with ``http.path``), synthesize the equivalent key from the
    method and path by collapsing the operation-id segment to the templated
    ``{operationId}`` the routes declare, so the two forms dispatch identically.
    """
    route_key = event.get("routeKey")
    if isinstance(route_key, str) and route_key:
        return route_key
    method = _method(event)
    path = _route(event)
    if not method or not path:
        return ""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) == 3 and segments[0] == "operations" and segments[2] in ("approve", "reject", "cancel"):
        return f"{method} /operations/{{operationId}}/{segments[2]}"
    if len(segments) == 2 and segments[0] == "operations" and segments[1] != "prepare":
        return f"{method} /operations/{{operationId}}"
    return f"{method} /{'/'.join(segments)}"


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
    # No usable gateway request id: use a random per-invocation id so two such
    # requests never share a correlation/request identifier.
    return f"apigw.{secrets.token_hex(16)}"


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
