"""E3 dispatcher handler: authenticated, admin-only workflow start (#415).

The dispatcher is the authenticated HTTP boundary that starts one execution
workflow. Like the E1/E2 handlers it trusts identity **only** from the API
Gateway JWT authorizer context (``requestContext.authorizer.jwt.claims``) that
API Gateway populates after verifying the Cognito access token — never from the
request body, headers, or path beyond the opaque operation id.

Identity binding (matching the E1 observation and E2 approval handlers)
--------------------------------------------------------------------------
API Gateway forwards the claims of a verified Cognito **access** token. An access
token identifies the calling app client with the ``client_id`` claim and carries
**no** ``aud`` claim and **no** ``custom:tenant_id``/``custom:workspace_id``
claims. Therefore the dispatcher derives from the token only the fields an access
token actually carries — ``sub``, ``client_id``, ``token_use``, ``exp`` (plus the
strictly parsed ``cognito:groups``/``scope``) — and binds the ``audience``,
``tenant_id`` and ``workspace_id`` of the :class:`VerifiedPrincipal` from the
**server-owned deployment configuration**. The trusted audience is the app client
id the deployment was configured to accept; the token's ``client_id`` must equal
it. Any attempt to influence the audience, tenant, or workspace through custom
claims, the request body, or headers is ignored — those inputs never enter the
identity binding.

Its contract is deliberately minimal and closed:

#. Extract the verified principal from the authorizer claims (fail 401 if
   absent/invalid). ``tenant``/``workspace``/``audience`` come from trusted
   config; the token's ``client_id`` must match the trusted audience (else 403).
#. Require the server-owned **admin** group (fail 403 otherwise). Execution is an
   admin action; a plain ``users`` caller can never dispatch.
#. Load the approved, workspace-scoped operation (fail 404 if not visible to the
   caller's workspace, 409 if it is not in the ``approved`` state).
#. Start a Step Functions **Standard** execution with an input of EXACTLY
   ``{"operation_id": <id>}`` and a **stable, deterministic execution name**
   derived from the operation id. No executable content, playbook, credential,
   capacity value, or any handler-derived payload is ever passed — the executor
   reloads and re-verifies everything itself. The stable name makes a duplicate
   dispatch idempotent at the Step Functions layer (a same-name start is a
   no-op/ExecutionAlreadyExists rather than a second run).

The dispatcher performs no provider write, no source-control call, no generic
API/shell/credential access, and no PassRole. It only starts the workflow.
"""

from __future__ import annotations

# Standard library
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

# Local modules
from operations.claims import ClaimParseError, parse_group_claim, parse_scope_claim
from operations.contracts import CONTRACT_VERSION
from operations.identity import VerifiedPrincipal

_LOGGER = logging.getLogger(__name__)
_APPROVED_STATE = "approved"
# Step Functions execution names are bounded to 80 chars; a stable prefix + a
# 32-hex operation digest stays well inside that.
_EXECUTION_NAME_PREFIX = "exec-"


class DispatchStorePort(Protocol):
    """Read-only port exposing the minimal approved-operation dispatch view."""

    def load_dispatch_view(self, operation_id: str) -> dict[str, Any] | None:
        """Return {operation_id, state, tenant_id, workspace_id} or None."""
        ...


class StepFunctionsPort(Protocol):
    """The narrow slice of the boto3 Step Functions client this handler uses."""

    def start_execution(self, **kwargs: Any) -> dict[str, Any]: ...


class DispatcherRequestHandler:
    """Bind API Gateway JWT authorizer context to a single workflow start."""

    def __init__(
        self,
        *,
        store: DispatchStorePort,
        step_functions: StepFunctionsPort,
        state_machine_arn: str,
        tenant_id: str,
        workspace_id: str,
        trusted_audience: str,
        admin_group: str,
        kill_switch_gate: Any = None,
        durable_control_gate: Any = None,
    ) -> None:
        for name, value in (
            ("state_machine_arn", state_machine_arn),
            ("tenant_id", tenant_id),
            ("workspace_id", workspace_id),
            ("trusted_audience", trusted_audience),
            ("admin_group", admin_group),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        self._store = store
        self._sfn = step_functions
        self._state_machine_arn = state_machine_arn
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._trusted_audience = trusted_audience
        self._admin_group = admin_group
        # Optional deployment-wide kill-switch gate (issue #416): the dispatch
        # phase is re-checked before the workflow is started. A None gate is a
        # no-op so the existing E3 dispatcher behavior is preserved.
        self._kill_switch_gate = kill_switch_gate
        self._durable_control_gate = durable_control_gate

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        try:
            principal = self._verified_principal(event)
        except _DispatchDenied as exc:
            return _error_response(exc.status, exc.error_code, exc.safe_message)
        except Exception:  # noqa: BLE001 - never leak internals
            _LOGGER.error("dispatcher identity resolution failed")
            return _error_response(500, "INTERNAL_ERROR", "dispatch failed")

        try:
            operation_id = _operation_id(event)
            self._require_admin(principal)
            self._require_dispatch_phase()
            self._load_authorized_operation(operation_id, principal)
            self._start_workflow(operation_id)
        except _DispatchDenied as exc:
            return _error_response(exc.status, exc.error_code, exc.safe_message)
        except Exception:  # noqa: BLE001 - never leak internals
            _LOGGER.error("dispatch failed unexpectedly")
            return _error_response(500, "INTERNAL_ERROR", "dispatch failed")

        return _json_response(202, {"operation_id": operation_id, "state": "dispatched"})

    # -- Identity --------------------------------------------------------

    def _verified_principal(self, event: Mapping[str, Any]) -> VerifiedPrincipal:
        claims = _authorizer_claims(event)
        if claims is None:
            raise _DispatchDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is required")

        # A Cognito *access* token carries ``sub``, ``client_id``, ``token_use``
        # and ``exp`` — but no ``aud`` and no ``custom:*`` tenant/workspace claims.
        # We derive ONLY those fields from the verified authorizer context; the
        # audience, tenant, and workspace are bound from server-owned config.
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
            raise _DispatchDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is invalid")

        try:
            principal = VerifiedPrincipal(
                subject_id=subject,
                client_id=client_id,
                # Audience/tenant/workspace are server-owned, never token-derived.
                audience=self._trusted_audience,
                tenant_id=self._tenant_id,
                workspace_id=self._workspace_id,
                expires_at=expires_at,
                groups=parse_group_claim(claims.get("cognito:groups")),
                scopes=parse_scope_claim(claims.get("scope")),
            )
        except (ClaimParseError, ValueError) as exc:
            raise _DispatchDenied(401, "IDENTITY_CONTEXT_INVALID", "authentication is invalid") from exc

        # The trusted audience is the app client id the deployment accepts; the
        # access token's own ``client_id`` must match it. A wrong app client is an
        # authorization failure and never reaches the admin gate or any store read.
        if principal.client_id != self._trusted_audience:
            raise _DispatchDenied(403, "AUTHORIZATION_DENIED", "client is not trusted for this workspace")
        return principal

    def _require_admin(self, principal: VerifiedPrincipal) -> None:
        if self._admin_group not in principal.groups:
            raise _DispatchDenied(403, "AUTHORIZATION_DENIED", "dispatch requires the admin group")

    def _require_dispatch_phase(self) -> None:
        """Enforce the kill-switch dispatch phase (fail closed on any denial).

        A None gate is a no-op. When a gate is present a PhaseDenied (disabled
        phase, or an unavailable/invalid/stale kill-switch) becomes a bounded
        403 so no workflow is started under a denied switch.
        """
        if self._kill_switch_gate is None:
            return
        try:
            decision = self._kill_switch_gate.require_phase("dispatch")
            if self._durable_control_gate is not None:
                self._durable_control_gate.require_phase("dispatch", deployed_decision=decision)
        except Exception as exc:  # noqa: BLE001 - any denial fails closed
            raise _DispatchDenied(403, "AUTHORIZATION_DENIED", "dispatch is disabled by the kill-switch") from exc

    def _load_authorized_operation(self, operation_id: str, principal: VerifiedPrincipal) -> dict[str, Any]:
        view = self._store.load_dispatch_view(operation_id)
        if view is None:
            raise _DispatchDenied(404, "OPERATION_NOT_FOUND", "operation is unavailable")
        if view.get("tenant_id") != self._tenant_id or view.get("workspace_id") != self._workspace_id:
            # Do not reveal existence across workspaces.
            raise _DispatchDenied(404, "OPERATION_NOT_FOUND", "operation is unavailable")
        if view.get("state") != _APPROVED_STATE:
            raise _DispatchDenied(409, "STATE_CONFLICT", "operation is not approved for execution")
        return view

    def _start_workflow(self, operation_id: str) -> None:
        # ONLY the operation id crosses to the workflow; nothing executable.
        payload = json.dumps({"operation_id": operation_id}, separators=(",", ":"), sort_keys=True)
        try:
            self._sfn.start_execution(
                stateMachineArn=self._state_machine_arn,
                name=_execution_name(operation_id),
                input=payload,
            )
        except Exception as exc:  # noqa: BLE001 - classify by bounded error code, never leak
            if _is_execution_already_exists(exc):
                # A same-name start of the same operation is Step Functions'
                # idempotency signal for a Standard workflow: a duplicate dispatch
                # is a no-op replay of the already-running/complete execution, not
                # a failure and never a second blind start.
                return
            raise


# Step Functions Standard idempotency signal for a same-name start. The modeled
# exception is ``ExecutionAlreadyExists``; some paths surface the full modeled
# name ``ExecutionAlreadyExistsException``. Match both, from the bounded error
# code only — the raw provider message is never inspected or surfaced.
_EXECUTION_ALREADY_EXISTS_CODES = frozenset({"ExecutionAlreadyExists", "ExecutionAlreadyExistsException"})


def _is_execution_already_exists(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return False
    return error.get("Code") in _EXECUTION_ALREADY_EXISTS_CODES


def _execution_name(operation_id: str) -> str:
    """Return a stable, deterministic Step Functions execution name.

    A same-operation retry produces the same name so a duplicate dispatch is a
    no-op (ExecutionAlreadyExists) rather than a second workflow run.
    """
    digest = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:48]
    return f"{_EXECUTION_NAME_PREFIX}{digest}"


class _DispatchDenied(Exception):
    def __init__(self, status: int, error_code: str, safe_message: str) -> None:
        self.status = status
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(safe_message)


def _operation_id(event: Mapping[str, Any]) -> str:
    params = event.get("pathParameters")
    operation_id = params.get("operationId") if isinstance(params, Mapping) else None
    if not isinstance(operation_id, str) or not operation_id.startswith("op_"):
        raise _DispatchDenied(400, "OPERATION_INVALID", "operation id is invalid")
    return operation_id


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
