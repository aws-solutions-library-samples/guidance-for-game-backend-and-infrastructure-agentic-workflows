# E1 Operations Observation Control Plane — Deployment & Runbook

This runbook covers the **optional, default-disabled** E1 operations
observation control plane for GitHub issue
[#413](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/413).
It implements the accepted synchronous, read-only GameLift observation design in
[ADR 0005](adr/0005-persist-operations-and-recover-workflows.md) and the
boundaries in [ADR 0002](adr/0002-use-protocol-neutral-operations-services.md)
and [ADR 0003](adr/0003-isolate-provider-writes.md). It provisions no
provider-write permission and no queue, worker, dead-letter queue, or Step
Functions.

> This document describes an **optional** design. Following it does not assert
> that the operations stack is deployed. The base stack and normal deployment
> (`./deploy-all.sh`) never create any E1 resource.

## Default-disabled invariant

`infrastructure/cloudformation/06-operations-observation.yaml` is a **separate
stack**. It is **not** referenced by `scripts/deploy.sh` or `deploy-all.sh`. A
normal deployment therefore creates **zero** E1 resources.

The template additionally gates every resource behind a `Condition`
(`OperationsEnabled`) driven by the `OperationsMode` parameter, whose default is
`disabled`. Deploying the optional stack with defaults still creates zero E1
resources; the operator must pass `OperationsMode=enabled` explicitly. This is a
belt-and-braces control: the stack is both un-wired from automation *and*
internally default-disabled.

## Frozen names

These names are frozen for E1. Backend code (issue #413 core), infrastructure,
and tests all bind to them.

### Handler

| Purpose | Value |
| --- | --- |
| Lambda handler | `operations.observe.lambda_entry.handler` |

### Environment / deployment bindings

| Name | Meaning |
| --- | --- |
| `GBAW_OPERATIONS_MODE` | `disabled` (default) or `enabled`; the runtime kill switch |
| `GBAW_OPERATIONS_TABLE_NAME` | DynamoDB operation-state / idempotency / ledger table |
| `GBAW_OPERATIONS_CONTENT_BUCKET` | S3 bucket for content-addressed observation records |
| `GBAW_OPERATIONS_METRIC_NAMESPACE` | CloudWatch namespace for E1 metrics (`GameAgent/Operations`) |
| `GBAW_OPERATIONS_REQUEST_DEADLINE_SECONDS` | Total request budget (`15`), below the 30 s gateway ceiling |
| `GBAW_TENANT_ID` | Server-side trusted tenant binding |
| `GBAW_WORKSPACE_ID` | Server-side trusted workspace binding |
| `COGNITO_ISSUER` | JWT issuer URL for the HTTP API authorizer |
| `COGNITO_CLIENT_ID` | Cognito app client id (JWT audience) |

### CloudWatch metric names (namespace `GameAgent/Operations`)

| Metric | Meaning |
| --- | --- |
| `ObservationFailures` | Count of failed observation requests |
| `ObservationTimeouts` | Count of requests that exceeded a sub-budget or the total deadline |
| `StuckOperations` | Count of stuck / lease-expired operations detected |
| `ObservationRequestLatency` | Request-completion latency (milliseconds) |

### GameLift read actions (exactly three, read-only)

| Provider read | IAM action |
| --- | --- |
| `describe_fleet_utilization` | `gamelift:DescribeFleetUtilization` |
| `describe_fleet_capacity` | `gamelift:DescribeFleetCapacity` |
| `describe_scaling_policies` | `gamelift:DescribeScalingPolicies` |

No other GameLift action is granted. No GameLift write, `iam:PassRole`, Step
Functions, DynamoDB `UpdateItem`/`DeleteItem`/`Scan`, or wildcard action appears
in any E1 policy.

## Deploy (explicit, opt-in)

E1 is deployed by its own wrapper, never by `deploy-all.sh`:

```bash
# From the repository root, with valid AWS credentials.
# Preview only (default): renders the plan, creates nothing.
./scripts/infrastructure/deploy-operations.sh

# Explicitly enable and deploy the optional stack.
GBAW_OPERATIONS_MODE=enabled ./scripts/infrastructure/deploy-operations.sh --enable
```

The wrapper refuses to create enabled resources unless **both** the
`GBAW_OPERATIONS_MODE=enabled` environment value **and** the `--enable` flag are
present. Without them it prints the plan and exits without mutating AWS.

Environment (`beta`/`prod`) is selected with `--environment`; production sets
DynamoDB deletion protection and a dedicated retention posture (see the
template `Environment` parameter).

## Disable / rollback (safe)

Disabling is a data-preserving, reversible operation:

```bash
# Re-deploy the stack in disabled mode: routes and compute stop serving,
# durable data (DynamoDB table, S3 records) is retained.
GBAW_OPERATIONS_MODE=disabled ./scripts/infrastructure/deploy-operations.sh --disable
```

Because durable resources use a retain policy in production, disabling removes
the request path (API route, authorizer, compute) while preserving audit data.
Re-enabling restores the path against the same data.

## Teardown (explicit, never automatic)

Teardown is **never** invoked by `teardown-all.sh` or any automation. It
requires an explicit confirmation token:

```bash
./scripts/infrastructure/teardown-operations.sh --confirm delete-operations
```

In production the DynamoDB table and content bucket are retained by policy;
teardown reports the retained resources and does not force-delete them without a
second explicit `--delete-data` acknowledgement. This prevents accidental audit
data loss.

## Verification without deploying

The template and wrappers are covered by parser- and scanner-verifiable tests
that assert, without any AWS call:

- positive resources exist (authenticated HTTP API, JWT authorizer on every
  route, access logs, Lambda with reserved concurrency and bounded timeout,
  DynamoDB PAY_PER_REQUEST with PK/SK, TTL, PITR, KMS, deletion protection,
  a distinct least-privilege observation role, alarms, metrics, log retention,
  outputs, and tags);
- negative IAM invariants (exactly three GameLift reads; no GameLift write,
  `iam:PassRole`, Step Functions, DynamoDB `UpdateItem`/`DeleteItem`/`Scan`, or
  wildcard action);
- the default-disabled behavior (every resource gated on `OperationsEnabled`,
  default `disabled`, and the stack un-wired from `deploy-all.sh`); and
- shell safety of the wrappers (`set -euo pipefail`, explicit opt-in, teardown
  never automatic).

Run them with:

```bash
cd backend && uv run pytest -m unit -k operations_observation
```
