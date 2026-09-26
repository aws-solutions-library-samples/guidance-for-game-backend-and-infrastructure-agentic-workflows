"""Unit tests for GameLift provider-response projections and sanitized errors.

Part of #457: the three raw GameLift Servers tools (get_fleet_utilization,
get_fleet_capacity, get_scaling_policies) previously returned the raw boto3
response and ``str(e)`` on failure. That path leaked account IDs and full ARNs
(FleetArn), provider exception text, and unbounded collections into model
context, and could not distinguish empty / denied / incomplete / truncated
results.

These tests prove, with synthetic sensitive values, that:

* account IDs, full ARNs, URLs / network coordinates, arbitrary nested fields,
  and raw provider exception text never appear in the model-visible projection;
* collections are bounded and truncation is signalled distinctly;
* empty, denied, incomplete, and truncated results are kept distinct.
"""

# Standard library
import json
from typing import Any
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit


# Synthetic sensitive values. None of these may appear in a projection.
SYNTHETIC_ACCOUNT_ID = "123456789012"
SYNTHETIC_FLEET_ARN = f"arn:aws:gamelift:us-west-2:{SYNTHETIC_ACCOUNT_ID}:fleet/fleet-synthetic-0001"
SYNTHETIC_URL = "https://internal.example.invalid/secret/path?token=abc123"
SYNTHETIC_IP = "10.11.12.13"
SYNTHETIC_SECRET_NESTED = {"CredentialArn": SYNTHETIC_FLEET_ARN, "Endpoint": SYNTHETIC_URL, "PrivateIp": SYNTHETIC_IP}
FLEET_ID = "fleet-synthetic-0001"


def _client_error(code: str, message: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"RequestId": "req-synthetic"}},
        "DescribeFleetUtilization",
    )


def _assert_no_sensitive(payload: dict[str, Any]) -> None:
    blob = json.dumps(payload)
    assert SYNTHETIC_ACCOUNT_ID not in blob, f"account id leaked: {blob}"
    assert "arn:aws" not in blob, f"ARN leaked: {blob}"
    assert SYNTHETIC_URL not in blob, f"URL leaked: {blob}"
    assert "example.invalid" not in blob, f"network coordinate leaked: {blob}"
    assert SYNTHETIC_IP not in blob, f"IP leaked: {blob}"


# ---------------------------------------------------------------------------
# get_fleet_utilization
# ---------------------------------------------------------------------------
class TestFleetUtilizationProjection:
    def _patch_client(self, describe_return=None, describe_side_effect=None):
        mock_gamelift = MagicMock()
        if describe_side_effect is not None:
            mock_gamelift.describe_fleet_utilization.side_effect = describe_side_effect
        else:
            mock_gamelift.describe_fleet_utilization.return_value = describe_return
        return mock_gamelift

    def test_projects_operational_fields_and_drops_arn_account(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                {
                    "FleetId": FLEET_ID,
                    "FleetArn": SYNTHETIC_FLEET_ARN,
                    "ActiveServerProcessCount": 4,
                    "ActiveGameSessionCount": 2,
                    "CurrentPlayerSessionCount": 7,
                    "MaximumPlayerSessionCount": 50,
                    "Location": "us-west-2",
                    "UnknownFutureField": SYNTHETIC_SECRET_NESTED,
                }
            ]
        }
        mock_gamelift = self._patch_client(describe_return=raw)
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_ID)

        assert result["status"] == "ok"
        assert len(result["FleetUtilization"]) == 1
        row = result["FleetUtilization"][0]
        # Operational fields preserved
        assert row["ActiveServerProcessCount"] == 4
        assert row["ActiveGameSessionCount"] == 2
        assert row["CurrentPlayerSessionCount"] == 7
        assert row["MaximumPlayerSessionCount"] == 50
        assert row["Location"] == "us-west-2"
        # Sensitive / arbitrary fields dropped
        assert "FleetArn" not in row
        assert "UnknownFutureField" not in row
        _assert_no_sensitive(result)

    def test_empty_is_distinct_from_denied(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        mock_gamelift = self._patch_client(describe_return={"FleetUtilization": []})
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_ID)

        assert result["status"] == "empty"
        assert result["FleetUtilization"] == []
        assert "error" not in result

    def test_access_denied_is_typed_and_sanitized(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        err = _client_error(
            "AccessDeniedException",
            f"User arn:aws:iam::{SYNTHETIC_ACCOUNT_ID}:role/x is not authorized; see {SYNTHETIC_URL}",
        )
        mock_gamelift = self._patch_client(describe_side_effect=err)
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_ID)

        assert result["status"] == "denied"
        assert result["FleetUtilization"] == []
        # Typed, sanitized error code — never the raw provider message
        assert result["error"]["code"] == "access_denied"
        _assert_no_sensitive(result)
        assert SYNTHETIC_URL not in json.dumps(result)

    def test_generic_provider_exception_is_sanitized_incomplete(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        err = Exception(f"boom at {SYNTHETIC_FLEET_ARN} contacting {SYNTHETIC_URL}")
        mock_gamelift = self._patch_client(describe_side_effect=err)
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_ID)

        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "provider_error"
        _assert_no_sensitive(result)

    def test_client_construction_failure_is_sanitized(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        with patch(
            "agents.gamelift_specialist.boto3.client",
            side_effect=Exception(f"cannot reach {SYNTHETIC_URL}"),
        ):
            result = get_fleet_utilization(FLEET_ID)

        assert result["status"] == "incomplete"
        assert result["FleetUtilization"] == []
        _assert_no_sensitive(result)

    def test_collection_is_bounded_and_truncation_signalled(self):
        # Local modules
        from agents.gamelift_specialist import GAMELIFT_MAX_PROJECTED_ITEMS, get_fleet_utilization

        rows = [
            {
                "FleetId": FLEET_ID,
                "FleetArn": SYNTHETIC_FLEET_ARN,
                "ActiveServerProcessCount": i,
                "Location": f"loc-{i}",
            }
            for i in range(GAMELIFT_MAX_PROJECTED_ITEMS + 25)
        ]
        mock_gamelift = self._patch_client(describe_return={"FleetUtilization": rows})
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_ID)

        assert result["status"] == "truncated"
        assert len(result["FleetUtilization"]) == GAMELIFT_MAX_PROJECTED_ITEMS
        assert result["truncated"] is True
        _assert_no_sensitive(result)

    def test_next_token_marks_incomplete_when_more_pages(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [{"FleetId": FLEET_ID, "ActiveServerProcessCount": 1, "Location": "us-west-2"}],
            "NextToken": "more-pages-token",
        }
        mock_gamelift = self._patch_client(describe_return=raw)
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_ID)

        # A residual NextToken means the provider did not return the full result
        # set in one call: distinct from a clean "ok".
        assert result["status"] in {"incomplete", "truncated"}
        assert result.get("truncated") is True


# ---------------------------------------------------------------------------
# get_fleet_capacity
# ---------------------------------------------------------------------------
class TestFleetCapacityProjection:
    def test_projects_instance_counts_and_drops_arn(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_capacity

        raw = {
            "FleetCapacity": [
                {
                    "FleetId": FLEET_ID,
                    "FleetArn": SYNTHETIC_FLEET_ARN,
                    "InstanceType": "c5.large",
                    "InstanceCounts": {
                        "DESIRED": 3,
                        "MINIMUM": 1,
                        "MAXIMUM": 10,
                        "PENDING": 0,
                        "ACTIVE": 3,
                        "IDLE": 0,
                        "TERMINATING": 0,
                    },
                    "Location": "us-west-2",
                    "SecretNested": SYNTHETIC_SECRET_NESTED,
                }
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.describe_fleet_capacity.return_value = raw
            mock_client.return_value = mock_gamelift
            result = get_fleet_capacity(FLEET_ID)

        assert result["status"] == "ok"
        row = result["FleetCapacity"][0]
        assert row["InstanceType"] == "c5.large"
        assert row["InstanceCounts"]["DESIRED"] == 3
        assert row["InstanceCounts"]["ACTIVE"] == 3
        assert row["Location"] == "us-west-2"
        assert "FleetArn" not in row
        assert "SecretNested" not in row
        _assert_no_sensitive(result)

    def test_denied_capacity_is_typed(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_capacity

        err = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": f"denied {SYNTHETIC_FLEET_ARN}"}},
            "DescribeFleetCapacity",
        )
        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.describe_fleet_capacity.side_effect = err
            mock_client.return_value = mock_gamelift
            result = get_fleet_capacity(FLEET_ID)

        assert result["status"] == "denied"
        assert result["error"]["code"] == "access_denied"
        assert result["FleetCapacity"] == []
        _assert_no_sensitive(result)

    def test_empty_capacity(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_capacity

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.describe_fleet_capacity.return_value = {"FleetCapacity": []}
            mock_client.return_value = mock_gamelift
            result = get_fleet_capacity(FLEET_ID)

        assert result["status"] == "empty"
        assert result["FleetCapacity"] == []


# ---------------------------------------------------------------------------
# get_scaling_policies
# ---------------------------------------------------------------------------
class TestScalingPoliciesProjection:
    def test_projects_policy_fields_and_drops_arn(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {
                    "FleetId": FLEET_ID,
                    "FleetArn": SYNTHETIC_FLEET_ARN,
                    "Name": "scale-up-on-util",
                    "Status": "ACTIVE",
                    "ScalingAdjustment": 2,
                    "ScalingAdjustmentType": "ChangeInCapacity",
                    "ComparisonOperator": "GreaterThanThreshold",
                    "Threshold": 80.0,
                    "EvaluationPeriods": 2,
                    "MetricName": "PercentAvailableGameSessions",
                    "PolicyType": "RuleBased",
                    "TargetConfiguration": {"TargetValue": 75.0},
                    "UpdateStatus": "PENDING_UPDATE",
                    "Location": "us-west-2",
                    "InternalEndpoint": SYNTHETIC_URL,
                }
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.describe_scaling_policies.return_value = raw
            mock_client.return_value = mock_gamelift
            result = get_scaling_policies(FLEET_ID)

        assert result["status"] == "ok"
        row = result["ScalingPolicies"][0]
        assert row["Name"] == "scale-up-on-util"
        assert row["Status"] == "ACTIVE"
        assert row["MetricName"] == "PercentAvailableGameSessions"
        assert row["TargetConfiguration"] == {"TargetValue": 75.0}
        assert "FleetArn" not in row
        assert "InternalEndpoint" not in row
        _assert_no_sensitive(result)

    def test_scaling_policies_empty(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.describe_scaling_policies.return_value = {"ScalingPolicies": []}
            mock_client.return_value = mock_gamelift
            result = get_scaling_policies(FLEET_ID)

        assert result["status"] == "empty"
        assert result["ScalingPolicies"] == []

    def test_scaling_policies_generic_error_incomplete(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        with patch("agents.gamelift_specialist.boto3.client") as mock_client:
            mock_gamelift = MagicMock()
            mock_gamelift.describe_scaling_policies.side_effect = Exception(f"kaboom {SYNTHETIC_FLEET_ARN}")
            mock_client.return_value = mock_gamelift
            result = get_scaling_policies(FLEET_ID)

        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "provider_error"
        _assert_no_sensitive(result)
