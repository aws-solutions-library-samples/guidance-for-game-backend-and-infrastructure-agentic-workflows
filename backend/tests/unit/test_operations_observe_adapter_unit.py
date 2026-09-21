"""Unit tests for the normalized read-only GameLift adapter (issue #413)."""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.observation import GameLiftObservationReader
from operations.observe.gamelift_adapter import GameLiftObservationAdapter


class FakeGameLift:
    def __init__(self, **responses: Any) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe_fleet_utilization(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("utilization", kwargs))
        return self._responses.get(
            "utilization",
            {
                "FleetUtilization": [
                    {
                        "ActiveServerProcessCount": 12,
                        "ActiveGameSessionCount": 8,
                        "CurrentPlayerSessionCount": 30,
                        "MaximumPlayerSessionCount": 100,
                        "FleetArn": "arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-x",
                    }
                ]
            },
        )

    def describe_fleet_location_capacity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("capacity", kwargs))
        return self._responses.get(
            "capacity",
            {
                "LocationCapacities": [
                    {
                        "Location": "us-west-2",
                        "InstanceCounts": {"DESIRED": 10, "MINIMUM": 2, "MAXIMUM": 20, "ACTIVE": 10, "IDLE": 2},
                    }
                ]
            },
        )

    def describe_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def describe_scaling_policies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("scaling", kwargs))
        return self._responses.get(
            "scaling",
            {"ScalingPolicies": [{"Name": "target", "Status": "ACTIVE", "MetricName": "PercentAvailable"}]},
        )


def test_read_utilization_is_normalized_and_drops_provider_fields() -> None:
    adapter = GameLiftObservationAdapter(FakeGameLift())
    result = adapter.read_utilization("fleet-x")
    assert result == {
        "active_server_processes": 12,
        "active_game_sessions": 8,
        "current_player_sessions": 30,
        "maximum_player_sessions": 100,
    }
    # No ARN or provider-specific key leaks into the normalized value.
    assert "FleetArn" not in result


def test_read_capacity_is_normalized_per_location() -> None:
    adapter = GameLiftObservationAdapter(FakeGameLift())
    result = adapter.read_capacity("fleet-x")
    assert result == [{"location": "us-west-2", "desired": 10, "minimum": 2, "maximum": 20, "active": 10, "idle": 2}]


def test_read_capacity_supports_single_fleet_capacity_shape() -> None:
    response = {
        "FleetCapacity": {
            "Location": "eu-west-1",
            "InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 3, "ACTIVE": 1, "IDLE": 0},
        }
    }
    adapter = GameLiftObservationAdapter(FakeGameLift(capacity=response))
    result = adapter.read_capacity("fleet-x")
    assert result[0]["location"] == "eu-west-1"


def test_read_scaling_policies_is_normalized() -> None:
    adapter = GameLiftObservationAdapter(FakeGameLift())
    result = adapter.read_scaling_policies("fleet-x")
    assert result == [{"name": "target", "status": "ACTIVE", "metric_name": "PercentAvailable"}]


def test_read_scaling_policies_empty_is_allowed() -> None:
    adapter = GameLiftObservationAdapter(FakeGameLift(scaling={"ScalingPolicies": []}))
    assert adapter.read_scaling_policies("fleet-x") == []


def test_unknown_scaling_status_fails_closed() -> None:
    adapter = GameLiftObservationAdapter(
        FakeGameLift(scaling={"ScalingPolicies": [{"Name": "n", "Status": "MYSTERY", "MetricName": "m"}]})
    )
    with pytest.raises(ValueError):
        adapter.read_scaling_policies("fleet-x")


def test_empty_utilization_fails_closed() -> None:
    adapter = GameLiftObservationAdapter(FakeGameLift(utilization={"FleetUtilization": []}))
    with pytest.raises(ValueError):
        adapter.read_utilization("fleet-x")


def test_negative_counter_fails_closed() -> None:
    adapter = GameLiftObservationAdapter(
        FakeGameLift(utilization={"FleetUtilization": [{"ActiveServerProcessCount": -1}]})
    )
    with pytest.raises(ValueError):
        adapter.read_utilization("fleet-x")


def test_adapter_exposes_exactly_three_read_methods() -> None:
    methods = {m for m in dir(GameLiftObservationAdapter) if not m.startswith("_")}
    assert methods == {"read_utilization", "read_capacity", "read_scaling_policies"}


def test_adapter_satisfies_the_reader_port_structurally() -> None:
    adapter = GameLiftObservationAdapter(FakeGameLift())
    port_methods = {m for m in dir(GameLiftObservationReader) if not m.startswith("_")}
    assert port_methods <= {name for name in dir(adapter) if not name.startswith("_")}
    assert all(callable(getattr(adapter, name)) for name in port_methods)
