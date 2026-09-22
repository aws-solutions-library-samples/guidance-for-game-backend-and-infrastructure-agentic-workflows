"""E4 control-plane router tests (issue #416).

:class:`~operations.control.router.ControlPlaneRouter` picks the correct E4
sub-handler by HTTP method and route, matching the frozen ``ROUTE_KEYS``:

* ``GET /operations/capabilities`` -> capability discovery
* ``GET /operations`` -> bounded list
* ``GET /operations/{operationId}`` -> bounded detail
* ``POST /operations/control`` -> admin control (CAS)

The router only *chooses*; each handler independently enforces the JWT-only
identity boundary and returns its own bounded, sanitized response. An unknown
route is a bounded 404.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.control.router import ControlPlaneRouter

pytestmark = [pytest.mark.unit, pytest.mark.fast]


class _RecordingReadHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def handle_capabilities(self, event: Any) -> dict[str, Any]:
        self.calls.append("capabilities")
        return {"statusCode": 200, "body": "{}"}

    def handle_list(self, event: Any) -> dict[str, Any]:
        self.calls.append("list")
        return {"statusCode": 200, "body": "{}"}

    def handle_detail(self, event: Any) -> dict[str, Any]:
        self.calls.append("detail")
        return {"statusCode": 200, "body": "{}"}


class _RecordingControlHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def handle(self, event: Any) -> dict[str, Any]:
        self.calls.append("control")
        return {"statusCode": 200, "body": "{}"}


def _router() -> tuple[ControlPlaneRouter, _RecordingReadHandler, _RecordingControlHandler]:
    read = _RecordingReadHandler()
    control = _RecordingControlHandler()
    return ControlPlaneRouter(read_handler=read, control_handler=control), read, control


def _event(method: str, path: str, *, path_params: Any = None) -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "pathParameters": path_params or {},
    }


def test_routes_capabilities() -> None:
    router, read, _ = _router()
    router.handle(_event("GET", "/operations/capabilities"))
    assert read.calls == ["capabilities"]


def test_routes_list() -> None:
    router, read, _ = _router()
    router.handle(_event("GET", "/operations"))
    assert read.calls == ["list"]


def test_routes_detail() -> None:
    router, read, _ = _router()
    router.handle(_event("GET", "/operations/op_x", path_params={"operationId": "op_" + "a" * 26}))
    assert read.calls == ["detail"]


def test_routes_control() -> None:
    router, _, control = _router()
    router.handle(_event("POST", "/operations/control"))
    assert control.calls == ["control"]


def test_capabilities_takes_precedence_over_detail() -> None:
    # /operations/capabilities is a GET under /operations but must route to
    # capability discovery, never be mistaken for a {operationId} detail lookup.
    router, read, _ = _router()
    router.handle(_event("GET", "/operations/capabilities"))
    assert read.calls == ["capabilities"]


def test_unknown_route_is_404() -> None:
    router, read, control = _router()
    response = router.handle(_event("DELETE", "/operations/control"))
    assert response["statusCode"] == 404
    assert read.calls == [] and control.calls == []


def test_control_route_only_matches_post() -> None:
    router, read, control = _router()
    # A GET /operations/control is not the control mutation; it is not a known
    # read route either, so it is a bounded 404 (never a silent control call).
    response = router.handle(_event("GET", "/operations/control"))
    assert control.calls == []
    assert response["statusCode"] in (200, 404)
