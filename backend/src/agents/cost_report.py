"""Deterministic, auditable AWS Cost Explorer reports."""

from __future__ import annotations

# Standard library
import threading
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, cast

# Third-party packages
import boto3
from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, Field
from strands import tool

# Local modules
from agents.chart_directive import CHART_CONTRACT_VERSION, render_chart_fence
from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG
from utils.logger import logger

_CENT = Decimal("0.01")
_PERCENT_TENTH = Decimal("0.1")
_TOP_SERVICE_COUNT = 5
_SUPPORTED_METRICS = frozenset(
    {
        "AmortizedCost",
        "BlendedCost",
        "NetAmortizedCost",
        "NetUnblendedCost",
        "UnblendedCost",
    }
)


class _ReportModel(BaseModel):
    """Base model for immutable camel-cased tool responses."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)


class CostReportPeriod(_ReportModel):
    start: str
    end_inclusive: str = Field(alias="endInclusive")
    end_exclusive: str = Field(alias="endExclusive")


class CostService(_ReportModel):
    rank: int
    service: str
    raw_amount: str = Field(alias="rawAmount")
    amount: str
    percentage: str


class CostReportValidation(_ReportModel):
    all_service_amounts_total: str = Field(alias="allServiceAmountsTotal")
    displayed_breakdown_total: str = Field(alias="displayedBreakdownTotal")
    all_service_percentages_total: str = Field(alias="allServicePercentagesTotal")
    total_matches: bool = Field(alias="totalMatches")
    rankings_match: bool = Field(alias="rankingsMatch")
    percentages_match: bool = Field(alias="percentagesMatch")


class CostReport(_ReportModel):
    schema_version: str = Field(alias="schemaVersion")
    report_id: str = Field(alias="reportId")
    source: str
    metric: str
    currency: str
    period: CostReportPeriod
    queried_at: str = Field(alias="queriedAt")
    estimated: bool
    total_raw: str = Field(alias="totalRaw")
    total: str
    top_services: list[CostService] = Field(alias="topServices")
    other_services_raw_total: str = Field(alias="otherServicesRawTotal")
    other_services_total: str = Field(alias="otherServicesTotal")
    validation: CostReportValidation


class CostReportSelection(_ReportModel):
    schema_version: str = Field(alias="schemaVersion")
    report_id: str = Field(alias="reportId")
    source: str
    metric: str
    currency: str
    period: CostReportPeriod
    queried_at: str = Field(alias="queriedAt")
    estimated: bool
    services: list[str]
    raw_amount: str = Field(alias="rawAmount")
    amount: str
    percentage: str
    snapshot_reused: bool = Field(alias="snapshotReused")


@dataclass(frozen=True)
class RawServiceCost:
    """Raw API decimal strings and their exact aggregate for one service."""

    service: str
    source_amounts: tuple[str, ...]
    amount: Decimal


@dataclass(frozen=True)
class CostReportSnapshot:
    """Cached immutable report plus exact amounts used for follow-up calculations."""

    report: CostReport
    raw_services: tuple[RawServiceCost, ...]


class CostReportError(RuntimeError):
    """A safe, typed failure that never contains unverified financial output."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            }
        }


class CostReportCache:
    """Thread-safe bounded cache for report snapshots."""

    def __init__(self, maxsize: int = 128, ttl_seconds: int = 1800) -> None:
        self._cache: TTLCache[str, CostReportSnapshot] = TTLCache(maxsize=maxsize, ttl=ttl_seconds)
        self._lock = threading.RLock()

    def put(self, snapshot: CostReportSnapshot) -> None:
        with self._lock:
            self._cache[snapshot.report.report_id] = snapshot

    def get(self, report_id: str) -> CostReportSnapshot | None:
        with self._lock:
            return cast(CostReportSnapshot | None, self._cache.get(report_id))

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


@dataclass(frozen=True)
class CostReportCapture:
    """Request-scoped capture handle used to bypass model-generated financial prose."""

    capture_id: str
    token: Token[str | None]


_active_capture_id: ContextVar[str | None] = ContextVar("cost_report_capture_id", default=None)
_captured_responses: dict[str, str] = {}
_capture_lock = threading.RLock()


def begin_cost_report_capture() -> CostReportCapture:
    """Start request-scoped capture of an authoritative cost rendering."""
    capture_id = uuid.uuid4().hex
    token = _active_capture_id.set(capture_id)
    with _capture_lock:
        _captured_responses.pop(capture_id, None)
    return CostReportCapture(capture_id=capture_id, token=token)


def finish_cost_report_capture(capture: CostReportCapture) -> str | None:
    """Finish a capture and return the last deterministic rendering, if any."""
    _active_capture_id.reset(capture.token)
    with _capture_lock:
        return _captured_responses.pop(capture.capture_id, None)


def _capture_authoritative_response(response: str) -> None:
    capture_id = _active_capture_id.get()
    if capture_id:
        with _capture_lock:
            _captured_responses[capture_id] = response


def _parse_period(start_date: str, end_date_inclusive: str) -> tuple[date, date, date]:
    try:
        start = date.fromisoformat(start_date)
        end_inclusive = date.fromisoformat(end_date_inclusive)
    except (TypeError, ValueError) as exc:
        raise CostReportError(
            "INVALID_DATE",
            "Cost report dates must use YYYY-MM-DD. Update the dates and try again.",
            retryable=False,
        ) from exc

    if start.isoformat() != start_date or end_inclusive.isoformat() != end_date_inclusive:
        raise CostReportError(
            "INVALID_DATE",
            "Cost report dates must use YYYY-MM-DD. Update the dates and try again.",
            retryable=False,
        )

    if end_inclusive < start:
        raise CostReportError(
            "INVALID_DATE_RANGE",
            "The cost report end date must be on or after the start date. Update the dates and try again.",
            retryable=False,
        )

    try:
        end_exclusive = end_inclusive + timedelta(days=1)
    except OverflowError as exc:
        raise CostReportError(
            "INVALID_DATE_RANGE",
            "The cost report end date cannot be converted to an exclusive API date.",
            retryable=False,
        ) from exc

    return start, end_inclusive, end_exclusive


def _decimal_from_api(value: Any) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise CostReportError(
            "INVALID_COST_EXPLORER_RESPONSE",
            "Cost Explorer returned an invalid monetary amount. Retry the report.",
            retryable=True,
        )

    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise CostReportError(
            "INVALID_COST_EXPLORER_RESPONSE",
            "Cost Explorer returned an invalid monetary amount. Retry the report.",
            retryable=True,
        ) from exc

    if not amount.is_finite():
        raise CostReportError(
            "INVALID_COST_EXPLORER_RESPONSE",
            "Cost Explorer returned a non-finite monetary amount. Retry the report.",
            retryable=True,
        )
    return amount


def _plain_decimal(value: Decimal) -> str:
    if value == 0:
        value = abs(value)
    return format(value, "f")


def _display_decimal(value: Decimal, quantum: Decimal) -> str:
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    exponent = quantum.as_tuple().exponent
    assert isinstance(exponent, int)
    decimal_places = max(0, -exponent)
    return format(rounded, f".{decimal_places}f")


def _allocate_rounded_values(
    values: list[tuple[str, Decimal]],
    *,
    target: Decimal,
    quantum: Decimal,
) -> dict[str, Decimal]:
    """Round each value while reconciling their displayed sum to the target."""
    rounded = {key: value.quantize(quantum, rounding=ROUND_HALF_UP) for key, value in values}
    rounded_total = sum(rounded.values(), Decimal("0"))
    delta_steps = int((target - rounded_total) / quantum)
    if delta_steps == 0:
        return rounded

    errors = {key: value - rounded[key] for key, value in values}
    if delta_steps > 0:
        order = sorted(values, key=lambda item: (-errors[item[0]], item[0].casefold(), item[0]))
        adjustment = quantum
    else:
        order = sorted(values, key=lambda item: (errors[item[0]], item[0].casefold(), item[0]))
        adjustment = -quantum

    for index in range(abs(delta_steps)):
        key = order[index % len(order)][0]
        rounded[key] += adjustment
    return rounded


def _rank_services(raw_services: tuple[RawServiceCost, ...]) -> list[RawServiceCost]:
    return sorted(raw_services, key=lambda item: (-item.amount, item.service.casefold(), item.service))


def _build_report(
    raw_services: tuple[RawServiceCost, ...],
    *,
    report_id: str,
    metric: str,
    currency: str,
    start: date,
    end_inclusive: date,
    end_exclusive: date,
    queried_at: str,
    estimated: bool,
) -> CostReport:
    ranked = _rank_services(raw_services)
    total_raw = sum((item.amount for item in ranked), Decimal("0"))
    total_display = total_raw.quantize(_CENT, rounding=ROUND_HALF_UP)
    displayed_amounts = _allocate_rounded_values(
        [(item.service, item.amount) for item in ranked],
        target=total_display,
        quantum=_CENT,
    )

    if total_raw == 0:
        raw_percentages = [(item.service, Decimal("0")) for item in ranked]
        percentage_target = Decimal("0.0")
    else:
        raw_percentages = [(item.service, item.amount * Decimal("100") / total_raw) for item in ranked]
        percentage_target = Decimal("100.0")
    displayed_percentages = _allocate_rounded_values(
        raw_percentages,
        target=percentage_target,
        quantum=_PERCENT_TENTH,
    )

    top_raw_services = ranked[:_TOP_SERVICE_COUNT]
    other_raw_services = ranked[_TOP_SERVICE_COUNT:]
    top_services = [
        CostService(
            rank=index,
            service=item.service,
            rawAmount=_plain_decimal(item.amount),
            amount=_display_decimal(displayed_amounts[item.service], _CENT),
            percentage=_display_decimal(displayed_percentages[item.service], _PERCENT_TENTH),
        )
        for index, item in enumerate(top_raw_services, start=1)
    ]
    other_raw_total = sum((item.amount for item in other_raw_services), Decimal("0"))
    other_display_total = sum((displayed_amounts[item.service] for item in other_raw_services), Decimal("0"))

    displayed_breakdown_total = sum((Decimal(item.amount) for item in top_services), Decimal("0"))
    displayed_breakdown_total += other_display_total
    all_percentages_total = sum(displayed_percentages.values(), Decimal("0"))
    expected_ranking = [item.service for item in ranked]
    actual_ranking = [item.service for item in top_raw_services] + [item.service for item in other_raw_services]

    validation = CostReportValidation(
        allServiceAmountsTotal=_display_decimal(total_raw, _CENT),
        displayedBreakdownTotal=_display_decimal(displayed_breakdown_total, _CENT),
        allServicePercentagesTotal=_display_decimal(all_percentages_total, _PERCENT_TENTH),
        totalMatches=displayed_breakdown_total == total_display,
        rankingsMatch=actual_ranking == expected_ranking,
        percentagesMatch=all_percentages_total == percentage_target,
    )
    if not (validation.total_matches and validation.rankings_match and validation.percentages_match):
        raise CostReportError(
            "COST_REPORT_VALIDATION_FAILED",
            "The Cost Explorer snapshot did not pass deterministic validation. Retry the report.",
            retryable=True,
        )

    return CostReport(
        schemaVersion="1.0",
        reportId=report_id,
        source="AWS Cost Explorer",
        metric=metric,
        currency=currency,
        period=CostReportPeriod(
            start=start.isoformat(),
            endInclusive=end_inclusive.isoformat(),
            endExclusive=end_exclusive.isoformat(),
        ),
        queriedAt=queried_at,
        estimated=estimated,
        totalRaw=_plain_decimal(total_raw),
        total=_display_decimal(total_display, _CENT),
        topServices=top_services,
        otherServicesRawTotal=_plain_decimal(other_raw_total),
        otherServicesTotal=_display_decimal(other_display_total, _CENT),
        validation=validation,
    )


def _default_cost_explorer_client() -> Any:
    return boto3.client("ce", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)


def _default_queried_at() -> datetime:
    return datetime.now(timezone.utc)


class CostReportService:
    """Fetch, validate, cache, and reuse deterministic Cost Explorer snapshots."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = _default_cost_explorer_client,
        cache: CostReportCache | None = None,
        now: Callable[[], datetime] = _default_queried_at,
        report_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._cache = cache or CostReportCache()
        self._now = now
        self._report_id_factory = report_id_factory or (lambda: f"cost-{uuid.uuid4().hex}")

    def create_report(
        self,
        start_date: str,
        end_date_inclusive: str,
        metric: str = "UnblendedCost",
    ) -> CostReportSnapshot:
        started_at = time.perf_counter()
        start, end_inclusive, end_exclusive = _parse_period(start_date, end_date_inclusive)
        if metric not in _SUPPORTED_METRICS:
            raise CostReportError(
                "UNSUPPORTED_COST_METRIC",
                f"Unsupported monetary metric '{metric}'. Use one of: {', '.join(sorted(_SUPPORTED_METRICS))}.",
                retryable=False,
            )

        query = {
            "TimePeriod": {"Start": start.isoformat(), "End": end_exclusive.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": [metric],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        source_amounts: dict[str, list[str]] = {}
        units: set[str] = set()
        estimated = False
        page_count = 0
        next_page_token: str | None = None
        seen_tokens: set[str] = set()

        try:
            client = self._client_factory()
            while True:
                request = dict(query)
                if next_page_token:
                    request["NextPageToken"] = next_page_token
                response = client.get_cost_and_usage(**request)
                page_count += 1

                results = response.get("ResultsByTime")
                if not isinstance(results, list):
                    raise CostReportError(
                        "INVALID_COST_EXPLORER_RESPONSE",
                        "Cost Explorer omitted the grouped time results. Retry the report.",
                        retryable=True,
                    )

                for result in results:
                    groups = result.get("Groups")
                    if not isinstance(groups, list):
                        raise CostReportError(
                            "INVALID_COST_EXPLORER_RESPONSE",
                            "Cost Explorer omitted grouped service costs. Retry the report.",
                            retryable=True,
                        )
                    estimated = estimated or result.get("Estimated") is True
                    for group in groups:
                        keys = group.get("Keys")
                        if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str) or not keys[0]:
                            raise CostReportError(
                                "INVALID_COST_EXPLORER_RESPONSE",
                                "Cost Explorer returned a cost group without a service name. Retry the report.",
                                retryable=True,
                            )
                        metric_value = group.get("Metrics", {}).get(metric)
                        if not isinstance(metric_value, dict):
                            raise CostReportError(
                                "INVALID_COST_EXPLORER_RESPONSE",
                                "Cost Explorer omitted the requested metric for a service. Retry the report.",
                                retryable=True,
                            )
                        amount_string = metric_value.get("Amount")
                        _decimal_from_api(amount_string)
                        unit = metric_value.get("Unit")
                        if not isinstance(unit, str) or not unit:
                            raise CostReportError(
                                "INVALID_COST_EXPLORER_RESPONSE",
                                "Cost Explorer omitted the currency for a service amount. Retry the report.",
                                retryable=True,
                            )
                        source_amounts.setdefault(keys[0], []).append(amount_string)
                        units.add(unit)

                next_page_token = response.get("NextPageToken")
                if not next_page_token:
                    break
                if not isinstance(next_page_token, str) or next_page_token in seen_tokens:
                    raise CostReportError(
                        "INVALID_COST_EXPLORER_RESPONSE",
                        "Cost Explorer returned an invalid pagination token. Retry the report.",
                        retryable=True,
                    )
                seen_tokens.add(next_page_token)
        except CostReportError:
            raise
        except Exception as exc:
            logger.error("Cost Explorer request failed while building a deterministic report", exc_info=True)
            raise CostReportError(
                "COST_EXPLORER_REQUEST_FAILED",
                "AWS Cost Explorer could not produce a validated report. Retry the request.",
                retryable=True,
            ) from exc

        if not source_amounts:
            raise CostReportError(
                "NO_COST_DATA",
                "Cost Explorer returned no grouped service costs for this period. Adjust the dates and try again.",
                retryable=False,
            )
        if len(units) != 1:
            raise CostReportError(
                "MULTIPLE_CURRENCIES",
                "Cost Explorer returned multiple currencies or units. No financial report was produced; retry the request.",
                retryable=True,
            )

        raw_services = tuple(
            RawServiceCost(
                service=service,
                source_amounts=tuple(amounts),
                amount=sum((_decimal_from_api(amount) for amount in amounts), Decimal("0")),
            )
            for service, amounts in source_amounts.items()
        )
        queried_at_datetime = self._now().astimezone(timezone.utc)
        queried_at = queried_at_datetime.isoformat(timespec="seconds").replace("+00:00", "Z")
        report = _build_report(
            raw_services,
            report_id=self._report_id_factory(),
            metric=metric,
            currency=next(iter(units)),
            start=start,
            end_inclusive=end_inclusive,
            end_exclusive=end_exclusive,
            queried_at=queried_at,
            estimated=estimated,
        )
        snapshot = CostReportSnapshot(report=report, raw_services=raw_services)
        self._cache.put(snapshot)

        latency_ms = round((time.perf_counter() - started_at) * 1000)
        logger.info(
            "Cost report generated "
            f"report_id={report.report_id} start={start.isoformat()} end_inclusive={end_inclusive.isoformat()} "
            f"queried_at={queried_at} validation=passed pages={page_count} latency_ms={latency_ms}"
        )
        return snapshot

    def reuse_report(self, report_id: str, service_names: list[str] | None = None) -> CostReport | CostReportSelection:
        snapshot = self._cache.get(report_id)
        if snapshot is None:
            raise CostReportError(
                "COST_REPORT_NOT_FOUND",
                "That cost report snapshot is unavailable or expired. Run a new cost report and try again.",
                retryable=True,
            )
        if not service_names:
            logger.info(
                f"Cost report reused report_id={report_id} start={snapshot.report.period.start} "
                f"end_inclusive={snapshot.report.period.end_inclusive} "
                f"queried_at={snapshot.report.queried_at} validation=passed"
            )
            return snapshot.report

        by_name = {item.service: item for item in snapshot.raw_services}
        by_casefold = {item.service.casefold(): item for item in snapshot.raw_services}
        selected: list[RawServiceCost] = []
        missing: list[str] = []
        seen_services: set[str] = set()
        for requested_name in service_names:
            match = by_name.get(requested_name) or by_casefold.get(requested_name.casefold())
            if match is None:
                missing.append(requested_name)
            elif match.service not in seen_services:
                selected.append(match)
                seen_services.add(match.service)
        if missing:
            raise CostReportError(
                "SERVICE_NOT_IN_REPORT",
                "One or more requested services are not present in that report snapshot. Check the service names and retry.",
                retryable=False,
            )

        selected_raw = sum((item.amount for item in selected), Decimal("0"))
        total_raw = Decimal(snapshot.report.total_raw)
        percentage = Decimal("0") if total_raw == 0 else selected_raw * Decimal("100") / total_raw
        logger.info(
            f"Cost report selection reused report_id={report_id} service_count={len(selected)} "
            f"start={snapshot.report.period.start} end_inclusive={snapshot.report.period.end_inclusive} "
            f"queried_at={snapshot.report.queried_at} validation=passed"
        )
        return CostReportSelection(
            schemaVersion="1.0",
            reportId=report_id,
            source=snapshot.report.source,
            metric=snapshot.report.metric,
            currency=snapshot.report.currency,
            period=snapshot.report.period,
            queriedAt=snapshot.report.queried_at,
            estimated=snapshot.report.estimated,
            services=[item.service for item in selected],
            rawAmount=_plain_decimal(selected_raw),
            amount=_display_decimal(selected_raw, _CENT),
            percentage=_display_decimal(percentage, _PERCENT_TENTH),
            snapshotReused=True,
        )


def _cost_report_chart_fence(report: CostReport) -> str:
    """Build a deterministic ```chart fence for a validated cost report.

    The chart is produced directly from the same validated, displayed amounts
    that appear in the markdown table — the model never sees or reconstructs
    these numbers — so the visual is exactly consistent with the authoritative
    figures (issue #255). Returns "" if no chartable data or if the spec fails
    the shared contract (fail-closed).
    """
    labels = [service.service for service in report.top_services]
    values = [float(service.amount) for service in report.top_services]
    if Decimal(report.other_services_total) != 0:
        labels.append("Other services")
        values.append(float(report.other_services_total))
    if not labels:
        return ""

    leader = report.top_services[0] if report.top_services else None
    summary = (
        f"{leader.service} leads at {report.currency} {leader.amount} ({leader.percentage}% of total)."
        if leader
        else f"Total {report.currency} {report.total}."
    )
    spec = {
        "type": "bar",
        "version": CHART_CONTRACT_VERSION,
        "title": f"Top services by {report.metric} ({report.currency})",
        "summary": summary[:280],
        "unit": report.currency,
        "x": {"label": "Service", "values": labels},
        "y": {"label": report.currency},
        "series": [{"name": report.metric, "values": values}],
    }
    return render_chart_fence(spec)


def render_cost_report(report: CostReport) -> str:
    """Render the immutable user-facing financial section."""
    lines = [
        "## Validated AWS Cost Explorer Report",
        "",
        f"**Report ID:** `{report.report_id}`  ",
        f"**Source:** {report.source}  ",
        f"**Metric:** {report.metric}  ",
        f"**Period:** {report.period.start} through {report.period.end_inclusive} "
        f"(API end: {report.period.end_exclusive}, exclusive)  ",
        f"**Queried at:** {report.queried_at}  ",
        f"**Estimated:** {'Yes' if report.estimated else 'No'}",
        "",
        "| Rank | Service | Amount | Share |",
        "| ---: | --- | ---: | ---: |",
    ]
    lines.extend(
        f"| {service.rank} | {service.service} | {report.currency} {service.amount} | {service.percentage}% |"
        for service in report.top_services
    )
    lines.extend(
        [
            "",
            f"**Other services:** {report.currency} {report.other_services_total}  ",
            f"**Total:** {report.currency} {report.total}  ",
            "**Validation:** Passed; the displayed breakdown reconciles to the total and rankings and shares "
            "were calculated from the same raw snapshot.",
        ]
    )
    if report.estimated:
        lines.extend(
            [
                "",
                "*Cost Explorer marked this snapshot as estimated. It is internally consistent at the query "
                "timestamp, but AWS may revise open-period billing data.*",
            ]
        )
    chart_fence = _cost_report_chart_fence(report)
    if chart_fence:
        lines.extend(["", chart_fence])
    return "\n".join(lines)


def render_cost_report_selection(selection: CostReportSelection) -> str:
    """Render a deterministic follow-up calculation from a cached snapshot."""
    service_list = ", ".join(selection.services)
    lines = [
        "## Validated Cost Report Snapshot Calculation",
        "",
        f"**Report ID:** `{selection.report_id}`  ",
        f"**Source:** {selection.source}  ",
        f"**Period:** {selection.period.start} through {selection.period.end_inclusive}  ",
        f"**Queried at:** {selection.queried_at}  ",
        f"**Selected services:** {service_list}  ",
        f"**Combined amount:** {selection.currency} {selection.amount}  ",
        f"**Combined share:** {selection.percentage}%  ",
        "**Snapshot reused:** Yes; no new Cost Explorer query was made.",
    ]
    if selection.estimated:
        lines.extend(
            [
                "",
                "*Cost Explorer marked the source snapshot as estimated. AWS may revise open-period billing data.*",
            ]
        )
    return "\n".join(lines)


def _render_error(error: CostReportError) -> str:
    retry_guidance = " Please retry." if error.retryable else ""
    return (
        f"## Cost Report Unavailable\n\n{error.message}{retry_guidance}\n\nNo unverified financial report was produced."
    )


_cost_report_service = CostReportService()


def create_cost_report_tool_bundle(
    service: CostReportService | None = None,
) -> tuple[list[Any], Callable[[str], str]]:
    """Create request-local tools and a finalizer that discards generated financial prose."""
    report_service = service or _cost_report_service
    authoritative_response: list[str] = []

    def record(response: str) -> None:
        authoritative_response[:] = [response]
        _capture_authoritative_response(response)

    @tool
    def get_cost_report(
        start_date: str,
        end_date_inclusive: str,
        metric: str = "UnblendedCost",
    ) -> dict[str, Any]:
        """Create a validated service cost report for an inclusive YYYY-MM-DD date range.

        Use this tool for actual cost totals, service rankings, and percentages. The tool performs all
        financial arithmetic. Return validatedFinancialSection exactly and do not calculate other values.
        """
        try:
            snapshot = report_service.create_report(start_date, end_date_inclusive, metric)
            rendered = render_cost_report(snapshot.report)
            record(rendered)
            return {
                "report": snapshot.report.model_dump(by_alias=True, mode="json"),
                "validatedFinancialSection": rendered,
            }
        except CostReportError as error:
            rendered = _render_error(error)
            record(rendered)
            return error.as_payload()

    @tool
    def reuse_cost_report(report_id: str, service_names: list[str] | None = None) -> dict[str, Any]:
        """Reuse a cached report ID, optionally calculating a combined share for named services.

        This tool never issues a new Cost Explorer query. Return validatedFinancialSection exactly and do not
        perform arithmetic in generated prose.
        """
        try:
            result = report_service.reuse_report(report_id, service_names)
            if isinstance(result, CostReport):
                rendered = render_cost_report(result)
                payload_key = "report"
            else:
                rendered = render_cost_report_selection(result)
                payload_key = "selection"
            record(rendered)
            return {
                payload_key: result.model_dump(by_alias=True, mode="json"),
                "validatedFinancialSection": rendered,
            }
        except CostReportError as error:
            rendered = _render_error(error)
            record(rendered)
            return error.as_payload()

    def finalize_response(model_response: str) -> str:
        return authoritative_response[-1] if authoritative_response else model_response

    return [get_cost_report, reuse_cost_report], finalize_response
