"""Unit tests for the single operations request router (issue #414).

The deployable ``operations.observe.lambda_entry`` handler routes BOTH the E1
observe surface and the E2 approval surface through one entry point. The
``OperationsRequestRouter`` chooses the correct sub-handler by method + route:

* ``POST /operations/observe`` -> E1 observation handler (preserved).
* ``POST /operations/prepare`` and ``POST /operations/{operationId}/{action}``
  -> E2 approval handler.
* ``GET /operations/{operationId}`` -> E1 status for an ``obs_`` id, E2 evidence
  for an ``op_`` id.

These tests drive the router with fake sub-handlers and assert dispatch. They
never touch a live table or provider.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.router import OperationsRequestRouter

pytestmark = pytest.mark.unit


class FakeHandler:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.events: list[dict[str, Any]] = []

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events.append(event)
        return {"statusCode": 200, "handled_by": self.tag}


def _event(method: str, path: str, *, path_params: dict | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "requestContext": {"http": {"method": method, "path": path}, "requestId": "r1"},
        "rawPath": path,
    }
    if path_params is not None:
        event["pathParameters"] = path_params
    return event


def _router() -> tuple[OperationsRequestRouter, FakeHandler, FakeHandler]:
    e1 = FakeHandler("e1")
    e2 = FakeHandler("e2")
    return OperationsRequestRouter(observation_handler=e1, approval_handler=e2), e1, e2


def test_observe_post_goes_to_e1() -> None:
    router, e1, e2 = _router()
    resp = router.handle(_event("POST", "/operations/observe"))
    assert resp["handled_by"] == "e1"


def test_prepare_post_goes_to_e2() -> None:
    router, e1, e2 = _router()
    resp = router.handle(_event("POST", "/operations/prepare"))
    assert resp["handled_by"] == "e2"


def test_approve_post_goes_to_e2() -> None:
    router, e1, e2 = _router()
    resp = router.handle(
        _event(
            "POST",
            "/operations/op_aaaaaaaaaaaaaaaaaaaaaaaaaa/approve",
            path_params={"operationId": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"},
        )
    )
    assert resp["handled_by"] == "e2"


def test_get_obs_id_goes_to_e1_status() -> None:
    router, e1, e2 = _router()
    resp = router.handle(
        _event(
            "GET",
            "/operations/obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            path_params={"operationId": "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"},
        )
    )
    assert resp["handled_by"] == "e1"


def test_get_op_id_goes_to_e2_evidence() -> None:
    router, e1, e2 = _router()
    resp = router.handle(
        _event(
            "GET",
            "/operations/op_aaaaaaaaaaaaaaaaaaaaaaaaaa",
            path_params={"operationId": "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"},
        )
    )
    assert resp["handled_by"] == "e2"


def test_get_unknown_prefix_defaults_to_e1_status() -> None:
    # A bare/unknown id preserves E1 status behavior (its handler returns the
    # bounded NOT_FOUND / CONTRACT_INVALID); it never silently hits E2.
    router, e1, e2 = _router()
    resp = router.handle(_event("GET", "/operations/whatever", path_params={"operationId": "whatever"}))
    assert resp["handled_by"] == "e1"
