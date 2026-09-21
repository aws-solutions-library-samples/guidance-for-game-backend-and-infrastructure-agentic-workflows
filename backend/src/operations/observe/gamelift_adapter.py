"""Normalized, read-only GameLift adapter for the observe phase (#413).

This adapter implements :class:`~operations.observation.GameLiftObservationReader`
over the ``boto3`` GameLift client. It exposes exactly three read-only methods —
``read_utilization``, ``read_capacity``, ``read_scaling_policies`` — and no
write method. Each method issues a single bounded ``describe_*`` call and
normalizes the raw provider response into the bounded domain shape the
application layer expects, dropping every provider-specific field: it never
returns an ARN, account id, fleet ARN, timestamps, or a raw response.

The adapter owns the SDK and least-privilege read-only credentials. Bounds are
enforced defensively here (location and policy counts) so an unexpectedly large
provider response fails closed downstream rather than being written.
"""

from __future__ import annotations

# Standard library
from typing import Any, Protocol

# Cap the normalized collections defensively; the application layer and schema
# enforce the same ceilings, so an oversized provider response fails closed.
_MAX_CAPACITY_LOCATIONS = 64
_MAX_SCALING_POLICIES = 50

_VALID_STATUSES = frozenset(
    {"ACTIVE", "UPDATE_REQUESTED", "UPDATING", "DELETE_REQUESTED", "DELETING", "DELETED", "ERROR"}
)


class GameLiftClient(Protocol):
    """The narrow read-only slice of the boto3 GameLift client this adapter uses."""

    def describe_fleet_utilization(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_scaling_policies(self, **kwargs: Any) -> dict[str, Any]: ...


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected a non-negative integer counter")
    if value < 0:
        raise ValueError("expected a non-negative integer counter")
    return value


class GameLiftObservationAdapter:
    """Read-only GameLift adapter returning bounded, normalized domain values."""

    def __init__(self, client: GameLiftClient) -> None:
        self._client = client

    def read_utilization(self, fleet_id: str) -> dict[str, int]:
        response = self._client.describe_fleet_utilization(FleetIds=[fleet_id])
        records = response.get("FleetUtilization") if isinstance(response, dict) else None
        if not isinstance(records, list) or not records:
            raise ValueError("no fleet utilization returned")
        record = records[0]
        if not isinstance(record, dict):
            raise ValueError("malformed fleet utilization record")
        return {
            "active_server_processes": _int(record.get("ActiveServerProcessCount", 0)),
            "active_game_sessions": _int(record.get("ActiveGameSessionCount", 0)),
            "current_player_sessions": _int(record.get("CurrentPlayerSessionCount", 0)),
            "maximum_player_sessions": _int(record.get("MaximumPlayerSessionCount", 0)),
        }

    def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
        # Use the exact E0-accepted read: describe_fleet_capacity(FleetIds=[...]).
        # It returns FleetCapacity as a per-location list already, so normalize
        # each entry into the bounded location list the contract expects.
        response = self._client.describe_fleet_capacity(FleetIds=[fleet_id])
        capacities = response.get("FleetCapacity") if isinstance(response, dict) else None
        if not isinstance(capacities, list):
            raise ValueError("no fleet capacity returned")
        result: list[dict[str, Any]] = []
        for entry in capacities[: _MAX_CAPACITY_LOCATIONS + 1]:
            if not isinstance(entry, dict):
                raise ValueError("malformed fleet capacity record")
            instances = entry.get("InstanceCounts")
            if not isinstance(instances, dict):
                raise ValueError("malformed fleet capacity counts")
            location = entry.get("Location")
            result.append(
                {
                    "location": location if isinstance(location, str) and location else "home",
                    "desired": _int(instances.get("DESIRED", 0)),
                    "minimum": _int(instances.get("MINIMUM", 0)),
                    "maximum": _int(instances.get("MAXIMUM", 0)),
                    "active": _int(instances.get("ACTIVE", 0)),
                    "idle": _int(instances.get("IDLE", 0)),
                }
            )
        return result

    def read_scaling_policies(self, fleet_id: str) -> list[dict[str, str]]:
        response = self._client.describe_scaling_policies(FleetId=fleet_id, StatusFilter="ACTIVE")
        policies = response.get("ScalingPolicies") if isinstance(response, dict) else None
        if not isinstance(policies, list):
            return []
        result: list[dict[str, str]] = []
        for entry in policies[: _MAX_SCALING_POLICIES + 1]:
            if not isinstance(entry, dict):
                raise ValueError("malformed scaling policy record")
            name = entry.get("Name")
            status = entry.get("Status")
            metric = entry.get("MetricName")
            if not isinstance(name, str) or not name:
                raise ValueError("scaling policy has no name")
            if status not in _VALID_STATUSES:
                # Default an unknown provider status to ACTIVE only when the
                # policy is otherwise well-formed would hide data; fail closed.
                raise ValueError("scaling policy has an unknown status")
            if not isinstance(metric, str) or not metric:
                raise ValueError("scaling policy has no metric name")
            result.append({"name": name, "status": status, "metric_name": metric})
        return result
