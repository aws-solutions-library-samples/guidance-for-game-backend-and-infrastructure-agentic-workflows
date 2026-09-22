# E3 Operations Execution Control Plane — Deployment & Runbook

This runbook covers the **optional, default-unprovisioned** E3 operations
*execution* control plane for GitHub issue
[#415](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/415).
It adds bounded, pre-approved GameLift **capacity remediation** on exactly one
enrolled fleet, on top of the accepted E1 observation (#413) and E2 advise /
human-approval (#414) control planes. It is delivered as a **separate**
CloudFormation stack, `infrastructure/cloudformation/07-operations-execution.yaml`.

> This document describes an **optional** design. Following it does not assert
> that the execution stack is deployed. The base stack and normal deployment
> (`./deploy-all.sh`) never create any E3 resource. A default deploy of the 07
> stack is **$0** and provisions **zero** resources.

## Where E3 sits

```
E1 observe (#413)  →  E2 advise + human-approval gate (#414)  →  E3 execute (#415)
   read-only            prepare / approve / reject / cancel        bounded capacity write
```

E1 and E2 live in the `06-operations-observation.yaml` stack. E3 is a **new,
separate** stack so that:

- a default deployment provisions nothing and the chat / general API / model /
  E1 / E2 roles retain **zero** provider-write authority and **cannot** assume
  or invoke the executor; and
- the only component in the whole solution that can perform a GameLift capacity
  write is the E3 executor role, scoped to exactly one enrolled fleet ARN.

The 06 stack is extended **only** for the safe-mode `remediate` vocabulary and
the cross-stack outputs 07 consumes (the operations table name and CMK ARN). The
06 handler gains **no** `states:`/`lambda:InvokeFunction`/GameLift-write
authority; E2 is preserved.

## Two independent levers (and provisioning vs. runtime authority)

Like 06, the 07 stack separates two concepts:

- **Provisioning** (`Provisioned`, default **`false`**) — whether the E3 resource
  set exists at all. Every resource is gated on `ResourcesProvisioned`
  (`Provisioned=true`), so a default deploy creates **zero** resources, costs
  **$0**, and holds nothing.
- **Runtime authority** (`ExecutionMode`, default **`disabled`**) — the kill
  switch for an *already-provisioned* execution plane. Only `remediate` is an
  enabled mode.

### The reversible two-lever emergency disable

`disable-operations-execution.sh --confirm` flips **only** `ExecutionMode` to
`disabled` while keeping `Provisioned=true`. That engages **both** fail-closed
levers at once, without deleting any resource or data:

1. **Lever 1 — gateway throttle.** The dispatch API stage `ThrottlingBurstLimit`
   and `ThrottlingRateLimit` drop to `0` when `ExecutionMode != remediate`, so
   API Gateway rejects every new dispatch **before** the integration.
2. **Lever 2 — injected kill switch.** `GBAW_OPERATIONS_EXECUTION_MODE=disabled`
   is injected into the executor (and dispatcher). The executor **fails closed**
   before constructing any boto3 client or performing any capacity write; the
   dispatcher returns `503`.

The disable rebuilds no code, runs no Docker, uploads no artifact, and reuses
every existing parameter value (`UsePreviousValue`). It is fully reversible via
`deploy-operations-execution.sh --enable`. Setting `Provisioned=false` is a
**teardown** decision, not a disable.

## Invocation contract: operation_id only, no blind retry

- The dispatch route (`POST /operations/{operationId}/dispatch`) is
  **JWT-authorized**; **admin** authority is additionally enforced in the
  dispatcher handler code on top of the API Gateway JWT authorizer.
- The dispatcher validates the approved operation (a single `GetItem`), then
  `StartExecution` on the **exact** state machine carrying **`operation_id`
  only**.
- The Step Functions **STANDARD** state machine invokes the executor with
  **`operation_id` only** — no fleet id, capacity number, or principal claim
  crosses the wire. The executor re-loads everything it needs from the
  operations table under `operation_id`, and re-checks authority.
- The state machine has **no blind automatic retry** on the executor task: a
  transient executor failure is surfaced (a `Fail` state), never silently
  re-attempted, so a capacity write is never blindly repeated.

## Least-privilege IAM (three separate roles)

| Role | May do | May NOT do |
|------|--------|------------|
| **Dispatcher** | `dynamodb:GetItem` on the exact table; KMS `Decrypt`/`DescribeKey` **via DynamoDB only**; own logs + scoped `PutMetricData`; `states:StartExecution` on the **exact** state machine | No GameLift action; **cannot** `lambda:InvokeFunction` the executor directly |
| **Workflow** (state machine) | `lambda:InvokeFunction` on the **exact** executor only; Step Functions log delivery | No DynamoDB, no GameLift, no KMS |
| **Executor** | `dynamodb:PutItem`/`UpdateItem`/`GetItem` on the exact table; KMS data-plane **via DynamoDB only**; own logs + scoped `PutMetricData`; `gamelift:DescribeFleetCapacity` + `gamelift:UpdateFleetCapacity` scoped to the **exact enrolled fleet ARN** | No other GameLift action; no `iam:PassRole`, secrets, source-control, generic `execute-api`, S3 |

There is **no** `iam:PassRole`, wildcard, or service-wildcard action anywhere in
the stack. The DynamoDB transaction legs are governed by the underlying
`PutItem`/`UpdateItem`/`GetItem` permissions — there is no
`dynamodb:TransactWriteItems` IAM action to grant.

## Deploy (double opt-in)

`deploy-operations-execution.sh` previews (read-only) by default. Enabling
requires a **double opt-in** and the cross-stack bindings:

```bash
GBAW_OPERATIONS_EXECUTION_MODE=remediate \
COGNITO_ISSUER=https://cognito-idp.us-west-2.amazonaws.com/us-west-2_example \
COGNITO_CLIENT_ID=<client-id> \
GBAW_OPERATIONS_ENROLLED_FLEET_ID=fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555 \
GBAW_OPERATIONS_TABLE_NAME=game-agent-operations \
GBAW_OPERATIONS_KMS_KEY_ARN=arn:aws:kms:us-west-2:<account>:key/<key-id> \
GBAW_OPERATIONS_ARTIFACT_BUCKET=<pre-existing-artifact-bucket> \
AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
  scripts/infrastructure/deploy-operations-execution.sh --enable --mode remediate
```

The wrapper verifies the profile/account/region and that the **explicit,
pre-existing** artifact bucket is reachable (`head-bucket`; it never discovers or
creates a bucket), builds one **deterministic** Lambda-ABI zip carrying both
frozen handlers, import-probes them, uploads under a content-hash key, and
deploys `Provisioned=true`/`ExecutionMode=remediate`.

## Live validation plan (separately approved)

The live shakedown exercises exactly two **separately approved** capacity
operations against the enrolled demo/test fleet, then disables:

1. **0 → 1** — approve an operation that raises desired capacity from `0` to `1`,
   dispatch it, and confirm the fleet reaches `1` via
   `gamelift:DescribeFleetCapacity`.
2. **1 → 0** — approve an operation that lowers desired capacity from `1` back to
   `0`, dispatch it, and confirm the fleet returns to `0`.

Each operation is human-approved through the E2 gate first; the dispatcher
enforces admin on top of the JWT authorizer. After the 1 → 0 operation, run
`disable-operations-execution.sh --confirm` and confirm both levers fail closed
(a further dispatch is throttled and the executor denies). No other capacity
change is performed.

## Teardown

`teardown-operations-execution.sh --confirm delete-operations-execution` deletes
**only** the 07 stack. The 06-owned operations table and CMK are **not** owned by
this stack and are untouched; E3 log groups use a `Retain` policy and survive.
Prefer the reversible `disable` over teardown for an operational OFF.

## Cost

A default deploy is **$0** (zero resources). See
[operations-e3-cost-notes.md](operations-e3-cost-notes.md) for the incremental
cost of an enabled E3 plane (Step Functions STANDARD state transitions, the
executor and dispatcher Lambdas, and the dispatch HTTP API). E3 adds no new data
store or KMS key — it reuses the 06 table and CMK — so it adds no incremental
storage or key charge.
