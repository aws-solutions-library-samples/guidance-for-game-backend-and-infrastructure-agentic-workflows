"""Admin control HTTP handler (issue #416, E4).

:class:`ControlRequestHandler` is the authenticated HTTP boundary that applies an
admin kill-switch control (``POST /operations/control``). It mirrors the
E1/E2/E3 handlers' identity contract exactly:

* Identity comes ONLY from the API Gateway JWT authorizer context
  (``requestContext.authorizer.jwt.claims``) — never the request body, headers,
  or path.
* A Cognito *access* token carries ``sub``/``client_id``/``token_use``/``exp``
  (plus strictly-parsed ``cognito:groups``/``scope``); the ``audience``,
  ``tenant_id``, and ``workspace_id`` of the :class:`VerifiedPrincipal` are bound
  from server-owned config. The token's ``client_id`` must equal the trusted
  audience or the request is denied.
* The acting admin must carry the server-owned admin group.

The request body carries ONLY the desired booleans plus ``expected_config_version``
(compare-and-set). It never carries identity: an ``actor``/identity field makes
the body fail its ``additionalProperties: false`` contract and is refused with a
400. The response likewise carries no identity — the acting admin is resolved
from the verified principal and handed to the control service out of band, which
records it only in the immutable audit store.

Outcome mapping: ``applied`` -> 200, ``version_conflict`` -> 409,
``denied`` -> 403; an authorization/identity failure -> 401/403; a malformed body
-> 400; an unexpected failure -> 500 (never leaking internals).
"""

from __future__ import annotations

# Standard library
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

# Local modules
from operations.claims import ClaimParseError, parse_group_claim, parse_scope_claim
from operations.contracts import CONTRACT_VERSION
from operations.control.control_service import ControlServiceError
from operations.identity import VerifiedPrincipal

_LOGGER = logging.getLogger(__name__)

_OUTCOME_STATUS = {"applied": 200, "version_conflict": 409, "denied": 403}


class ControlServicePort(Protocol):
    def apply(self, *, request: dict[str, Any], principal: Any, current_document: Any = None) -> dict[str, Any]: ...


class ControlRequestHandler:
    """Bind API Gateway JWT context to one admin control-service call."""

    def __init__(
        self,
        *,
        control_service: ControlServicePort,
        tenant_id: str,
        workspace_id: str,
        trusted_audience: str,
        admin_group: str,
    ) -> None:
        for name, value in (
            ("tenant_id", tenant_id),
            ("workspace_id", workspace_id),
            ("trusted_audience", trusted_audience),
            ("admin_group", admin_group),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        self._control_service = control_service
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._trusted_audience = trusted_audience
        self._admin_group = admin_group

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        try:
            principal = self._verified_principal(event)
        except _ControlDenied as exc:
            return _error_response(exc.status, exc.error_code, exc.safe_message)
        except Exception:  # noqa: BLE001 - never leak internals
            _LOGGER.error("control identity resolution failed")
            return _error_response(500, "INTERNAL_ERROR", "control failed")

        try:
            self._require_admin(principal)
            request = _parse_body(event)
            response = self._control_service.apply(request=request, principal=principal)
        except _ControlDenied as exc:
            return _error_response(exc.status, exc.error_code, exc.safe_message)
        except ControlServiceError as exc:
            return _error_response(_service_status(exc), exc.error_code, "control could not be applied")
        except Exception:  # noqa: BLE001 - never leak internals
            _LOGGER.error("control application failed unexpectedly")
            return _error_response(500, "INTERNAL_ERROR", "control failed")

        status = _OUTCOME_STATUS.get(response.get("outcome", ""), 200)
        return _json_response(status, response)

    # -- identity --------------------------------------------------------

    def _verified_principal(self, event: Mapping[str, Any]) -> VerifiedPrincipal:
        claims = _authorizer_claims(event)
        if claims is None:
            raise _ControlDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is required")

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
            raise _ControlDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is invalid")

        try:
            principal = VerifiedPrincipal(
                subject_id=subject,
                client_id=client_id,
                audience=self._trusted_audience,
                tenant_id=self._tenant_id,
                workspace_id=self._workspace_id,
                expires_at=expires_at,
                groups=parse_group_claim(claims.get("cognito:groups")),
                scopes=parse_scope_claim(claims.get("scope")),
            )
        except (ClaimParseError, ValueError) as exc:
            raise _ControlDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is invalid") from exc

        if principal.client_id != self._trusted_audience:
            raise _ControlDenied(403, "AUTHORIZATION_DENIED", "client is not trusted for this workspace")
        return principal

    def _require_admin(self, principal: VerifiedPrincipal) -> None:
        if self._admin_group not in principal.groups:
            raise _ControlDenied(403, "AUTHORIZATION_DENIED", "control requires the admin group")


def _service_status(exc: ControlServiceError) -> int:
    if exc.error_code == "AUTHORIZATION_DENIED":
        return 403
    if exc.error_code in ("CONTRACT_INVALID", "PHASE_ORDER_INVALID"):
        return 400
    if exc.error_code == "CONTROL_UNAVAILABLE" or exc.error_code == "CONTROL_PUBLISH_FAILED":
        return 503
    return 500


class _ControlDenied(Exception):
    def __init__(self, status: int, error_code: str, safe_message: str) -> None:
        self.status = status
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(safe_message)


def _parse_body(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise _ControlDenied(400, "CONTRACT_INVALID", "control request body is required")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise _ControlDenied(400, "CONTRACT_INVALID", "control request body is invalid") from exc
    if not isinstance(parsed, dict):
        raise _ControlDenied(400, "CONTRACT_INVALID", "control request body must be an object")
    # The body must never carry identity; the control-request contract's
    # additionalProperties:false rejects it, but we also refuse the well-known
    # identity keys early with a clear 400 so an identity-bearing body never
    # reaches the service even if a contract were loosened by mistake.
    for identity_key in ("actor", "principal", "subject_id", "client_id", "identity", "credential"):
        if identity_key in parsed:
            raise _ControlDenied(400, "CONTRACT_INVALID", "control request must not carry identity")
    return parsed


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


def _expiry(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return None


def _json_response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(dict(body), separators=(",", ":"), sort_keys=True),
    }


def _error_response(status: int, error_code: str, safe_message: str) -> dict[str, Any]:
    body = {
        "error_contract_version": CONTRACT_VERSION,
        "error_code": error_code,
        "safe_message": safe_message,
        "retryable": status >= 500,
    }
    return _json_response(status, body)
