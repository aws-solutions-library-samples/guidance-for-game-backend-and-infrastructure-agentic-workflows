"""E4 read handler: capabilities / list / detail (issue #416).

:class:`ControlReadHandler` is the authenticated HTTP boundary for the read-only
E4 surfaces:

* ``GET /operations/capabilities`` — the server-owned capability discovery
  projection;
* ``GET /operations`` — the bounded, workspace-scoped operation list; and
* ``GET /operations/{operationId}`` — the bounded, workspace-scoped detail
  projection.

Like every operations handler it trusts identity ONLY from the API Gateway JWT
authorizer context and binds the ``tenant``/``workspace``/``audience`` from
server-owned config; the token's ``client_id`` must equal the trusted audience.
The workspace is therefore never caller-supplied, so a caller only ever sees its
own workspace's operations. Responses are the bounded, public-safe projections
the services produce; the handler adds no identity or provider data.
"""

from __future__ import annotations

# Standard library
import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

# Local modules
from operations.claims import ClaimParseError, parse_group_claim, parse_scope_claim
from operations.contracts import CONTRACT_VERSION, MAX_PAGE_SIZE
from operations.identity import VerifiedPrincipal

_LOGGER = logging.getLogger(__name__)
_DEFAULT_PAGE_SIZE = 25


class ProjectionServicePort(Protocol):
    def list_operations(self, *, workspace_id: str, page_size: int, cursor: Any = None) -> dict[str, Any]: ...

    def get_detail(self, *, operation_id: str, workspace_id: str) -> dict[str, Any] | None: ...


class DiscoveryServicePort(Protocol):
    def discover(self) -> dict[str, Any]: ...


class ControlReadHandler:
    """Bind API Gateway JWT context to the read-only E4 projections."""

    def __init__(
        self,
        *,
        projection_service: ProjectionServicePort,
        discovery_service: DiscoveryServicePort,
        tenant_id: str,
        workspace_id: str,
        trusted_audience: str,
    ) -> None:
        for name, value in (
            ("tenant_id", tenant_id),
            ("workspace_id", workspace_id),
            ("trusted_audience", trusted_audience),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        self._projection_service = projection_service
        self._discovery_service = discovery_service
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._trusted_audience = trusted_audience

    def handle_capabilities(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return self._guarded(event, self._capabilities)

    def handle_list(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return self._guarded(event, self._list)

    def handle_detail(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return self._guarded(event, self._detail)

    # -- guarded dispatch ------------------------------------------------

    def _guarded(
        self,
        event: Mapping[str, Any],
        action: "Callable[[Mapping[str, Any], VerifiedPrincipal], dict[str, Any]]",
    ) -> dict[str, Any]:
        try:
            principal = self._verified_principal(event)
        except _ReadDenied as exc:
            return _error_response(exc.status, exc.error_code, exc.safe_message)
        except Exception:  # noqa: BLE001 - never leak internals
            _LOGGER.error("read identity resolution failed")
            return _error_response(500, "INTERNAL_ERROR", "read failed")
        try:
            result: dict[str, Any] = action(event, principal)
            return result
        except _ReadDenied as exc:
            return _error_response(exc.status, exc.error_code, exc.safe_message)
        except Exception:  # noqa: BLE001 - never leak internals
            _LOGGER.error("read failed unexpectedly")
            return _error_response(500, "INTERNAL_ERROR", "read failed")

    def _capabilities(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        return _json_response(200, self._discovery_service.discover())

    def _list(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        query = event.get("queryStringParameters") or {}
        page_size = _page_size(query.get("page_size") if isinstance(query, Mapping) else None)
        cursor = query.get("cursor") if isinstance(query, Mapping) else None
        response = self._projection_service.list_operations(
            workspace_id=principal.workspace_id,
            page_size=page_size,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
        )
        return _json_response(200, response)

    def _detail(self, event: Mapping[str, Any], principal: VerifiedPrincipal) -> dict[str, Any]:
        operation_id = _operation_id(event)
        detail = self._projection_service.get_detail(operation_id=operation_id, workspace_id=principal.workspace_id)
        if detail is None:
            raise _ReadDenied(404, "OPERATION_NOT_FOUND", "operation is unavailable")
        return _json_response(200, detail)

    # -- identity --------------------------------------------------------

    def _verified_principal(self, event: Mapping[str, Any]) -> VerifiedPrincipal:
        claims = _authorizer_claims(event)
        if claims is None:
            raise _ReadDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is required")
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
            raise _ReadDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is invalid")
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
            raise _ReadDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is invalid") from exc
        if principal.client_id != self._trusted_audience:
            raise _ReadDenied(403, "AUTHORIZATION_DENIED", "client is not trusted for this workspace")
        return principal


class _ReadDenied(Exception):
    def __init__(self, status: int, error_code: str, safe_message: str) -> None:
        self.status = status
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(safe_message)


def _page_size(raw: object) -> int:
    if isinstance(raw, str) and raw.strip().isdigit():
        return max(1, min(int(raw.strip()), MAX_PAGE_SIZE))
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(1, min(raw, MAX_PAGE_SIZE))
    return _DEFAULT_PAGE_SIZE


def _operation_id(event: Mapping[str, Any]) -> str:
    params = event.get("pathParameters")
    operation_id = params.get("operationId") if isinstance(params, Mapping) else None
    if not isinstance(operation_id, str) or not operation_id.startswith("op_"):
        raise _ReadDenied(400, "OPERATION_INVALID", "operation id is invalid")
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
