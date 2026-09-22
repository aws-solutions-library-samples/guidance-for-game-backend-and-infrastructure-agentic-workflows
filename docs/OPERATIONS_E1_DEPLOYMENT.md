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

No other GameLift action is granted.

### DynamoDB actions (exactly `PutItem` + `UpdateItem` + `GetItem`) — issue #413

The deployed E1 store is `operations.observation_store.DynamoDbObservationStore`
— the module the Lambda `Handler` actually loads. (It is **not** the E0
`operations.validation.e0_persistence` sink; that persistence sink is a separate
E0 concern and is deliberately not part of this runtime IAM invariant.) Across
its two-phase lifecycle the store issues:

- **`begin_observation`** — one `TransactWriteItems` of four conditional `Put`
  legs (idempotency mapping, state snapshot, initial transition, initial ledger).
- **idempotency/replay resolution and `load_status`** — strongly-consistent
  `GetItem` reads (the mapping, the snapshot, and the stored result).
- **stale-lease reclaim** — one `TransactWriteItems` of an `Update` leg (fencing
  generation + lease) and a `Put` leg (recovery ledger).
- **`complete_observation`** — one `TransactWriteItems` of a `Put` (result), an
  `Update` (snapshot advance), and two `Put` legs (transition + ledger).
- **`fail_observation`** — one `TransactWriteItems` of an `Update` (snapshot) and
  two `Put` legs (transition + ledger).

Per the AWS **"Using IAM with DynamoDB transactions"** guide, permissions for
the `Put`/`Update`/`Delete`/`Get` legs of a `TransactWriteItems` call are
governed by the **underlying** `PutItem`/`UpdateItem`/`DeleteItem`/`GetItem`
permissions. **There is no `dynamodb:TransactWriteItems` IAM action** — granting
it is ineffective, and `cfn-lint` rejects it as **W3037**
(`'transactwriteitems' is not one of …`).

So the observation role grants **exactly `dynamodb:PutItem`, `dynamodb:UpdateItem`,
and `dynamodb:GetItem`**, scoped to the operations table ARN — the exact
underlying action set the store's `Put` legs, fenced `Update` legs, and
consistent `GetItem` reads require. This corrects an earlier fix that granted
**only `PutItem`**: that under-grant left the store's `Update` legs
(reclaim/complete/fail) and its `GetItem` reads (idempotency/replay/status)
unauthorized, so `complete_observation`, `fail_observation`, stale-lease
recovery, and status lookups would have failed with `AccessDeniedException` even
though `begin`'s pure-`Put` transaction succeeded. `Query`, `Scan`, `DeleteItem`,
`Batch*`, `ConditionCheckItem`, and provider writes remain denied by omission.

> Reference: <https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html>

The store→IAM binding is drift-proofed by
`test_iam_grants_exactly_the_underlying_actions_the_store_transacts`, which
imports and drives the real `DynamoDbObservationStore` through begin, a
conditional replay/get, a stale-lease reclaim (`Update`+`Put`), complete
(`Update`+`Put`), fail (`Update`+`Put`), and status (`Get`) with a leg-capturing
fake using real `ClientError` shapes; it maps **every** captured transaction leg
and direct read to its underlying item action and asserts the template grants
exactly that derived set — so any future drift in the store's legs/reads or the
template fails the build.
`test_dynamodb_grant_is_scoped_to_the_exact_operations_table_arn` pins the
resource to the operations table ARN.

No S3 runtime access, `iam:PassRole`, Step Functions, or wildcard action appears
in any E1 policy. The CMK key policy additionally grants the CloudWatch Logs
service principal `kms:Decrypt`/`kms:GenerateDataKey`, scoped to this account's
operations log groups, so the CMK-encrypted Lambda and API access log groups work
under least privilege.

### Runtime KMS grant for the customer-managed key (issue #413)

The operations DynamoDB table is encrypted at rest with the operations
customer-managed key (CMK). With a CMK, the **caller's** IAM role — not just the
DynamoDB service — must be authorized to use the key, because DynamoDB calls KMS
on the caller's behalf when it generates and unwraps the per-table data key. The
runtime role therefore holds the documented DynamoDB CMK data-plane action set:

| Action | Why DynamoDB needs it from the caller |
| --- | --- |
| `kms:Encrypt` | Encrypt data/table keys when persisting items |
| `kms:Decrypt` | Decrypt the table key to read items |
| `kms:ReEncrypt*` | Re-wrap data keys on key rotation / key change |
| `kms:GenerateDataKey*` | Generate the envelope data keys for the table |
| `kms:DescribeKey` | Resolve key metadata before use |
| `kms:CreateGrant` | Let DynamoDB hold a grant for background maintenance |

**Second root cause fixed here (KMS layer):** beyond the DynamoDB action fix
above, the live `begin_observation` call also had to clear the CMK layer. The
runtime role's KMS grant originally held only `kms:Decrypt` +
`kms:GenerateDataKey`, so the CMK-backed write path was denied at the KMS layer
even once the correct DynamoDB item permissions (`PutItem`/`UpdateItem`/`GetItem`) were in place. The IAM
policy simulator does not model the KMS authorization DynamoDB performs on the
caller's behalf, which is why a simulator run over the DynamoDB actions can pass
while the live call fails. The fix grants exactly the documented minimum above —
nothing more.

**Tightly constrained, not broadened.** The grant is scoped so the runtime can
never use the key for direct, generic KMS calls:

- Both KMS statements are pinned to DynamoDB with
  `kms:ViaService = dynamodb.<region>.amazonaws.com` (via
  `!Sub 'dynamodb.${AWS::Region}.amazonaws.com'`), so the key is only usable
  *through DynamoDB* on this caller's behalf.
- Every statement targets only the specific operations CMK ARN — never `*`.
- `kms:CreateGrant` is isolated in its **own** statement, additionally guarded by
  `kms:GrantIsForAWSResource = true`, so the runtime can only create grants on
  behalf of the AWS resource (DynamoDB), never arbitrary grants to arbitrary
  grantees.
- The CloudWatch Logs key-policy grant is **unchanged**: this fix touches only
  the runtime execution role, not the logs service-principal grant on the key
  policy, and does not add any direct/generic KMS use.

**Public AWS references:**

- DynamoDB encryption at rest usage notes (customer-managed-key model, caller
  key usage):
  <https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html>
- AWS KMS condition keys (`kms:ViaService`, `kms:GrantIsForAWSResource`):
  <https://docs.aws.amazon.com/kms/latest/developerguide/conditions-kms.html>

**Regression coverage.** `backend/tests/unit/test_operations_observation_infra_unit.py`
parses the template as data and enforces this contract structurally (no AWS
calls). The KMS tests fail against the pre-fix policy and reject regressions:
`test_runtime_kms_grant_covers_the_documented_dynamodb_cmk_action_set` (rejects a
missing action), `test_runtime_kms_grant_has_no_unexpected_actions` (rejects
wildcard / generic KMS actions), `test_runtime_kms_data_statement_is_pinned_to_dynamodb_via_service`
(rejects a missing `kms:ViaService` or a wildcard resource),
`test_runtime_kms_create_grant_is_isolated_and_resource_guarded` (rejects a
merged or unconstrained `CreateGrant`), and
`test_execution_role_kms_grant_stays_bounded_to_the_dynamodb_cmk_minimum`
(rejects any action beyond the documented minimum leaking onto the role).

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

## Access-log DestinationArn drift note (issue #413)

The API stage streams access logs to a dedicated CloudWatch Logs group. Its
`AccessLogSettings.DestinationArn` is set with a constructed `!Sub` ARN rather
than `!GetAtt AccessLogGroup.Arn`, because the two forms are **not** byte-equal
after deploy:

- `!GetAtt <LogGroup>.Arn` renders the log-group ARN **with** a trailing `:*`
  (the log-stream wildcard):
  `arn:aws:logs:<region>:<account>:log-group:/aws/apigateway/<name>:*`.
- API Gateway **normalizes** the value it stores on the stage to the **bare**
  log-group ARN **without** `:*`:
  `arn:aws:logs:<region>:<account>:log-group:/aws/apigateway/<name>`.

CloudFormation drift detection compares the template's rendered value against
the API's stored value, so the `:*` form makes the stage report **MODIFIED**
(`/AccessLogSettings/DestinationArn`) on every drift run — pure noise that
masks real drift. The template therefore supplies the exact bare ARN API
Gateway keeps, built from `${AWS::Partition}`/`${AWS::Region}`/`${AWS::AccountId}`
and the same `LogGroupName` the `AccessLogGroup` resource declares. Because the
constructed ARN no longer carries the implicit `!GetAtt` dependency, the stage
declares `DependsOn: AccessLogGroup` explicitly, and the vended-log delivery
resource policy (`delivery.logs.amazonaws.com`) is unchanged. Drift-contract
unit tests assert the rendered form matches the API-stored form and fail if the
`!GetAtt` wildcard form (or any `:*`-suffixed value) ever returns.

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
  exactly `PutItem`/`UpdateItem`/`GetItem` — the deployed store's underlying
  actions; no ineffective/invalid `TransactWriteItems`, no
  `Query`/`Scan`/`DeleteItem`/`Batch*`/`ConditionCheckItem`; no S3, GameLift
  write, `iam:PassRole`, Step Functions, or wildcard action);
- the runtime KMS grant for the customer-managed key: exactly the documented
  DynamoDB CMK data-plane action set plus an isolated `kms:CreateGrant`, every
  statement pinned to DynamoDB via `kms:ViaService` and `CreateGrant` guarded by
  `kms:GrantIsForAWSResource=true`, on the specific CMK ARN — no wildcard
  resource, no generic direct KMS use, no missing action (issue #413); and
- shell safety of the wrappers (`set -euo pipefail`, explicit opt-in, read-only
  preview, caller-identity verification, teardown never automatic and with no
  data-erasing flag).

Run them with:

```bash
cd backend && uv run pytest -m unit -k "operations_observation or operations_wrappers or operations_observe_lambda_entry"
```
