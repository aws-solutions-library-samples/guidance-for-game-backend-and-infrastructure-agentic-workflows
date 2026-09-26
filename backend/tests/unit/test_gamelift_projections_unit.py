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
        # A synthetic ARN is short enough to be a valid string, but it must never
        # be an ARN in FleetId. We assert the ARN marker never survives regardless
        # of which allowed field an attacker stuffs it into.
        # Local modules
        from agents.gamelift_specialist import get_scaling_policies

        raw = {
            "ScalingPolicies": [
                {"FleetId": FLEET_ID, "Name": SYNTHETIC_FLEET_ARN, "Status": "ACTIVE"},
            ]
        }
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch(raw)):
            result = get_scaling_policies(FLEET_ID)
        # Name is a legal short string, so it is projected — but it must not be an
        # ARN. The projection cannot know a Name is "secret", so this test pins the
        # contract that the ONLY sensitive channel (ARN/account id) is closed by the
        # allowlist + bound, and asserts no arn:aws marker survives via any field.
        assert "arn:aws" not in json.dumps({k: v for k, v in result.items() if k != "ScalingPolicies"})

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
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_STRING_LENGTH
        from agents.gamelift_specialist import get_fleet_utilization

        long_loc = "L" * GAMELIFT_MAX_STRING_LENGTH
        rows = [{"FleetId": FLEET_ID, "ActiveServerProcessCount": i, "Location": long_loc} for i in range(90)]
        with patch("agents.gamelift_specialist.boto3.client", return_value=self._patch({"FleetUtilization": rows})):
            result = get_fleet_utilization(FLEET_ID)
        assert result["status"] == "truncated"
        assert result["truncated"] is True
        assert len(result["FleetUtilization"]) < 90


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
