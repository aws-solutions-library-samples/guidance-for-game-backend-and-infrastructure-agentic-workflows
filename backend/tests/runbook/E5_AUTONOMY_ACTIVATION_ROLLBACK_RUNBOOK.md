# E5 Bounded-Autonomy — Activation & Rollback Runbook (Issue #440, Track C)

> **Scope.** This runbook covers standing up, validating, and tearing down the
> **optional, reviewed** E5 bounded-autonomy stack. E5 autonomy is
> **default-disabled and default-zero**: the canonical `deploy-all.sh`
> deployment creates **no** autonomy resources, grants **no** provider-write
> permission, and mints **no** executor credential. Nothing in this runbook is
> part of the default deployment.
>
> **Never** add autonomy write permission to the chat runtime (see `AGENTS.md`
> "Repository Reality" / "Important Boundaries"). E5 is a separate control
> plane, activated only through an optional stack wrapper an operator reviews
> and applies deliberately.

## 0. Invariants this runbook must never violate

- **Main deployment remains `deploy-all.sh` only.** Optional stack wrappers may
  deploy reviewed E5; the main path never does.
- **Separate AppConfig autonomy switch.** E5 is gated by its own AppConfig
  document (`operations.autonomy_switch`), disabled by default and independent of
  the E4 kill-switch. **Enabling E4 never enables autonomy.** The frozen E4
  control-plane schema is **not** edited, extended, or re-hashed by E5.
- **No provider-write permission or executor credential on any autonomy
  component.** Only the existing durable, authenticated Standard workflow may
  invoke the existing narrow executor, and then only with an `operation_id`.
- **No self-hosted runners.** Any CI that touches E5 uses hosted runners only.
- **Emergency disable blocks both evaluation and the immediate pre-write.** The
  composite gate re-checks emergency disablement immediately before the single
  provider write.
- **Bounds are `0/1/1`.** The authorized demo fleet starts and ends at
  `desired=0 / minimum=0 / maximum=1`; live validation restores zero.

## 1. Preconditions (all required before activation)

1. The reviewed optional E5 stack wrapper has been read and approved. It layers
   on top of the base deployment; it does not modify stacks `00`–`08`.
2. The AppConfig **autonomy switch** document is present and can be published
   independently of the E4 kill-switch document.
3. The demo fleet is enrolled for the target workspace, in the target
   **profile/region**, and its current capacity is exactly `0/0/1`.
4. Alarms are green and there is no CloudFormation drift on the base stacks.
5. Least-privilege / ReadOnly credentials are used for every read step; the
   single write path is the executor's own scoped role, reached only via the
   authenticated Standard workflow.
6. The **06 observation HTTPS API endpoint** (`GBAW_E5_ENDPOINT`, reused as the
   observe endpoint) and a **short-lived observe bearer** (`GBAW_E5_OBSERVE_BEARER`)
   are available out of band. The trusted E1 observation is obtained by an
   AUTHENTICATED call to `POST {endpoint}/operations/observe` (a two-key body
   `{fleet_id, idempotency_token}`) followed by polling `GET {endpoint}/operations/{id}`
   for a `succeeded` state — never by a direct 06 Lambda invoke (an invoke with
   empty JWT claims cannot succeed). The bearer is supplied ONLY in the
   environment; it is never placed on an argv, in a URL/query, or in a log.

## 2. Activation

> Perform each step with the least-privilege credential that suffices. Prefer
> describe/list/read operations; the only write is the executor's own capacity
> update, reached only through the authenticated workflow.

1. **Deploy the optional E5 stack wrapper** (reviewed) into the target account.
   This creates the autonomy runtime resources (the evaluator Lambda, its
   retained log group and retained dead-letter queue, the SEPARATE autonomy
   AppConfig application/environment/profile, the scheduled rule, and the four
   AWS-native alarms) **disabled**. It does NOT create a reservation store, a
   Step Functions state machine, or an audit ledger: reservations and the audit
   ledger live in the REUSED 06 operations table, and autonomous execution
   reuses the EXISTING 07 STANDARD workflow by ARN. It grants no new
   provider-write permission to any non-executor component.
2. **Publish the AppConfig autonomy switch** enabling the autonomy primary flag
   and the `autonomous_write` capability flag, with a fresh
   `issued_at`/`not_after` window carrying an explicit UTC offset. Leave the E4
   kill-switch document unchanged.
3. **Confirm the composite gate reads fresh-enabled** from every source:
   `GBAW_OPERATIONS_AUTONOMY_ENABLED` on, static mode exactly `operate`, the
   separate autonomy switch enabled, and the E4 `dispatch`/`execute` phases and
   durable intent permitting.
4. **Run the live shakedown** (see §3) with the mandatory `--adapter command`
   flag (the evaluator has no HTTP API — the observe step still uses the 06
   HTTPS API; without the flag the harness refuses).
   It refuses unless every preflight condition holds and both confirmations are
   supplied.

## 3. Live validation (`e5_shakedown`)

The harness (`backend/src/operations/validation/e5_shakedown.py`) drives the
whole lifecycle against the already-deployed optional stack and **refuses the
entire run** — making no authenticated request — unless **all** of:

- the exact forward confirmation `EXECUTE-LIVE-AUTONOMY-0-TO-1` **and** the
  distinct inverse confirmation `EXECUTE-LIVE-AUTONOMY-INVERSE-1-TO-0` are
  supplied out of band (never sourced from a response body);
- operator **profile/region/fleet** exactly match server enrollment;
- **starting capacity is exactly `0/0/1`**;
- **static + E4 + separate-autonomy** gates are each fresh-enabled;
- **alarms and drift** are both safe.

```bash
# Read-only preflight facts and short-lived tokens are supplied out of band;
# nothing is persisted or echoed. Endpoint/tokens/ids are never logged.
# The observe bearer is passed ONLY through the environment (never argv), and the
# observe step calls the 06 HTTPS API at $GBAW_E5_ENDPOINT.
export GBAW_E5_OBSERVE_BEARER   # short-lived; carried in the Authorization header only
python -m operations.validation.e5_shakedown \
  --endpoint "$GBAW_E5_ENDPOINT" \
  --admin-bearer "$GBAW_E5_ADMIN_BEARER" \
  --fleet-id "$GBAW_E5_FLEET_ID" \
  --observation-id "$GBAW_E5_OBSERVATION_ID" \
  --operation-id "$GBAW_E5_OPERATION_ID" \
  --profile "$GBAW_E5_PROFILE" --enrolled-profile "$GBAW_E5_ENROLLED_PROFILE" \
  --region "$GBAW_E5_REGION" --enrolled-region "$GBAW_E5_ENROLLED_REGION" \
  --enrolled-fleet-id "$GBAW_E5_ENROLLED_FLEET_ID" \
  --starting-desired 0 --starting-minimum 0 --starting-maximum 1 \
  --static-gate-fresh-enabled true --e4-gate-fresh-enabled true \
  --autonomy-switch-fresh-enabled true \
  --alarms-safe true --drift-safe true \
  --confirm-forward "$GBAW_E5_CONFIRM_FORWARD" \
  --confirm-inverse "$GBAW_E5_CONFIRM_INVERSE" \
  --adapter command
```

`--adapter command` is MANDATORY: the E5 **evaluator** has no HTTP API, so the
harness drives concrete `aws` CLI commands (lambda/stepfunctions/dynamodb/
cloudtrail/gamelift/appconfig) and refuses without the flag. The one exception
is the trusted E1 **observation**, which is a 06-owned AUTHENTICATED HTTPS API
call (`POST /operations/observe` + status poll) using `$GBAW_E5_ENDPOINT` and
the `$GBAW_E5_OBSERVE_BEARER` bearer — not a Lambda invoke. The
`--alarms-safe`, `--autonomy-switch-fresh-enabled`, and `--starting-*` values
are OVERLAID by MEASURED provider reads: a supplied "safe" value cannot
override an observed unsafe state, and the run fails closed on any conflict.

Exit codes: `0` accepted, `1` a check failed, `2` summary failed public-safety
(never emitted), `3` refused at preflight (no request made).

The harness resolves the exact 07 `ExecutionStateMachineArn` and `ExecutorRoleArn`
from the deployed stack before preflight; an absent, malformed, or mismatched
operator-supplied coordinate refuses the run before any write-capable step. It
performs, in order, one **trusted E1 observation**, an evaluator-authorized
**`0 -> 1`** write, then verifies the write was **audited**, **atomically
reserved**, ran through **Step Functions**, and that CloudTrail binds the
executor role, exact `UpdateFleetCapacity` request ID, and frozen capacity triple
to this operation; verifies **capacity is 1**; proves an **immediate retry is
denied** by cooldown/frequency/concurrency; performs a **separately-confirmed
inverse `1 -> 0`**; verifies **capacity is 0**; **disables autonomy**; and
proves a **forced evaluator/executor attempt cannot write**. During guaranteed
teardown it confirms disablement, waits for every known dispatched execution to
be terminal, reconciles through the separately confirmed operator inverse when
needed, and only then accepts a final `0/0/1` read. Any ambiguous result (for
example a missing integer `desired`, an unreadable invocation, or a dispatch
whose execution cannot be identified) fails **closed**.

## 4. Rollback / teardown

Rollback is always safe to perform and leaves the fleet at rest.

1. **Disable the autonomy switch** — publish the AppConfig autonomy document
   with the primary/`autonomous_write` flags off (or let its window lapse). The
   composite gate then fails closed on the next evaluation and immediate
   pre-write. This is the emergency disable; it requires no stack change.
2. **Quiesce known executions before final reconciliation.** Describe every
   shakedown operation that reached or might have reached `StartExecution` until
   it is terminal. An unknown, running, or redrive-pending execution is not a
   safe teardown result. Do not accept an earlier capacity-zero read as proof.
3. **Confirm capacity is `0/0/1` after quiescence.** If a write left the demo
   fleet at `1`, use the separately confirmed bounded inverse and then re-read
   `desired=0`. A missing provider request receipt or CloudTrail correlation is
   an unavailable proof, not a successful write attribution.
4. **Verify no forced write is possible** — a forced evaluator/executor attempt
   returns a denial and performs no write (`forced_evaluator_executor_cannot_write`).
5. **(Full teardown)** Delete the optional E5 stack wrapper
   (`teardown-operations-autonomy.sh --confirm delete-operations-autonomy`).
   Because it is layered and separate, deleting it does not touch stacks
   `00`–`08`, the E4 schema, or the chat runtime. Deleting the 09 stack removes
   the evaluator, the schedule rule, the autonomy AppConfig application, and the
   four alarms, but **two resources are retained by design** and SURVIVE the
   teardown: the evaluator **log group** and the evaluator **dead-letter queue**
   both use a `Retain` deletion policy so the audit trail and any captured
   failures outlive the stack. They continue to incur minimal storage cost until
   deleted by hand. The account therefore returns to the enabled-autonomy
   posture minus the billable evaluation surface — **not** a literal zero-resource
   state. Confirm those two retained resources are the only autonomy artifacts
   left, and that no evaluator, schedule, AppConfig autonomy document, or alarm
   remains.

## 5. Ambiguous-result / abort handling

- If the shakedown returns exit `3` (refused), **no request was made**; fix the
  failing preflight fact and re-run. Do not bypass any refusal.
- If any check reports `AMBIGUOUS_RESULT` or a check fails, **stop**, disable the
  autonomy switch (§4.1), restore the fleet to `0/0/1`, and investigate before
  retrying. Never "retry through" an ambiguous capacity read.
- Treat every write as real. The demo fleet is the only authorized target and
  the only permitted change is within the `0/1/1` window.

## 6. Post-run checklist

- [ ] Autonomy switch disabled (default-zero posture restored).
- [ ] Demo fleet at `desired=0 / minimum=0 / maximum=1`.
- [ ] Audit ledger contains the executor-attributed write and the inverse.
- [ ] Alarms green, no drift.
- [ ] Sanitized shakedown summary archived (contains only short hashes and
      booleans/observed codes — no endpoint, token, fleet, or account id).
