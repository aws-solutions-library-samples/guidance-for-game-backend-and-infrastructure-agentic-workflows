"""Cross-contract E3 dispatch-route drift guard (#415).

The E3 shakedown harness POSTs to a *deployed* dispatch route. That route is
frozen by the reviewed 07 execution template's API Gateway ``RouteKey``
(``POST /operations/{operationId}/dispatch``). A live shakedown against a real
deployment 404s for every check if the harness posts a different suffix — which
is exactly what happened when the harness used ``/execute``.

These tests fail *before* deployment on any drift between three artifacts that
must agree on the single frozen dispatch route:

1. the harness's own frozen route constant,
2. the concrete URL the harness actually builds, and
3. the 07 CloudFormation template's ``RouteKey`` (read as data when the template
   is present in this checkout or a sibling infra worktree).

The constant checks are never skipped, so drift is caught even in a core-only
checkout that does not ship the 07 template. The template check runs whenever
the template can be located, binding harness and infra to the same route.
"""

from __future__ import annotations

# Standard library
import json
import pathlib
from typing import Any, Optional

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template
from operations.validation.e1_shakedown import HttpResponse
from operations.validation.e3_shakedown import (
    DISPATCH_ROUTE_TEMPLATE,
    E3ShakedownConfig,
    E3ShakedownHarness,
)

# The single frozen dispatch route. API Gateway ``RouteKey`` form (method + the
# ``{operationId}`` path variable). Handler, harness, and template must all agree
# on this exact string; any divergence is a pre-deployment failure.
_FROZEN_ROUTE_KEY = "POST /operations/{operationId}/dispatch"
_FROZEN_PATH_TEMPLATE = "/operations/{operationId}/dispatch"

_ENDPOINT = "https://abc123.execute-api.us-west-2.amazonaws.com"
_OP = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_ADMIN = "admin-token-SECRET"
_FLEET = "fleet-1234abcd-5678-90ef"

# 07 execution template relative path, and sibling infra worktree names that may
# own it in the split two-worktree developer layout.
_TEMPLATE_REL = "infrastructure/cloudformation/07-operations-execution.yaml"
_SIBLING_INFRA_DIRS = ("issue-415-infra",)


class _RecordingTransport:
    """A callable transport that records the exact URL it was asked to POST."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Optional[dict[str, str]],
        body: Optional[bytes],
    ) -> HttpResponse:
        self.urls.append(url)
        return HttpResponse(
            status=202,
            content_type="application/json",
            body_bytes=json.dumps({"state": "dispatched"}).encode("utf-8"),
        )


def _config() -> E3ShakedownConfig:
    return E3ShakedownConfig(endpoint=_ENDPOINT, operation_id=_OP, admin_bearer=_ADMIN, fleet_id=_FLEET)


def _find_execution_template() -> Optional[pathlib.Path]:
    """Locate the 07 execution template in this checkout or a sibling worktree."""
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    candidates = [repo_root / _TEMPLATE_REL]
    for sibling in _SIBLING_INFRA_DIRS:
        candidates.append(repo_root.parent / sibling / _TEMPLATE_REL)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def test_harness_frozen_route_constant_is_the_dispatch_route() -> None:
    """The harness's frozen route constant is the dispatch route, not /execute."""
    assert DISPATCH_ROUTE_TEMPLATE == _FROZEN_PATH_TEMPLATE
    assert "/execute" not in DISPATCH_ROUTE_TEMPLATE


def test_harness_builds_dispatch_url_not_execute() -> None:
    """The concrete URL the harness POSTs ends in the frozen /dispatch suffix."""
    transport = _RecordingTransport()
    harness = E3ShakedownHarness(_config(), transport)
    harness.check_admin_dispatch_accepted()
    assert transport.urls, "harness must issue at least one POST"
    for url in transport.urls:
        assert url.endswith(f"/operations/{_OP}/dispatch"), url
        assert "/execute" not in url, url


def test_harness_route_matches_07_template_route_key() -> None:
    """Cross-contract: the 07 template RouteKey matches the harness route.

    Reads the reviewed 07 template as data when present (this checkout or a
    sibling infra worktree). Skips only when no template can be located; the
    constant/URL guards above still run in that case so drift never goes
    unnoticed in a core-only checkout.
    """
    template_path = _find_execution_template()
    if template_path is None:
        pytest.skip("07 execution template not present in this checkout or a sibling worktree")

    template: dict[str, Any] = load_cfn_template(template_path.read_text(encoding="utf-8"))
    routes = [
        body["Properties"].get("RouteKey")
        for body in template["Resources"].values()
        if body.get("Type") == "AWS::ApiGatewayV2::Route"
    ]
    assert routes, f"no ApiGatewayV2 routes found in {template_path}"

    # The single dispatch route the template exposes must equal the frozen route
    # the harness posts to, and must not be the stale /execute suffix.
    assert _FROZEN_ROUTE_KEY in routes, routes
    for route_key in routes:
        assert route_key is not None
        assert "/execute" not in route_key, route_key
        # Every operation-scoped route key path must match the harness template.
        method, _, path = route_key.partition(" ")
        assert path == DISPATCH_ROUTE_TEMPLATE, route_key
