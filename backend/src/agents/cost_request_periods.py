"""Resolve the historical periods a cost question names explicitly.

A forecast turn can produce validated historical reports the user never asked
for (for example, baseline months the Cost specialist fetched while working on
the forecast). The orchestrator keeps such a report only when its period equals
a period the question names. This module recognizes only unambiguous calendar
phrases and explicit ISO dates. Anything it does not recognize names no period,
so the report is dropped rather than guessed at.

Recognized phrases (case-insensitive), relative to today's UTC date:

- ``today``, ``yesterday``
- ``this month`` / ``current month`` / ``month to date`` / ``MTD``
- ``last month`` / ``previous month``
- ``this quarter`` / ``current quarter`` / ``quarter to date`` / ``QTD``
- ``last quarter`` / ``previous quarter``
- ``this year`` / ``current year`` / ``year to date`` / ``YTD``
- ``last year`` / ``previous year``
- a full month name after a preposition (``in July``), before a year
  (``July 2026``), or in the possessive (``July's``). Without a year, a month
  later than the current one names no period, because it can be the coming
  month as easily as last year's.
- ``YYYY-MM-DD`` dates and ``YYYY-MM-DD to YYYY-MM-DD`` ranges

Phrases with no single calendar meaning (``last week``, ``the last 30 days``)
name no period. A to-date period accepts a report that ends on today's date or
the day before, because the current day's cost data is incomplete.
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
_MONTH_ALTERNATION = "|".join(_MONTHS)
_PREPOSITIONS = r"in|for|during|of|from|since|through|thru|until|till|to|between|and|vs\.?|versus|over"

_RELATIVE = re.compile(
    r"\b(?P<when>this|current|last|previous)\s+(?P<unit>month|quarter|year)\b"
    r"|\b(?P<to_date>month|quarter|year)[\s-]+to[\s-]+date\b"
    r"|\b(?P<abbrev>mtd|qtd|ytd)\b"
    r"|\b(?P<day>today|yesterday)\b",
    re.IGNORECASE,
)
_MONTH_AFTER_PREPOSITION = re.compile(
    rf"\b(?:{_PREPOSITIONS})\s+(?:the\s+month\s+of\s+)?(?P<month>{_MONTH_ALTERNATION})\b(?:\s+(?P<year>\d{{4}})\b)?",
    re.IGNORECASE,
)
_MONTH_WITH_YEAR = re.compile(rf"\b(?P<month>{_MONTH_ALTERNATION})\s+(?P<year>\d{{4}})\b", re.IGNORECASE)
_MONTH_POSSESSIVE = re.compile(rf"\b(?P<month>{_MONTH_ALTERNATION})['\u2019]s\b", re.IGNORECASE)
_ISO_RANGE = re.compile(
    r"\b(?P<start>\d{4}-\d{2}-\d{2})\s*(?:to|through|thru|until|till|-|\u2013|\u2014)\s*(?P<end>\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


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
        """Return whether a report period, as ISO dates, is this period."""
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


def _month_end(year: int, month: int) -> date:
    first_of_next = date(year + (month == 12), month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _relative_period(when: str, unit: str, today: date) -> RequestedPeriod:
    if unit == "month":
        this_start = today.replace(day=1)
        if when in ("this", "current"):
            return _to_date(this_start, today)
        last_end = this_start - timedelta(days=1)
        return _complete(last_end.replace(day=1), last_end)
    if unit == "quarter":
        this_start = _quarter_start(today)
        if when in ("this", "current"):
            return _to_date(this_start, today)
        last_end = this_start - timedelta(days=1)
        return _complete(_quarter_start(last_end), last_end)
    this_start = date(today.year, 1, 1)
    if when in ("this", "current"):
        return _to_date(this_start, today)
    return _complete(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))


def _month_period(name: str, year_text: str | None, today: date) -> RequestedPeriod | None:
    month = _MONTHS.index(name.lower()) + 1
    if year_text:
        year = int(year_text)
    elif month <= today.month:
        year = today.year
    else:
        # A later month without a year can mean the coming one (a forecast
        # target) as easily as last year's, so it names no historical period.
        return None
    if (year, month) > (today.year, today.month):
        return None
    start = date(year, month, 1)
    if (year, month) == (today.year, today.month):
        return _to_date(start, today)
    return _complete(start, _month_end(year, month))


def _parse_iso(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def requested_historical_periods(query: str, today: date) -> list[RequestedPeriod]:
    """Return the distinct historical periods ``query`` names, in order of appearance."""
    found: list[tuple[int, RequestedPeriod]] = []

    for match in _RELATIVE.finditer(query):
        if match.group("day"):
            day = today if match.group("day").lower() == "today" else today - timedelta(days=1)
            found.append((match.start(), _complete(day, day)))
        elif match.group("to_date") or match.group("abbrev"):
            unit = (
                match.group("to_date")
                or {"mtd": "month", "qtd": "quarter", "ytd": "year"}[match.group("abbrev").lower()]
            ).lower()
            found.append((match.start(), _relative_period("this", unit, today)))
        else:
            found.append(
                (match.start(), _relative_period(match.group("when").lower(), match.group("unit").lower(), today))
            )

    month_spans: set[tuple[int, int]] = set()
    for pattern in (_MONTH_AFTER_PREPOSITION, _MONTH_WITH_YEAR, _MONTH_POSSESSIVE):
        for match in pattern.finditer(query):
            span = match.span("month")
            if span in month_spans:
                continue
            month_spans.add(span)
            period = _month_period(match.group("month"), match.groupdict().get("year"), today)
            if period:
                found.append((span[0], period))

    range_spans: list[tuple[int, int]] = []
    for match in _ISO_RANGE.finditer(query):
        range_spans.append(match.span())
        start, end = _parse_iso(match.group("start")), _parse_iso(match.group("end"))
        if start and end and start <= end <= today:
            found.append((match.start(), _complete(start, end)))
    for match in _ISO_DATE.finditer(query):
        if any(lo <= match.start() < hi for lo, hi in range_spans):
            continue
        day = _parse_iso(match.group(0))
        if day and day <= today:
            found.append((match.start(), _complete(day, day)))

    periods: list[RequestedPeriod] = []
    for _, period in sorted(found, key=lambda item: item[0]):
        if period not in periods:
            periods.append(period)
    return periods
