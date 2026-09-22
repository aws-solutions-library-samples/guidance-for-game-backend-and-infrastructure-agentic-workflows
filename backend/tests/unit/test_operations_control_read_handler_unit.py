"""E4 read handler tests: capabilities / list / detail (issue #416).

:class:`~operations.control.read_handler.ControlReadHandler` is the authenticated
HTTP boundary for the read-only E4 surfaces. Like every operations handler it
binds identity ONLY from the API Gateway JWT authorizer context and resolves the
workspace from server-owned config, so a caller only ever sees its own
workspace's operations. It returns validated, bounded, public-safe projections
(capability discovery, list, detail) and never leaks identity or provider data.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.control.read_handler import ControlReadHandler

pytestmark = [pytest.mark.unit, pytest.mark.fast]


class _FakeProjection:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.detail_calls: list[dict[str, Any]] = []
        self.detail_result: dict[str, Any] | None = {"contract_version": "1.0", "operation_id": "op_x"}

    def list_operations(self, *, workspace_id: str, page_size: int, cursor: Any = None) -> dict[str, Any]:
        self.list_calls.append({"workspace_id": workspace_id, "page_size": page_size, "cursor": cursor})
        return {"contract_version": "1.0", "page_size": page_size, "operations": []}

    def get_detail(self, *, operation_id: str, workspace_id: str) -> dict[str, Any] | None:
        self.detail_calls.append({"operation_id": operation_id, "workspace_id": workspace_id})
        return self.detail_result


class _FakeDiscovery:
    def discover(self) -> dict[str, Any]:
        return {"contract_version": "1.0", "capabilities": []}


def _handler(projection: Any = None, discovery: Any = None) -> ControlReadHandler:
    return ControlReadHandler(
        projection_service=projection or _FakeProjection(),
        discovery_service=discovery or _FakeDiscovery(),
        tenant_id="tenant-1",
        workspace_id="ws-1",
        trusted_audience="trusted-audience",
    )


def _event(
    method: str, path: str, *, path_params: Any = None, query: Any = None, client_id: str = "trusted-audience"
) -> dict[str, Any]:
    return {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "user-1",
                        "client_id": client_id,
                        "token_use": "access",
                        "exp": "9999999999",
                        "cognito:groups": "[users]",
                    }
                }
            },
        },
        "pathParameters": path_params or {},
        "queryStringParameters": query or {},
    }


def test_capabilities_returns_discovery() -> None:
    response = _handler().handle_capabilities(_event("GET", "/operations/capabilities"))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["capabilities"] == []


def test_list_binds_workspace_from_config() -> None:
    projection = _FakeProjection()
    response = _handler(projection).handle_list(_event("GET", "/operations", query={"page_size": "20"}))
    assert response["statusCode"] == 200
    assert projection.list_calls[0]["workspace_id"] == "ws-1"
    assert projection.list_calls[0]["page_size"] == 20


def test_list_passes_cursor() -> None:
    projection = _FakeProjection()
    _handler(projection).handle_list(_event("GET", "/operations", query={"cursor": "abc123"}))
    assert projection.list_calls[0]["cursor"] == "abc123"


def test_detail_returns_projection() -> None:
    projection = _FakeProjection()
    response = _handler(projection).handle_detail(
        _event("GET", "/operations/op_x", path_params={"operationId": "op_" + "a" * 26})
    )
    assert response["statusCode"] == 200
    assert projection.detail_calls[0]["workspace_id"] == "ws-1"


def test_detail_missing_returns_404() -> None:
    projection = _FakeProjection()
    projection.detail_result = None
    response = _handler(projection).handle_detail(
        _event("GET", "/operations/op_x", path_params={"operationId": "op_" + "a" * 26})
    )
    assert response["statusCode"] == 404


def test_missing_auth_returns_401() -> None:
    response = _handler().handle_list({"requestContext": {"http": {"method": "GET", "path": "/operations"}}})
    assert response["statusCode"] == 401


def test_wrong_client_returns_403() -> None:
    response = _handler().handle_list(_event("GET", "/operations", client_id="other"))
    assert response["statusCode"] == 403


def test_invalid_page_size_defaults_safely() -> None:
    projection = _FakeProjection()
    response = _handler(projection).handle_list(_event("GET", "/operations", query={"page_size": "not-a-number"}))
    assert response["statusCode"] == 200
    # A malformed page_size falls back to the default, never an error/unbounded read.
    assert 1 <= projection.list_calls[0]["page_size"] <= 50
