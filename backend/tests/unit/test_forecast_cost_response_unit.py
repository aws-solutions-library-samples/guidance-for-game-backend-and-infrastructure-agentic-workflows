#!/usr/bin/env python3
"""Recognized forecast questions never return a historical report for a period they do not name.

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
from botocore.exceptions import ClientError

# Local modules
from agents import orchestrator
from agents.cost_report import CostReportCache, CostReportService, create_cost_report_tool_bundle
from agents.cost_request_periods import requested_historical_periods
from agents.specialist_capture import record_specialist_output

pytestmark = pytest.mark.unit

# The unit-test conftest replaces ``run_orchestrator`` with a stub for every
# test; keep a reference to the real function, taken at import time.
_real_run_orchestrator = orchestrator.run_orchestrator

TODAY = date(2026, 10, 16)
FORECAST = "Forecast my AWS cost for next month."
GUIDANCE = orchestrator._COST_ADVISORY_GUIDANCE
LIVE_BASELINES = [
    ("2026-10-01", "2026-10-15"),
    ("2026-04-01", "2026-09-30"),
    ("2026-08-01", "2026-08-31"),
    ("2026-09-01", "2026-09-30"),
    ("2026-07-01", "2026-07-31"),
]


def _client(fail: bool = False) -> MagicMock:
    client = MagicMock()
    if fail:
        client.get_cost_and_usage.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetCostAndUsage"
        )
        return client
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
    return client


def _service(count: int, *, fail: bool = False, prefix: str = "cost-report-") -> CostReportService:
    ids = iter(f"{prefix}{i}" for i in range(count))
    return CostReportService(
        client_factory=lambda: _client(fail),
        cache=CostReportCache(maxsize=16, ttl_seconds=60),
        now=lambda: datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: next(ids),
    )


def _orchestrate(query: str, agent_body, *, today=TODAY) -> str:
    """Run the real orchestrator with a stub agent whose body builds reports."""

    class StubOrchestratorAgent:
        def __call__(self, _query: str) -> str:
            return agent_body()

    today_mock = MagicMock(return_value=today)
    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
        patch.object(orchestrator, "_utc_today", today_mock),
    ):
        return _real_run_orchestrator(query)


def _run(query: str, periods: list[tuple[str, str]], *, raise_after: bool = False, cost_ran: bool = False) -> str:
    """Build one report per period, then return model prose or raise."""
    service = _service(len(periods))

    def body() -> str:
        if cost_ran:
            record_specialist_output("Cost", "Next month should cost about USD 999.00.")
        tools, _ = create_cost_report_tool_bundle(service)
        for start, end in periods:
            tools[0](start, end)
        if raise_after:
            raise RuntimeError("model call failed after the reports were built")
        return "Next month should cost about USD 999.00."

    return _orchestrate(query, body)


class TestForecastWithoutNamedPeriod:
    """A forecast that names no historical period gets the fixed guidance."""

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    def test_incidental_baseline_report_is_not_the_answer(self, raise_after):
        response = _run(FORECAST, [("2026-07-01", "2026-07-31")], raise_after=raise_after)

        assert response == GUIDANCE
        assert "Report ID" not in response
        assert "USD" not in response

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    def test_live_run_baselines_are_all_discarded(self, raise_after):
        assert _run(FORECAST, LIVE_BASELINES, raise_after=raise_after) == GUIDANCE

    def test_failed_report_rendering_is_not_the_answer(self):
        """A typed report error for an unnamed period is dropped too."""
        response = _run(FORECAST, [("2026-07-31", "2026-07-01")])

        assert response == GUIDANCE
        assert "Cost Report Unavailable" not in response

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    def test_production_route_keeps_specialist_markers_and_guidance(self, raise_after):
        """With the Cost and GameLift specialists recorded, the advisory composition is returned."""
        service = _service(1)

        def body() -> str:
            record_specialist_output("GameLift", "The fleet runs 4 instances.")
            record_specialist_output("Cost", "Next month should cost about USD 999.00.")
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-07-01", "2026-07-31")
            if raise_after:
                raise RuntimeError("model call failed after the report was built")
            return "Next month should cost about USD 999.00."

        response = _orchestrate(FORECAST, body)

        assert response.startswith("## GameLift")
        assert response.endswith(GUIDANCE)
        assert "Report ID" not in response
        assert "USD" not in response

    def test_raised_turn_logs_that_reports_were_dropped(self):
        with patch.object(orchestrator.logger, "warning") as warning:
            _run(FORECAST, [("2026-07-01", "2026-07-31")], raise_after=True)

        assert any("Forecast request failed after a validated cost report" in str(c) for c in warning.call_args_list)

    @pytest.mark.parametrize(
        ("query", "period"),
        [
            ("Forecast my AWS cost for February.", ("2026-02-01", "2026-02-28")),
            ("What is July's forecast for my AWS bill?", ("2026-07-01", "2026-07-31")),
            ("Forecast my AWS costs from November through January.", ("2026-01-01", "2026-01-31")),
            ("What is my AWS cost forecast for this month?", ("2026-10-01", "2026-10-15")),
            ("What is this year's projection for my AWS spend?", ("2026-01-01", "2026-10-15")),
            ("Forecast my AWS cost for this quarter.", ("2026-10-01", "2026-10-15")),
        ],
    )
    def test_month_names_and_current_periods_name_no_history(self, query, period):
        """These phrases can name the forecast target, so a matching baseline is dropped."""
        assert _run(query, [period]) == GUIDANCE

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    @pytest.mark.parametrize(
        ("query", "periods"),
        [
            ("Forecast my AWS cost for the last month of the year.", LIVE_BASELINES),
            ("What is my AWS cost forecast for the last quarter of the year?", [("2026-07-01", "2026-09-30")]),
            ("Forecast our AWS spend for the last quarter of 2026.", [("2026-07-01", "2026-09-30")]),
            ("Forecast my AWS cost for the last quarter in 2026.", [("2026-07-01", "2026-09-30")]),
            ("What will my AWS bill be for the last quarter this year?", [("2026-07-01", "2026-09-30")]),
            ("Forecast my AWS cost for the last month in the year.", LIVE_BASELINES),
            ("Forecast my AWS cost for October 2026.", LIVE_BASELINES),
            ("Forecast my AWS cost for 2026-10-16.", [("2026-10-16", "2026-10-16")]),
        ],
    )
    def test_phrases_that_can_name_the_forecast_target_keep_no_report(self, query, periods, raise_after):
        assert _run(query, periods, raise_after=raise_after, cost_ran=True) == GUIDANCE

    @pytest.mark.parametrize(
        "query", ["Predict my AWS costs for the next 30 days.", "What are cost optimization strategies for EKS?"]
    )
    def test_raised_turn_without_a_report_logs_a_fixed_warning(self, query):
        def body() -> str:
            record_specialist_output("Cost", "Next month should cost about USD 999.00.")
            raise RuntimeError("model call failed before any report")

        with patch.object(orchestrator, "logger") as log:
            response = _orchestrate(query, body)

        assert response == GUIDANCE
        assert any("request raised before a validated cost report" in str(c) for c in log.warning.call_args_list)
        assert "model call failed before any report" not in str(log.mock_calls)

    def test_raised_turn_that_drops_its_reports_logs_one_warning(self):
        with patch.object(orchestrator.logger, "warning") as warning:
            _run(FORECAST, [("2026-07-01", "2026-07-31")], raise_after=True)

        messages = [str(c) for c in warning.call_args_list]
        assert any("Forecast request failed after a validated cost report" in m for m in messages)
        assert not any("request raised before a validated cost report" in m for m in messages)

    def test_year_zero_does_not_fail_the_request(self):
        assert _run("Forecast next month and compare it with July 0000.", [("2026-09-01", "2026-09-30")]) == GUIDANCE

    def test_resolver_failure_returns_guidance(self):
        with patch.object(orchestrator, "requested_historical_periods", side_effect=ValueError("bad input")):
            assert _run(FORECAST, [("2026-07-01", "2026-07-31")]) == GUIDANCE

    @pytest.mark.parametrize(
        "query",
        [
            "Predict my AWS costs for the next 30 days.",
            "How much will I spend over the next three months?",
            "Estimate my AWS spend for the rest of the year.",
            "Project my AWS costs through December.",
            "What will my AWS bill be in November?",
            "What is my expected AWS bill for November?",
            "Estimate my December bill.",
            "What's the outlook for my AWS spend?",
            "How much should I expect to pay in November?",
        ],
    )
    def test_wider_forecast_phrasings_get_guidance(self, query):
        """Not the last baseline, and not the historical-report failure notice."""
        response = _run(query, LIVE_BASELINES, cost_ran=True)

        assert response == GUIDANCE

    def test_historical_question_with_an_incidental_forecast_word_keeps_no_unnamed_report(self):
        """The trade-off: a forecast phrase without a named period keeps no report."""
        response = _run(
            "Show my cost breakdown by service. I want to plan for next month.", [("2026-10-01", "2026-10-15")]
        )

        assert response == GUIDANCE


class TestForecastNamingAHistoricalPeriod:
    """A forecast question that also names a past period keeps that period's report."""

    QUERY = "How much did I spend last month, and what is the forecast for next month?"

    @pytest.mark.parametrize("raise_after", [False, True], ids=["completed", "raised"])
    def test_report_for_the_named_period_is_kept_with_guidance(self, raise_after):
        response = _run(
            self.QUERY, [("2026-09-01", "2026-09-30"), ("2026-07-01", "2026-07-31")], raise_after=raise_after
        )

        assert "**Report ID:** `cost-report-0`" in response
        assert "cost-report-1" not in response
        assert "2026-09-01" in response
        assert response.endswith(GUIDANCE)

    def test_most_recent_rendering_for_a_period_is_kept(self):
        response = _run(self.QUERY, [("2026-09-01", "2026-09-30"), ("2026-09-01", "2026-09-30")])

        assert "**Report ID:** `cost-report-1`" in response
        assert "cost-report-0" not in response

    def test_reused_report_for_the_named_period_is_kept(self):
        service = _service(1, prefix="cost-0123456789abcdef0123456789abcde")
        create_tools, _ = create_cost_report_tool_bundle(service)
        create_tools[0]("2026-09-01", "2026-09-30")

        def body() -> str:
            tools, _ = create_cost_report_tool_bundle(service)
            tools[1]("cost-0123456789abcdef0123456789abcde0")
            return "Reused."

        response = _orchestrate(self.QUERY, body)

        assert "cost-0123456789abcdef0123456789abcde0" in response
        assert response.endswith(GUIDANCE)

    def test_typed_error_for_the_named_period_is_kept_with_guidance(self):
        service = _service(1, fail=True)

        def body() -> str:
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-09-01", "2026-09-30")
            return "Last month was USD 999.00."

        response = _orchestrate(self.QUERY, body)

        assert response.startswith("## Cost Report Unavailable")
        assert response.endswith(GUIDANCE)
        assert "999" not in response

    def test_report_matching_two_named_periods_is_shown_once(self):
        response = _run(
            "Forecast the rest of the month from my month to date spend (2026-10-01 to 2026-10-15).",
            [("2026-10-01", "2026-10-15")],
        )

        assert response.count("**Report ID:** `cost-report-0`") == 1
        assert response.endswith(GUIDANCE)

    def test_no_report_matches_the_named_period(self):
        assert _run(self.QUERY, [("2026-07-01", "2026-07-31")]) == GUIDANCE

    def test_unrecognized_period_phrase_falls_back_to_guidance(self):
        assert _run("Compare last week's spend with next week's forecast.", [("2026-10-05", "2026-10-11")]) == GUIDANCE

    def test_date_is_read_before_the_turn(self):
        """A turn that crosses UTC midnight still resolves 'last month' as of its start."""
        service = _service(1)
        today_mock = MagicMock(return_value=date(2026, 10, 31))

        def body() -> str:
            today_mock.return_value = date(2026, 11, 1)
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-09-01", "2026-09-30")
            return "Reports built."

        class StubOrchestratorAgent:
            def __call__(self, _query: str) -> str:
                return body()

        with (
            patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
            patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
            patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
            patch.object(orchestrator, "_utc_today", today_mock),
        ):
            response = _real_run_orchestrator(self.QUERY)

        assert "**Report ID:** `cost-report-0`" in response


class TestNonForecastBehaviorIsUnchanged:
    """Historical questions and report follow-ups still return the captured report."""

    def test_historical_question_returns_last_captured_report(self):
        response = _run("Show my costs for last month", [("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30")])

        assert "**Report ID:** `cost-report-1`" in response
        assert GUIDANCE not in response

    def test_estimated_charges_to_date_return_the_report(self):
        """Cost Explorer calls open-period actuals estimated, so this is not a forecast."""
        response = _run("What are my estimated AWS charges so far this month?", [("2026-10-01", "2026-10-15")])

        assert "**Report ID:** `cost-report-0`" in response
        assert GUIDANCE not in response

    def test_report_followup_with_forecast_wording_returns_the_report(self):
        service = _service(1, prefix="cost-0123456789abcdef0123456789abcde")
        create_tools, _ = create_cost_report_tool_bundle(service)
        create_tools[0]("2026-09-01", "2026-09-30")

        def body() -> str:
            tools, _ = create_cost_report_tool_bundle(service)
            tools[1]("cost-0123456789abcdef0123456789abcde0")
            return "Reused."

        response = _orchestrate("Use cost-0123456789abcdef0123456789abcde0 to project next month", body)

        assert "cost-0123456789abcdef0123456789abcde0" in response
        assert GUIDANCE not in response


class TestForecastDetection:
    @pytest.mark.parametrize(
        "query",
        [
            "Forecast my AWS cost for next month.",
            "What is the projected spend?",
            "Predict my AWS costs for the next 30 days.",
            "How much will I spend over the next three months?",
            "Estimate my AWS spend for the rest of the year.",
            "Project my AWS costs through December.",
            "What will my AWS bill be in November?",
            "What is my upcoming bill?",
            "How much am I going to spend?",
            "What will I owe AWS in November?",
            # Each phrasing below matches only one of the wider alternatives.
            "Predict my AWS bill.",
            "What do the next 3 months look like for my spend?",
            "Show my spend for the remainder of the quarter.",
            "Show my spend for the rest of the quarter.",
            "How much will the AWS bill be in November?",
            "How much will the company owe AWS in November?",
            "Will we be charged more in November?",
            "Will we owe AWS more in November?",
            "What is my expected AWS bill for November?",
            "How much should I expect to pay in November?",
            "How much do I expect my bill to be in November?",
            "Estimate my December bill.",
            "Give me an estimate of my November bill.",
            "What is my estimated AWS bill for November?",
            "What's the outlook for my AWS spend?",
        ],
    )
    def test_forecast_phrasings_are_detected(self, query):
        assert orchestrator._is_forecast_request(query)

    @pytest.mark.parametrize(
        "query",
        [
            "What did I spend last month?",
            "Show my AWS costs by service for last month.",
            "How much am I spending this month?",
            "What are cost optimization strategies for EKS?",
            "Estimate the cost of a c5.large instance.",
            "What is the estimated cost of a c5.large instance?",
            "What is the expected cost of running a GameLift fleet?",
            "Will my fleet scale out during peak hours?",
            "What were my AWS costs at the end of the month?",
            "My bill was higher than expected last month.",
            "Why was my bill higher than I expected to pay?",
            "Is last month's bill estimated or final?",
            "What are my estimated AWS charges so far this month?",
            "Show my estimated charges for this month.",
        ],
    )
    def test_historical_and_advisory_phrasings_are_not_detected(self, query):
        assert not orchestrator._is_forecast_request(query)


def _periods(query: str, today: date = TODAY) -> set[tuple[str, str, str]]:
    return {
        (p.start.isoformat(), p.end_min.isoformat(), p.end_max.isoformat())
        for p in requested_historical_periods(query, today)
    }


SEPTEMBER = ("2026-09-01", "2026-09-30", "2026-09-30")
OCTOBER_TO_DATE = ("2026-10-01", "2026-10-15", "2026-10-16")


class TestRequestedHistoricalPeriods:
    """The period resolver recognizes only phrases that cannot name the forecast target."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("my spend last month", {SEPTEMBER}),
            ("last month's spend", {SEPTEMBER}),
            ("last month in EC2", {SEPTEMBER}),
            ("the previous month", {SEPTEMBER}),
            ("month-to-date spend", {OCTOBER_TO_DATE}),
            ("MTD", {OCTOBER_TO_DATE}),
            ("quarter to date", {OCTOBER_TO_DATE}),
            ("last quarter", {("2026-07-01", "2026-09-30", "2026-09-30")}),
            ("last year", {("2025-01-01", "2025-12-31", "2025-12-31")}),
            ("YTD", {("2026-01-01", "2026-10-15", "2026-10-16")}),
            ("yesterday", {("2026-10-15", "2026-10-15", "2026-10-15")}),
            ("costs in July 2026", {("2026-07-01", "2026-07-31", "2026-07-31")}),
            ("July 2026's spend", {("2026-07-01", "2026-07-31", "2026-07-31")}),
            ("September 2026", {SEPTEMBER}),
            ("for December 2025", {("2025-12-01", "2025-12-31", "2025-12-31")}),
            ("from 2026-07-01 to 2026-07-31", {("2026-07-01", "2026-07-31", "2026-07-31")}),
            ("from 2026-10-01 to 2026-10-15", {("2026-10-01", "2026-10-15", "2026-10-15")}),
            ("on 2026-07-04", {("2026-07-04", "2026-07-04", "2026-07-04")}),
            ("on 2026-10-15", {("2026-10-15", "2026-10-15", "2026-10-15")}),
            ("last month and month to date", {SEPTEMBER, OCTOBER_TO_DATE}),
        ],
    )
    def test_recognized_phrases(self, query, expected):
        assert _periods(query) == expected

    @pytest.mark.parametrize(
        "query",
        [
            "Forecast my AWS cost for next month.",
            "What will I spend next quarter?",
            "costs this month",
            "this quarter",
            "this year",
            "current month",
            "today",
            "costs in July",
            "July's spend",
            "Forecast my spend for November",
            "for December",
            "between June and July",
            "last week",
            "the last 30 days",
            "I may need a forecast",
            "March forward with savings",
            "July 0000",
            "October 2026",
            "November 2026",
            "the last month of the year",
            "last month of the year",
            "the last quarter of 2026",
            "the last year of the contract",
            "the last quarter in 2026",
            "the last quarter this year",
            "the last month in the year",
            "The  last month",
            "2026-13-40",
            "from 2026-10-01 to 2026-10-16",
            "from 2026-10-01 to 2026-11-30",
            "on 2026-10-16",
            "on 2026-12-25",
        ],
    )
    def test_unrecognized_phrases(self, query):
        assert _periods(query) == set()

    def test_period_boundaries_at_year_and_quarter_start(self):
        assert _periods("last month", date(2026, 1, 1)) == {("2025-12-01", "2025-12-31", "2025-12-31")}
        assert _periods("month to date", date(2026, 1, 1)) == {("2026-01-01", "2026-01-01", "2026-01-01")}
        assert _periods("last quarter", date(2026, 4, 1)) == {("2026-01-01", "2026-03-31", "2026-03-31")}
        assert _periods("last quarter", date(2026, 1, 15)) == {("2025-10-01", "2025-12-31", "2025-12-31")}
        assert _periods("yesterday", date(2028, 3, 1)) == {("2028-02-29", "2028-02-29", "2028-02-29")}

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
        (period,) = requested_historical_periods("month to date", TODAY)
        assert period.matches(start, end) is expected
