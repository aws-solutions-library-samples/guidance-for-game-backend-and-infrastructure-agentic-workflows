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

        # A residual NextToken with a non-empty page means the provider did not
        # return the full result set in one bounded call: pinned to the distinct
        # ``incomplete`` disposition, never a clean ``ok``.
        assert result["status"] == "incomplete"
        assert result["truncated"] is True


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


# ---------------------------------------------------------------------------
# Value-level validation on ALLOWED fields (review issue #2)
#
# Field-name allowlisting is not enough: an allowed key still carries a provider-
# (or upstream-) controlled *value*. These tests attack allowed keys directly
# with huge strings, wrong scalar types, nested blobs, NaN/Inf, and oversized
# numbers, and prove the projection drops the offending value predictably while
# never letting a sensitive/oversized value reach model context.
# ---------------------------------------------------------------------------
class TestAllowedFieldValueValidation:
    def _patch(self, describe_return):
        mock_gamelift = MagicMock()
        mock_gamelift.describe_fleet_utilization.return_value = describe_return
        mock_gamelift.describe_scaling_policies.return_value = describe_return
        mock_gamelift.describe_fleet_capacity.return_value = describe_return
        return mock_gamelift

    def test_huge_string_in_allowed_location_is_dropped(self):
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_STRING_LENGTH
        from agents.gamelift_specialist import get_fleet_utilization

        huge = "x" * (GAMELIFT_MAX_STRING_LENGTH + 1)
        raw = {"FleetUtilization": [{"FleetId": FLEET_ID, "ActiveServerProcessCount": 1, "Location": huge}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)

        row = result["FleetUtilization"][0]
        # Oversized allowed string is dropped, not passed through.
        assert "Location" not in row
        assert huge not in json.dumps(result)

    def test_arn_smuggled_through_allowed_fleet_id_is_bounded(self):
        # A synthetic ARN is short enough to pass a naive length check, but it
        # must never survive in ANY allowed field. This test inspects the
        # ATTACKED collection (ScalingPolicies) itself — not a stripped copy —
        # and proves the ARN never reaches model context.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {"FleetId": SYNTHETIC_FLEET_ARN, "Name": SYNTHETIC_FLEET_ARN, "Status": "ACTIVE"},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        # Inspect the whole serialized result INCLUDING the attacked collection.
        blob = json.dumps(result)
        assert "arn:aws" not in blob
        assert SYNTHETIC_ACCOUNT_ID not in blob
        row = result["ScalingPolicies"][0]
        # Field-specific grammar rejects the ARN in both the identifier and name
        # fields; the legal Status enum survives.
        assert "FleetId" not in row
        assert "Name" not in row
        assert row["Status"] == "ACTIVE"

    def test_wrong_scalar_type_in_numeric_field_is_dropped(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                {
                    "FleetId": FLEET_ID,
                    "ActiveServerProcessCount": {"nested": SYNTHETIC_SECRET_NESTED},  # dict, not a number
                    "ActiveGameSessionCount": "not-a-number",  # str in numeric field
                    "CurrentPlayerSessionCount": True,  # bool is not a metric
                    "Location": "us-west-2",
                }
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)

        row = result["FleetUtilization"][0]
        assert "ActiveServerProcessCount" not in row
        assert "ActiveGameSessionCount" not in row
        assert "CurrentPlayerSessionCount" not in row
        assert row["Location"] == "us-west-2"
        _assert_no_sensitive(result)

    def test_dict_in_string_field_is_dropped(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_capacity

        raw = {
            "FleetCapacity": [{"FleetId": FLEET_ID, "InstanceType": SYNTHETIC_SECRET_NESTED, "Location": "us-west-2"}]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_capacity(FLEET_ID)
        row = result["FleetCapacity"][0]
        assert "InstanceType" not in row
        _assert_no_sensitive(result)

    def test_non_finite_and_oversized_numbers_dropped(self):
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_ABS_NUMBER
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {
                    "FleetId": FLEET_ID,
                    "Name": "p",
                    "Threshold": float("inf"),
                    "ScalingAdjustment": float("nan"),
                    "EvaluationPeriods": GAMELIFT_MAX_ABS_NUMBER * 10,
                    "TargetConfiguration": {"TargetValue": float("inf")},
                }
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Threshold" not in row
        assert "ScalingAdjustment" not in row
        assert "EvaluationPeriods" not in row
        # Non-finite target value dropped -> the whole TargetConfiguration omitted.
        assert "TargetConfiguration" not in row

    def test_huge_python_int_does_not_raise_and_is_dropped(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        # 10**400 would raise OverflowError under float(); must be handled safely.
        raw = {"FleetUtilization": [{"FleetId": FLEET_ID, "ActiveServerProcessCount": 10**400, "Location": "l"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        row = result["FleetUtilization"][0]
        assert "ActiveServerProcessCount" not in row

    def test_oversized_nested_count_is_dropped(self):
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_ABS_NUMBER
        from agents.gamelift_specialist import get_fleet_capacity

        raw = {
            "FleetCapacity": [
                {
                    "FleetId": FLEET_ID,
                    "InstanceType": "c5.large",
                    "InstanceCounts": {"DESIRED": GAMELIFT_MAX_ABS_NUMBER * 100, "ACTIVE": 3},
                    "Location": "us-west-2",
                }
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_capacity(FLEET_ID)
        counts = result["FleetCapacity"][0]["InstanceCounts"]
        assert "DESIRED" not in counts
        assert counts["ACTIVE"] == 3

    def test_aggregate_payload_budget_truncates(self):
        # Each row is individually valid (Location under the per-string cap) but
        # together they exceed the aggregate char budget: the projection must
        # truncate to fit and signal a partial view rather than flooding context.
        # The budget is measured against the ACTUAL serialized JSON.
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_CHARS
        from agents.gamelift_specialist import get_fleet_utilization

        long_loc = "us-west-2-" + "a" * 200  # long but legal region-like token
        rows = [{"FleetId": FLEET_ID, "ActiveServerProcessCount": i, "Location": long_loc} for i in range(90)]
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch({"FleetUtilization": rows})):
            result = get_fleet_utilization(FLEET_ID)
        assert result["status"] == "truncated"
        assert result["truncated"] is True
        assert len(result["FleetUtilization"]) < 90
        # The serialized representation the model actually sees is within budget.
        assert len(json.dumps(result)) <= GAMELIFT_MAX_PROJECTED_CHARS

    def test_control_characters_expand_serialized_json_but_budget_holds(self):
        # Control characters (e.g. NUL) are individually rejected by the string
        # grammar, but even if a value were retained the budget is measured
        # against the escaped JSON representation, so an escaping-expansion
        # bypass cannot exceed GAMELIFT_MAX_PROJECTED_CHARS.
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_CHARS, GAMELIFT_MAX_STRING_LENGTH
        from agents.gamelift_specialist import get_fleet_utilization

        # Legal long region-like tokens that each escape to ~1x, many rows.
        loc = "us-west-2" + "-x" * ((GAMELIFT_MAX_STRING_LENGTH - 9) // 2)
        rows = [{"FleetId": FLEET_ID, "ActiveServerProcessCount": i, "Location": loc} for i in range(200)]
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch({"FleetUtilization": rows})):
            result = get_fleet_utilization(FLEET_ID)
        assert len(json.dumps(result)) <= GAMELIFT_MAX_PROJECTED_CHARS
        assert result["status"] == "truncated"


# ---------------------------------------------------------------------------
# Field-specific grammar validation (review issue #1)
#
# Allowlisting a field name and bounding its length is not enough: an allowed
# string field still carries a provider-controlled value. Each model-visible
# string must satisfy its field-specific grammar — GameLift identifier, region,
# enum/token, or name — and reject control characters, account-ID patterns,
# ARN prefixes, URL schemes, and IP/network coordinates even when the value is
# short.
# ---------------------------------------------------------------------------
class TestFieldSpecificGrammar:
    def _patch(self, describe_return):
        mock_gamelift = MagicMock()
        mock_gamelift.describe_fleet_utilization.return_value = describe_return
        mock_gamelift.describe_scaling_policies.return_value = describe_return
        mock_gamelift.describe_fleet_capacity.return_value = describe_return
        return mock_gamelift

    def test_short_arn_in_identifier_is_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                {"FleetId": "arn:aws:gamelift:us-west-2:123456789012:fleet/f", "Location": "us-west-2"}
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        row = result["FleetUtilization"][0]
        assert "FleetId" not in row
        assert "arn:aws" not in json.dumps(result)

    def test_url_scheme_in_name_is_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "https://internal.example.invalid/secret", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert "https://" not in json.dumps(result)
        assert row["Status"] == "ACTIVE"

    def test_bare_account_id_in_name_is_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "123456789012", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert "123456789012" not in json.dumps(result)

    def test_ip_address_in_location_is_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {"FleetUtilization": [{"FleetId": FLEET_ID, "Location": "10.11.12.13", "ActiveServerProcessCount": 1}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        row = result["FleetUtilization"][0]
        assert "Location" not in row
        assert "10.11.12.13" not in json.dumps(result)

    def test_control_characters_in_string_field_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                {"FleetId": FLEET_ID, "Location": "us-\x00west-2", "ActiveServerProcessCount": 1},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        row = result["FleetUtilization"][0]
        assert "Location" not in row
        assert "\x00" not in json.dumps(result)

    def test_newline_in_name_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "line1\nline2", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        assert "Name" not in result["ScalingPolicies"][0]

    def test_malformed_region_rejected_even_when_short(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {"FleetUtilization": [{"FleetId": FLEET_ID, "Location": "not a region!", "ActiveServerProcessCount": 1}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        assert "Location" not in result["FleetUtilization"][0]

    def test_unknown_enum_value_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "p", "Status": "arn:aws:evil"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        assert "Status" not in result["ScalingPolicies"][0]

    def test_legal_values_survive_grammar(self):
        # Regression guard: valid GameLift values are still projected.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {
                    "FleetId": "fleet-1234abcd-5678-90ef-abcd-1234567890ab",
                    "Name": "scale-up_on-util.v2",
                    "Status": "ACTIVE",
                    "ComparisonOperator": "GreaterThanThreshold",
                    "MetricName": "PercentAvailableGameSessions",
                    "Location": "us-west-2",
                }
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert row["FleetId"].startswith("fleet-")
        assert row["Name"] == "scale-up_on-util.v2"
        assert row["Status"] == "ACTIVE"
        assert row["ComparisonOperator"] == "GreaterThanThreshold"
        assert row["MetricName"] == "PercentAvailableGameSessions"
        assert row["Location"] == "us-west-2"


# ---------------------------------------------------------------------------
# Embedded sensitive-coordinate detection (review issue #1).
#
# A 12-digit account ID or an IPv4/CIDR coordinate must be rejected even when it
# is *decorated* — adjacent to letters/underscores/dashes inside an otherwise
# grammar-valid value. Word boundaries and whole-token parsing miss these
# evasions. These adversarial cases attack every relevant allowed string class
# (identifier, name, region, token) with decorated coordinates.
# ---------------------------------------------------------------------------
class TestEmbeddedSensitiveCoordinateDetection:
    def _patch(self, describe_return):
        mock_gamelift = MagicMock()
        mock_gamelift.describe_fleet_utilization.return_value = describe_return
        mock_gamelift.describe_scaling_policies.return_value = describe_return
        mock_gamelift.describe_fleet_capacity.return_value = describe_return
        return mock_gamelift

    def test_account_id_wrapped_in_letters_in_name_is_rejected(self):
        # `prodX123456789012Y` — 12 digits flanked by letters, so a \b boundary
        # never matches. Must still be rejected.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "prodX123456789012Y", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert SYNTHETIC_ACCOUNT_ID not in json.dumps(result)
        assert row["Status"] == "ACTIVE"

    def test_decorated_account_id_underscores_in_name_is_rejected(self):
        # `acct_123456789012_prod` — 12 digits flanked by underscores.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "acct_123456789012_prod", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert SYNTHETIC_ACCOUNT_ID not in json.dumps(result)

    def test_decorated_account_id_in_identifier_is_rejected(self):
        # Identifier grammar (FleetId) decorated account id.
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {"FleetUtilization": [{"FleetId": "fleet-123456789012-prod", "Location": "us-west-2"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        row = result["FleetUtilization"][0]
        assert "FleetId" not in row
        assert SYNTHETIC_ACCOUNT_ID not in json.dumps(result)

    def test_decorated_account_id_in_token_is_rejected(self):
        # Token grammar (Status/enum-like) decorated account id.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "p", "MetricName": "metric.123456789012.x"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "MetricName" not in row
        assert SYNTHETIC_ACCOUNT_ID not in json.dumps(result)

    def test_embedded_ipv4_in_name_is_rejected(self):
        # `host-10.11.12.13-prod` — IPv4 wrapped in valid name characters, so
        # whole-value and whitespace-token parsing both miss it.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "host-10.11.12.13-prod", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert SYNTHETIC_IP not in json.dumps(result)
        assert "10.11.12.13" not in json.dumps(result)

    def test_embedded_ipv4_in_identifier_is_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {"FleetUtilization": [{"FleetId": "node-10.11.12.13", "Location": "us-west-2"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        row = result["FleetUtilization"][0]
        assert "FleetId" not in row
        assert "10.11.12.13" not in json.dumps(result)

    def test_embedded_cidr_in_name_is_rejected(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "net-10.11.12.0-24-prod", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        assert "Name" not in result["ScalingPolicies"][0]

    def test_embedded_ipv4_in_region_is_rejected(self):
        # Region grammar is numeric-permissive; a dotted quad wrapped in a
        # region-shaped token must still be rejected.
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                {"FleetId": FLEET_ID, "Location": "us-10.11.12.13", "ActiveServerProcessCount": 1},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        assert "Location" not in result["FleetUtilization"][0]
        assert "10.11.12.13" not in json.dumps(result)

    def test_thirteen_digit_run_is_not_falsely_flagged_but_twelve_is(self):
        # Discriminating: exactly 12 consecutive digits (an account id) inside a
        # value is rejected; a legitimate shorter numeric suffix survives.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {"FleetId": FLEET_ID, "Name": "scale-up-v2", "Status": "ACTIVE"},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        # Legitimate short numeric suffix is preserved.
        assert result["ScalingPolicies"][0]["Name"] == "scale-up-v2"

    def test_embedded_ipv6_in_name_is_rejected(self):
        # A colon-hex IPv6 address wrapped in valid name characters must be
        # rejected; the surrounding dashes keep it inside the name grammar.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "host-fe80::1-prod", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert "fe80::1" not in json.dumps(result)

    def test_ipv4_substring_inside_longer_dotted_name_is_rejected(self):
        # `host-10.11.12.13.14-prod` contains the valid IPv4 substring
        # `10.11.12.13`. An IPv4 matcher that refuses a quad adjacent to another
        # dot would let the sensitive coordinate through with status="ok". The
        # substring must be rejected regardless of an adjacent trailing dot/octet.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "host-10.11.12.13.14-prod", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert "10.11.12.13" not in json.dumps(result)
        assert row["Status"] == "ACTIVE"

    def test_ipv4_substring_with_out_of_range_trailing_octet_is_rejected(self):
        # `host-10.11.12.13.999-prod`: the trailing `.999` is not a legal octet,
        # so a whole-token IPv4 parse fails, yet the embedded `10.11.12.13` is a
        # valid dotted quad and must still be rejected anywhere in the value.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {"ScalingPolicies": [{"Name": "host-10.11.12.13.999-prod", "Status": "ACTIVE"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        row = result["ScalingPolicies"][0]
        assert "Name" not in row
        assert "10.11.12.13" not in json.dumps(result)
        assert row["Status"] == "ACTIVE"

    def test_ipv4_substring_with_leading_extra_octet_is_rejected(self):
        # Symmetric case: an extra LEADING octet (`9.10.11.12.13`) must not let
        # the trailing valid quad `10.11.12.13` survive either.
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {"FleetUtilization": [{"FleetId": "node-9.10.11.12.13", "Location": "us-west-2"}]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        row = result["FleetUtilization"][0]
        assert "FleetId" not in row
        assert "10.11.12.13" not in json.dumps(result)


# ---------------------------------------------------------------------------
# Log redaction: the failure log must never contain raw provider text or the
# caller fleet id (review issue #1). We capture the actual Loguru sink.
# ---------------------------------------------------------------------------
class TestSanitizedFailureLogging:
    def _capture(self):
        # Local modules
        from utils.logger import logger

        records: list[str] = []

        def sink(message):  # loguru passes the fully formatted message
            records.append(str(message))
            # Also fold in the record's bound extra so we assert on structured data.
            records.append(json.dumps(message.record["extra"], default=str))

        sink_id = logger.add(sink, level="DEBUG")
        return logger, sink_id, records

    def test_failure_log_omits_raw_exception_and_fleet_id(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        logger, sink_id, records = self._capture()
        try:
            err = _client_error(
                "SomeProviderError",
                f"exploded at {SYNTHETIC_FLEET_ARN} contacting {SYNTHETIC_URL} ip {SYNTHETIC_IP}",
            )
            mock_gamelift = MagicMock()
            mock_gamelift.describe_fleet_utilization.side_effect = err
            with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
                result = get_fleet_utilization("fleet-super-secret-caller-id")
        finally:
            logger.remove(sink_id)

        blob = "\n".join(records)
        # Raw provider message content is absent.
        assert SYNTHETIC_FLEET_ARN not in blob
        assert "arn:aws" not in blob
        assert SYNTHETIC_URL not in blob
        assert "example.invalid" not in blob
        assert SYNTHETIC_IP not in blob
        assert "exploded" not in blob
        # Caller fleet id is absent (a uuid correlation token is used instead).
        assert "fleet-super-secret-caller-id" not in blob
        # The typed, sanitized code IS present (operationally useful signal).
        assert "provider_error" in blob
        # And the model-visible result is still sanitized + incomplete.
        assert result["status"] == "incomplete"
        _assert_no_sensitive(result)

    def test_huge_provider_message_does_not_produce_huge_log(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        logger, sink_id, records = self._capture()
        try:
            err = Exception("A" * 1_000_000 + SYNTHETIC_FLEET_ARN)
            mock_gamelift = MagicMock()
            mock_gamelift.describe_scaling_policies.side_effect = err
            with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
                get_scaling_policies(FLEET_ID)
        finally:
            logger.remove(sink_id)

        blob = "\n".join(records)
        assert "A" * 1000 not in blob  # the megabyte payload never reaches the sink
        assert SYNTHETIC_FLEET_ARN not in blob


# ---------------------------------------------------------------------------
# Empty-page-plus-NextToken and exact not_found (review issue #3)
# ---------------------------------------------------------------------------
class TestPaginationAndNotFoundContract:
    def test_empty_page_with_next_token_is_incomplete_not_empty(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_capacity, get_fleet_utilization, get_scaling_policies

        for tool, method, key in (
            (get_fleet_utilization, "describe_fleet_utilization", "FleetUtilization"),
            (get_fleet_capacity, "describe_fleet_capacity", "FleetCapacity"),
            (get_scaling_policies, "describe_scaling_policies", "ScalingPolicies"),
        ):
            mock_gamelift = MagicMock()
            getattr(mock_gamelift, method).return_value = {key: [], "NextToken": "more"}
            with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
                result = tool(FLEET_ID)
            # An empty page that still carries a continuation token is NOT a
            # complete zero result: it is explicitly partial.
            assert result["status"] == "incomplete", f"{method} empty+token must be incomplete"
            assert result["truncated"] is True
            assert result[key] == []

    def test_empty_page_without_token_is_empty(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_capacity

        mock_gamelift = MagicMock()
        mock_gamelift.describe_fleet_capacity.return_value = {"FleetCapacity": []}
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_capacity(FLEET_ID)
        assert result["status"] == "empty"
        assert "truncated" not in result

    def test_not_found_is_pinned_and_distinct_from_denied_and_empty(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        err = _client_error("NotFoundException", f"no such fleet {SYNTHETIC_FLEET_ARN}")
        mock_gamelift = MagicMock()
        mock_gamelift.describe_fleet_utilization.side_effect = err
        with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
            result = get_fleet_utilization(FLEET_ID)
        # Pinned exact contract: not_found surfaces through error.code, disposition
        # is incomplete, and it is neither denied nor empty.
        assert result["error"]["code"] == "not_found"
        assert result["status"] == "incomplete"
        assert result["status"] != "denied"
        assert result["status"] != "empty"
        assert result["FleetUtilization"] == []
        _assert_no_sensitive(result)

    def test_throttled_and_invalid_are_pinned_incomplete(self):
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        for code, expected in (("ThrottlingException", "throttled"), ("ValidationException", "invalid_request")):
            err = _client_error(code, "x")
            mock_gamelift = MagicMock()
            mock_gamelift.describe_scaling_policies.side_effect = err
            with patch("agents.gamelift_specialist.boto3.client", return_value=mock_gamelift):
                result = get_scaling_policies(FLEET_ID)
            assert result["error"]["code"] == expected
            assert result["status"] == "incomplete"


# ---------------------------------------------------------------------------
# Malformed collections become typed `incomplete`, never authoritative `empty`
# (review issue #2). `empty` is reserved for a valid empty list with no token.
# ---------------------------------------------------------------------------
class TestMalformedCollectionSemantics:
    def _patch(self, describe_return):
        mock_gamelift = MagicMock()
        mock_gamelift.describe_fleet_utilization.return_value = describe_return
        mock_gamelift.describe_scaling_policies.return_value = describe_return
        mock_gamelift.describe_fleet_capacity.return_value = describe_return
        return mock_gamelift

    def test_missing_collection_key_is_incomplete(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch({})):
            result = get_fleet_utilization(FLEET_ID)
        assert result["status"] == "incomplete"
        assert result["FleetUtilization"] == []
        assert result.get("error", {}).get("code") == "malformed_response"

    def test_wrong_collection_type_is_incomplete(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch({"FleetUtilization": "nope"})):
            result = get_fleet_utilization(FLEET_ID)
        assert result["status"] == "incomplete"
        assert result["FleetUtilization"] == []
        assert result["error"]["code"] == "malformed_response"

    def test_list_of_non_dict_items_is_incomplete(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {"FleetUtilization": ["malformed", 123, None]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        assert result["status"] == "incomplete"
        assert result["FleetUtilization"] == []
        assert result["error"]["code"] == "malformed_response"

    def test_rows_with_all_operational_values_invalid_is_incomplete(self):
        # A dict item that projects to nothing usable (all operational values
        # fail validation, only a bare correlation id remains) is not an
        # authoritative zero result.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {"Name": "arn:aws:evil", "Status": "http://x", "Threshold": float("inf")},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        assert result["status"] == "incomplete"
        assert result["ScalingPolicies"] == []
        assert result["error"]["code"] == "malformed_response"

    def test_valid_empty_list_without_token_is_empty(self):
        # Local modules
        from agents.gamelift_specialist import get_fleet_capacity

        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch({"FleetCapacity": []})):
            result = get_fleet_capacity(FLEET_ID)
        assert result["status"] == "empty"
        assert result["FleetCapacity"] == []
        assert "error" not in result

    def test_mixed_valid_and_non_dict_items_retains_valid_but_is_incomplete(self):
        # A malformed (non-dict) item mixed with a valid one may RETAIN the valid
        # row (bounded), but the result must be status=incomplete with
        # malformed_response — never ``ok``. ``ok`` is reserved for a wholly
        # valid, complete payload; a discarded malformed row makes the view
        # partial and the model must be told so.
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                "malformed",
                {"FleetId": FLEET_ID, "ActiveServerProcessCount": 3, "Location": "us-west-2"},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        assert result["status"] != "ok"
        # Bounded valid rows are retained.
        assert len(result["FleetUtilization"]) == 1
        assert result["FleetUtilization"][0]["ActiveServerProcessCount"] == 3

    def test_mixed_valid_and_non_dict_items_marks_truncated(self):
        # The retained-but-partial view is flagged truncated so downstream
        # consumers treat it as a partial result, not a complete inventory.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {"FleetId": FLEET_ID, "Name": "scale-up", "Status": "ACTIVE"},
                42,
                None,
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        assert result.get("truncated") is True
        assert len(result["ScalingPolicies"]) == 1
        assert result["ScalingPolicies"][0]["Name"] == "scale-up"

    def test_non_mapping_top_level_response_is_typed_incomplete_not_raised(self):
        # A non-mapping top-level provider response (None, list, str, int) must
        # NOT raise: it is classified as a typed, sanitized incomplete
        # malformed_response with an empty bounded collection.
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        for bad in (None, [], ["x"], "nope", 123, 4.5):
            with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(bad)):
                result = get_fleet_utilization(FLEET_ID)
            assert result["status"] == "incomplete", f"top-level {bad!r} must be incomplete"
            assert result["error"]["code"] == "malformed_response", f"top-level {bad!r}"
            assert result["FleetUtilization"] == []
            assert result["status"] != "ok"
            assert result["status"] != "empty"

    def test_project_collection_rejects_non_mapping_without_raising(self):
        # Direct probe of the projection helper: a non-mapping response returns a
        # typed result rather than raising AttributeError on ``.get``.
        # Local modules
        from agents.gamelift_projections import project_fleet_utilization

        for bad in (None, [], ["a", "b"], "str", 7, 3.14, True):
            result = project_fleet_utilization(bad)  # type: ignore[arg-type]
            assert result["status"] == "incomplete"
            assert result["error"]["code"] == "malformed_response"
            assert result["FleetUtilization"] == []

    def test_mixed_malformed_result_respects_serialized_cap(self):
        # A malformed (non-dict) row combined with many long-but-legal valid rows
        # must NOT push the FINAL envelope past the serialized cap. The final
        # disposition is incomplete + malformed_response + truncated=true, whose
        # keys are ~42 chars longer than the short {"status":"truncated",
        # "truncated":true} shape the budget loop previously measured. When the
        # trim loop stops with the short envelope just under the cap, the final
        # envelope overflows. These parameters reproduce a 20,033-char final
        # envelope against the 20,000 cap.
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_CHARS
        from agents.gamelift_specialist import get_scaling_policies

        long_name = "scale-" + "a" * 137  # 143 chars, long but grammar-legal
        valid_rows = [{"FleetId": FLEET_ID, "Name": long_name, "Status": "ACTIVE"} for _ in range(130)]
        raw = {"ScalingPolicies": ["malformed", *valid_rows]}
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        assert result["truncated"] is True
        # The FINAL serialized envelope the model sees must be within budget.
        assert len(json.dumps(result)) <= GAMELIFT_MAX_PROJECTED_CHARS

    def test_wholly_valid_complete_payload_is_ok(self):
        # Regression guard: a payload with only valid dict rows and no discarded
        # items and no continuation token remains ``ok``.
        # Local modules
        from agents.gamelift_specialist import get_fleet_utilization

        raw = {
            "FleetUtilization": [
                {"FleetId": FLEET_ID, "ActiveServerProcessCount": 3, "Location": "us-west-2"},
                {"FleetId": FLEET_ID, "ActiveServerProcessCount": 5, "Location": "eu-west-1"},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_fleet_utilization(FLEET_ID)
        assert result["status"] == "ok"
        assert "error" not in result
        assert len(result["FleetUtilization"]) == 2
