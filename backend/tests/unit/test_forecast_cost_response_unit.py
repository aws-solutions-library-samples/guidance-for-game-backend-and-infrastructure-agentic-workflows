#!/usr/bin/env python3
"""Forecast questions never return a historical cost report for a period they do not name.

The Cost specialist may build validated historical reports while it works on a
forecast (for example, baseline months). Those reports are captured as
authoritative renderings, so without a period check the orchestrator would
return the last one as the answer to the forecast question. These tests drive
the real orchestrator composition with the real report tools and a stubbed
Cost Explorer client (no network).
"""

# Standard library
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from agents import orchestrator
from agents.cost_report import CostReportCache, CostReportService, create_cost_report_tool_bundle
from agents.cost_request_periods import requested_historical_periods

pytestmark = pytest.mark.unit

# The unit-test conftest replaces ``run_orchestrator`` with a stub for every
# test; keep a reference to the real function, taken at import time.
_real_run_orchestrator = orchestrator.run_orchestrator

TODAY = date(2026, 10, 16)
FORECAST = "Forecast my AWS cost for next month."


def _service(report_ids: list[str]) -> CostReportService:
    client = MagicMock()
    client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Estimated": False,
                "Groups": [
                    {"Keys": ["Amazon EKS"], "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}}},
                    {"Keys": ["Other services"], "Metrics": {"UnblendedCost": {"Amount": "170.00", "Unit": "USD"}}},
                ],
            }
        ]
    }
    ids = iter(report_ids)
    return CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=8, ttl_seconds=60),
        now=lambda: datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: next(ids),
    )


def _run(query: str, periods: list[tuple[str, str]], *, raise_after: bool = False) -> str:
    """Run the orchestrator with a stub agent that builds one report per period."""
    service = _service([f"cost-report-{i}" for i in range(len(periods))])

    class StubOrchestratorAgent:
        def __call__(self, _query: str) -> str:
            tools, _ = create_cost_report_tool_bundle(service)
            for start, end in periods:
                tools[0](start, end)
            if raise_after:
                raise RuntimeError("model call failed after the reports were built")
            return "Next month should cost about USD 999.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
        patch.object(orchestrator, "_utc_today", return_value=TODAY),
    ):
        return _real_run_orchestrator(query)


class TestForecastWithoutNamedPeriod:
    """A forecast that names no historical period gets the fixed guidance."""

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    def test_incidental_baseline_report_is_not_the_answer(self, raise_after):
        response = _run(FORECAST, [("2026-07-01", "2026-07-31")], raise_after=raise_after)

        assert response == orchestrator._COST_ADVISORY_GUIDANCE
        assert "Report ID" not in response
        assert "USD" not in response

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    def test_several_baseline_reports_are_all_discarded(self, raise_after):
        periods = [
            ("2026-10-01", "2026-10-15"),
            ("2026-04-01", "2026-09-30"),
            ("2026-08-01", "2026-08-31"),
            ("2026-09-01", "2026-09-30"),
            ("2026-07-01", "2026-07-31"),
        ]
        response = _run(FORECAST, periods, raise_after=raise_after)

        assert response == orchestrator._COST_ADVISORY_GUIDANCE

    def test_failed_report_rendering_is_not_the_answer(self):
        """A typed report error captured during a forecast turn is dropped too."""
        response = _run(FORECAST, [("2026-07-31", "2026-07-01")])

        assert response == orchestrator._COST_ADVISORY_GUIDANCE
        assert "Cost Report Unavailable" not in response


class TestForecastNamingAHistoricalPeriod:
    """A forecast question that also names a past period keeps that period's report."""

    QUERY = "How much did I spend last month, and what is the forecast for next month?"

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    def test_report_for_the_named_period_is_kept_with_guidance(self, raise_after):
        periods = [("2026-09-01", "2026-09-30"), ("2026-07-01", "2026-07-31")]
        response = _run(self.QUERY, periods, raise_after=raise_after)

        assert "**Report ID:** `cost-report-0`" in response
        assert "cost-report-1" not in response
        assert "2026-09-01" in response
        assert response.rstrip().endswith(orchestrator._COST_ADVISORY_GUIDANCE)

    def test_no_report_matches_the_named_period(self):
        response = _run(self.QUERY, [("2026-07-01", "2026-07-31")])

        assert response == orchestrator._COST_ADVISORY_GUIDANCE

    def test_unrecognized_period_phrase_falls_back_to_guidance(self):
        query = "Compare last week's spend with next week's forecast."
        response = _run(query, [("2026-10-05", "2026-10-11")])

        assert response == orchestrator._COST_ADVISORY_GUIDANCE


class TestNonForecastBehaviorIsUnchanged:
    """Historical questions and report follow-ups still return the captured report."""

    def test_historical_question_returns_last_captured_report(self):
        response = _run("Show my costs for last month", [("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30")])

        assert "**Report ID:** `cost-report-1`" in response
        assert orchestrator._COST_ADVISORY_GUIDANCE not in response

    def test_report_followup_with_forecast_wording_returns_the_report(self):
        service = _service(["cost-0123456789abcdef0123456789abcdef"])
        create_tools, _ = create_cost_report_tool_bundle(service)
        create_tools[0]("2026-09-01", "2026-09-30")

        class StubOrchestratorAgent:
            def __call__(self, _query: str) -> str:
                tools, _ = create_cost_report_tool_bundle(service)
                tools[1]("cost-0123456789abcdef0123456789abcdef")
                return "Reused."

        with (
            patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
            patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
            patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
            patch.object(orchestrator, "_utc_today", return_value=TODAY),
        ):
            response = _real_run_orchestrator("Use cost-0123456789abcdef0123456789abcdef to project next month")

        assert "cost-0123456789abcdef0123456789abcdef" in response
        assert orchestrator._COST_ADVISORY_GUIDANCE not in response


def _periods(query: str, today: date = TODAY) -> set[tuple[str, str, str]]:
    return {
        (p.start.isoformat(), p.end_min.isoformat(), p.end_max.isoformat())
        for p in requested_historical_periods(query, today)
    }


SEPTEMBER = ("2026-09-01", "2026-09-30", "2026-09-30")
OCTOBER_TO_DATE = ("2026-10-01", "2026-10-15", "2026-10-16")


class TestRequestedHistoricalPeriods:
    """The period resolver recognizes only unambiguous calendar phrases."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("my spend last month", {SEPTEMBER}),
            ("the previous month", {SEPTEMBER}),
            ("costs this month", {OCTOBER_TO_DATE}),
            ("month-to-date spend", {OCTOBER_TO_DATE}),
            ("MTD", {OCTOBER_TO_DATE}),
            ("last quarter", {("2026-07-01", "2026-09-30", "2026-09-30")}),
            ("this quarter", {OCTOBER_TO_DATE}),
            ("last year", {("2025-01-01", "2025-12-31", "2025-12-31")}),
            ("year to date", {("2026-01-01", "2026-10-15", "2026-10-16")}),
            ("yesterday", {("2026-10-15", "2026-10-15", "2026-10-15")}),
            ("today", {("2026-10-16", "2026-10-16", "2026-10-16")}),
            ("costs in July", {("2026-07-01", "2026-07-31", "2026-07-31")}),
            ("July's spend", {("2026-07-01", "2026-07-31", "2026-07-31")}),
            ("in October", {OCTOBER_TO_DATE}),
            ("for December 2025", {("2025-12-01", "2025-12-31", "2025-12-31")}),
            ("for May 2026", {("2026-05-01", "2026-05-31", "2026-05-31")}),
            ("in may", {("2026-05-01", "2026-05-31", "2026-05-31")}),
            (
                "between June and July",
                {("2026-06-01", "2026-06-30", "2026-06-30"), ("2026-07-01", "2026-07-31", "2026-07-31")},
            ),
            ("from 2026-07-01 to 2026-07-31", {("2026-07-01", "2026-07-31", "2026-07-31")}),
            ("on 2026-07-04", {("2026-07-04", "2026-07-04", "2026-07-04")}),
            ("last month and this month", {SEPTEMBER, OCTOBER_TO_DATE}),
        ],
    )
    def test_recognized_phrases(self, query, expected):
        assert _periods(query) == expected

    @pytest.mark.parametrize(
        "query",
        [
            "Forecast my AWS cost for next month.",
            "What will I spend next quarter?",
            "Forecast my spend for November",
            "for December",
            "last week",
            "the last 30 days",
            "I may need a forecast",
            "March forward with savings",
            "2026-13-40",
            "from 2026-10-01 to 2026-11-30",
            "on 2026-12-25",
        ],
    )
    def test_unrecognized_phrases(self, query):
        assert _periods(query) == set()

    def test_period_boundaries_at_year_and_quarter_start(self):
        assert _periods("last month", date(2026, 1, 1)) == {("2025-12-01", "2025-12-31", "2025-12-31")}
        assert _periods("this month", date(2026, 1, 1)) == {("2026-01-01", "2026-01-01", "2026-01-01")}
        assert _periods("last quarter", date(2026, 4, 1)) == {("2026-01-01", "2026-03-31", "2026-03-31")}
        assert _periods("last quarter", date(2026, 1, 15)) == {("2025-10-01", "2025-12-31", "2025-12-31")}

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            ("2026-10-01", "2026-10-15", True),
            ("2026-10-01", "2026-10-16", True),
            ("2026-10-01", "2026-10-14", False),
            ("2026-10-02", "2026-10-15", False),
            (None, None, False),
            ("not-a-date", "2026-10-15", False),
        ],
    )
    def test_to_date_period_accepts_today_or_yesterday_as_the_end(self, start, end, expected):
        (period,) = requested_historical_periods("this month", TODAY)
        assert period.matches(start, end) is expected
