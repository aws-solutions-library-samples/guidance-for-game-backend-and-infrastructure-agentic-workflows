"""Fail-closed detection and sanitization of unvalidated financial content.

Only the validated Cost Explorer report path (``agents.cost_report``) may emit
financial figures. Any *other* specialist's model-authored text that appears to
contain a monetary value must never reach the user, because the orchestrator
cannot prove that value was derived from an authoritative snapshot.

This module provides a conservative, deterministic classifier plus a sanitizer.
The design bias is fail-closed: when a non-cost specialist section looks like it
contains a currency amount, a currency code next to a value, or financial-value
language, the *entire* section is replaced with a fixed notice rather than
attempting to strip or retain the offending numbers. Legitimate non-financial
operational output (fleet names, pod counts, ports, node status) is preserved
verbatim because it carries no currency marker.

The classifier is intentionally keyed on *currency markers* (symbols, ISO codes,
currency words) rather than bare numbers, so operational values such as "3
fleets", "5 pods", or "port 7777" do not trip it while "USD 999.00" or "$999"
do.
"""

from __future__ import annotations

# Standard library
import re

# Currency symbols that, when adjacent to a digit (either order), indicate a
# monetary amount. Kept as a character class; every character here is literal
# inside ``[...]``.
_CURRENCY_SYMBOLS = "$€£¥₹"

# Common ISO 4217 codes. Conservative but broad; adjacency to a number is what
# actually triggers a match, so listing extra codes cannot cause false matches
# on their own.
_ISO_CODES = (
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CNY",
    "CAD",
    "AUD",
    "INR",
    "CHF",
    "SEK",
    "NZD",
    "BRL",
    "ZAR",
    "SGD",
    "HKD",
    "NOK",
    "KRW",
    "MXN",
    "RUB",
)
_ISO_ALTERNATION = "|".join(_ISO_CODES)

# A currency symbol immediately adjacent to a digit, in either order:
#   "$999", "999$", "€ 12", "12 £"
_SYMBOL_AMOUNT_RE = re.compile(rf"[{_CURRENCY_SYMBOLS}]\s*\d|\d\s*[{_CURRENCY_SYMBOLS}]")

# An ISO currency code adjacent to a numeric value, in either order:
#   "USD 999.00", "USD999", "999.00 USD", "999USD"
# No word boundary is required *between* the code and the digits, because a
# glued form like "USD999" has no boundary there. A boundary is still required
# on the outer side so a code embedded in another word does not match.
# The number-then-code branch starts only at the beginning of a run of digits,
# commas, and periods (leading separators included), so a long run is scanned
# once instead of once per digit. It matches exactly the same text.
_CODE_AMOUNT_RE = re.compile(
    rf"\b(?:{_ISO_ALTERNATION})[\s:]*\$?\d"  # code then number ("USD 5", "USD999")
    rf"|(?<![\d,.])[.,]*\d[\d,.]*\s*(?:{_ISO_ALTERNATION})\b",  # number then code ("999 USD", "999USD")
    re.IGNORECASE,
)

# Explicit currency words require an adjacent value; a bare discussion of
# "dollars" or "yen" is not itself a financial figure.
_CURRENCY_WORD = r"dollars?|cents?|euros?|yen"
# A money number starts where no ASCII letter or digit precedes it. It does not
# start right after a digit and a comma ("1,|200"), because the same number
# already matches from the start of that run, so a long comma-separated run is
# scanned once instead of once per group. When the run is glued to a letter
# ("Rs1,200"), the second branch starts at the first group instead. Every text
# that matched before still matches; a glued run can now start a match one
# group earlier, which only adds matches near the edge of a word window.
_MONEY_NUMBER = (
    r"(?:(?<![A-Za-z0-9])(?<!\d,)|(?<=[A-Za-z])\d+,)" r"(?:\d[\d,]*(?:\.\d+)?|\.\d+)" r"(?![A-Za-z0-9]|,\d|\.\d|\s*%)"
)
_CURRENCY_WORD_AMOUNT_RE = re.compile(
    rf"(?:\b(?:{_CURRENCY_WORD})\b[^\n]{{0,20}}{_MONEY_NUMBER})"
    rf"|(?:{_MONEY_NUMBER}[^\n]{{0,20}}\b(?:{_CURRENCY_WORD})\b)",
    re.IGNORECASE,
)

# Financial-value language: a financial noun on the same line as any
# monetary-shaped number (integer, one or more decimals, or leading decimal).
# Percentages are excluded so operational utilization remains valid. Generic
# "total" is not a financial noun: "total latency is 12.00 ms" must remain
# operational text.
#
# The noun-to-number window spans up to 200 non-newline characters on the same
# line in either order. Plain quantities (counts, times, sizes, Markdown list
# markers, Region codes) are neutralized before scanning, so an operational
# number near a financial-sounding word is not read as money.
#
# Across lines, only a *labelled* value is linked: a financial noun that ends a
# label ("Monthly cost:") followed within a bounded window by a number, so a
# model cannot split "cost:" and "999" across Markdown blocks. An unlabelled
# noun is never linked to a number on another line: operational output such as
# a GameLift fleet table with a "Billing" (On-Demand/Spot) column or a "rate
# limit" sentence must not withhold an unrelated instance count nearby.
_FINANCIAL_NOUN = (
    r"cost|costs|spend|spends|spending|spent|bill|billed|billing|"
    r"charge|charged|charges|price|priced|rate|rates|estimate|estimated|balance|amount|"
    r"invoice|expenses?|budgets?|savings?"
)
_FINANCIAL_WORD_RE = re.compile(rf"\b(?:{_FINANCIAL_NOUN}|{_CURRENCY_WORD})\b", re.IGNORECASE)
_FINANCIAL_TOPIC_RE = re.compile(
    rf"\b(?:{_FINANCIAL_NOUN}|{_CURRENCY_WORD}|discounts?|monetary)\b|[{_CURRENCY_SYMBOLS}]|"
    rf"\b(?:{_ISO_ALTERNATION})\b",
    re.IGNORECASE,
)
# A label line ends with the financial noun plus at most two allowlisted
# financial-label words and Markdown/colon decoration: "Monthly cost:",
# "**Billing total**", "Cost per month:". Restricting the suffix prevents
# ordinary operational prose such as "cost efficiency" from becoming a label
# merely because a numeric line follows it.
_FINANCIAL_LABEL_WORD = (
    r"total|amount|estimate|value|rate|price|cost|spend|spending|savings?|"
    r"charges?|bill|billing|per|hour|day|week|month|year|hourly|daily|weekly|"
    r"monthly|annual|yearly"
)
# An explicit colon is a strong label delimiter, so allow bounded arbitrary
# qualifiers before it ("Amount due:", "Invoice subtotal:", "Cost per
# instance:"). Without a colon, allow only known financial-label words so
# ordinary prose such as "cost efficiency" cannot bind to a later count.
_EXPLICIT_LABEL_LINE_END = r"[^\n:]{0,80}:[ \t*_]*\n"
_IMPLICIT_LABEL_LINE_END = rf"(?:\s+(?:{_FINANCIAL_LABEL_WORD})){{0,2}}[ \t*_]*\n"
_LABEL_LINE_END = rf"(?:{_EXPLICIT_LABEL_LINE_END}|{_IMPLICIT_LABEL_LINE_END})"
_VALUE_LANGUAGE_RE = re.compile(
    rf"(?:\b(?:{_FINANCIAL_NOUN})\b[^\n]{{0,200}}{_MONEY_NUMBER})"
    rf"|(?:{_MONEY_NUMBER}[^\n]{{0,200}}\b(?:{_FINANCIAL_NOUN})\b)"
    rf"|(?:\b(?:{_FINANCIAL_NOUN})\b{_LABEL_LINE_END}[\s\S]{{0,200}}{_MONEY_NUMBER})",
    re.IGNORECASE,
)
# Savings, discount, and price-comparison words. A percentage next to any of
# them is a financial claim ("saves up to 90%", "90% cheaper", "40% better
# price performance") and is withheld; the product does not repeat savings claims.
_PERCENT_NOUN = (
    r"cost|costs|spend|spending|sav(?:e|es|ed|ing|ings)|discounts?|share|bills?|billed|billing|drop"
    r"|cheaper|cheapest|expensive|price|prices|pricing"
)
# A percentage starts only at the beginning of a digit run, so a long run is
# scanned once instead of once per digit. It matches exactly the same text.
_PERCENT_VALUE = r"(?<!\d)\d+(?:\.\d+)?\s*(?:%|percent\b)"
# "N% off" is a discount unless it describes an operating period or a distance
# from a target ("20% off-hours", "15% off its target utilization").
_OFF_NOT_DISCOUNT = (
    r"(?![-\u2010-\u2015]?\s*(?:peak|hours?|season|cycle|target|baseline)\b"
    r"|\s+(?:its|their|the|our|your)\s+(?:\w+\s+)?(?:peak|highs?|lows?|target|baseline|max|maximum)\b)"
)
_FINANCIAL_PERCENT_RE = re.compile(
    rf"(?:\b(?:{_PERCENT_NOUN})\b[^\n]{{0,200}}{_PERCENT_VALUE})"
    rf"|(?:{_PERCENT_VALUE}[^\n]{{0,200}}\b(?:{_PERCENT_NOUN})\b)"
    rf"|(?:\b(?:{_PERCENT_NOUN})\b{_LABEL_LINE_END}[\s\S]{{0,200}}{_PERCENT_VALUE})"
    rf"|(?:{_PERCENT_VALUE}\s+off\b{_OFF_NOT_DISCOUNT})",
    re.IGNORECASE,
)
# Markdown underscore emphasis ("_cost_", "__Monthly cost__") would hide a
# financial word from the word-boundary checks, because "_" is a word
# character. Underscores that open or close a word are replaced with a space
# before scanning; interior underscores in identifiers ("cost_report") stay.
_EMPHASIS_UNDERSCORE_RE = re.compile(r"(?<![A-Za-z0-9_])_{1,3}(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])_{1,3}(?![A-Za-z0-9_])")

# Shell positional variables such as `$1` and `${10}` are code, not money.
# Neutralize them only inside code spans or lines that clearly invoke shell
# tooling; a prose amount such as "price is $5" must remain detectable.
_SHELL_POSITIONAL_RE = re.compile(r"(?<!')\$(?:[1-9](?![\d,.])|\{[1-9]\d*\})")
_CODE_FRAGMENT_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_SHELL_LINE_RE = re.compile(r"^.*\b(?:awk|bash|printf|sed|xargs)\b.*$", re.IGNORECASE | re.MULTILINE)
# Documentation links and versioned URLs can contain unrelated digits long after
# financial vocabulary in ordinary prose (for example, an "AWS Costs" capability
# bullet followed by a Python 3 tutorial URL). URLs are references, not claims.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# EC2 Spot allocation-strategy names are identifiers, not prices.
# "price-capacity-optimized" never appears as ordinary prose, so it is rewritten
# before every check. "lowest-price" is also an English adjective that can name
# a bare price ("the lowest-price pool at 0.0385 per hour"), so it is rewritten
# only for the percentage check, where the strategy name caused false positives.
_SPOT_STRATEGY_RE = re.compile(r"\bprice-capacity-optimized\b", re.IGNORECASE)
_LOWEST_PRICE_STRATEGY_RE = re.compile(r"\blowest-price\b", re.IGNORECASE)

# A number immediately followed by a count, time, or size unit is a quantity,
# not money ("2 instances", "3 fleets", "40-minute", "500 players", "10 nodes",
# "30 seconds", "4 GiB"). The list is explicit and reviewed. ``_neutralize_quantities``
# preserves a number when the text just before it ends with a currency symbol,
# ISO code, or currency word. Byte-size units deliberately omit a bare "b"/"B"
# so an isolated letter cannot swallow an amount.
#
# A count unit accepts only an integer ("500 players", "4,096 requests"): a
# decimal next to a count unit is a price, not a count ("0.0624 vCPU"). A size
# unit accepts a decimal but refuses a leading "0." so a sub-one size reads as a
# rate and stays visible ("0.023 GB"). A time unit accepts only a plain integer,
# because a decimal next to a time unit reads as a rate ("0.12 hr").
_QUANTITY_COUNT_UNIT = (
    r"instances?|fleets?|regions?|zones?|azs?|availability zones?|nodes?|pods?|replicas?|containers?|clusters?|"
    r"servers?|sessions?|players?|users?|requests?|connections?|tasks?|games?|matches?|"
    r"vcpus?|cpus?|cores?|gpus?"
)
_QUANTITY_SIZE_UNIT = r"kib|mib|gib|tib|kb|mb|gb|tb"
_QUANTITY_TIME_UNIT = (
    r"ms|milliseconds?|seconds?|secs?|minutes?|mins?|hours?|hrs?|hr|days?|weeks?|months?|years?|"
    r"minute|hour|day|week|month|year"
)
# A number followed by a time word, or by "to date"/"to-date", is a rate or a
# running total, not a quantity. The following look-ahead, shared by every
# branch, keeps such a number visible to the value-language detector so a bare
# amount spelled with spaces ("999 month to date") is not neutralized.
_QUANTITY_RATE_LOOKAHEAD = (
    rf"(?![-/\u2010-\u2015]\w|\s+(?:{_QUANTITY_TIME_UNIT})\b|\s+to[\s-]date\b"
    rf"|\s+(?:over|on|to)\s+(?:{_QUANTITY_TIME_UNIT})\b)"
)
# A size followed by a billing period is a storage or transfer rate, not a
# quantity, however the period is spelled: "2 GB per month", "2 GB a month",
# "1.5 TB an hour", "2 GB each month", "2 GB every hour", "2 GB monthly". The
# size branch refuses to neutralize such a number so it stays visible to the
# value-language detector. Each alternative is a fixed word sequence anchored
# at the end of the size match, so the guard adds no backtracking.
_SIZE_RATE_LOOKAHEAD = (
    rf"(?!\s+(?:per|a|an|each|every)\s+(?:{_QUANTITY_TIME_UNIT})\b"
    r"|\s+(?:hourly|daily|weekly|monthly|yearly|annually)\b)"
)
# The unit may follow the number directly, after whitespace, or after a single
# hyphen ("40-minute"). The ``_QUANTITY_RATE_LOOKAHEAD`` keeps a compound pricing
# unit ("GB-month") and a period-over-period or "to date" running total ("950
# month over month", "999 month to date") out of the quantity match, so the
# surviving number stays visible to the currency and value-language detectors.
# A spaced decimal rate such as "0.04048 vCPU per hour" stays visible because
# the count branch accepts an integer only, so the decimal never enters the
# quantity match. The size branch adds ``_SIZE_RATE_LOOKAHEAD`` so "0.04048 GB
# per hour" and "2 GB a month" are preserved. A trailing word boundary keeps
# "4 GiB" from matching a longer token. A currency symbol immediately before the number blocks the match
# here, and a preceding code or word is handled in ``_neutralize_quantities``.
#
# An integer count followed by "per <time>" ("2 instances per minute", "5 vCPU
# per hour") is treated as operational: the count branch neutralizes the integer
# and the "per <time>" phrase describes a scaling rate, not a monetary rate.
#
# Known ambiguous case: an integer directly followed by a bare time unit with no
# other currency marker ("The rate is 12 hr.") is treated as an operational
# quantity and is not withheld. The decimal spelling ("0.12 hr") is withheld as
# a rate; the integer spelling is kept clean so operational durations such as
# "a 12 hr maintenance window" are returned.
_QUANTITY_RE = re.compile(
    rf"(?<![{_CURRENCY_SYMBOLS}\d.,])\b(?:[1-9]\d{{0,2}}(?:,\d{{3}})+|\d+)[\s-]?(?:{_QUANTITY_COUNT_UNIT})\b"
    rf"{_QUANTITY_RATE_LOOKAHEAD}"
    rf"|(?<![{_CURRENCY_SYMBOLS}\d.,])\b(?!0[\d.,])(?:[1-9]\d{{0,2}}(?:,\d{{3}})+|\d+)(?:\.\d+)?"
    rf"[\s-]?(?:{_QUANTITY_SIZE_UNIT})\b"
    rf"{_QUANTITY_RATE_LOOKAHEAD}{_SIZE_RATE_LOOKAHEAD}"
    rf"|(?<![{_CURRENCY_SYMBOLS}\d.,])\b\d+[\s-]?(?:{_QUANTITY_TIME_UNIT})\b{_QUANTITY_RATE_LOOKAHEAD}",
    re.IGNORECASE,
)
# A Markdown ordered-list marker at the start of a line ("1.", "2)") is
# structure, not money. ``_neutralize_quantities`` tracks a stack of list
# counters so nested numbered sub-lists are followed: a "1" opens a new level, a
# number that repeats or increments the top counter continues that level, and a
# number that continues an ancestor pops back to it. Only a marker accepted as a
# list continuation is neutralized; a lone 1-3 digit value that ends a sentence
# ("999. That is the cost.") keeps its digits so it stays visible to the
# value-language detector. A numbered Markdown heading ("### 4. Control Costs")
# or bold numbered pseudo-heading ("**4. Reduce cost**", "**4.** Reduce cost",
# "__4.__ Reduce cost"; the closing delimiter must match the opening one)
# follows the same rule with its own counter stack, so heading numbers and the
# list items between headings do not interrupt each other's sequence, and one
# kind of sequence cannot prime the other into hiding a value.
_ORDERED_LIST_MARKER_RE = re.compile(
    r"^[ \t]*(?:(?P<heading>#{1,6}[ \t]+)|(?P<bold>\*\*|__))?(?P<number>\d{1,3})[.)](?(bold)(?P=bold)?)[ \t]+",
    re.MULTILINE,
)
# A currency word or ISO code directly after a marker (only decoration between)
# means the marker number may be
# an amount ("1. dollars per hour", "2. USD per month"); its digits are kept
# visible. A currency symbol is not used here: after a marker it is usually a
# shell variable or prompt ("1. Set `$FLEET_ID`"), and a symbol next to a digit
# is already caught by the symbol-amount check.
_MARKER_CURRENCY_TRAIL_RE = re.compile(
    rf"[ \t*_`(]{{0,4}}(?:(?:US[ \t]+)?\b(?i:{_CURRENCY_WORD})\b|\b(?:{_ISO_ALTERNATION})\b)"
)
# An AWS Region code ("us-west-2", "eu-central-1", "us-gov-east-1") is an
# identifier, not money. Its trailing digit must not be read as an amount. The
# pattern is anchored to real Region prefixes and directions so an arbitrary
# "xx-word-digit" token (for example "up-to-5", "ab-cd-9") is not treated as a
# Region and does not hide a nearby amount.
_REGION_PREFIX = r"us|eu|ap|sa|ca|me|af|il|mx|cn"
_REGION_DIRECTION = r"east|west|north|south|central|northeast|northwest|southeast|southwest"
_REGION_CODE_RE = re.compile(
    rf"\b(?:eusc-de|(?:{_REGION_PREFIX})(?:-gov|-iso|-isob)?)-(?:{_REGION_DIRECTION})-\d\b",
    re.IGNORECASE,
)

# A currency marker (symbol, ISO code, or currency word) near a number means the
# number is monetary and must survive neutralization. ``_CURRENCY_LEAD_RE``
# matches a marker directly before the number, tested against the forty
# characters that precede the quantity match and against the last eight
# non-space characters of a wider four-hundred-character window, so a symbol or
# ISO code separated from the number by a long run of whitespace (a padded
# plain-text table) still preserves the amount. ``_CURRENCY_TRAIL_RE`` matches a
# currency word within twenty characters after the match, so phrasings such as
# "0.023 GB in dollars" are preserved too.
_CURRENCY_LEAD_RE = re.compile(
    rf"(?:[{_CURRENCY_SYMBOLS}]\s*$)"
    rf"|(?:\b(?:{_ISO_ALTERNATION})[\s:]*$)"
    rf"|(?:\b(?:{_CURRENCY_WORD})\b[^\n]{{0,20}}$)",
    re.IGNORECASE,
)
_CURRENCY_TRAIL_RE = re.compile(rf"^[^\n]{{0,20}}\b(?:{_CURRENCY_WORD})\b", re.IGNORECASE)


def _neutralize_quantities(text: str) -> str:
    """Blank plain quantities (counts, times, sizes) and non-monetary numeric structure.

    Each quantity's number is replaced with the token ``N`` so no stray ``.`` or
    ``,`` is left behind to read as a sentence boundary. A number is preserved
    when a currency symbol, ISO currency code, or currency word sits within forty
    characters before it (or when a symbol or ISO code precedes it after a longer
    run of whitespace), or a currency word sits within twenty characters after
    it, so a monetary amount such as "0.023 GB in dollars" stays visible.
    Markdown list markers and numbered headings that continue a (possibly
    nested) sequence, and AWS Region codes, are also neutralized so their digits
    cannot bind to a nearby financial word. A lone 1-3 digit value that merely
    ends a sentence keeps its digits.
    """

    def neutralize(match: re.Match[str]) -> str:
        fragment = match.group(0)
        preceding = _EMPHASIS_UNDERSCORE_RE.sub(" ", text[max(0, match.start() - 40) : match.start()])
        wide_preceding = _EMPHASIS_UNDERSCORE_RE.sub(" ", text[max(0, match.start() - 400) : match.start()])
        wide_preceding = wide_preceding.rstrip()[-8:]
        following = _EMPHASIS_UNDERSCORE_RE.sub(" ", text[match.end() : match.end() + 40])
        if (
            _CURRENCY_LEAD_RE.search(preceding)
            or _CURRENCY_LEAD_RE.search(wide_preceding)
            or _CURRENCY_TRAIL_RE.search(following)
        ):
            return fragment
        return re.sub(r"\d[\d,]*(?:\.\d+)?", "N", fragment, count=1)

    def neutralize_list_markers(scannable: str) -> str:
        # Plain list markers and numbered headings keep separate counter stacks.
        stacks: dict[bool, list[int]] = {False: [], True: []}

        def replace(match: re.Match[str]) -> str:
            counters = stacks[match.group("heading") is not None or match.group("bold") is not None]
            number = int(match.group("number"))
            if number == 1:
                counters.append(1)
            elif counters and number in (counters[-1], counters[-1] + 1):
                counters[-1] = number
            else:
                for level in range(len(counters) - 2, -1, -1):
                    if number in (counters[level], counters[level] + 1):
                        del counters[level + 1 :]
                        counters[-1] = number
                        break
                else:
                    return match.group(0)
            trail = _EMPHASIS_UNDERSCORE_RE.sub(" ", scannable[match.end() : match.end() + 40])
            if _MARKER_CURRENCY_TRAIL_RE.match(trail):
                return match.group(0)
            start = match.start("number") - match.start()
            end = match.end("number") - match.start()
            marker = match.group(0)
            return marker[:start] + "N" + marker[end:]

        return _ORDERED_LIST_MARKER_RE.sub(replace, scannable)

    text = neutralize_list_markers(text)
    text = _REGION_CODE_RE.sub(lambda m: re.sub(r"\d", "N", m.group(0)), text)
    return _QUANTITY_RE.sub(neutralize, text)


def _neutralize_shell_positionals(text: str) -> str:
    """Remove shell positional variables only from code-shaped contexts."""

    def neutralize(match: re.Match[str]) -> str:
        fragment = match.group(0)
        if _FINANCIAL_WORD_RE.search(_EMPHASIS_UNDERSCORE_RE.sub(" ", fragment)):
            return fragment
        return _SHELL_POSITIONAL_RE.sub("", fragment)

    def neutralize_code(match: re.Match[str]) -> str:
        fragment = match.group(0)
        return neutralize(match) if _SHELL_LINE_RE.search(fragment) else fragment

    text = _CODE_FRAGMENT_RE.sub(neutralize_code, text)
    return _SHELL_LINE_RE.sub(neutralize, text)


def contains_unvalidated_financial_content(text: str) -> bool:
    """Return whether ``text`` appears to contain a monetary/financial value.

    Conservative and fail-closed for prose: currency symbols/codes next to a
    value count as financial content. A financial noun links to a number on the
    same line, and a plain quantity (``2 instances``, ``40-minute``, ``4 GiB``)
    is neutralized before scanning so counts, times, and sizes near
    financial-sounding words are not read as money. Financial percentages are
    blocked when paired with cost/savings language on the same
    line, while operational percentages remain valid. Shell positional
    variables are neutralized before scanning, while financial values remain
    detectable even when wrapped in code formatting.
    """
    if not text:
        return False
    scannable = _neutralize_shell_positionals(_SPOT_STRATEGY_RE.sub("allocation-strategy", text))
    scannable = _URL_RE.sub("", scannable)
    scannable = _neutralize_quantities(scannable)
    scannable = _EMPHASIS_UNDERSCORE_RE.sub(" ", scannable)
    return bool(
        _SYMBOL_AMOUNT_RE.search(scannable)
        or _CODE_AMOUNT_RE.search(scannable)
        or _CURRENCY_WORD_AMOUNT_RE.search(scannable)
        or _VALUE_LANGUAGE_RE.search(scannable)
        or _FINANCIAL_PERCENT_RE.search(_LOWEST_PRICE_STRATEGY_RE.sub("allocation-strategy", scannable))
    )


def _withheld_notice(service_name: str) -> str:
    """Deterministic replacement for a section that contained financial content."""
    display = service_name.strip() or "Specialist"
    return (
        f"## {display}\n\n"
        f"The {display} specialist response was withheld because it contained financial "
        "figures that did not come from the validated cost report. Only the cost report "
        "may provide monetary values.\n\n"
        "No unverified financial value was shown."
    )


def sanitize_specialist_section(service_name: str, output: str) -> str:
    """Return ``output`` unchanged if it is financially clean, else a safe notice.

    A non-cost specialist section that contains any unvalidated financial content
    is replaced wholesale with a fixed, number-free notice. Sections without
    currency markers are returned as-is so legitimate operational output is
    preserved.
    """
    if contains_unvalidated_financial_content(output):
        return _withheld_notice(service_name)
    return output


def sanitize_advisory_section(service_name: str, output: str) -> str:
    """Fail closed when advisory prose contains unvalidated financial claims.

    Cross-paragraph reconstruction makes partial retention unsafe: a model can
    separate a financial label and value across arbitrary Markdown blocks. If
    any claim is detected, replace the complete section with deterministic,
    number-free guidance. Financially clean advisory text passes unchanged.
    """
    if not output or not output.strip():
        return output
    if not contains_unvalidated_financial_content(output):
        return output

    display = service_name.strip() or "Cost"
    return (
        f"## {display}\n\n"
        "Unvalidated financial figures were withheld. Review utilization, "
        "right-sizing, idle resources, and commitment options, then use the "
        "validated cost report path for account totals, shares, and historical spending."
    )


def withheld_financial_reply_notice() -> str:
    """Deterministic, number-free notice for a withheld non-cost reply.

    Used when the orchestrator's own direct reply to a non-cost query contains
    an unvalidated monetary value. The notice carries no Markdown heading and
    no cost guidance, so a non-cost question is not answered as if it were a
    cost question; it only states why the reply was withheld and how to obtain
    validated figures.
    """
    return (
        "This reply was withheld because it included financial figures that did not "
        "come from the validated cost report. Ask a cost question to get validated figures."
    )


def contains_financial_topic(text: str) -> bool:
    """Return whether text discusses money, billing, pricing, or cost."""
    return bool(text and _FINANCIAL_TOPIC_RE.search(text))


def sanitize_strict_specialist_section(service_name: str, output: str) -> str:
    """Withhold non-cost specialist text that enters the financial domain."""
    if contains_financial_topic(output) or contains_unvalidated_financial_content(output):
        return _withheld_notice(service_name)
    return output
