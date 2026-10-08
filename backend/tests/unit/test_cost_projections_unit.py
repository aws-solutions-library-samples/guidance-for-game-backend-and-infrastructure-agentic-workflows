"""Unit tests for the Billing MCP output projections (#465).

Directly attacks each ``project_*`` function with hostile synthetic values and
asserts that account IDs, full ARNs, URLs/network coordinates, control
characters, non-finite/oversized numbers, tags, and arbitrary nested blobs never
survive into the projected mapping, and that empty / malformed / truncated /
paginated states stay distinct.
"""

# Standard library
import json

# Third-party packages
import pytest

# Local modules
from agents.cost_projections import (
    MAX_ITEMS,
    project_compute_optimizer,
    project_cost_optimization_detail,
    project_cost_optimization_recommendations,
    project_cost_optimization_summaries,
    project_forecast,
    valid_typed_string,
)

pytestmark = pytest.mark.unit

ACCT = "123456789012"
ARN = f"arn:aws:ce:us-east-1:{ACCT}:recommendation/x"
URL = "https://host.example.invalid/p"


def _no_sensitive(payload) -> None:
    blob = json.dumps(payload)
    assert ACCT not in blob
    assert "arn:aws" not in blob
    assert URL not in blob
    assert "example.invalid" not in blob
    assert "\x07" not in blob


class TestValidTypedString:
    @pytest.mark.parametrize(
        "value",
        [ARN, URL, ACCT, "10.11.12.13", "ctl\x07char", "a" * 300, "fe80::1", "010.1.2.3", 123, None],
    )
    def test_rejects_hostile(self, value):
        assert valid_typed_string(value, "token") is False

    def test_accepts_clean_token(self):
        assert valid_typed_string("m5.large", "token") is True
        assert valid_typed_string("us-east-1", "region") is True


class TestForecast:
    def test_projects_total_and_periods_estimated(self):
        data = {
            "Total": {"Amount": "123.45", "Unit": "USD"},
            "ForecastResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-01-01", "End": "2026-02-01"},
                    "MeanValue": "100.0",
                    "PredictionIntervalLowerBound": "90.0",
                    "PredictionIntervalUpperBound": "110.0",
                }
            ],
        }
        out = project_forecast(data)
        assert out["estimated"] is True
        assert out["total_amount"] == 123.45
        assert out["unit"] == "USD"
        assert out["periods"][0]["mean"] == 100.0
        assert out["status"] == "ok"

    def test_drops_arbitrary_fields_and_nonfinite(self):
        data = {
            "Total": {"Amount": "123.45", "Unit": "USD"},
            "ForecastResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-01-01", "End": "2026-02-01"},
                    "MeanValue": "nan",
                    "Endpoint": URL,
                    "AccountNote": ACCT,
                    "Blob": [ARN],
                }
            ],
        }
        out = project_forecast(data)
        _no_sensitive(out)
        assert "Endpoint" not in json.dumps(out)
        assert "mean" not in out["periods"][0]  # 'nan' rejected

    def test_non_mapping_is_malformed(self):
        assert project_forecast("x") is None  # type: ignore[arg-type]
        assert project_forecast({"ForecastResultsByTime": "x"}) is None

    def test_period_cap_truncates(self):
        data = {
            "Total": {"Amount": "1", "Unit": "USD"},
            "ForecastResultsByTime": [
                {"TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"}, "MeanValue": "1.0"}
                for _ in range(MAX_ITEMS + 5)
            ],
        }
        out = project_forecast(data)
        assert out["status"] == "truncated"
        assert out["truncated"] is True


class TestComputeOptimizer:
    def test_derives_resource_id_from_arn_suffix(self):
        data = {
            "recommendations": [
                {
                    "instance_arn": "arn:aws:ec2:us-east-1:" + ACCT + ":instance/i-0abc123",
                    "account_id": ACCT,
                    "current_instance": {"instance_type": "m5.large", "finding": "OVERPROVISIONED"},
                    "recommendation_options": [],
                    "last_refresh_timestamp": "2026-01-01T00:00:00",
                }
            ]
        }
        out = project_compute_optimizer(data)
        _no_sensitive(out)
        assert out["recommendations"][0]["resource_id"] == "i-0abc123"
        assert out["recommendations"][0]["finding"] == "OVERPROVISIONED"

    def test_empty_is_distinct(self):
        assert project_compute_optimizer({"recommendations": []})["status"] == "empty"

    def test_missing_collection_malformed(self):
        assert project_compute_optimizer({"recommendations": None}) is None
        assert project_compute_optimizer({}) is None

    def test_pagination_marks_partial(self):
        out = project_compute_optimizer(
            {"recommendations": [{"current_instance": {"instance_type": "m5.large"}}], "next_token": "opaque"}
        )
        assert out["status"] == "incomplete"
        assert out["paginated"] is True
        assert "opaque" not in json.dumps(out)


class TestCostOptimizationHub:
    def test_recommendation_omits_arn_account_tags(self):
        data = {
            "recommendations": [
                {
                    "recommendation_id": "rec-1",
                    "account_id": ACCT,
                    "resource_arn": ARN,
                    "region": "us-east-1",
                    "action_type": "Stop",
                    "estimated_monthly_savings": 9.5,
                    "currency_code": "USD",
                    "restart_needed": True,
                    "rollback_possible": False,
                    "tags": {"x": "y"},
                    "recommended_resource_details": {"blob": URL},
                }
            ]
        }
        out = project_cost_optimization_recommendations(data)
        _no_sensitive(out)
        rec = out["recommendations"][0]
        assert rec["recommendation_id"] == "rec-1"
        assert rec["estimated_monthly_savings"] == 9.5
        assert rec["restart_needed"] is True
        assert "tags" not in rec and "recommended_resource_details" not in rec

    def test_summaries_estimated_total(self):
        data = {
            "group_by": "Region",
            "currency_code": "USD",
            "estimated_total_savings": 100.0,
            "summaries": [{"group": "us-east-1", "estimated_monthly_savings": 100.0, "recommendation_count": 3}],
        }
        out = project_cost_optimization_summaries(data)
        assert out["estimated"] is True
        assert out["estimated_total_savings"] == 100.0
        assert out["summaries"][0]["group"] == "us-east-1"
        assert out["status"] == "ok"

    def test_detail_projection(self):
        data = {
            "recommendation_id": "rec-9",
            "account_id": ACCT,
            "resource_arn": ARN,
            "region": "us-west-2",
            "action_type": "Rightsize",
            "estimated_monthly_savings": 5.0,
            "currency_code": "USD",
            "last_refresh_timestamp": "2026-01-01T00:00:00",
            "cost_calculation_lookback_period_in_days": 30,
            "tags": {"a": "b"},
        }
        out = project_cost_optimization_detail(data)
        _no_sensitive(out)
        assert out["recommendation_id"] == "rec-9"
        assert out["estimated"] is True
        assert out["cost_calculation_lookback_period_in_days"] == 30
        assert "tags" not in out

    def test_mixed_rows_partial(self):
        data = {"recommendations": [{"recommendation_id": "rec-ok"}, "garbage", {"region": ARN}]}
        out = project_cost_optimization_recommendations(data)
        _no_sensitive(out)
        assert out["status"] == "incomplete"
        assert out["error"]["code"] == "malformed_response"
        assert out["partial"] is True
        # Dropped rows (without an item-cap overflow) are partial, not truncated.
        assert "truncated" not in out
        # the one valid row is retained
        assert out["recommendations"][0]["recommendation_id"] == "rec-ok"


class TestOverlappingDispositionsStayIndependent:
    """When dropped (malformed), capped (item-limit), and paginated (residual
    continuation) conditions overlap, each code-owned flag survives independently
    instead of collapsing into one misleading state."""

    def test_dropped_rows_with_residual_token_keep_paginated(self):
        data = {"recommendations": [{"recommendation_id": "rec-ok"}, "garbage"], "next_token": "more"}
        out = project_cost_optimization_recommendations(data)
        assert out is not None
        assert out["partial"] is True
        assert out["error"]["code"] == "malformed_response"
        assert out["paginated"] is True
        assert out["status"] == "incomplete"
        assert "more" not in json.dumps(out)

    def test_dropped_rows_beyond_cap_keep_truncated_and_partial(self):
        # 60 valid rows + 1 malformed row past the item cap: the malformed row is
        # still detected (partial + malformed) and the cap is still flagged.
        rows = [{"recommendation_id": f"rec-{i}"} for i in range(60)]
        rows.append("garbage")
        out = project_cost_optimization_recommendations({"recommendations": rows})
        assert out is not None
        assert out["partial"] is True
        assert out["truncated"] is True
        assert out["error"]["code"] == "malformed_response"
        assert len(out["recommendations"]) == MAX_ITEMS

    def test_forecast_dropped_and_capped_keep_both_signals(self):
        rows = [{"TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"}, "MeanValue": "1.0"} for _ in range(60)]
        rows.append({"TimePeriod": {"Start": "x", "End": "y"}})  # no usable field -> dropped
        out = project_forecast({"Total": {"Amount": "1", "Unit": "USD"}, "ForecastResultsByTime": rows})
        assert out is not None
        assert out["truncated"] is True, "cap signal must survive alongside dropped rows"
        assert out["partial"] is True
        assert out["error"]["code"] == "malformed_response"
        assert len(out["periods"]) == MAX_ITEMS


# =========================================================================== #
# Helper-truncation detection, malformed-forecast disposition, and            #
# valid-row-only cap accounting.                                              #
# =========================================================================== #
class TestHelperTruncationBound:
    """The Cost Optimization Hub list helpers paginate internally and truncate
    to exactly ``maxResults`` with NO residual token. The guard therefore asks
    for ``bound + 1`` and passes ``bound`` to the projector; when more rows than
    the bound arrive, the projector must drop the extra and mark the result
    incomplete + paginated + partial so it never looks complete."""

    def test_recommendations_more_than_bound_marks_incomplete_paginated(self):
        rows = [{"recommendation_id": f"rec-{i}"} for i in range(26)]  # bound+1 arrived
        out = project_cost_optimization_recommendations({"recommendations": rows}, bound=25)
        assert out is not None
        assert out["status"] == "incomplete"
        assert out["paginated"] is True
        assert out["partial"] is True
        assert len(out["recommendations"]) == 25  # extra row dropped

    def test_recommendations_exactly_bound_is_ok(self):
        rows = [{"recommendation_id": f"rec-{i}"} for i in range(25)]
        out = project_cost_optimization_recommendations({"recommendations": rows}, bound=25)
        assert out is not None
        assert out["status"] == "ok"
        assert "paginated" not in out
        assert len(out["recommendations"]) == 25

    def test_summaries_more_than_bound_marks_incomplete_paginated(self):
        rows = [{"group": f"us-west-{i}", "recommendation_count": 1} for i in range(6)]
        out = project_cost_optimization_summaries(
            {"summaries": rows, "group_by": "Region", "currency_code": "USD"}, bound=5
        )
        assert out is not None
        assert out["status"] == "incomplete"
        assert out["paginated"] is True
        assert out["partial"] is True
        assert len(out["summaries"]) == 5


class TestForecastMalformedDisposition:
    """A missing/wrong-typed ``ForecastResultsByTime`` or a present-but-invalid
    ``Total`` is incomplete + partial + malformed_response (keeping valid fields),
    not a clean ``ok``."""

    def test_total_present_but_amount_unparseable_is_malformed(self):
        data = {"Total": {"Amount": "not-a-number", "Unit": "USD"}, "ForecastResultsByTime": []}
        out = project_forecast(data)
        assert out is not None
        assert out["status"] == "incomplete"
        assert out["partial"] is True
        assert out["error"]["code"] == "malformed_response"

    def test_total_present_but_not_a_mapping_is_malformed(self):
        data = {
            "Total": "500",
            "ForecastResultsByTime": [{"TimePeriod": {"Start": "2026-01-01", "End": "2026-02-01"}, "MeanValue": "1.0"}],
        }
        out = project_forecast(data)
        assert out is not None
        assert out["status"] == "incomplete"
        assert out["partial"] is True
        assert out["error"]["code"] == "malformed_response"
        # valid period fields are preserved
        assert out["periods"][0]["mean"] == 1.0

    def test_results_present_but_wrong_typed_with_valid_total_is_malformed(self):
        data = {"Total": {"Amount": "500.0", "Unit": "USD"}, "ForecastResultsByTime": "nope"}
        out = project_forecast(data)
        assert out is not None
        assert out["status"] == "incomplete"
        assert out["partial"] is True
        assert out["error"]["code"] == "malformed_response"
        assert out["total_amount"] == 500.0  # valid Total preserved


class TestCappedCountsValidRowsOnly:
    """``truncated`` must reflect valid rows exceeding the item cap, not the
    raw row count inflated by dropped malformed rows."""

    def test_malformed_rows_do_not_trip_truncated_when_valid_within_cap(self):
        rows = [{"recommendation_id": f"rec-{i}"} for i in range(10)]
        rows += ["garbage"] * 60  # 60 malformed rows push raw count past the cap
        out = project_cost_optimization_recommendations({"recommendations": rows})
        assert out is not None
        assert out["partial"] is True
        assert out["error"]["code"] == "malformed_response"
        assert "truncated" not in out, "dropped rows must not be counted as a cap-truncation"
        assert len(out["recommendations"]) == 10


class TestForecastMissingResultsIsMalformed:
    """An absent ``ForecastResultsByTime`` key (with an otherwise valid Total)
    is a malformed shape, not a clean ``ok`` with an empty period list: nothing
    documents absence as meaning empty for this response, so it is
    incomplete + partial + malformed_response while keeping the valid Total."""

    def test_absent_results_key_with_valid_total_is_malformed(self):
        data = {"Total": {"Amount": "500.0", "Unit": "USD"}}  # no ForecastResultsByTime key
        out = project_forecast(data)
        assert out is not None
        assert out["status"] == "incomplete"
        assert out["partial"] is True
        assert out["error"]["code"] == "malformed_response"
        # The valid Total is preserved despite the malformed shape.
        assert out["total_amount"] == 500.0
        assert out["unit"] == "USD"
        # No usable periods were present.
        assert out["periods"] == []


class TestProbeRowAtCapIsNotTruncated:
    """When the guard over-fetches by one (bound + 1), a bound-50 request that
    receives the 51st probe row must report residual data (paginated + partial)
    but NOT ``truncated``: the single probe row is the only residual signal, not
    a genuine hard-item-cap overflow. Only valid rows BEYOND MAX_ITEMS + 1 set
    ``truncated``."""

    def test_bound_50_receiving_51_rows_is_paginated_not_truncated(self):
        rows = [{"recommendation_id": f"rec-{i:04d}"} for i in range(51)]  # bound+1 probe row
        out = project_cost_optimization_recommendations({"recommendations": rows}, bound=50)
        assert out is not None
        assert out["status"] == "incomplete"
        assert out["paginated"] is True
        assert out["partial"] is True
        assert "truncated" not in out, "the single probe row must not set the hard-cap truncation flag"
        assert len(out["recommendations"]) == MAX_ITEMS

    def test_valid_rows_beyond_cap_plus_one_still_truncated(self):
        # A provider that ignores maxResults and returns far more than the probe
        # row (e.g. 60 valid rows) is still a genuine item-cap truncation.
        rows = [{"recommendation_id": f"rec-{i:04d}"} for i in range(60)]
        out = project_cost_optimization_recommendations({"recommendations": rows}, bound=50)
        assert out is not None
        assert out["truncated"] is True
        assert out["paginated"] is True
        assert len(out["recommendations"]) == MAX_ITEMS
