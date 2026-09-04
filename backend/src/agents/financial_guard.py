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
_CODE_AMOUNT_RE = re.compile(
    rf"\b(?:{_ISO_ALTERNATION})[\s:]*\$?\d"  # code then number ("USD 5", "USD999")
    rf"|\d[\d,.]*\s*(?:{_ISO_ALTERNATION})\b",  # number then code ("999 USD", "999USD")
    re.IGNORECASE,
)

# Explicit currency words require an adjacent value; a bare discussion of
# "dollars" or "yen" is not itself a financial figure.
_CURRENCY_WORD = r"dollars?|cents?|euros?|yen"
_MONEY_NUMBER = r"(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?!(?:\d|,\d|\.\d|\s*%))"
_CURRENCY_WORD_AMOUNT_RE = re.compile(
    rf"(?:\b(?:{_CURRENCY_WORD})\b[^\n]{{0,20}}{_MONEY_NUMBER})"
    rf"|(?:{_MONEY_NUMBER}[^\n]{{0,20}}\b(?:{_CURRENCY_WORD})\b)",
    re.IGNORECASE,
)

# Financial-value language: a financial noun on the same line as any monetary-
# shaped number (integer, one or more decimals, or leading decimal). Percentages
# are excluded so operational utilization remains valid. Generic "total" is not
# a financial noun: "total latency is 12.00 ms" must remain operational text.
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
_VALUE_LANGUAGE_RE = re.compile(
    rf"(?:\b(?:{_FINANCIAL_NOUN})\b[\s\S]{{0,200}}{_MONEY_NUMBER})"
    rf"|(?:{_MONEY_NUMBER}[\s\S]{{0,200}}\b(?:{_FINANCIAL_NOUN})\b)",
    re.IGNORECASE,
)
_FINANCIAL_PERCENT_RE = re.compile(
    rf"(?:\b(?:cost|costs|spend|spending|save|saved|savings?|discount|share|bill|billed|billing|drop)\b"
    rf"[\s\S]{{0,200}}\d+(?:\.\d+)?\s*%)"
    rf"|(?:\d+(?:\.\d+)?\s*%[\s\S]{{0,200}}"
    rf"\b(?:cost|costs|spend|spending|save|saved|savings?|discount|share|bill|billed|billing|drop)\b)",
    re.IGNORECASE,
)

# Shell positional variables such as `$1` and `${10}` are code, not money.
# Neutralize them only inside code spans or lines that clearly invoke shell
# tooling; a prose amount such as "price is $5" must remain detectable.
_SHELL_POSITIONAL_RE = re.compile(r"(?<!')\$(?:[1-9](?![\d,.])|\{[1-9]\d*\})")
_CODE_FRAGMENT_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_SHELL_LINE_RE = re.compile(r"^.*\b(?:awk|bash|printf|sed|xargs)\b.*$", re.IGNORECASE | re.MULTILINE)


def _neutralize_shell_positionals(text: str) -> str:
    """Remove shell positional variables only from code-shaped contexts."""

    def neutralize(match: re.Match[str]) -> str:
        fragment = match.group(0)
        if _FINANCIAL_WORD_RE.search(fragment):
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
    value count as financial content. Financial percentages are blocked when
    paired with cost/savings language, while operational percentages remain
    valid. Shell positional variables are neutralized before scanning, while
    financial values remain detectable even when wrapped in code formatting.
    """
    if not text:
        return False
    scannable = _neutralize_shell_positionals(text)
    return bool(
        _SYMBOL_AMOUNT_RE.search(scannable)
        or _CODE_AMOUNT_RE.search(scannable)
        or _CURRENCY_WORD_AMOUNT_RE.search(scannable)
        or _VALUE_LANGUAGE_RE.search(scannable)
        or _FINANCIAL_PERCENT_RE.search(scannable)
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


def contains_financial_topic(text: str) -> bool:
    """Return whether text discusses money, billing, pricing, or cost."""
    return bool(text and _FINANCIAL_TOPIC_RE.search(text))


def sanitize_strict_specialist_section(service_name: str, output: str) -> str:
    """Withhold non-cost specialist text that enters the financial domain."""
    if contains_financial_topic(output) or contains_unvalidated_financial_content(output):
        return _withheld_notice(service_name)
    return output
