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

## Default-unprovisioned invariant (and provisioning vs. runtime authority)

`infrastructure/cloudformation/06-operations-observation.yaml` is a **separate
stack**. It is **not** referenced by `scripts/deploy.sh` or `deploy-all.sh`. A
normal deployment therefore creates **zero** E1 resources.

The template separates two independent concepts:

- **Provisioning** (`Provisioned`, default **`false`**) — whether the E1 resource
  set exists at all. Every resource is gated on the `ResourcesProvisioned`
  condition (`Provisioned=true`). A defaults deploy leaves `Provisioned=false`,
  so it creates **zero** resources and costs **$0** and holds no data.
- **Runtime authority** (`OperationsMode`, default **`disabled`**) — the kill
  switch for an *already-provisioned* control plane. It is injected as
  `GBAW_OPERATIONS_MODE` and additionally throttles the HTTP API stage to zero
  when it is not `observe`, so a provisioned-but-disabled stack **fails closed**
  at both the API and the handler. It does **not** gate resource existence.

This split is what makes disable safe and reversible. An **emergency disable**
sets `OperationsMode=disabled` while keeping `Provisioned=true`: every resource
stays under CloudFormation with **stable physical names** and **retained audit
data**, and a later re-enable simply flips the authority back. Because resource
existence is gated on `Provisioned` (not on `OperationsMode`), disabling deletes
**nothing** — the earlier single-condition design, where disabling removed every
resource and a re-enable then collided on retained physical names, is fixed.

The template's `Rules` enforce the safe combinations: `OperationsMode=observe`
**requires** `Provisioned=true` (you cannot enable runtime authority against a
zero-resource stack), and whenever `Provisioned=true` the bindings the resources
reference (JWT issuer/audience, tenant, workspace, and the Lambda code artifact
bucket/key) **must be non-empty** — in `observe` *and* in
disabled-after-provision. That is why an emergency disable **reuses** the stack's
existing parameter values rather than blanking them.

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
| `GBAW_OPERATIONS_MODE` | `disabled` (default) or `observe`; the runtime kill switch, injected from the `OperationsMode` parameter. Independent of `Provisioned`: a disabled-but-provisioned stack keeps all resources |
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

## Disable / rollback (safe, data-preserving, reversible)

Disabling is an emergency, data-preserving, reversible operation on an
**already-provisioned** stack:

```bash
# Keep every resource under CloudFormation; only flip the runtime authority to
# disabled so the API and handler fail closed. No code rebuild, no Docker.
./scripts/infrastructure/deploy-operations.sh --disable
```

What `--disable` does — and does **not** — do:

- It **keeps `Provisioned=true`** and every resource in place: the KMS key,
  DynamoDB table, log groups, IAM role, Lambda function, HTTP API, and alarms
  are **not deleted** and keep their **stable physical names**. Disabling deletes
  **nothing**.
- It sets only **`OperationsMode=disabled`**, so `GBAW_OPERATIONS_MODE=disabled`
  is injected (the handler fails closed) and the HTTP API stage throttles to
  zero (the gateway fails closed). Direct/API calls are rejected.
- It **reuses** the stack's current parameter values via CloudFormation
  `UsePreviousValue` (issuer, audience, tenant, workspace, code artifact
  bucket/key, budgets). It does **not rebuild code or run Docker** — an emergency
  disable must not depend on a working build.
- It first **verifies the exact target stack exists** in the credential's
  account/region and that it is actually provisioned (`Provisioned=true`),
  refusing (exit 7) otherwise, then **updates through CloudFormation**
  (`update-stack`) and waits for completion.

Because resources and their retained data stay under CloudFormation, a later
`--enable` is fully **reversible**: it flips `OperationsMode` back to `observe`
against the same resources, names, and data — no re-create, no name collision.

> **Cost note.** A **disabled-but-provisioned** stack is **not** the same as the
> default `$0` state. The retained DynamoDB table (storage + PITR), the KMS key,
> the log groups (storage), and the CloudWatch alarms/metrics continue to incur
> their standing (fixed) charges and continue to **retain audit data** while
> disabled. Only the **default, unprovisioned** stack (`Provisioned=false`, the
> deploy default) costs $0 and holds no data. To stop the standing charges,
> tear the stack down explicitly (see below) — teardown, not disable, is the
> path that removes resources.

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

## Deployed shakedown (manual, credentialed, read-only)

The template/wrapper tests above prove the deployed *shape* without any AWS
call. The **deployed shakedown** is the complementary step: it exercises an
already-deployed, `observe`-mode E1 endpoint end-to-end over HTTP to prove the
live boundary *behaves* the way the frozen contract promises. It is a
disposable, read-only harness — it performs **no** AWS mutation, never deploys,
enables, disables, or tears down anything, and emits only a **sanitized**
public-safe summary.

It is a manual, credentialed step, deliberately kept **out** of the default
no-credential unit run (`./test-unit.sh`). Run it by hand against a deployed
stack, with a short-lived Cognito **access** token you mint yourself.

### Inputs (from environment or arguments; never logged)

The endpoint, the short-lived access token, the classic fleet id, and the
optional id token are read **only** from the environment or flags and are
**never** logged, echoed, or written to the summary. Prefer the environment form
so tokens never land in shell history or a process listing:

| Input | Environment variable | Flag |
| --- | --- | --- |
| Deployed API base URL (`https://…`) | `GBAW_E1_ENDPOINT` | `--endpoint` |
| Short-lived Cognito **access** token | `GBAW_E1_ACCESS_TOKEN` | `--access-token` |
| Classic GameLift fleet id | `GBAW_E1_FLEET_ID` | `--fleet-id` |
| A second, different classic fleet id | `GBAW_E1_ALT_FLEET_ID` | `--alt-fleet-id` |
| Optional Cognito **id** token (proves it is denied) | `GBAW_E1_ID_TOKEN` | `--id-token` |
| AWS profile / region for the optional postcheck | `AWS_PROFILE` / `AWS_REGION` | `--profile` / `--region` |

The two fleet ids must both be valid classic (`EC2`) fleet ids and must differ:
the second is used only to prove that reusing an idempotency token against a
*changed but still valid* target conflicts before any provider read.

### Run

```bash
cd backend
GBAW_E1_ENDPOINT=https://<api-id>.execute-api.us-west-2.amazonaws.com \
GBAW_E1_ACCESS_TOKEN=<short-lived-cognito-access-token> \
GBAW_E1_FLEET_ID=<classic-fleet-id> \
GBAW_E1_ALT_FLEET_ID=<second-classic-fleet-id> \
GBAW_E1_ID_TOKEN=<optional-cognito-id-token> \
  PYTHONPATH=src uv run python -m operations.validation.e1_shakedown \
    --profile <read-only-profile> --region us-west-2 \
    --out ../docs/evidence/e1-shakedown-<date>.json
```

The exit code is `0` only when every non-skipped check passes; `2` when a
required input is missing (the message names *which* input, never a value); and
`4` when the shakedown ran but a check failed.

### What it asserts

Each item is one discriminating check (the emitted summary records pass/skip,
the observed HTTP status, and — for typed application errors — the observed
`error_code`, but never a raw body):

1. Unauthenticated POST is denied at the gateway (401/403).
2. A malformed / identity-injection payload is denied (`400 CONTRACT_INVALID`);
   identity is never read from the body.
3. A valid authenticated POST returns `200` with a bounded, typed observation.
4. An identical retry replays the byte-equivalent stored result with the **same**
   `operation_id`.
5. The same token with a changed valid-looking target returns
   `409 IDEMPOTENCY_CONFLICT` before any provider access.
6. `GET /operations/{operationId}` returns a matching `succeeded` status/result.
7. An id token cannot gain access — denied at the gateway or the handler
   (skipped, not passed, when no id token is supplied).
8. An unknown operation returns a bounded `404 NOT_FOUND`.
9. Every response is bounded (JSON content type, under the size ceiling) and
   free of any raw AWS ARN, 12-digit account id, or provider payload.

### Optional read-only CloudWatch postcheck

When a `--profile`/`--region` (or `AWS_PROFILE`/`AWS_REGION`) is supplied, the
harness additionally runs **read-only** CloudWatch `ListMetrics` calls to report
whether the E1 metric names (`ObservationFailures`, `ObservationTimeouts`,
`StuckOperations`, `ObservationRequestLatency`) are present in the
`GameAgent/Operations` namespace. This is best-effort and **never mutates** AWS:
a missing metric or absent credentials is reported, never fatal. Pass
`--skip-postcheck` to omit it.

### Discriminating unit coverage (runs in the default suite)

The harness's assertions are proven **discriminating** by unit tests that drive
it against a local stdlib fake HTTP server: a compliant fake passes every check,
and a family of deliberately broken fakes (accepts identity injection,
non-deterministic replay, no idempotency conflict, id-token allowed, unknown
returns 500, leaky content type, leaky body) each fail exactly the matching
check. These require **no** credentials and run in the default unit suite:

```bash
cd backend && uv run pytest -m unit -k operations_e1_shakedown
```
