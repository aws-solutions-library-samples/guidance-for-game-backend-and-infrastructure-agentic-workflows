"""Single-entry operations request router for E1 + E2 (issue #414).

The deployable operations Lambda exposes exactly one ``handler`` entry point but
serves two surfaces: the read-only E1 observation surface (issue #413) and the
E2 prepare/approval surface (issue #414). ``OperationsRequestRouter`` picks the
correct sub-handler by the API Gateway HTTP API ``routeKey`` (falling back to
the method and path when a raw invocation carries no ``routeKey``) so E1 routes
stay byte-for-byte preserved while the E2 routes are added additively:

* ``POST /operations/observe`` -> the E1 observation handler.
* ``POST /operations/prepare`` and ``POST /operations/{operationId}/<action>``
  (approve/reject/cancel) -> the E2 approval handler.
* ``GET /operations/{operationId}`` -> E1 status when the id is an ``obs_``
  observation id, E2 evidence when the id is an ``op_`` prepared-operation id.
  Any other id preserves E1 status behavior (its bounded NOT_FOUND /
  CONTRACT_INVALID), so an unknown id never silently reaches the E2 surface.

Dispatching on the gateway's own ``routeKey`` rather than the raw request path
keeps routing correct under a custom-domain base path mapping, where the path
carries a stage/base-path prefix the templated routes do not; the two E2 POST
and GET route keys still reach the E2 handler there.

The router only *chooses* a handler; each sub-handler independently enforces the
JWT-only identity boundary and returns its own bounded, sanitized response.
"""

from __future__ import annotations

# Standard library
from collections.abc import Mapping
from typing import Any, Protocol

_OBSERVE_ROUTE = "/operations/observe"
_PREPARE_ROUTE = "/operations/prepare"
_E2_ID_PREFIX = "op_"
_E2_POST_SUFFIXES = ("/approve", "/reject", "/cancel")
_OBSERVE_ROUTE_KEY = "POST /operations/observe"
_PREPARE_ROUTE_KEY = "POST /operations/prepare"
_E2_POST_ROUTE_KEYS = frozenset(
    {
        "POST /operations/{operationId}/approve",
        "POST /operations/{operationId}/reject",
        "POST /operations/{operationId}/cancel",
    }
)
_EVIDENCE_ROUTE_KEY = "GET /operations/{operationId}"


class RequestHandlerPort(Protocol):
    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]: ...


class OperationsRequestRouter:
    """Route one API Gateway invocation to the E1 or E2 sub-handler."""

    def __init__(self, *, observation_handler: RequestHandlerPort, approval_handler: RequestHandlerPort) -> None:
        self._observation_handler = observation_handler
        self._approval_handler = approval_handler

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        route_key = _route_key(event)
        if route_key:
            return self._route_by_key(event, route_key)
        return self._route_by_method_and_path(event)

    def _route_by_key(self, event: Mapping[str, Any], route_key: str) -> dict[str, Any]:
        if route_key == _OBSERVE_ROUTE_KEY:
            return self._observation_handler.handle(event)
        if route_key == _PREPARE_ROUTE_KEY or route_key in _E2_POST_ROUTE_KEYS:
            return self._approval_handler.handle(event)
        if route_key == _EVIDENCE_ROUTE_KEY:
            operation_id = _operation_id(event)
            if isinstance(operation_id, str) and operation_id.startswith(_E2_ID_PREFIX):
                return self._approval_handler.handle(event)
            # An obs_ id or any other/unknown id preserves E1 status behavior.
            return self._observation_handler.handle(event)
        # An unknown route key preserves the E1 handler's bounded rejection.
        return self._observation_handler.handle(event)

    def _route_by_method_and_path(self, event: Mapping[str, Any]) -> dict[str, Any]:
        method = _method(event)
        route = _route(event)

        if method == "POST":
            if route == _OBSERVE_ROUTE:
                return self._observation_handler.handle(event)
            if route == _PREPARE_ROUTE or route.endswith(_E2_POST_SUFFIXES):
                return self._approval_handler.handle(event)
            # An unknown POST route preserves the E1 handler's bounded rejection.
            return self._observation_handler.handle(event)

        if method == "GET":
            operation_id = _operation_id(event)
            if isinstance(operation_id, str) and operation_id.startswith(_E2_ID_PREFIX):
                return self._approval_handler.handle(event)
            # An obs_ id or any other/unknown id preserves E1 status behavior.
            return self._observation_handler.handle(event)

        # Any other method is handled (and rejected) by the E1 handler, keeping
        # the observe surface's method contract unchanged.
        return self._observation_handler.handle(event)


def _route_key(event: Mapping[str, Any]) -> str:
    route_key = event.get("routeKey")
    return route_key if isinstance(route_key, str) and route_key else ""


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


def _operation_id(event: Mapping[str, Any]) -> str | None:
    params = event.get("pathParameters")
    operation_id = params.get("operationId") if isinstance(params, Mapping) else None
    return operation_id if isinstance(operation_id, str) else None
