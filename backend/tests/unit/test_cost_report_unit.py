"""Unit tests for deterministic Cost Explorer reports."""

# Standard library
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

# Third-party packages
import pytest

# Local modules
from agents.cost_report import (
    CostReportCache,
    CostReportError,
    CostReportService,
    begin_cost_report_capture,
    create_cost_report_tool_bundle,
    finish_cost_report_capture,
)

pytestmark = pytest.mark.unit

_FIXED_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _group(service: str, amount: str, unit: str = "USD", metric: str = "UnblendedCost") -> dict:
    return {
        "Keys": [service],
        "Metrics": {metric: {"Amount": amount, "Unit": unit}},
    }


def _page(groups: list[dict], *, estimated: bool = False, next_token: str | None = None) -> dict:
    page = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-05-01", "End": "2026-05-16"},
                "Estimated": estimated,
                "Groups": groups,
            }
        ]
    }
    if next_token:
        page["NextPageToken"] = next_token
    return page


def _service(client: MagicMock, report_id: str = "cost-fixture-250") -> CostReportService:
    return CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=8, ttl_seconds=300),
        now=lambda: _FIXED_NOW,
        report_id_factory=lambda: report_id,
    )


def _fictional_groups() -> list[dict]:
    return [
        _group("Amazon EKS", "80.00"),
        _group("Amazon Bedrock AgentCore", "65.00"),
        _group("Amazon Inspector", "35.00"),
        _group("Amazon GameLift", "25.00"),
        _group("Amazon Elastic Load Balancing", "15.00"),
        _group("Amazon CloudWatch", "10.00"),
        _group("AWS Key Management Service", "8.00"),
        _group("Amazon S3", "7.00"),
        _group("AWS Lambda", "5.00"),
    ]


class TestCostReportContract:
    def test_fictional_250_report_reconciles_and_reuses_snapshot(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(_fictional_groups(), estimated=True)
        service = _service(client)

        snapshot = service.create_report("2026-05-01", "2026-05-15")
        report = snapshot.report

        client.get_cost_and_usage.assert_called_once_with(
            TimePeriod={"Start": "2026-05-01", "End": "2026-05-16"},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        assert report.model_dump(by_alias=True, mode="json") == {
            "schemaVersion": "1.0",
            "reportId": "cost-fixture-250",
            "source": "AWS Cost Explorer",
            "metric": "UnblendedCost",
            "currency": "USD",
            "period": {
                "start": "2026-05-01",
                "endInclusive": "2026-05-15",
                "endExclusive": "2026-05-16",
            },
            "queriedAt": "2026-05-16T12:00:00Z",
            "estimated": True,
            "totalRaw": "250.00",
            "total": "250.00",
            "topServices": [
                {
                    "rank": 1,
                    "service": "Amazon EKS",
                    "rawAmount": "80.00",
                    "amount": "80.00",
                    "percentage": "32.0",
                },
                {
                    "rank": 2,
                    "service": "Amazon Bedrock AgentCore",
                    "rawAmount": "65.00",
                    "amount": "65.00",
                    "percentage": "26.0",
                },
                {
                    "rank": 3,
                    "service": "Amazon Inspector",
                    "rawAmount": "35.00",
                    "amount": "35.00",
                    "percentage": "14.0",
                },
                {
                    "rank": 4,
                    "service": "Amazon GameLift",
                    "rawAmount": "25.00",
                    "amount": "25.00",
                    "percentage": "10.0",
                },
                {
                    "rank": 5,
                    "service": "Amazon Elastic Load Balancing",
                    "rawAmount": "15.00",
                    "amount": "15.00",
                    "percentage": "6.0",
                },
            ],
            "otherServicesRawTotal": "30.00",
            "otherServicesTotal": "30.00",
            "validation": {
                "allServiceAmountsTotal": "250.00",
                "displayedBreakdownTotal": "250.00",
                "allServicePercentagesTotal": "100.0",
                "totalMatches": True,
                "rankingsMatch": True,
                "percentagesMatch": True,
            },
        }

        selection = service.reuse_report(
            report.report_id,
            ["Amazon EKS", "Amazon Bedrock AgentCore"],
        )
        assert selection.raw_amount == "145.00"
        assert selection.amount == "145.00"
        assert selection.percentage == "58.0"
        assert selection.snapshot_reused is True
        client.get_cost_and_usage.assert_called_once()

    def test_pagination_is_one_logical_query_and_aggregates_duplicate_services(self):
        client = MagicMock()
        client.get_cost_and_usage.side_effect = [
            _page([_group("Amazon EKS", "50.00")], next_token="page-2"),
            _page(
                [
                    _group("Amazon EKS", "30.00"),
                    _group("Amazon Bedrock AgentCore", "65.00"),
                ],
                estimated=True,
            ),
        ]
        service = _service(client, report_id="cost-paginated")

        report = service.create_report("2026-05-01", "2026-05-15").report

        assert report.total == "145.00"
        assert report.estimated is True
        assert [(item.service, item.raw_amount) for item in report.top_services] == [
            ("Amazon EKS", "80.00"),
            ("Amazon Bedrock AgentCore", "65.00"),
        ]
        assert client.get_cost_and_usage.call_count == 2
        first_request = client.get_cost_and_usage.call_args_list[0].kwargs
        second_request = client.get_cost_and_usage.call_args_list[1].kwargs
        assert second_request == {**first_request, "NextPageToken": "page-2"}

    def test_raw_ranking_ties_are_broken_by_service_name(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [
                _group("Zulu Service", "1.00"),
                _group("alpha service", "1.00"),
                _group("Alpha Service", "1.00"),
            ]
        )

        report = _service(client).create_report("2026-05-01", "2026-05-15").report

        assert [item.service for item in report.top_services] == [
            "Alpha Service",
            "alpha service",
            "Zulu Service",
        ]

    def test_fractional_cents_are_reconciled_after_display_rounding(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [
                _group("A Service", "0.005"),
                _group("B Service", "0.005"),
            ]
        )

        report = _service(client).create_report("2026-05-01", "2026-05-15").report

        assert report.total_raw == "0.010"
        assert report.total == "0.01"
        assert [(item.service, item.amount) for item in report.top_services] == [
            ("A Service", "0.00"),
            ("B Service", "0.01"),
        ]
        assert sum((Decimal(item.amount) for item in report.top_services), Decimal("0")) == Decimal(report.total)
        assert report.validation.total_matches is True

    def test_zero_cost_services_and_negative_credits_use_raw_total(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [
                _group("Positive Service", "10.00"),
                _group("Zero Service", "0.00"),
                _group("Credit Adjustment", "-2.00"),
            ]
        )

        report = _service(client).create_report("2026-05-01", "2026-05-15").report

        assert report.total == "8.00"
        assert [(item.service, item.amount, item.percentage) for item in report.top_services] == [
            ("Positive Service", "10.00", "125.0"),
            ("Zero Service", "0.00", "0.0"),
            ("Credit Adjustment", "-2.00", "-25.0"),
        ]
        assert report.validation.all_service_percentages_total == "100.0"


class TestCostReportFailures:
    @pytest.mark.parametrize(
        ("start", "end", "code"),
        [
            ("not-a-date", "2026-05-15", "INVALID_DATE"),
            ("20260501", "2026-05-15", "INVALID_DATE"),
            ("2026-05-16", "2026-05-15", "INVALID_DATE_RANGE"),
        ],
    )
    def test_invalid_dates_fail_without_query(self, start: str, end: str, code: str):
        client = MagicMock()
        service = _service(client)

        with pytest.raises(CostReportError, match="date") as raised:
            service.create_report(start, end)

        assert raised.value.code == code
        assert raised.value.retryable is False
        client.get_cost_and_usage.assert_not_called()

    def test_missing_service_name_fails_closed(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [{"Keys": [], "Metrics": {"UnblendedCost": {"Amount": "1.00", "Unit": "USD"}}}]
        )

        with pytest.raises(CostReportError) as raised:
            _service(client).create_report("2026-05-01", "2026-05-15")

        assert raised.value.code == "INVALID_COST_EXPLORER_RESPONSE"
        assert raised.value.retryable is True

    def test_multiple_currencies_fail_closed(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [
                _group("USD Service", "1.00", "USD"),
                _group("EUR Service", "1.00", "EUR"),
            ]
        )

        with pytest.raises(CostReportError) as raised:
            _service(client).create_report("2026-05-01", "2026-05-15")

        assert raised.value.code == "MULTIPLE_CURRENCIES"
        assert raised.value.retryable is True

    def test_cost_explorer_api_errors_are_safe_and_retryable(self):
        client = MagicMock()
        client.get_cost_and_usage.side_effect = RuntimeError("secret-bearing upstream detail")

        with pytest.raises(CostReportError) as raised:
            _service(client).create_report("2026-05-01", "2026-05-15")

        assert raised.value.code == "COST_EXPLORER_REQUEST_FAILED"
        assert raised.value.retryable is True
        assert "secret-bearing" not in raised.value.message

    def test_expired_or_unknown_snapshot_does_not_issue_query(self):
        client = MagicMock()
        service = _service(client)

        with pytest.raises(CostReportError) as raised:
            service.reuse_report("missing-report", ["Amazon EKS"])

        assert raised.value.code == "COST_REPORT_NOT_FOUND"
        client.get_cost_and_usage.assert_not_called()


class TestCostReportRenderingPath:
    def test_tool_bundle_returns_typed_data_and_overrides_generated_prose(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(_fictional_groups(), estimated=True)
        tools, finalize_response = create_cost_report_tool_bundle(_service(client))

        payload = tools[0]("2026-05-01", "2026-05-15")
        validated_section = payload["validatedFinancialSection"]

        assert payload["report"]["total"] == "250.00"
        assert "USD 250.00" in validated_section
        assert "marked this snapshot as estimated" in validated_section
        assert finalize_response("The model independently changed the total to USD 999.00.") == validated_section
        assert "999.00" not in finalize_response("USD 999.00")

    def test_orchestrator_capture_receives_tool_rendering(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(_fictional_groups())
        tools, _ = create_cost_report_tool_bundle(_service(client))
        capture = begin_cost_report_capture()

        payload = tools[0]("2026-05-01", "2026-05-15")
        captured = finish_cost_report_capture(capture)

        assert captured == payload["validatedFinancialSection"]
        assert "USD 250.00" in captured

    def test_validation_error_tool_response_contains_no_financial_section(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [
                _group("USD Service", "1.00", "USD"),
                _group("EUR Service", "1.00", "EUR"),
            ]
        )
        tools, finalize_response = create_cost_report_tool_bundle(_service(client))

        payload = tools[0]("2026-05-01", "2026-05-15")
        rendered = finalize_response("Model-generated fallback with USD 2.00")

        assert payload == {
            "error": {
                "code": "MULTIPLE_CURRENCIES",
                "message": (
                    "Cost Explorer returned multiple currencies or units. "
                    "No financial report was produced; retry the request."
                ),
                "retryable": True,
            }
        }
        assert "No unverified financial report was produced." in rendered
        assert "USD 2.00" not in rendered
