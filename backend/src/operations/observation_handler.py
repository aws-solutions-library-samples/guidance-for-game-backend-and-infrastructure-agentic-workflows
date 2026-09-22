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
import logging
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
        # ``stage`` is a single-element list mutated by ``_dispatch`` as it
        # advances, so the unexpected catch-all can name WHERE the failure
        # surfaced using only a static, allowlisted literal — never a
        # request-derived value.
        stage = ["dispatch"]
        try:
            return self._dispatch(event, stage)
        except ObservationBoundaryError as exc:
            # A typed boundary error is an EXPECTED outcome, not an unexpected
            # internal failure; it maps to its HTTP status and is never logged
            # through the unexpected-diagnostic channel.
            return _error_response(exc)
        except Exception as exc:  # noqa: BLE001 - catch-all: never leak an internal detail
            _log_unexpected_exception(stage[0], _method(event), exc)
            return _error_response(
                ObservationBoundaryError(ObservationErrorCode.INTERNAL_ERROR, "observation request failed")
            )

    def _dispatch(self, event: Mapping[str, Any], stage: list[str]) -> dict[str, Any]:
        method = _method(event)
        stage[0] = "identity"
        principal = self._verified_principal(event)
        if method == "POST":
            stage[0] = "parse_observe"
            request = self._parse_observe_request(event)
            context = ObservationRequestContext(
                requester=principal,
                request_id=_request_id(event),
                authority_inputs=self._authority_inputs,
                capability_id=self._capability_id,
                capability_version=self._capability_version,
            )
            stage[0] = "service_observe"
            observation = self._service.observe(request, context)
            return _json_response(200, observation)
        if method == "GET":
            stage[0] = "parse_status"
            status_request = self._parse_status_request(event)
            status_context = StatusRequestContext(requester=principal, request_id=_request_id(event))
            stage[0] = "service_status"
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


# -- Safe diagnostic logging at the handler's unexpected catch-all (#413) ------
#
# Live diagnosis of the deployed E1 observe path found the protocol adapter's
# bare ``except Exception`` maps any unexpected error to a sanitized generic 500
# and returns, emitting NO signal — the same blind spot #413 root-caused at the
# store, one layer up. This boundary emits a single BOUNDED, sanitized record
# naming only: a fixed event name, the request STAGE (an allowlisted literal),
# the HTTP METHOD (an allowlisted literal), the exception TYPE, and the bounded
# AWS ``Error.Code`` / transaction cancellation-reason codes when a
# botocore-shaped exception carries them.
#
# It is deliberately stricter than #409's general ``logger.exception`` policy:
# the handler processes an untrusted event, body, idempotency token, operation
# id, and path parameter, any of which can appear in an exception MESSAGE or in
# stack locals. So it emits sanitized METADATA ONLY: never the exception
# message, ``str(exc)``, the event/body, an id/token/ARN/account, a traceback,
# or ``exc_info``. Bounded lengths cap any adversarial code a provider returns.
_MAX_CODE_LEN = 128
_MAX_REASON_CODES = 16

# The emit uses the Python standard-library ``logging`` module, NOT loguru. This
# handler ships in the minimal E1 observe Lambda whose dependency closure
# deliberately excludes loguru; importing it here would break the real package
# import before deployment (the #409/#413 regression). ``logging`` is always
# present in the Lambda runtime, so the diagnostic stays Lambda-safe without
# expanding that closure.
#
# The whole record lives in the ``LogRecord`` message string: a CloudWatch/Lambda
# handler renders ``%(message)s`` and carries no structured ``extra`` mapping, so
# a datum bound as ``extra`` would be dropped before it reached CloudWatch. Any
# provider-defined code echoed into that string is reduced to ``[A-Za-z0-9._-]``
# (anything else becomes ``.``), keeping the record a single, unambiguous line
# and foreclosing log-forging via an embedded newline, separator, or brace.
_DIAG_EVENT = "observation_handler_exception"
_DIAG_FIELD_SEP = " "
_SAFE_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

# The strict allowlist of dispatch stages. Any value outside it collapses to
# ``"unknown"`` so a stage token is always a fixed literal, never free text.
_STAGE_ALLOWLIST = frozenset(
    {
        "dispatch",
        "identity",
        "parse_observe",
        "service_observe",
        "parse_status",
        "service_status",
    }
)

# The strict allowlist of HTTP methods. The two routed verbs render verbatim;
# every other (or empty/hostile) method collapses to ``"OTHER"``.
_METHOD_ALLOWLIST = frozenset({"POST", "GET"})

# The handler-boundary diagnostic channel (stdlib logging, Lambda-safe).
_LOGGER = logging.getLogger(__name__)


def _safe_token(value: str) -> str:
    """Reduce a bounded provider token to a single-line, separator-safe form."""
    return "".join(ch if ch in _SAFE_TOKEN_CHARS else "." for ch in value)


def _allowlisted_stage(stage: str) -> str:
    return stage if stage in _STAGE_ALLOWLIST else "unknown"


def _allowlisted_method(method: str) -> str:
    return method if method in _METHOD_ALLOWLIST else "OTHER"


def _aws_error_code(exc: Exception) -> str | None:
    """The bounded AWS error code from a botocore-shaped exception, or None.

    Reads only ``exc.response["Error"]["Code"]`` (a short, provider-defined
    token). Never reads the error MESSAGE.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    code = response.get("Error", {}).get("Code")
    if not isinstance(code, str) or not code:
        return None
    return code[:_MAX_CODE_LEN]


def _bounded_reason_codes(exc: Exception) -> list[str]:
    """The bounded, sorted set of transaction cancellation-reason codes.

    Reads only the ``Code`` of each entry in ``CancellationReasons`` (dropping
    the inert ``"None"`` marker). Never reads any reason ``Message``.
    """
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
    """Emit a bounded, sanitized diagnostic for an UNEXPECTED handler failure.

    Logs ONLY a fixed event name, the allowlisted dispatch stage, the
    allowlisted HTTP method, the exception TYPE, and the bounded AWS error /
    cancellation-reason codes. It never logs the exception message, ``str(exc)``,
    the request event/body, an idempotency token, an operation/request id, a
    path parameter, an ARN, an account, provider data, or stack locals, and it
    never attaches a traceback or ``exc_info``.

    ``stage`` / ``method`` are coerced through strict allowlists to fixed
    literals and ``exception_type`` is a Python class name, so only the two
    provider-controlled tokens (the AWS error code and each cancellation-reason
    code) are passed through :func:`_safe_token`. The record is a fully-formed
    ``key=value`` string emitted with no ``args`` — ``logging`` never runs
    ``%``-formatting over it — and no ``exc_info``.
    """
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
