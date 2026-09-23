"""Accurate, testable E5 bounded-autonomy cost model (Issue #440, Track C).

Two postures are modeled deterministically, in whole US cents to avoid any
floating-point money:

* **default-zero** — the canonical ``deploy-all.sh`` deployment. E5 autonomy is
  default-disabled and creates **no** autonomy resources, so its incremental
  monthly cost is **exactly $0.00**. This is the load-bearing fact of Track C:
  the default deployment adds no autonomy cost.
* **enabled** — an operator has stood up the *optional*, reviewed E5 stack. This
  posture adds the incremental monthly cost of the autonomy control plane on top
  of the base deployment (the base ~$752/month from the guidance README is
  unchanged and not re-counted here).

The enabled figures are the human-facing companion of the machine-checked single
source of truth, ``docs/operations-e5-cost-model.json`` (whose ``enabled_idle``
scenario totals **$0.44/month**). They reconcile to that model exactly:
``test_e5_cost_model_unit.py`` asserts this posture equals the canonical
enabled-idle cents. E5 provisions **no** KMS key, **no** DynamoDB table, and no
audit trail of its own — it REUSES the 06 operations table and its CMK (for the
DynamoDB data plane only) and the 06/08 CloudTrail audit path — so those services
are billed under 06/07/08 and are NOT incremental E5 cost lines here. Counting
them was the reviewed $0.85 double-count defect.

The model is pure and imports nothing from ``operations``: it exists so the
runbook's numbers are reproducible and unit-tested rather than asserted prose.
Nothing here calls AWS.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostLine:
    """One monthly cost line item, in whole US cents."""

    service: str
    dimension: str
    monthly_cents: int

    def __post_init__(self) -> None:
        if self.monthly_cents < 0:
            raise ValueError("monthly_cents must be non-negative")


# --------------------------------------------------------------------------- #
# default-zero posture
# --------------------------------------------------------------------------- #

# The default deployment creates no autonomy resources. Its incremental autonomy
# cost is exactly zero — represented explicitly as a single zero line so the
# fact is asserted, not implied.
DEFAULT_ZERO_LINES: tuple[CostLine, ...] = (
    CostLine(
        service="(none)",
        dimension="E5 autonomy is default-disabled; deploy-all.sh creates no autonomy resources",
        monthly_cents=0,
    ),
)


# --------------------------------------------------------------------------- #
# enabled posture (optional, reviewed stack) — incremental over the base
# --------------------------------------------------------------------------- #

# Conservative us-west-2 list-price estimates for a low-volume enabled-idle
# autonomy control plane, reconciled EXACTLY to the canonical
# docs/operations-e5-cost-model.json enabled_idle scenario ($0.44/month). Only
# the services E5 actually adds appear here: the four E5-owned CloudWatch alarms,
# the scheduled evaluator Lambda, and the two AppConfig configuration
# retrievals. DynamoDB (reuses the 06 table + CMK) and the CloudTrail/KMS audit
# path (reused from 06/08) are billed under those stacks and are deliberately
# absent — counting them was the reviewed $0.85 double-count.
ENABLED_LINES: tuple[CostLine, ...] = (
    # CloudWatch: the FOUR E5-owned alarms at $0.10 each, watching AWS-emitted
    # metrics only (evaluator Errors + Throttles, schedule FailedInvocations,
    # dead-letter depth). This is the dominant incremental cost.
    CostLine("Amazon CloudWatch", "four E5-owned alarms on AWS-emitted metrics ($0.10 each)", 40),
    # Lambda: the evaluator scheduled every 5 minutes (~8,640 invocations/month)
    # at ~0.25 GB-second each, near the free tier — a few cents.
    CostLine("AWS Lambda", "scheduled evaluator invocations (~8,640/mo, near free tier)", 3),
    # AppConfig: the evaluator reads two documents (E4 kill switch + SEPARATE E5
    # autonomy switch) through the extension cache; retrievals track the cache
    # refresh cadence, not per-request — a fraction of a cent, rounded to 1.
    CostLine("AWS AppConfig", "two autonomy-switch document retrievals via the extension cache", 1),
)


def total_monthly_cents(lines: tuple[CostLine, ...]) -> int:
    """Return the summed monthly cost of a posture's line items, in whole cents."""
    return sum(line.monthly_cents for line in lines)


def default_zero_total_cents() -> int:
    """The incremental monthly autonomy cost of the default deployment: zero."""
    return total_monthly_cents(DEFAULT_ZERO_LINES)


def enabled_total_cents() -> int:
    """The incremental monthly autonomy cost when the optional stack is enabled."""
    return total_monthly_cents(ENABLED_LINES)


def format_cents(cents: int) -> str:
    """Render whole cents as a ``$X.YY`` string."""
    return f"${cents // 100}.{cents % 100:02d}"
