"""Integration coverage for GameLift provider projections (#457).

These drive the three raw GameLift Servers tools through a *real* boto3 GameLift
client backed by ``botocore.stub.Stubber``. Unlike the unit tests (which mock
the client), the Stubber validates the stubbed response against the actual
GameLift service model, so this proves the projection allowlist holds against
schema-valid provider responses — including the sensitive ``FleetArn`` that the
real API returns — without any live AWS call.
"""

# Standard library
import json
from unittest.mock import patch

# Third-party packages
import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

pytestmark = pytest.mark.integration

SYNTHETIC_ACCOUNT_ID = "123456789012"
SYNTHETIC_FLEET_ARN = f"arn:aws:gamelift:us-west-2:{SYNTHETIC_ACCOUNT_ID}:fleet/fleet-int-0001"
FLEET_ID = "fleet-int-0001"


def _no_sensitive(result: dict) -> None:
    blob = json.dumps(result)
    assert SYNTHETIC_ACCOUNT_ID not in blob
    assert "arn:aws" not in blob


def _stubbed_client():
    client = boto3.client(
        "gamelift",
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(retries={"max_attempts": 0}),
    )
    return client, Stubber(client)


def test_get_fleet_utilization_projects_real_shape():
    # Local modules
    from agents.gamelift_specialist import get_fleet_utilization

    client, stubber = _stubbed_client()
    stubber.add_response(
        "describe_fleet_utilization",
        {
            "FleetUtilization": [
                {
                    "FleetId": FLEET_ID,
                    "FleetArn": SYNTHETIC_FLEET_ARN,
                    "ActiveServerProcessCount": 5,
                    "ActiveGameSessionCount": 3,
                    "CurrentPlayerSessionCount": 9,
                    "MaximumPlayerSessionCount": 40,
                    "Location": "us-west-2",
                }
            ]
        },
        {"FleetIds": [FLEET_ID]},
    )
    with stubber, patch("agents.gamelift_specialist.boto3.client", return_value=client):
        result = get_fleet_utilization(FLEET_ID)

    assert result["status"] == "ok"
    row = result["FleetUtilization"][0]
    assert row["ActiveServerProcessCount"] == 5
    assert "FleetArn" not in row
    _no_sensitive(result)


def test_get_fleet_capacity_denied_is_typed():
    # Local modules
    from agents.gamelift_specialist import get_fleet_capacity

    client, stubber = _stubbed_client()
    stubber.add_client_error(
        "describe_fleet_capacity",
        service_error_code="AccessDeniedException",
        service_message=f"not authorized for {SYNTHETIC_FLEET_ARN}",
    )
    with stubber, patch("agents.gamelift_specialist.boto3.client", return_value=client):
        result = get_fleet_capacity(FLEET_ID)

    assert result["status"] == "denied"
    assert result["error"]["code"] == "access_denied"
    assert result["FleetCapacity"] == []
    _no_sensitive(result)


def test_get_scaling_policies_empty_distinct():
    # Local modules
    from agents.gamelift_specialist import get_scaling_policies

    client, stubber = _stubbed_client()
    stubber.add_response("describe_scaling_policies", {"ScalingPolicies": []}, {"FleetId": FLEET_ID})
    with stubber, patch("agents.gamelift_specialist.boto3.client", return_value=client):
        result = get_scaling_policies(FLEET_ID)

    assert result["status"] == "empty"
    assert result["ScalingPolicies"] == []
    assert "error" not in result
