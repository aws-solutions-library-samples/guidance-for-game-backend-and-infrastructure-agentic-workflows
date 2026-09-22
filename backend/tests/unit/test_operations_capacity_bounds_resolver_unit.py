"""Unit tests for the deployment-configured server-owned bounds resolver (#414).

The E2 advise layer resolves enrollment/policy bounds through a trusted,
server-owned :class:`~operations.advice.CapacityBoundsPort`. These bounds are
never client input: they are code/deployment-owned. The resolver here treats a
fleet as enrolled precisely because a fresh, successful E1 observation exists for
it (proof of observability under the workspace) and applies the deployment's
configured floor/ceiling/max-step and policy/enrollment identifiers.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.advice import CapacityBounds
from operations.capacity_bounds import DeploymentCapacityBoundsResolver
from operations.capacity_state import E1ObservationCapacityStatePort
from operations.identity import VerifiedPrincipal
from operations.observation import ObservationStatus, ObservationStatusView

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
OBS_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
OBS_HASH = "sha256:" + "a" * 64


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject_id="user.requester",
        client_id="client.requester",
        audience="operations-api",
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expires_at=NOW + timedelta(hours=1),
    )


def _observation() -> dict:
    observed_at = (NOW - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    expires_at = (NOW + timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
    return {
        "observation_id": OBS_ID,
        "target": {"provider": "gamelift", "fleet_id": FLEET},
        "observed_at": observed_at,
        "expires_at": expires_at,
        "results": {
            "utilization": {
                "active_server_processes": 4,
                "active_game_sessions": 2,
                "current_player_sessions": 6,
                "maximum_player_sessions": 40,
            },
            "capacity": [{"location": LOCATION, "desired": 10, "minimum": 2, "maximum": 20, "active": 9, "idle": 1}],
            "scaling_policies": [],
        },
    }


class FakeStatusLoader:
    def __init__(self, status: ObservationStatus | None) -> None:
        self._status = status

    def load_status(self, *, operation_id: str, workspace_id: str) -> ObservationStatus | None:
        return self._status


def _state_port(status: ObservationStatus | None) -> E1ObservationCapacityStatePort:
    return E1ObservationCapacityStatePort(
        status_loader=FakeStatusLoader(status), observation_id=OBS_ID, clock=lambda: NOW
    )


def _succeeded() -> ObservationStatus:
    return ObservationStatus(
        operation_id=OBS_ID,
        workspace_id="workspace.default",
        state=ObservationStatusView.SUCCEEDED,
        observation=_observation(),
        observation_hash=OBS_HASH,
    )


def _resolver(state_port: E1ObservationCapacityStatePort) -> DeploymentCapacityBoundsResolver:
    return DeploymentCapacityBoundsResolver(
        state_port=state_port,
        floor=0,
        ceiling=100,
        max_step=25,
        enrollment_id="enrollment.gamelift.capacity",
        enrollment_version="1",
        policy_id="policy.gamelift.capacity",
        policy_version="1",
    )


def test_enrolled_when_fresh_observation_exists() -> None:
    bounds = _resolver(_state_port(_succeeded())).resolve_bounds(
        requester=_principal(), fleet_id=FLEET, location=LOCATION
    )
    assert isinstance(bounds, CapacityBounds)
    assert bounds.target_enrolled is True
    assert bounds.floor == 0
    assert bounds.ceiling == 100
    assert bounds.max_step == 25
    assert bounds.policy_id == "policy.gamelift.capacity"
    assert bounds.enrollment_id == "enrollment.gamelift.capacity"


def test_not_enrolled_when_no_fresh_observation() -> None:
    bounds = _resolver(_state_port(None)).resolve_bounds(requester=_principal(), fleet_id=FLEET, location=LOCATION)
    # Not enrolled/observable => a bounds object that fails the enrollment check,
    # so advice deterministically denies with TARGET_NOT_ENROLLED (never None-
    # crashes and never silently authorizes).
    assert bounds is not None
    assert bounds.target_enrolled is False


def test_invalid_deployment_config_fails_closed() -> None:
    with pytest.raises(ValueError):
        DeploymentCapacityBoundsResolver(
            state_port=_state_port(_succeeded()),
            floor=10,
            ceiling=5,  # ceiling below floor
            max_step=25,
            enrollment_id="enrollment.gamelift.capacity",
            enrollment_version="1",
            policy_id="policy.gamelift.capacity",
            policy_version="1",
        )
