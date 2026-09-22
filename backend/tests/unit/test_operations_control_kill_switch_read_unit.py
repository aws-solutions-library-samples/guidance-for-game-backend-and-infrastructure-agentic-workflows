"""GET /operations/control/kill-switch read route + metrics (issue #416, E4).

The E4 control plane exposes a bounded, admin-visible read of the current
kill-switch state at ``GET /operations/control/kill-switch``. The router must
dispatch that exact route to the read handler's kill-switch reader (before the
``/operations/{operationId}`` detail catch-all), and the reader must:

* return the bounded, public-safe current state (``operations_enabled``, the
  per-phase flags, and ``config_version``) when the switch reads fresh; and
* fail closed AND emit the ``KillSwitchUnavailable`` control metric when the
  switch cannot be read as a fresh, valid document — never leaking provider
  detail.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID, KILL_SWITCH_ROUTE
from operations.control.kill_switch_gate import KillSwitchUnavailable

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TRUSTED = "trusted-app-client"


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _Decision:
    def __init__(self, *, enabled: bool, version: int, phases: dict[str, bool]) -> None:
        self.operations_enabled = enabled
        self.config_version = version
        self._phases = phases

    def phase_allowed(self, phase: str) -> bool:
        return self._phases[phase]


class _FreshGate:
    def evaluate(self) -> _Decision:
        return _Decision(enabled=True, version=9, phases={"prepare": True, "dispatch": True, "execute": False})


class _UnavailableGate:
    def evaluate(self) -> Any:
        raise KillSwitchUnavailable("extension unavailable")


class _RecordingMetrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, name: str, **kwargs: Any) -> None:
        self.events.append(name)


def _event(path: str = KILL_SWITCH_ROUTE) -> dict[str, Any]:
    return {
        "requestContext": {
            "http": {"method": "GET", "path": path},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "admin-1",
                        "client_id": _TRUSTED,
                        "token_use": "access",
                        "exp": str(int((_NOW + timedelta(hours=1)).timestamp())),
                        "cognito:groups": "[admin]",
                    }
                }
            },
        }
    }


def _read_handler(gate: Any, metrics: Any) -> Any:
    from operations.control.read_handler import ControlReadHandler

    class _Projection:
        def list_operations(self, **kwargs: Any) -> dict[str, Any]:
            return {"operations": []}

        def get_detail(self, **kwargs: Any) -> dict[str, Any] | None:
            return None

    class _Discovery:
        def discover(self) -> dict[str, Any]:
            return {"capabilities": []}

    return ControlReadHandler(
        projection_service=_Projection(),
        discovery_service=_Discovery(),
        tenant_id="tenant-default",
        workspace_id="workspace-default",
        trusted_audience=_TRUSTED,
        kill_switch_gate=gate,
        metrics=metrics,
    )


def test_kill_switch_read_returns_bounded_state() -> None:
    metrics = _RecordingMetrics()
    handler = _read_handler(_FreshGate(), metrics)
    response = handler.handle_kill_switch(_event())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["operations_enabled"] is True
    assert body["config_version"] == 9
    assert body["phases"] == {"prepare": True, "dispatch": True, "execute": False}
    assert "KillSwitchUnavailable" not in metrics.events
    # No account id / ARN / provider payload leaks into the bounded projection.
    assert "arn" not in response["body"].lower()


def test_kill_switch_read_fails_closed_and_emits_metric_when_unavailable() -> None:
    metrics = _RecordingMetrics()
    handler = _read_handler(_UnavailableGate(), metrics)
    response = handler.handle_kill_switch(_event())
    assert response["statusCode"] == 503
    body = json.loads(response["body"])
    assert body["error_code"] == "KILL_SWITCH_UNAVAILABLE"
    assert "kill_switch.unavailable" in metrics.events


def test_router_dispatches_kill_switch_route() -> None:
    from operations.control.router import ControlPlaneRouter

    class _Read:
        def __init__(self) -> None:
            self.called = ""

        def handle_capabilities(self, event: Any) -> dict[str, Any]:
            self.called = "capabilities"
            return {"statusCode": 200}

        def handle_list(self, event: Any) -> dict[str, Any]:
            self.called = "list"
            return {"statusCode": 200}

        def handle_detail(self, event: Any) -> dict[str, Any]:
            self.called = "detail"
            return {"statusCode": 200}

        def handle_kill_switch(self, event: Any) -> dict[str, Any]:
            self.called = "kill_switch"
            return {"statusCode": 200}

    class _Control:
        def handle(self, event: Any) -> dict[str, Any]:
            return {"statusCode": 200}

    read = _Read()
    router = ControlPlaneRouter(read_handler=read, control_handler=_Control())
    router.handle({"requestContext": {"http": {"method": "GET", "path": KILL_SWITCH_ROUTE}}})
    assert read.called == "kill_switch"
