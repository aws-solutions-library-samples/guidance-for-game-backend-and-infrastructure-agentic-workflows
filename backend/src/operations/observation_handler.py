"""API Gateway Lambda handler for the read-only GameLift observation (#413).

This handler is the protocol adapter for the observe phase. It trusts **only**
the API Gateway JWT authorizer context that API Gateway itself populates after
cryptographically verifying the Cognito access token
(``event.requestContext.authorizer.jwt.claims``). It never reads identity from
the request body, query string, or a custom header, so a direct or unattributed
invocation — one without a verified authorizer context — is rejected before any
provider read.

The handler dispatches two routes on the verified caller:

* ``POST /operations/observe`` parses the untrusted body into an
  :class:`~operations.observation.ObservationRequest` (fleet id and idempotency
  token only) and delegates to :meth:`ObservationService.observe`.
* ``GET /operations/{operationId}`` parses the path into a
  :class:`~operations.observation.StatusRequest` and delegates to
  :meth:`ObservationService.get_status`, which enforces trusted workspace
  ownership.

It exposes no provider-write method and maps any typed
:class:`~operations.observation.ObservationBoundaryError` to a bounded
application-error response with the correct HTTP status. Any unexpected
exception is caught and returned as a sanitized generic 500 — a raw stack trace,
provider payload, or internal detail never crosses the boundary.
"""

from __future__ import annotations

# Standard library
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

# Local modules
from operations.contracts import CONTRACT_VERSION
from operations.identity import VerifiedPrincipal
from operations.observation import (
    AuthorityInputs,
    ObservationBoundaryError,
    ObservationErrorCode,
    ObservationRequest,
    ObservationRequestContext,
    ObservationService,
    ObservationStatus,
    StatusRequest,
    StatusRequestContext,
)

# HTTP status for each typed boundary error. Everything else is a generic 500.
_STATUS_BY_ERROR = {
    ObservationErrorCode.CONTRACT_INVALID: 400,
    ObservationErrorCode.IDENTITY_CONTEXT_INVALID: 401,
    ObservationErrorCode.AUTHORIZATION_DENIED: 403,
    ObservationErrorCode.NOT_FOUND: 404,
    ObservationErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ObservationErrorCode.STATE_CONFLICT: 409,
    ObservationErrorCode.PROVIDER_UNAVAILABLE: 503,
    ObservationErrorCode.INTERNAL_ERROR: 500,
}

_OBSERVE_ROUTE = "/operations/observe"


class HandlerConfigError(RuntimeError):
    """The handler was constructed without a required trusted binding."""


class ObservationRequestHandler:
    """Adapter binding API Gateway JWT authorizer context to the service."""

    def __init__(
        self,
        *,
        service: ObservationService,
        tenant_id: str,
        workspace_id: str,
        trusted_audience: str,
        capability_id: str,
        capability_version: str,
        authority_inputs: AuthorityInputs,
    ) -> None:
        if not tenant_id or not workspace_id or not trusted_audience:
            raise HandlerConfigError("tenant_id, workspace_id, and trusted_audience are required")
        self._service = service
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._trusted_audience = trusted_audience
        self._capability_id = capability_id
        self._capability_version = capability_version
        self._authority_inputs = authority_inputs

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one API Gateway HTTP API (payload v2) invocation."""
        try:
            return self._dispatch(event)
        except ObservationBoundaryError as exc:
            return _error_response(exc)
        except Exception:  # noqa: BLE001 - catch-all: never leak an internal detail
            return _error_response(
                ObservationBoundaryError(ObservationErrorCode.INTERNAL_ERROR, "observation request failed")
            )

    def _dispatch(self, event: Mapping[str, Any]) -> dict[str, Any]:
        method = _method(event)
        principal = self._verified_principal(event)
        if method == "POST":
            request = self._parse_observe_request(event)
            context = ObservationRequestContext(
                requester=principal,
                request_id=_request_id(event),
                authority_inputs=self._authority_inputs,
                capability_id=self._capability_id,
                capability_version=self._capability_version,
            )
            observation = self._service.observe(request, context)
            return _json_response(200, observation)
        if method == "GET":
            status_request = self._parse_status_request(event)
            status_context = StatusRequestContext(requester=principal, request_id=_request_id(event))
            status = self._service.get_status(status_request, status_context)
            return _json_response(200, _status_body(status))
        raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "unsupported method")

    def _parse_observe_request(self, event: Mapping[str, Any]) -> ObservationRequest:
        raw_body = event.get("body")
        if event.get("isBase64Encoded"):
            # The observe request is small JSON; a base64 body is unexpected and
            # rejected rather than decoded, keeping the input surface minimal.
            raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "observation request is invalid")
        if not isinstance(raw_body, str) or not raw_body.strip():
            raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "observation request is invalid")
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise ObservationBoundaryError(
                ObservationErrorCode.CONTRACT_INVALID, "observation request is invalid"
            ) from exc
        return ObservationRequest.from_payload(payload)

    def _parse_status_request(self, event: Mapping[str, Any]) -> StatusRequest:
        params = event.get("pathParameters")
        # The GET status route is ``/operations/{operationId}``, so the API
        # Gateway path parameter is ``operationId`` (camelCase).
        operation_id = params.get("operationId") if isinstance(params, Mapping) else None
        if not isinstance(operation_id, str):
            raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "status request is invalid")
        return StatusRequest(operation_id=operation_id)

    def _verified_principal(self, event: Mapping[str, Any]) -> VerifiedPrincipal:
        claims = _authorizer_claims(event)
        if claims is None:
            # No verified authorizer context: a direct or unattributed call.
            raise ObservationBoundaryError(
                ObservationErrorCode.IDENTITY_CONTEXT_INVALID, "request is not attributed to a verified caller"
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
            raise ObservationBoundaryError(
                ObservationErrorCode.IDENTITY_CONTEXT_INVALID, "verified caller context is incomplete"
            )

        try:
            return VerifiedPrincipal(
                subject_id=subject,
                client_id=client_id,
                audience=self._trusted_audience,
                tenant_id=self._tenant_id,
                workspace_id=self._workspace_id,
                expires_at=expires_at,
                groups=_string_set(claims.get("cognito:groups")),
                scopes=_string_set(claims.get("scope")),
            )
        except (ValueError, ObservationBoundaryError) as exc:
            raise ObservationBoundaryError(
                ObservationErrorCode.IDENTITY_CONTEXT_INVALID, "verified caller context is invalid"
            ) from exc


def _status_body(status: ObservationStatus) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status_contract_version": CONTRACT_VERSION,
        "operation_id": status.operation_id,
        "state": status.state.value,
    }
    if status.observation is not None:
        body["observation"] = status.observation
    return body


def _method(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    method = http.get("method") if isinstance(http, Mapping) else None
    if isinstance(method, str) and method:
        return method.upper()
    return ""


def _authorizer_claims(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the JWT claims API Gateway placed in the authorizer context.

    Only the ``requestContext.authorizer.jwt.claims`` shape is trusted. Body,
    query string, and custom headers are never consulted for identity.
    """
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
        # Normalize into the operations identifier space (>= 3 chars, safe set).
        safe = "".join(ch for ch in candidate if ch.isalnum() or ch in "._:-")
        if len(safe) >= 3:
            return f"apigw.{safe}"[:128]
    return "apigw.observation-request"


def _expiry(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return None


def _string_set(value: object) -> frozenset[str]:
    if isinstance(value, str):
        parts = value.split()
    elif isinstance(value, (list, tuple)):
        parts = [item for item in value if isinstance(item, str)]
    else:
        return frozenset()
    return frozenset(item for item in parts if item and len(item) <= 256)


def _json_response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _error_response(error: ObservationBoundaryError) -> dict[str, Any]:
    status = _STATUS_BY_ERROR.get(error.error_code, 500)
    body = {
        "error_contract_version": CONTRACT_VERSION,
        "error_code": error.error_code.value,
        "safe_message": error.safe_message,
        "retryable": error.retryable,
    }
    return _json_response(status, body)
