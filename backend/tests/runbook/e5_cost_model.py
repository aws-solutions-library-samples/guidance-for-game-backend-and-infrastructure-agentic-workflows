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
  unchanged and not re-counted here). The figures are line-item estimates at
  us-west-2 public list prices; they are intentionally conservative and rounded
  to whole cents.

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

# Conservative us-west-2 list-price estimates for a low-volume demo autonomy
# control plane (a few evaluations/day against one enrolled demo fleet). These
# are *incremental* to the base deployment and rounded to whole cents.
ENABLED_LINES: tuple[CostLine, ...] = (
    # Step Functions Standard: one execution per autonomous operation, a handful
    # of state transitions each; a few operations per day. $0.025 / 1K
    # transitions. ~ (a few hundred transitions/mo) -> rounds to ~1 cent.
    CostLine("AWS Step Functions", "Standard workflow state transitions (low volume)", 1),
    # Lambda: evaluator + reservation + verifier invocations, all short and
    # within/near the free tier at demo volume.
    CostLine("AWS Lambda", "evaluator/reservation/verifier invocations (near free tier)", 2),
    # DynamoDB on-demand: reservation store + audit ledger writes/reads at demo
    # volume, plus a small amount of stored data.
    CostLine("Amazon DynamoDB", "reservation store + audit ledger (on-demand, low volume)", 25),
    # AppConfig: the separate autonomy switch document — configuration retrievals
    # via the AppConfig extension polling loop.
    CostLine("AWS AppConfig", "separate autonomy switch document retrievals", 12),
    # CloudWatch: autonomy alarms (a small number of metric alarms) + a little
    # log ingestion for the autonomy runtime.
    CostLine("Amazon CloudWatch", "autonomy alarms + runtime log ingestion", 40),
    # CloudTrail data events / KMS usage for the audited executor write path are
    # negligible incremental at demo volume.
    CostLine("AWS CloudTrail + KMS", "audited write-path events (negligible incremental)", 5),
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
