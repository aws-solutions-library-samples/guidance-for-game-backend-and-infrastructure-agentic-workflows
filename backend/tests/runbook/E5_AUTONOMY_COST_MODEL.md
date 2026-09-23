# E5 Bounded-Autonomy — Cost Model (Issue #440, Track C)

The figures below are produced and unit-tested by
[`e5_cost_model.py`](./e5_cost_model.py) (see
[`test_e5_cost_model_unit.py`](./test_e5_cost_model_unit.py)) so the numbers are
reproducible, not asserted prose. All money is computed in whole US cents to
avoid floating-point error.

## Default-zero posture (the canonical deployment)

**Incremental monthly autonomy cost: `$0.00`.**

E5 autonomy is **default-disabled and default-zero**. The canonical
`deploy-all.sh` deployment creates **no** autonomy resources: no Step Functions
state machine, no evaluator/reservation/verifier Lambdas, no reservation or
audit tables, no autonomy AppConfig document, and no autonomy alarms. Because
nothing is created, there is nothing to bill. This is the load-bearing fact of
Track C and is locked by `test_default_zero_posture_costs_exactly_nothing`.

The base guidance deployment (~$752/month per the README, dominated by model
token usage) is **unchanged** — E5's default posture neither adds to nor
re-counts it.

## Enabled posture (optional, reviewed stack only)

When an operator stands up the **optional, reviewed** E5 stack, the autonomy
control plane adds a small, bounded **incremental** monthly cost on top of the
base deployment. These are conservative us-west-2 public list-price estimates
for a low-volume demo (a few autonomous evaluations per day against one enrolled
demo fleet), rounded to whole cents:

| Service | Dimension | Monthly (est.) |
|---|---|---|
| AWS Step Functions | Standard workflow state transitions (low volume) | $0.01 |
| AWS Lambda | evaluator / reservation / verifier invocations (near free tier) | $0.02 |
| Amazon DynamoDB | reservation store + audit ledger (on-demand, low volume) | $0.25 |
| AWS AppConfig | separate autonomy switch document retrievals | $0.12 |
| Amazon CloudWatch | autonomy alarms + runtime log ingestion | $0.40 |
| AWS CloudTrail + KMS | audited write-path events (negligible incremental) | $0.05 |
| **Total incremental** | | **$0.85** |

Notes:

- The single provider write (`UpdateFleetCapacity`, `0 -> 1 -> 0` on the demo
  fleet) has no per-call charge; its cost surfaces only as negligible CloudTrail
  / KMS activity, already captured above.
- The estimate scales with autonomous-operation volume (Step Functions
  transitions, Lambda invocations, DynamoDB and CloudWatch usage). At the demo
  volume this runbook targets it stays well under $10/month incremental, which
  `test_enabled_posture_is_a_small_positive_incremental` guards.
- Enabling E5 does **not** change the E4 kill-switch or any base stack, so no
  base-stack cost line moves.

## Why the split matters

The two postures make the safety posture legible as a cost fact: a reader can
confirm at a glance that turning E5 **on** is a deliberate, separately-deployed,
small-but-nonzero decision, while the **default** deployment carries no autonomy
cost or resources at all.
