"""Normalized GameLift execution adapter tests (#415, E3 execute).

The E3 execution adapter owns the single provider write method permitted in
this milestone — GameLift ``UpdateFleetCapacity`` — plus the read used before
and after the write, ``DescribeFleetCapacity``. These tests assert:

* the adapter exposes exactly one write method and no other write surface;
* the write passes exactly the bounded desired/min/max for the fleet+location;
* reads normalize to the bounded capacity shape and drop every ARN/account id;
* a deterministic provider error is classified as a clear ``PROVIDER_ERROR``
  failure (not a lost response);
* a read/connect timeout on the write is classified as an *inconclusive*
  (lost-response) outcome, never a clear success or failure — so the caller
  must Describe before any retry and never blind-retries.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.execute.gamelift_adapter import (
    GameLiftExecutionAdapter,
    ProviderWriteInconclusive,
    ProviderWriteRejected,
)


class _FakeGameLift:
    def __init__(self) -> None:
        self.update_calls: list[dict[str, Any]] = []
        self.describe_responses: list[Any] = []
        self.update_effect: Any = {"FleetId": "fleet-1234abcd", "FleetArn": "arn:aws:gamelift:...:fleet/x"}

    def describe_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]:
        if self.describe_responses:
            effect = self.describe_responses.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        raise AssertionError("unexpected describe call")

    def update_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        if isinstance(self.update_effect, Exception):
            raise self.update_effect
        return self.update_effect


def _capacity_response(desired: int, minimum: int, maximum: int, location: str = "us-west-2") -> dict[str, Any]:
    return {
        "FleetCapacity": [
            {
                "FleetId": "fleet-1234abcd",
                "FleetArn": "arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-1234abcd",
                "Location": location,
                "InstanceCounts": {
                    "DESIRED": desired,
                    "MINIMUM": minimum,
                    "MAXIMUM": maximum,
                    "ACTIVE": desired,
                    "IDLE": 0,
                },
            }
        ]
    }


def _client_error(code: str):
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "raw provider message with fleet arn"}}, "UpdateFleetCapacity"
    )


def _timeout():
    # Third-party packages
    from botocore.exceptions import ReadTimeoutError

    return ReadTimeoutError(endpoint_url="https://gamelift.us-west-2.amazonaws.com")


def test_adapter_exposes_single_write_method() -> None:
    write_methods = [
        name for name in dir(GameLiftExecutionAdapter) if not name.startswith("_") and name not in {"describe_capacity"}
    ]
    assert write_methods == ["update_capacity"]


def test_describe_capacity_normalizes_and_drops_arns() -> None:
    fake = _FakeGameLift()
    fake.describe_responses = [_capacity_response(10, 2, 20)]
    adapter = GameLiftExecutionAdapter(fake)
    observed = adapter.describe_capacity(fleet_id="fleet-1234abcd", location="us-west-2")
    assert observed == {"desired": 10, "minimum": 2, "maximum": 20}
    assert "arn" not in repr(observed).lower()


def test_update_capacity_issues_exact_bounded_write() -> None:
    fake = _FakeGameLift()
    adapter = GameLiftExecutionAdapter(fake)
    adapter.update_capacity(
        fleet_id="fleet-1234abcd",
        location="us-west-2",
        desired=14,
        minimum=2,
        maximum=20,
    )
    assert len(fake.update_calls) == 1
    call = fake.update_calls[0]
    assert call == {
        "FleetId": "fleet-1234abcd",
        "Location": "us-west-2",
        "DesiredInstances": 14,
        "MinSize": 2,
        "MaxSize": 20,
    }


def test_provider_error_is_a_clear_rejection() -> None:
    fake = _FakeGameLift()
    fake.update_effect = _client_error("InvalidRequestException")
    adapter = GameLiftExecutionAdapter(fake)
    with pytest.raises(ProviderWriteRejected) as exc:
        adapter.update_capacity(fleet_id="fleet-1234abcd", location="us-west-2", desired=14, minimum=2, maximum=20)
    # Public-safe: raw provider message never surfaces.
    assert "raw provider message" not in str(exc.value)
    assert exc.value.error_code == "PROVIDER_ERROR"


def test_write_timeout_is_inconclusive_not_a_failure() -> None:
    fake = _FakeGameLift()
    fake.update_effect = _timeout()
    adapter = GameLiftExecutionAdapter(fake)
    with pytest.raises(ProviderWriteInconclusive):
        adapter.update_capacity(fleet_id="fleet-1234abcd", location="us-west-2", desired=14, minimum=2, maximum=20)
    # The write may or may not have landed; the caller must Describe before any
    # retry. Exactly one call was attempted.
    assert len(fake.update_calls) == 1
