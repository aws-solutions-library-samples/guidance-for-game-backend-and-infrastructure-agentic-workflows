# E4 Operations Control Plane — Incremental Cost Notes (#416)

These notes explain the **incremental** cost of the OPTIONAL, default-
unprovisioned E4 operations control plane (GitHub issue #416). The numbers here
are derived from [`operations-e4-cost-model.json`](operations-e4-cost-model.json),
which is the single machine-checked source of truth:
`backend/tests/unit/test_operations_e4_cost_model_unit.py` recomputes every
scenario total from the model's own rates and asserts the published totals match,
so these notes cannot drift from the pricing inputs.

This document does **not** assert that any E4 infrastructure is deployed. A
default deploy provisions **zero** resources and costs **$0**.

## Scenarios (us-west-2, USD/month, incremental over E1/E2/E3)

| Scenario | Monthly total | What it is |
| --- | --- | --- |
| Default (unprovisioned) | **$0.00** | `Provisioned=false`: no resource exists. |
| Enabled, idle | **≈ $2.87** | `Provisioned=true`, `ControlMode=enabled`, no admin actions. |
| Representative busy month | **≈ $2.87** | 200 admin control actions on top of idle. |

## Why

- **CloudWatch** dominates: seven E4 alarms ($0.10 each) + up to seven custom
  metrics ($0.30 each) ≈ $2.80/month fixed.
- **AWS AppConfig** bills per configuration retrieved (data-plane
  `GetLatestConfiguration`). The Lambda extension caches the document in-process,
  so consumers poll on a bounded cadence rather than per request; at the modeled
  cadence this is a few cents/month. The application / environment / profile /
  deployment-strategy resources themselves are **not** billed.
- **Lambda + HTTP API + EventBridge** are request/schedule-scoped and scale to
  zero. The 2-minute freshness sweeper and any admin control actions are
  fractions of a cent.
- **No new storage or KMS key**: E4 appends audit records to the 06 table under
  the 06 CMK, already accounted for in the 06 cost model.

Prices are subject to change; see the per-service pricing pages cited in the
model's `pricing.sources`. All account ids/regions used in examples are
public-safe placeholders.
