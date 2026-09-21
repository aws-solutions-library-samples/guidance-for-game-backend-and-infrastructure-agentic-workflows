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

The handler module (`backend/src/operations/observe/lambda_entry.py`) is the
infrastructure-owned packaging seam. It honors the `GBAW_OPERATIONS_MODE` kill
switch and **fails closed** (HTTP 503) until issue #413 core registers the real
observation callable; it never returns a placeholder success.

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
| `GBAW_OPERATIONS_REQUEST_DEADLINE_SECONDS` | Total request budget (`15`), below the 30 s gateway ceiling |
| `GBAW_OPERATIONS_PROVIDER_READ_BUDGET_SECONDS` | Per-provider-read sub-budget (`3`) |
| `GBAW_OPERATIONS_PERSISTENCE_BUDGET_SECONDS` | Persistence + canonical serialization sub-budget (`3`) |
| `GBAW_OPERATIONS_RECORD_TTL_SECONDS` | TTL applied to the DynamoDB `ttl` attribute for transient records |
| `COGNITO_ISSUER` | JWT issuer URL for the HTTP API authorizer |
| `COGNITO_CLIENT_ID` | Cognito app client id (JWT audience) |

The four budget/TTL variables follow the ADR 0005 sub-budget model (total
request deadline, per-read budget, persistence budget) plus the transient-record
TTL that drives the DynamoDB `ttl` attribute. There is **no** content-bucket
binding: the unused S3 content bucket, its runtime permissions, its environment
binding, and its output were removed.

### DynamoDB TTL

The table's TTL attribute is `ttl` (frozen). Only transient observation records
carry a `ttl`; the append-only audit ledger and idempotency mapping are retained
for the full audit window and are not TTL-managed.

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
  AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
  ./scripts/infrastructure/deploy-operations.sh --enable
```

The wrapper refuses to create enabled resources unless **both** the
`GBAW_OPERATIONS_MODE=observe` environment value **and** the `--enable` flag are
present, and it requires the Cognito issuer/audience and the tenant/workspace
bindings. Before any write it verifies `AWS_PROFILE`/`AWS_REGION` and the caller
identity (`aws sts get-caller-identity`).

### Real packaging path (no placeholder code)

On `--enable` the wrapper builds a **deterministic, minimal Lambda zip**
containing the `operations` code and the required third-party dependency
(`rfc8785`, used by the canonical serializer). It verifies the packaged handler
imports in a clean environment, uploads the zip to an **explicitly supplied or
safely discovered existing** deployment bucket (`CODE_S3_BUCKET`; the wrapper
never creates a bucket) under a **content-hash** key
(`operations/observe/<sha256>.zip`), and passes `CodeS3Bucket`/`CodeS3Key` to the
stack. If the frozen handler module is absent from the source tree, the wrapper
fails closed — no enabled route ever points at placeholder code. The template's
`Rules` block independently rejects an `observe`-mode deploy that is missing the
issuer, audience, tenant, workspace, or code artifact inputs.

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
