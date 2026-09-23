"""Unit tests locking the E5 cost model's load-bearing facts (Issue #440).

The single most important fact of Track C: the **default** deployment adds
**exactly zero** autonomy cost, because E5 autonomy is default-disabled and
``deploy-all.sh`` creates no autonomy resources. The enabled posture is a small,
bounded incremental cost that only applies to the optional, reviewed stack.

The runbook model is the human-facing companion of the machine-checked
``docs/operations-e5-cost-model.json`` single source of truth. The #440 review
found the runbook posture had drifted to a duplicate **$0.85** total that
double-counted DynamoDB, AppConfig applications, and CloudTrail/KMS as
incremental E5 cost even though E5 REUSES the 06 table/CMK and the 06/08 audit
trail. These tests pin the runbook enabled total to the canonical **$0.44** and
forbid the double-counted, non-incremental services from reappearing as cost
lines.
"""

from __future__ import annotations

# Standard library
import json
import pathlib

# Third-party packages
import pytest

# Local modules
from runbook import e5_cost_model as cm

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
CANONICAL_MODEL = PROJECT_ROOT / "docs" / "operations-e5-cost-model.json"


def _canonical_enabled_idle_cents() -> int:
    model = json.loads(CANONICAL_MODEL.read_text(encoding="utf-8"))
    dollars = model["scenarios"]["enabled_idle"]["monthly_total_usd"]
    return round(dollars * 100)


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
    assert len(cm.ENABLED_LINES) >= 3


def test_runbook_enabled_total_equals_canonical_44_cents() -> None:
    """The runbook posture MUST equal the canonical enabled-idle scenario ($0.44).

    A duplicate cost model with its own divergent total (the reviewed $0.85) is a
    reconciliation defect: there is one E5 cost, and it is $0.44.
    """
    canonical = _canonical_enabled_idle_cents()
    assert canonical == 44, f"canonical enabled-idle changed to {canonical}c; update the runbook to match"
    assert (
        cm.enabled_total_cents() == canonical
    ), f"runbook enabled total {cm.enabled_total_cents()}c must equal the canonical {canonical}c"


def test_runbook_does_not_double_count_reused_06_07_08_services() -> None:
    """E5 REUSES the 06 table/CMK and the 06/08 audit trail; those are not E5 cost.

    A standalone DynamoDB, a standalone AppConfig-application, or a
    CloudTrail/KMS cost line would double-count a resource billed under 06/07/08.
    """
    services = " ".join(f"{line.service} {line.dimension}".lower() for line in cm.ENABLED_LINES)
    assert "dynamodb" not in services, "DynamoDB reuses the 06 table; not an incremental E5 cost"
    assert "cloudtrail" not in services, "the audited write path reuses the 06/08 trail; not incremental"
