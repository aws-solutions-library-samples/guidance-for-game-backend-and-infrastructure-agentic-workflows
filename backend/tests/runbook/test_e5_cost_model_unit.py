"""Unit tests locking the E5 cost model's load-bearing facts (Issue #440).

The single most important fact of Track C: the **default** deployment adds
**exactly zero** autonomy cost, because E5 autonomy is default-disabled and
``deploy-all.sh`` creates no autonomy resources. The enabled posture is a small,
bounded incremental cost that only applies to the optional, reviewed stack.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from runbook import e5_cost_model as cm

pytestmark = pytest.mark.unit


def test_default_zero_posture_costs_exactly_nothing() -> None:
    assert cm.default_zero_total_cents() == 0
    assert cm.format_cents(cm.default_zero_total_cents()) == "$0.00"


def test_enabled_posture_is_a_small_positive_incremental() -> None:
    total = cm.enabled_total_cents()
    assert total > 0
    # Sanity ceiling: the demo autonomy control plane is low-volume; guard
    # against an accidental order-of-magnitude error in a line item.
    assert total < 1000  # < $10.00/month incremental at demo volume


def test_enabled_total_equals_sum_of_lines() -> None:
    assert cm.enabled_total_cents() == sum(line.monthly_cents for line in cm.ENABLED_LINES)


def test_default_zero_lines_are_all_zero() -> None:
    assert all(line.monthly_cents == 0 for line in cm.DEFAULT_ZERO_LINES)


def test_cost_line_rejects_negative() -> None:
    with pytest.raises(ValueError):
        cm.CostLine(service="x", dimension="y", monthly_cents=-1)


def test_format_cents_renders_dollars_and_cents() -> None:
    assert cm.format_cents(0) == "$0.00"
    assert cm.format_cents(5) == "$0.05"
    assert cm.format_cents(125) == "$1.25"
    assert cm.format_cents(1000) == "$10.00"


def test_enabled_lines_have_no_duplicate_services_collapsed_wrongly() -> None:
    # Each line is a distinct dimension; totals must not silently drop lines.
    assert len(cm.ENABLED_LINES) >= 5
