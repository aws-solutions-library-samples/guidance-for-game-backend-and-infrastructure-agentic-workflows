# Optional E3 execution control plane — incremental cost notes (#415)

This note summarizes the **incremental** monthly cost of the optional,
default-unprovisioned E3 operations *execution* control plane
(`07-operations-execution.yaml`). The numbers here are recomputed and asserted
against [`operations-e3-cost-model.json`](operations-e3-cost-model.json) by
`backend/tests/unit/test_operations_e3_cost_model_unit.py`, so the documentation
cannot drift from the pricing inputs.

E3 **reuses** the 06 DynamoDB table and CMK, so it adds **no** incremental
storage or KMS-key charge — those are already accounted for in the E1 cost model
([`operations-cost-model.json`](operations-cost-model.json)). E3's own new
always-on footprint is just its CloudWatch metrics/alarms; everything else scales
to zero.

| Scenario | Fixed USD/mo | Variable USD/mo | Total USD/mo |
|----------|--------------|-----------------|--------------|
| Default (unprovisioned) | 0.00 | 0.00 | **$0.00** |
| Enabled, idle (0 dispatches) | 1.20 | 0.00 | **$1.20** |
| Representative (1,000 remediations/mo) | 1.20 | 0.06 | **$1.26** |

Notes:

- **Default is $0.** A default deploy of the 07 stack provisions zero resources.
- **Step Functions STANDARD** is billed per state transition (US West (Oregon)
  `$0.000025`/transition; the free 4,000/month is conservatively excluded). Each
  remediation runs a small, fixed number of transitions (Execute → terminal), so
  even at 1,000 remediations/month the Step Functions line is a few cents. The
  state machine performs **no blind retry**, so a failing execution cannot loop
  and inflate transitions.
- The **executor and dispatcher Lambdas** and the **dispatch HTTP API** are
  request-priced and scale to zero.
- **Denial-of-wallet controls:** dispatch API stage throttling (zero when
  disabled), reserved Lambda concurrency, no-blind-retry state machine, and
  E3 alarms on `DispatchFailures`/`ExecutionFailures`/`ExecutionDenied`.

See [OPERATIONS_E3_EXECUTION.md](OPERATIONS_E3_EXECUTION.md) for the full runbook.
