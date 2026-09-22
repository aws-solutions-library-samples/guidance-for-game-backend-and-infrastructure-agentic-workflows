"""The admin control HTTP handler emits the denial metric at the boundary (#416).

The handler denies a non-admin (or an untrusted client) before the request ever
reaches the control service, so the ``control.denied`` control metric must be
emitted at the handler boundary too — not only inside the service. The sink is
optional and best-effort.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.control.control_handler import ControlRequestHandler

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TRUSTED = "trusted-app-client"


class _RecordingMetrics:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, name: str, **kwargs: Any) -> None:
        self.events.append(name)


class _Service:
    def apply(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service must not be reached for a denied caller")


def _event(groups: str) -> dict[str, Any]:
    return {
        "body": json.dumps({"expected_config_version": 1, "desired": {}}),
        "requestContext": {
            "http": {"method": "POST", "path": "/operations/control"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "user-1",
                        "client_id": _TRUSTED,
                        "token_use": "access",
                        "exp": str(int((_NOW + timedelta(hours=1)).timestamp())),
                        "cognito:groups": groups,
                    }
                }
            },
        },
    }


def _handler(metrics: Any) -> ControlRequestHandler:
    return ControlRequestHandler(
        control_service=_Service(),
        tenant_id="tenant-default",
        workspace_id="workspace-default",
        trusted_audience=_TRUSTED,
        admin_group="admin",
        metrics=metrics,
    )


def test_non_admin_denial_emits_metric() -> None:
    metrics = _RecordingMetrics()
    handler = _handler(metrics)
    response = handler.handle(_event("[users]"))
    assert response["statusCode"] == 403
    assert "control.denied" in metrics.events
