"""Deployable AWS Lambda entry point for the E3 dispatcher (issue #415).

The dispatcher is the JWT-authorized entry point behind the execution HTTP API.
API Gateway verifies the JWT before the request reaches this handler; the
handler additionally enforces ADMIN authority in code, validates the approved
operation, and starts the exact Step Functions state machine carrying
``operation_id`` ONLY.

This slice DEFINES the frozen entrypoint and its FAIL-CLOSED kill-switch
contract (lever 2 of the reversible emergency disable, mirrored in the API-stage
throttle that is lever 1): when the injected ``GBAW_OPERATIONS_EXECUTION_MODE``
is not ``remediate`` the dispatcher refuses before starting any execution and
without constructing any boto3 client. The E3 core wires the admin-enforced
validation and ``StartExecution`` behind this entrypoint in a later slice.
"""

from __future__ import annotations

# Standard library
import json
from collections.abc import Mapping
from typing import Any

# Local modules
from operations.execute.settings import resolve_execution_settings

# The JWT group that authorizes a direct dispatch. Admin-only, enforced in code
# on top of the API Gateway JWT authorizer.
_ADMIN_GROUP = "admin"


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":"), sort_keys=True),
    }


def _claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    ctx = event.get("requestContext", {}) if isinstance(event, Mapping) else {}
    authorizer = ctx.get("authorizer", {}) if isinstance(ctx, Mapping) else {}
    jwt = authorizer.get("jwt", {}) if isinstance(authorizer, Mapping) else {}
    claims = jwt.get("claims", {}) if isinstance(jwt, Mapping) else {}
    return claims if isinstance(claims, Mapping) else {}


def _operation_id(event: Mapping[str, Any]) -> str:
    params = event.get("pathParameters", {}) if isinstance(event, Mapping) else {}
    if isinstance(params, Mapping):
        return str(params.get("operationId") or "")
    return ""


def handle_event(event: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Pure, injectable core of the dispatcher handler.

    Fails closed (503) on the disabled kill switch and denies (403) a non-admin
    caller before any execution is started. Returns an HTTP-shaped response;
    never raises for the ordinary denial paths.
    """
    settings = resolve_execution_settings(env)

    # Lever 2: fail closed before any work when the kill switch is off.
    if not settings.enabled:
        return _response(
            503, {"error": "execution disabled", "detail": "GBAW_OPERATIONS_EXECUTION_MODE is not remediate"}
        )

    operation_id = _operation_id(event)
    if not operation_id:
        return _response(400, {"error": "operationId path parameter is required"})

    # Admin enforcement in code on top of the API Gateway JWT authorizer.
    claims = _claims(event)
    groups_raw = claims.get("cognito:groups", "")
    groups = _parse_groups(groups_raw)
    if _ADMIN_GROUP not in groups:
        return _response(403, {"error": "admin authority required to dispatch execution"})

    # The admin-enforced validation + StartExecution is wired by the E3 core in
    # a later slice. Until then we fail closed rather than start an unverified
    # execution.
    return _response(503, {"error": "dispatch not wired (E3 core pending); failing closed"})


def _parse_groups(raw: Any) -> frozenset[str]:
    """Parse the ``cognito:groups`` claim, which arrives as a bracketed string
    (``[admin users]``) or a list depending on the authorizer path. Uses the
    same strict, non-whitespace-splitting shape the E1/E2 authorizer parser
    uses so a bracketed single group is not silently split.
    """
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(str(g).strip() for g in raw if str(g).strip())
    text = str(raw or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return frozenset(g for g in (part.strip() for part in text.split()) if g)


def handler(event: Any, context: Any = None) -> dict[str, Any]:  # noqa: ANN401 - Lambda contract
    """AWS Lambda entry point invoked by the dispatch HTTP API."""
    return handle_event(event if isinstance(event, Mapping) else {})
