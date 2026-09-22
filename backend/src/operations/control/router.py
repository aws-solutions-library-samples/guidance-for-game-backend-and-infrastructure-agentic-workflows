"""E4 control-plane request router (issue #416).

The deployable E4 control-plane Lambda exposes one ``handler`` entry point but
serves four routes. :class:`ControlPlaneRouter` picks the correct sub-handler by
HTTP method and route, matching the frozen control-plane ``ROUTE_KEYS``:

* ``GET  /operations/capabilities``        -> capability discovery
* ``GET  /operations``                     -> bounded, workspace-scoped list
* ``GET  /operations/{operationId}``       -> bounded, workspace-scoped detail
* ``POST /operations/control``             -> admin control (compare-and-set)

``/operations/capabilities`` and ``/operations/control`` are matched before the
``/operations/{operationId}`` detail catch-all so a reserved sub-path is never
mistaken for an operation id. The router only *chooses* a handler; each handler
independently enforces the JWT-only identity boundary and returns its own
bounded, sanitized response. An unknown method/route is a bounded 404 and never
silently reaches a handler.
"""

from __future__ import annotations

# Standard library
import json
from collections.abc import Mapping
from typing import Any, Protocol

# Local modules
from operations.contracts import CONTRACT_VERSION
from operations.contracts.control_plane import (
    CAPABILITIES_ROUTE,
    CONTROL_ROUTE,
    KILL_SWITCH_ROUTE,
    OPERATIONS_LIST_ROUTE,
)


class ReadHandlerPort(Protocol):
    def handle_capabilities(self, event: Mapping[str, Any]) -> dict[str, Any]: ...

    def handle_list(self, event: Mapping[str, Any]) -> dict[str, Any]: ...

    def handle_detail(self, event: Mapping[str, Any]) -> dict[str, Any]: ...

    def handle_kill_switch(self, event: Mapping[str, Any]) -> dict[str, Any]: ...


class ControlHandlerPort(Protocol):
    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]: ...


class ControlPlaneRouter:
    """Route one API Gateway invocation to the correct E4 sub-handler."""

    def __init__(self, *, read_handler: ReadHandlerPort, control_handler: ControlHandlerPort) -> None:
        self._read_handler = read_handler
        self._control_handler = control_handler

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        method = _method(event)
        route = _route(event)

        if method == "POST" and route == CONTROL_ROUTE:
            return self._control_handler.handle(event)

        if method == "GET":
            if route == CAPABILITIES_ROUTE:
                return self._read_handler.handle_capabilities(event)
            if route == KILL_SWITCH_ROUTE:
                return self._read_handler.handle_kill_switch(event)
            if route == OPERATIONS_LIST_ROUTE:
                return self._read_handler.handle_list(event)
            # A GET under /operations/<id> is a detail lookup — but never for the
            # reserved control sub-paths (control is POST-only; kill-switch is
            # matched above), so a reserved path is never read as an operation id.
            if route.startswith("/operations/") and route not in (CONTROL_ROUTE, KILL_SWITCH_ROUTE):
                return self._read_handler.handle_detail(event)

        return _not_found()


def _method(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    method = http.get("method") if isinstance(http, Mapping) else None
    return method.upper() if isinstance(method, str) and method else ""


def _route(event: Mapping[str, Any]) -> str:
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    path = http.get("path") if isinstance(http, Mapping) else None
    if isinstance(path, str) and path:
        return path
    raw = event.get("rawPath")
    return raw if isinstance(raw, str) else ""


def _not_found() -> dict[str, Any]:
    body = {
        "error_contract_version": CONTRACT_VERSION,
        "error_code": "ROUTE_NOT_FOUND",
        "safe_message": "no such operations control route",
        "retryable": False,
    }
    return {
        "statusCode": 404,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":"), sort_keys=True),
    }
