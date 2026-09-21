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
resources; the operator must pass `OperationsMode=observe` explicitly. This is a
belt-and-braces control: the stack is both un-wired from automation *and*
internally default-disabled.

## Frozen names

These names are frozen for E1. Backend code (issue #413 core), infrastructure,
and tests all bind to them.

### Handler

| Purpose | Value |
| --- | --- |
| Lambda handler | `operations.observe.lambda_entry.handler` |

The handler module (`backend/src/operations/observe/lambda_entry.py`) is owned
and implemented by issue #413 **core** — it is the real, deployable handler. The
infrastructure does not ship a placeholder for this path (any prior infra-owned
placeholder was removed so core's handler is the sole owner). The handler honors
the `GBAW_OPERATIONS_MODE` kill switch and **fails closed** rather than returning
a placeholder success. On a combined tree the infrastructure wrapper packages and
invokes this core handler directly.

### OperationsMode vocabulary

| Value | Meaning |
| --- | --- |
| `disabled` | Default. Creates zero E1 resources. |
| `observe` | The only enabled E1 mode: synchronous, read-only GameLift observation. |

### Environment / deployment bindings

| Name | Meaning |
| --- | --- |
| `GBAW_OPERATIONS_MODE` | `disabled` (default) or `observe`; the runtime kill switch |
| `GBAW_OPERATIONS_TABLE_NAME` | DynamoDB operation-state / idempotency / ledger table |
| `GBAW_OPERATIONS_METRIC_NAMESPACE` | CloudWatch namespace for E1 metrics (`GameAgent/Operations`) |
| `GBAW_OPERATIONS_TENANT_ID` | Server-side trusted tenant binding |
| `GBAW_OPERATIONS_WORKSPACE_ID` | Server-side trusted workspace binding |
| `GBAW_OPERATIONS_TRUSTED_AUDIENCE` | Server-side trusted audience (defaults to the Cognito client id) |
| `GBAW_OPERATIONS_PER_READ_BUDGET_S` | Per-provider-read sub-budget (`3`) |
| `GBAW_OPERATIONS_PERSISTENCE_BUDGET_S` | Persistence + canonical serialization sub-budget (`3`) |
| `GBAW_OPERATIONS_CANCELLATION_MARGIN_S` | Cancellation-margin sub-budget (`3`) |
| `GBAW_OPERATIONS_OBSERVATION_TTL_S` | Observation freshness / DynamoDB `ttl` horizon for transient records (`1800` = 30 min) |
| `COGNITO_ISSUER` | JWT issuer URL for the HTTP API authorizer |
| `COGNITO_CLIENT_ID` | Cognito app client id (JWT audience) |

These four budget/TTL variables are the **frozen `_S` names** the core settings
module (`resolve_operations_settings`) actually reads. They follow the ADR 0005
sub-budget model — per-read budget, persistence budget, and cancellation margin —
plus the transient-record TTL the handler stamps on the DynamoDB `ttl` attribute.
The core service derives the **total request deadline** from the sub-budgets plus
the margin, so there is **no** request-deadline environment variable: the
`RequestDeadlineSeconds` parameter is used only as the Lambda function `Timeout`.
There is likewise **no** content-bucket binding: the unused S3 content bucket, its
runtime permissions, its environment binding, and its output were removed. Each
budget/TTL variable is backed by a validated CloudFormation parameter
(`PerReadBudgetSeconds`, `PersistenceBudgetSeconds`, `CancellationMarginSeconds`,
`ObservationTtlSeconds`) so operators tune real, bounded runtime knobs.

### DynamoDB TTL

The table's TTL attribute is `ttl` (frozen). Only transient observation records
carry a `ttl`, expiring after `GBAW_OPERATIONS_OBSERVATION_TTL_S` seconds
(default `1800` = 30 minutes, matching the core default); the append-only audit
ledger and idempotency mapping are retained for the full audit window and are not
TTL-managed.

### CloudWatch metric names (namespace `GameAgent/Operations`)

| Metric | Meaning |
| --- | --- |
| `ObservationFailures` | Count of failed observation requests |
| `ObservationTimeouts` | Count of requests that exceeded a sub-budget or the total deadline |
| `StuckOperations` | Count of stuck / lease-expired operations detected |
| `ObservationRequestLatency` | Request-completion latency (milliseconds) |

The latency alarm on `ObservationRequestLatency` uses the p99 **ExtendedStatistic**
(percentiles are not valid `Statistic` values) against the 27,000 ms acceptance
ceiling from ADR 0005.

### GameLift read actions (exactly three, read-only)

| Provider read | IAM action |
| --- | --- |
| `describe_fleet_utilization` | `gamelift:DescribeFleetUtilization` |
| `describe_fleet_capacity` | `gamelift:DescribeFleetCapacity` |
| `describe_scaling_policies` | `gamelift:DescribeScalingPolicies` |

No other GameLift action is granted. The runtime DynamoDB actions are exactly
`GetItem`, `Query`, and `TransactWriteItems` — no `Scan`, `UpdateItem`,
`DeleteItem`, or `PutItem`. KMS is limited to `Decrypt` and `GenerateDataKey`
(no `Encrypt`). No S3 runtime access, `iam:PassRole`, Step Functions, or
wildcard action appears in any E1 policy. The CMK key policy additionally grants
the CloudWatch Logs service principal `kms:Decrypt`/`kms:GenerateDataKey`,
scoped to this account's operations log groups, so the CMK-encrypted Lambda and
API access log groups work under least privilege.

## Deploy (explicit, opt-in)

E1 is deployed by its own wrapper, never by `deploy-all.sh`:

```bash
# From the repository root, with valid AWS credentials.
# Preview only (default): READ-ONLY validate + lint. Creates nothing, no change set.
./scripts/infrastructure/deploy-operations.sh

# Explicitly enable and deploy the optional stack.
GBAW_OPERATIONS_MODE=observe \
  COGNITO_ISSUER=https://cognito-idp.us-west-2.amazonaws.com/us-west-2_example \
  COGNITO_CLIENT_ID=<client-id> \
  TENANT_ID=<tenant> WORKSPACE_ID=<workspace> \
  GBAW_OPERATIONS_ARTIFACT_BUCKET=<pre-existing-artifact-bucket> \
  AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
  ./scripts/infrastructure/deploy-operations.sh --enable
```

The wrapper refuses to create enabled resources unless **both** the
`GBAW_OPERATIONS_MODE=observe` environment value **and** the `--enable` flag are
present, and it requires the Cognito issuer/audience, the tenant/workspace
bindings, and an explicit, pre-existing `GBAW_OPERATIONS_ARTIFACT_BUCKET`. All of
these inputs are validated **before** any AWS call. `AWS_PROFILE` is passed
explicitly to every `aws` invocation (not relied on ambiently), and before any
write the wrapper verifies `AWS_PROFILE`/`AWS_REGION` and the caller identity
(`aws sts get-caller-identity`).

### Real packaging path (no placeholder code)

On `--enable` the wrapper builds a **deterministic, Lambda-compatible Python 3.13
/ x86_64 zip** from a **combined tree** (the infra worktree merged with issue #413
core, which owns the real `operations/observe/lambda_entry.py`). The infra
worktree ships **no** `operations/observe` placeholder — core's handler is the
sole owner. The zip carries the `operations` code plus **every transitive runtime
dependency pinned at the version frozen in `backend/uv.lock`**: `rfc8785==0.1.4`
(canonical JSON) and `jsonschema==4.26.0` + `referencing==0.36.2` (contract
validation) with their dependencies `jsonschema-specifications==2025.9.1`,
`attrs==25.4.0`, and the native `rpds-py==2026.5.1`.

Dependencies are installed for the **Lambda `manylinux2014_x86_64` / cp313 ABI**
via a deterministic cross-platform `pip`/`uv` platform install (binary-only), or,
when a container runtime is available, an explicitly versioned Lambda build
container (`public.ecr.aws/lambda/python:3.13-x86_64`). **Host-architecture native
wheels are never packaged**: the wrapper aborts if any non-Linux/x86 native wheel
(macOS, arm64/aarch64, Windows) is staged, and requires the native `rpds-py`
Linux extension to be present. A **clean Linux/x86 import probe** then imports the
frozen handler from the built artifact alone — inside the Lambda runtime image
when a container runtime is available, otherwise via a fail-closed structural
probe that rejects a package missing the handler or any required dependency.

The wrapper uploads the zip to an **explicit, pre-existing artifact bucket**
named by `GBAW_OPERATIONS_ARTIFACT_BUCKET` under a **content-hash** key
(`operations/observe/<sha256>.zip`), and passes `CodeS3Bucket`/`CodeS3Key` to the
stack. There is **no bucket-name discovery and no bucket creation**: the wrapper
verifies the bucket exists and is owned by the caller's account
(`aws s3api head-bucket --expected-bucket-owner`) and that its Region matches the
deploy Region (`aws s3api get-bucket-location`) before uploading. If the frozen
handler module is absent from the source tree, the wrapper fails closed — no
enabled route ever points at placeholder code. The template's `Rules` block
independently rejects an `observe`-mode deploy that is missing the issuer,
audience, tenant, workspace, or code artifact inputs.

Environment (`beta`/`prod`) is selected with `--environment`; production sets
DynamoDB deletion protection and a dedicated retention posture (see the
template `Environment` parameter).

## Disable / rollback (safe)

Disabling is a data-preserving, reversible operation:

```bash
# Re-deploy the stack in disabled mode: routes and compute stop serving,
# durable data (DynamoDB table) is retained.
./scripts/infrastructure/deploy-operations.sh --disable
```

Because durable resources use a retain policy, disabling removes the request
path (API route, authorizer, compute) while preserving audit data. Re-enabling
restores the path against the same data.

## Teardown (explicit, never automatic)

Teardown is **never** invoked by `teardown-all.sh` or any automation. It
requires an explicit confirmation token and deletes only the CloudFormation
stack:

```bash
./scripts/infrastructure/teardown-operations.sh --confirm delete-operations
```

The DynamoDB table and KMS key are **retained** by policy; their audit data is
left intact. This wrapper never erases audit data. Removing retained audit data
is a **separate, explicit, manual future step** performed by hand after
confirming the data is no longer needed.

## Verification without deploying

The template and wrappers are covered by parser- and scanner-verifiable tests
that assert, without any AWS call:

- positive resources exist (authenticated HTTP API, JWT authorizer on every
  route, access logs, Lambda with reserved concurrency and bounded timeout,
  DynamoDB PAY_PER_REQUEST with PK/SK, `ttl` TTL, PITR, KMS, deletion
  protection, a distinct least-privilege observation role, alarms, metrics, log
  retention, outputs, and tags);
- the frozen handler, `observe` mode vocabulary, exact Lambda environment
  bindings, the `ttl` attribute, the ExtendedStatistic p99 latency alarm, the
  multi-tenant/code-artifact parameters with enabled-mode `Rules`, the S3
  Code artifact wiring, and the CloudWatch Logs KMS key-policy grant;
- negative IAM invariants (exactly three GameLift reads; DynamoDB limited to
  `GetItem`/`Query`/`TransactWriteItems`; no S3, `kms:Encrypt`, GameLift write,
  `iam:PassRole`, Step Functions, `UpdateItem`/`DeleteItem`/`PutItem`/`Scan`, or
  wildcard action); and
- shell safety of the wrappers (`set -euo pipefail`, explicit opt-in, read-only
  preview, caller-identity verification, teardown never automatic and with no
  data-erasing flag).

Run them with:

```bash
cd backend && uv run pytest -m unit -k "operations_observation or operations_wrappers or operations_observe_lambda_entry"
```
