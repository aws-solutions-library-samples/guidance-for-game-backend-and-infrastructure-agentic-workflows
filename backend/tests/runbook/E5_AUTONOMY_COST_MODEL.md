# E5 Bounded-Autonomy — Cost Model (Issue #440, Track C)

The figures below are produced and unit-tested by
[`e5_cost_model.py`](./e5_cost_model.py) (see
[`test_e5_cost_model_unit.py`](./test_e5_cost_model_unit.py)) so the numbers are
reproducible, not asserted prose. All money is computed in whole US cents to
avoid floating-point error. This runbook is the human-facing companion of the
machine-checked single source of truth,
[`docs/operations-e5-cost-model.json`](../../../docs/operations-e5-cost-model.json);
the test reconciles this posture's total to that model's `enabled_idle`
scenario **exactly**, so the two can never diverge again.

## Default-zero posture (the canonical deployment)

**Incremental monthly autonomy cost: `$0.00`.**

E5 autonomy is **default-disabled and default-zero**. The canonical
`deploy-all.sh` deployment creates **no** autonomy resources: no evaluator
Lambda, no EventBridge schedule rule, no autonomy AppConfig document, and no
autonomy alarms. Because nothing is created, there is nothing to bill. This is
the load-bearing fact of Track C and is locked by
`test_default_zero_posture_costs_exactly_nothing`.

The base guidance deployment (~$752/month per the README, dominated by model
token usage) is **unchanged** — E5's default posture neither adds to nor
re-counts it.

## Enabled posture (optional, reviewed stack only)

When an operator stands up the **optional, reviewed** E5 stack, the autonomy
control plane adds a small, bounded **incremental** monthly cost on top of the
base deployment. These are conservative us-west-2 public list-price estimates
for the enabled-idle steady state (the evaluator fires every 5 minutes; each
authorized action adds only one un-metered `StartExecution`), rounded to whole
cents and reconciled to the canonical model:

| Service | Dimension | Monthly (est.) |
|---|---|---|
| Amazon CloudWatch | four E5-owned alarms on AWS-emitted metrics ($0.10 each) | $0.40 |
| AWS Lambda | scheduled evaluator invocations (~8,640/mo, near free tier) | $0.03 |
| AWS AppConfig | two autonomy-switch document retrievals via the extension cache | $0.01 |
| **Total incremental** | | **$0.44** |

Notes:

- **E5 provisions no KMS key, no DynamoDB table, and no audit trail of its own.**
  It REUSES the 06 operations table and its customer-managed KMS key (for the
  DynamoDB data plane only) and the 06/08 CloudTrail audit path. Those services
  are billed under the 06/07/08 stacks and are **not** incremental E5 cost —
  counting them as separate E5 lines was the earlier `$0.85` double-count this
  model now corrects.
- **E5 creates no executor and no new workflow.** The single provider write
  (`UpdateFleetCapacity`, `0 -> 1 -> 0` on the demo fleet) is performed by the
  unchanged E3 executor role in the 07 stack and starts the exact existing E3
  workflow; the `StartExecution` call is not separately metered.
- The estimate is dominated by the fixed four-alarm cost; the schedule cadence
  (not the autonomous-action volume) sets the Lambda/AppConfig figures, so a
  busy month equals enabled-idle. It stays well under $10/month incremental,
  which `test_enabled_posture_is_a_small_positive_incremental` guards.
- Enabling E5 does **not** change the E4 kill-switch or any base stack, so no
  base-stack cost line moves.

## Why the split matters

The two postures make the safety posture legible as a cost fact: a reader can
confirm at a glance that turning E5 **on** is a deliberate, separately-deployed,
small-but-nonzero decision, while the **default** deployment carries no autonomy
cost or resources at all.
