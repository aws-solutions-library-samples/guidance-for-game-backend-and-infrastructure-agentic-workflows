"""Unit tests for how long the financial guard takes on long runs of digits and separators.

The guard scans every non-cost reply. A pattern that restarts at each digit of
a long run of digits, commas, or periods takes time that grows with the square
of the run's length. These tests pin that such runs are scanned in linear time,
and that the amounts the affected patterns detect are still detected.
"""

# Standard library
import time

# Third-party packages
import pytest

# Local modules
from agents.financial_guard import contains_unvalidated_financial_content

pytestmark = pytest.mark.unit

# About the longest reply the orchestrator can produce at its 4,096-token
# output limit. Before the linear-scan patterns, these inputs took several
# seconds each; now each takes a few milliseconds. The budget leaves room for
# slow CI runners.
_REPLY_CHARS = 16_000
_BUDGET_SECONDS = 1.0


@pytest.mark.parametrize("unit", ["1", "1.", "1,", "12,", "1,000,", "10.0.0.1,", "1 ", "x1,", "a1,2"])
def test_long_number_runs_are_scanned_in_linear_time(unit):
    text = unit * (_REPLY_CHARS // len(unit))

    start = time.perf_counter()
    contains_unvalidated_financial_content(text)
    elapsed = time.perf_counter() - start

    assert elapsed < _BUDGET_SECONDS, f"{unit!r} x {len(text)} chars took {elapsed:.2f}s"


@pytest.mark.parametrize(
    "text",
    [
        "Your fleet costs 999 USD per month.",
        "That is .5 USD per hour.",
        "Price: x.5USD",
        "Cost ..5 USD",
        "Total 1,000.00 EUR",
        "The cost is Rs1,200.",
        "Costs rose 1,200,000 this quarter.",
        "Spend share is 12.5% this month.",
        "You saved 3.5% on compute.",
        "1,000,000 dollars",
    ],
)
def test_amounts_near_the_rewritten_patterns_stay_withheld(text):
    assert contains_unvalidated_financial_content(text)


@pytest.mark.parametrize(
    "text",
    [
        "Fleet IPs 10.0.0.1, 10.0.0.2 serve 3 regions.",
        "Scale to 1,200 players.",
        "99.9% availability",
    ],
)
def test_operational_numbers_near_the_rewritten_patterns_stay_visible(text):
    assert not contains_unvalidated_financial_content(text)
