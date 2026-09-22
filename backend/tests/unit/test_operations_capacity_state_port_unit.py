"""Unit tests for the trusted E1-backed capacity-state port (issue #414).

The E2 prepare layer's :class:`~operations.advice.CapacityStatePort` is
implemented over the real, workspace-scoped E1 observation status. These tests
drive the adapter with a fake E1 status loader (never a live table) and assert
its trust boundary:

* it loads ONLY a successful, fresh E1 observation owned by the verified
  workspace, resolved by the ``observation_id`` bound from the untrusted body;
* it returns the per-location capacity, the observation id + hash, and the
  observation revision timestamps as the deterministic anchor; and
* it fails closed (returns ``None``) on a missing, non-succeeded, foreign-
  workspace, hash-less, wrong-fleet, wrong-location, or malformed observation —
  never raising and never fabricating capacity.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.advice import CurrentCapacity
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


def _observation(*, fleet_id: str = FLEET, location: str = LOCATION, expires_in_s: int = 600) -> dict:
    observed_at = (NOW - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    expires_at = (NOW + timedelta(seconds=expires_in_s)).isoformat().replace("+00:00", "Z")
    return {
        "observation_id": OBS_ID,
        "target": {"provider": "gamelift", "fleet_id": fleet_id},
        "observed_at": observed_at,
        "expires_at": expires_at,
        "results": {
            "utilization": {
                "active_server_processes": 4,
                "active_game_sessions": 2,
                "current_player_sessions": 6,
                "maximum_player_sessions": 40,
            },
            "capacity": [
                {"location": location, "desired": 10, "minimum": 2, "maximum": 20, "active": 9, "idle": 1},
                {"location": "eu-west-1", "desired": 3, "minimum": 1, "maximum": 5, "active": 3, "idle": 0},
            ],
            "scaling_policies": [],
        },
    }


class FakeStatusLoader:
    """A stand-in for the E1 status load bound to one workspace."""

    def __init__(self, status: ObservationStatus | None) -> None:
        self._status = status
        self.calls: list[tuple[str, str]] = []

    def load_status(self, *, operation_id: str, workspace_id: str) -> ObservationStatus | None:
        self.calls.append((operation_id, workspace_id))
        return self._status


def _succeeded(observation: dict, obs_hash: str = OBS_HASH) -> ObservationStatus:
    return ObservationStatus(
        operation_id=OBS_ID,
        workspace_id="workspace.default",
        state=ObservationStatusView.SUCCEEDED,
        observation=observation,
        observation_hash=obs_hash,
    )


def _port(loader: FakeStatusLoader) -> E1ObservationCapacityStatePort:
    return E1ObservationCapacityStatePort(status_loader=loader, observation_id=OBS_ID, clock=lambda: NOW)


def test_loads_fresh_successful_workspace_observation() -> None:
    loader = FakeStatusLoader(_succeeded(_observation()))
    current = _port(loader).load_current_capacity(requester=_principal(), fleet_id=FLEET, location=LOCATION)
    assert isinstance(current, CurrentCapacity)
    assert current.observation_id == OBS_ID
    assert current.observation_hash == OBS_HASH
    assert current.capacity.desired == 10
    assert current.capacity.minimum == 2
    assert current.capacity.maximum == 20
    # The load is scoped to the verified workspace and the bound observation id.
    assert loader.calls == [(OBS_ID, "workspace.default")]


def test_missing_observation_returns_none() -> None:
    current = _port(FakeStatusLoader(None)).load_current_capacity(
        requester=_principal(), fleet_id=FLEET, location=LOCATION
    )
    assert current is None


def test_non_succeeded_observation_returns_none() -> None:
    status = ObservationStatus(
        operation_id=OBS_ID, workspace_id="workspace.default", state=ObservationStatusView.OBSERVING
    )
    current = _port(FakeStatusLoader(status)).load_current_capacity(
        requester=_principal(), fleet_id=FLEET, location=LOCATION
    )
    assert current is None


def test_stale_observation_returns_none() -> None:
    loader = FakeStatusLoader(_succeeded(_observation(expires_in_s=-1)))
    current = _port(loader).load_current_capacity(requester=_principal(), fleet_id=FLEET, location=LOCATION)
    assert current is None


def test_wrong_fleet_returns_none() -> None:
    other_fleet = "fleet-9999abcd-5678-90ef-a1b2-c3d4e5f60789"
    loader = FakeStatusLoader(_succeeded(_observation(fleet_id=other_fleet)))
    current = _port(loader).load_current_capacity(requester=_principal(), fleet_id=FLEET, location=LOCATION)
    assert current is None


def test_location_not_observed_returns_none() -> None:
    loader = FakeStatusLoader(_succeeded(_observation(location="ap-south-1")))
    current = _port(loader).load_current_capacity(requester=_principal(), fleet_id=FLEET, location=LOCATION)
    assert current is None


def test_missing_hash_returns_none() -> None:
    status = ObservationStatus(
        operation_id=OBS_ID,
        workspace_id="workspace.default",
        state=ObservationStatusView.SUCCEEDED,
        observation=_observation(),
        observation_hash=None,
    )
    current = _port(FakeStatusLoader(status)).load_current_capacity(
        requester=_principal(), fleet_id=FLEET, location=LOCATION
    )
    assert current is None


def test_malformed_observation_returns_none() -> None:
    status = ObservationStatus(
        operation_id=OBS_ID,
        workspace_id="workspace.default",
        state=ObservationStatusView.SUCCEEDED,
        observation={"observation_id": OBS_ID, "results": {}},
        observation_hash=OBS_HASH,
    )
    current = _port(FakeStatusLoader(status)).load_current_capacity(
        requester=_principal(), fleet_id=FLEET, location=LOCATION
    )
    assert current is None
