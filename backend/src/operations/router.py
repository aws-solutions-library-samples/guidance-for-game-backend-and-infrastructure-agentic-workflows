"""Single-entry operations request router for E1 + E2 (issue #414).

The deployable operations Lambda exposes exactly one ``handler`` entry point but
serves two surfaces: the read-only E1 observation surface (issue #413) and the
E2 prepare/approval surface (issue #414). ``OperationsRequestRouter`` picks the
correct sub-handler by HTTP method and route so E1 routes stay byte-for-byte
preserved while the E2 routes are added additively:

* ``POST /operations/observe`` -> the E1 observation handler.
* ``POST /operations/prepare`` and ``POST /operations/{operationId}/<action>``
  (approve/reject/cancel) -> the E2 approval handler.
* ``GET /operations/{operationId}`` -> E1 status when the id is an ``obs_``
  observation id, E2 evidence when the id is an ``op_`` prepared-operation id.
  Any other id preserves E1 status behavior (its bounded NOT_FOUND /
  CONTRACT_INVALID), so an unknown id never silently reaches the E2 surface.

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


class RequestHandlerPort(Protocol):
    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]: ...


class OperationsRequestRouter:
    """Route one API Gateway invocation to the E1 or E2 sub-handler."""

    def __init__(self, *, observation_handler: RequestHandlerPort, approval_handler: RequestHandlerPort) -> None:
        self._observation_handler = observation_handler
        self._approval_handler = approval_handler

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
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
