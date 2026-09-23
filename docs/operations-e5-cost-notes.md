# E5 Bounded-Autonomy Control Plane — Incremental Cost Notes (#440)

These notes explain the **incremental** cost of the OPTIONAL, default-
unprovisioned E5 bounded-autonomy control plane (GitHub issue #440). The numbers
here are derived from
[`operations-e5-cost-model.json`](operations-e5-cost-model.json), which is the
single machine-checked source of truth:
`backend/tests/unit/test_operations_e5_cost_model_unit.py` recomputes every
scenario total from the model's own rates and reconciles the model's alarm count
against the deployed 09 template, so these notes cannot drift from the pricing
inputs or from what is actually provisioned.

This document does **not** assert that any E5 infrastructure is deployed. A
default deploy provisions **zero** resources and costs **$0**. The main
`deploy-all.sh` path never references the E5 stack; it is an explicit,
double-opt-in owner action via `scripts/infrastructure/deploy-operations-autonomy.sh`.

## Scenarios (us-west-2, USD/month, incremental over E1/E2/E3/E4)

| Scenario | Monthly total | What it is |
| --- | --- | --- |
| Default (unprovisioned) | **$0.00** | `Provisioned=false`: no resource exists. |
| Provisioned, disabled | **≈ $0.40** | `Provisioned=true`, `AutonomyMode=disabled`: schedule DISABLED, evaluator never fires; only the four alarms. |
| Enabled, idle | **≈ $0.44** | `Provisioned=true`, `AutonomyMode=operate`: the evaluator fires every 5 minutes and evaluates the policy, starting no workflow when no change is warranted. |
| Representative busy month | **≈ $0.44** | Equal to enabled-idle; the schedule cadence dominates and each authorized action only adds one un-metered `StartExecution`. |

## Why

- **CloudWatch**: the **four** E5-owned alarms ($0.10 each) = $0.40/month. They
  watch **AWS-emitted** metrics only — the evaluator Lambda `Errors` and
  `Throttles` (`AWS/Lambda`), the schedule rule `FailedInvocations`
  (`AWS/Events`), and the dead-letter queue depth (`AWS/SQS`) — so E5 emits
  **no** custom CloudWatch metrics of its own and no alarm depends on backend
  instrumentation a default/disabled plane never emits. `AutonomyEvaluatorErrors`
  is the primary actionable alarm and also drives the AppConfig autonomy-switch
  deployment rollback monitor. Autonomy-outcome coverage upstream/downstream is
  provided by the **reused** E1 (06), E3 (07), and E4 (08) alarms and is billed
  under those stacks, not double-counted here.
- **Lambda**: the evaluator is scheduled every 5 minutes (**8,640**
  invocations/month) at a modeled 0.25 GB-second each — a few cents/month.
  `ReservedConcurrentExecutions=1` guarantees at most one evaluation at a time.
- **AWS AppConfig** bills per configuration retrieved. The evaluator reads **two**
  documents (the E4 kill switch and the SEPARATE E5 autonomy switch) through the
  local Lambda extension cache, so retrievals track the cache-refresh cadence,
  not per-request — a fraction of a cent/month. The application / environment /
  profile resources themselves are **not** billed (and are owned by the 08 stack).
- **EventBridge** scheduled-rule invocations are not separately billed at this
  volume.
- **No KMS key, no Secrets Manager secret, no API**: E5 reuses the 06 operations
  table and its customer-managed KMS key **for DynamoDB data-plane access only
  (via DynamoDB)** and creates no data store, key, secret, or API of its own.
  The evaluator log group uses **default (service-managed) CloudWatch
  encryption**: the 06 CMK's policy pins usage to DynamoDB and cannot encrypt a
  log group, so no CMK is attached to it.
- **No executor**: E5 creates no executor and no new workflow. The single
  GameLift capacity-write grant (`UpdateFleetCapacity`) remains solely on the
  unchanged E3 executor role in the 07 stack; the E5 evaluator holds zero
  GameLift IAM and only starts the exact existing E3 workflow.

## Reversibility

An emergency disable (`disable-operations-autonomy.sh --confirm`) flips
`AutonomyMode=disabled` while keeping `Provisioned=true`, dropping the steady
state to the provisioned-disabled row (**≈ $0.40/month**) without deleting any
resource or data. A teardown (`teardown-operations-autonomy.sh --confirm
delete-operations-autonomy`) returns the incremental cost to **$0.00**; it is
never invoked automatically.
