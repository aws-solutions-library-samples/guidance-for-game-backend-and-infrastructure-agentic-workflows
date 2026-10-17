"""Resolve the historical periods a forecast question names explicitly.

A forecast turn can produce validated historical reports the user never asked
for (for example, baseline months the Cost specialist fetched while working on
the forecast). The orchestrator keeps such a report only when its period equals
a period the question names. This resolver runs only for forecast questions, so
it recognizes only phrases that cannot name the forecast target itself. Anything
it does not recognize names no period, and the report is dropped rather than
guessed at.

Recognized phrases (case-insensitive), relative to today's UTC date:

- ``yesterday``
- ``last month`` / ``previous month``, ``last quarter`` / ``previous quarter``,
  ``last year`` / ``previous year``
- ``month to date`` / ``MTD``, ``quarter to date`` / ``QTD``,
  ``year to date`` / ``YTD``
- a full month name with a four-digit year (``July 2026``), when that month has
  started
- ``YYYY-MM-DD`` dates and ``YYYY-MM-DD to YYYY-MM-DD`` ranges that are not in
  the future

Phrases that can name the forecast target (``this month``, ``today``, or a month
name without a year, such as "in July") and phrases with no single calendar
meaning (``last week``, ``the last 30 days``) name no period. A to-date period
accepts a report that ends on today's date or the day before, because the
current day's cost data is incomplete.
"""

# Standard library
import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

_RELATIVE = re.compile(
    r"\b(?:last|previous)\s+(?P<unit>month|quarter|year)\b"
    r"|\b(?P<to_date>month|quarter|year)[\s-]+to[\s-]+date\b"
    r"|\b(?P<abbrev>mtd|qtd|ytd)\b"
    r"|\b(?P<yesterday>yesterday)\b",
    re.IGNORECASE,
)
_MONTH_WITH_YEAR = re.compile(rf"\b(?P<month>{'|'.join(_MONTHS)})\s+(?P<year>\d{{4}})\b", re.IGNORECASE)
_ISO_RANGE = re.compile(
    r"\b(?P<start>\d{4}-\d{2}-\d{2})\s*(?:to|through|thru|until|till|-|\u2013|\u2014)\s*(?P<end>\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TO_DATE_UNITS = {"mtd": "month", "qtd": "quarter", "ytd": "year"}


@dataclass(frozen=True)
class RequestedPeriod:
    """A historical period named by a question.

    A report matches when it starts on ``start`` and its inclusive end date
    falls between ``end_min`` and ``end_max``.
    """

    start: date
    end_min: date
    end_max: date

    def matches(self, start: str | None, end_inclusive: str | None) -> bool:
        """Return whether a report period, given as ISO dates, is this period."""
        if not start or not end_inclusive:
            return False
        try:
            report_start = date.fromisoformat(start)
            report_end = date.fromisoformat(end_inclusive)
        except ValueError:
            return False
        return report_start == self.start and self.end_min <= report_end <= self.end_max


def _complete(start: date, end: date) -> RequestedPeriod:
    return RequestedPeriod(start=start, end_min=end, end_max=end)


def _to_date(start: date, today: date) -> RequestedPeriod:
    return RequestedPeriod(start=start, end_min=max(start, today - timedelta(days=1)), end_max=today)


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _unit_start(unit: str, today: date) -> date:
    if unit == "month":
        return today.replace(day=1)
    if unit == "quarter":
        return _quarter_start(today)
    return date(today.year, 1, 1)


def _previous(unit: str, today: date) -> RequestedPeriod:
    this_start = _unit_start(unit, today)
    last_end = this_start - timedelta(days=1)
    return _complete(_unit_start(unit, last_end), last_end)


def _month_with_year(name: str, year_text: str, today: date) -> RequestedPeriod | None:
    month = _MONTHS.index(name.lower()) + 1
    year = int(year_text)
    if year < 1 or (year, month) > (today.year, today.month):
        return None
    start = date(year, month, 1)
    if (year, month) == (today.year, today.month):
        return _to_date(start, today)
    first_of_next = date(year + (month == 12), month % 12 + 1, 1)
    return _complete(start, first_of_next - timedelta(days=1))


def _parse_iso(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def requested_historical_periods(query: str, today: date) -> list[RequestedPeriod]:
    """Return the distinct historical periods ``query`` names, in order of appearance."""
    found: list[tuple[int, RequestedPeriod]] = []

    for match in _RELATIVE.finditer(query):
        if match.group("yesterday"):
            day = today - timedelta(days=1)
            found.append((match.start(), _complete(day, day)))
        elif match.group("unit"):
            found.append((match.start(), _previous(match.group("unit").lower(), today)))
        else:
            unit = (match.group("to_date") or _TO_DATE_UNITS[match.group("abbrev").lower()]).lower()
            found.append((match.start(), _to_date(_unit_start(unit, today), today)))

    for match in _MONTH_WITH_YEAR.finditer(query):
        period = _month_with_year(match.group("month"), match.group("year"), today)
        if period:
            found.append((match.start(), period))

    range_spans: list[tuple[int, int]] = []
    for match in _ISO_RANGE.finditer(query):
        range_spans.append(match.span())
        start, end = _parse_iso(match.group("start")), _parse_iso(match.group("end"))
        if start and end and start <= end <= today:
            found.append((match.start(), _complete(start, end)))
    for match in _ISO_DATE.finditer(query):
        if any(low <= match.start() < high for low, high in range_spans):
            continue
        day = _parse_iso(match.group(0))
        if day and day <= today:
            found.append((match.start(), _complete(day, day)))

    periods: list[RequestedPeriod] = []
    for _, period in sorted(found, key=lambda item: item[0]):
        if period not in periods:
            periods.append(period)
    return periods
