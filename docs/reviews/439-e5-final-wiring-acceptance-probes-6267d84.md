# Issue #439 — E5 final runtime wiring: independent acceptance-probe specification

Read-only review of the operations source at commit `6267d84`
(`fix(operations): Preserve E3 executor service`), the last confirmed state after
reservation hardening and the v1-service fix. No source, infrastructure, or AWS
mutation was performed; nothing was pushed. Evidence is cited as `path:line` at
`6267d84`.

## Purpose and scope

This document does **not** claim the work is done. It independently specifies the
**exact acceptance probes** that must pass for the final E5 source-level runtime
to be considered complete, for each of the six confirmed remaining blockers, plus
a scan for any other source-level blocker. Each probe is written so that it fails
at `6267d84` and passes only once the corresponding wiring is added — with no
deferred infrastructure.

The invariants every probe must preserve (from the acceptance bar):

- **Default off** — autonomy dispatch is impossible unless a fresh, separate
  switch is explicitly enabled.
- **No fake approval** — the autonomous path never fabricates or replays a v1
  human approval.
- **Identifier-only workflow** — only `operation_id` crosses the Step Functions
  boundary.
- **E4 + separate switch** — the E4 kill-switch/durable-control gate and a
  distinct autonomy switch (its own AppConfig profile) both gate dispatch.
- **Current reservation** — the pre-dispatch reservation advances the current
  durable window state by exactly one revision.
- **Sole existing executor provider write** — the existing `ExecutorService`
  remains the only GameLift writer.
- **No model authority** — no policy/decision/authority value is derived from
  model output or request-body fields; all trusted inputs are server-owned.

## State confirmed at `6267d84` (what exists, what is still missing)

Confirmed present:

- `autonomy_runtime/evaluator_entry.py` builds a live handler from env:
  `resolve_autonomy_evaluator_settings` (`evaluator_entry.py:125`),
  `build_evaluator_handler` (`evaluator_entry.py:228`),
  `StepFunctionsStartExecution` (`evaluator_entry.py:174`),
  `DynamoDbWindowStateLoader` (`evaluator_entry.py:202`).
- `AutonomyRuntimeHandler.dispatch` composes prepare → persist bundle → reserve →
  gate → StartExecution, failing closed at each step
  (`autonomy_runtime/handler.py:126`).
- `autonomy_switch.py` has a strict in-code contract and a freshness check
  (`autonomy_switch.py:90`, `:240`).
- The v1/v2 executor split is preserved: the autonomous `ExecutorService` is
  constructed with a verifier that raises if the v1 `execute`/approval path is
  ever reached (`execute/executor_entry.py`, the `verify()` that raises
  "the autonomous executor service does not run the v1 approval verifier").

Confirmed **still missing / still open** at `6267d84` (the blockers):

1. **No module-level Lambda handler** in `evaluator_entry.py` — there is a
   *builder* (`build_evaluator_handler`) and a *class method*
   (`AutonomyRuntimeHandler.dispatch`), but no `def handler(event, context)`
   entrypoint like every other deployed entry (`control/control_entry.py:153`,
   `execute/dispatcher_entry.py:122`, `execute/executor_entry.py:590`,
   `observe/lambda_entry.py:333`). Grep for `^def handler`/`^def lambda_handler`
   in the module returns nothing.
2. **Trusted input loaders unproven end-to-end** — `DynamoDbWindowStateLoader`
   exists, but there is no code-owned *policy loader* and *observation loader*
   assembled into the `AutonomyRuntimeInputs` the handler consumes; the settings
   carry `autonomy_policy_id/version/hash` (`evaluator_entry.py:164-166`) but no
   loader turns them into a verified policy body.
3. **Autonomy policy settings unused for loading** — the resolved
   `autonomy_policy_*` settings are validated but not consumed by a loader that
   fetches-and-verifies the pinned policy before evaluation.
4. **No durable requested/dispatched audit at the dispatch layer** — the
   reservation store keeps an immutable reservation-lifecycle audit
   (`store.py:116`, `store.py:426` `reservation_record`), but the dispatch layer
   records nothing that distinguishes a **requested** operation from a
   **dispatched** one; `DispatchEnvelope`/`build_dispatch_envelope`
   (`dispatch.py:67`, `:100`) produce a payload and a `DispatchResult` with no
   durable state transition.
5. **StepFunctions has no stable name / no `ExecutionAlreadyExists` handling** —
   `StepFunctionsStartExecution.__call__` calls `start_execution(stateMachineArn,
   input=...)` with **no `name=`** and **no** catch of `ExecutionAlreadyExists`
   (`evaluator_entry.py:191-200`). A retry/replay can start a second execution
   for the same `operation_id`.
6. **Autonomy switch accepts naive timestamps** — `_instant` coerces a naive
   value to UTC rather than rejecting it: `if parsed.tzinfo is None: parsed =
   parsed.replace(tzinfo=timezone.utc)` (`autonomy_switch.py:256-257`; the same
   coercion appears in `evaluator_entry.py`'s `_instant`). A document with a
   naive `issued_at`/`not_after` is silently accepted.

## Acceptance probes

Each probe is a unit test (backend `pytest -m unit`, mocked; no live AWS) unless
noted. "Fails at `6267d84`" states the current observed behavior that makes the
probe red today.

### Probe 1 — Final evaluator Lambda handler

- **P1.a (entrypoint exists, identifier-only, no fabricated identity).** Import
  `operations.autonomy_runtime.evaluator_entry`; assert a module-level
  `handler(event, context)` exists and is callable. Invoke it with an injected
  fake `session`/handler so no boto3 client is created. Assert the trusted inputs
  passed to `AutonomyRuntimeHandler.dispatch` are derived **only** from
  server-owned settings + durable loaders, **never** from `event` body fields
  (feed an `event` carrying attacker-controlled `policy`, `decision`,
  `authority`, `operation_id`; assert none of those values reach the dispatched
  inputs).
- **P1.b (client allow-list).** With an injected fake `session`, assert the only
  clients created are `dynamodb`, `stepfunctions`, and the AppConfig extension —
  and **never** `gamelift` and **never** `lambda`. (Mirror the assertion the
  `build_evaluator_handler` docstring promises, at the handler boundary.)
- **P1.c (default off).** With the autonomy switch document absent/disabled, the
  handler returns a REFUSED outcome and issues **no** `start_execution` call.
- **Fails at `6267d84`:** no module-level `handler` exists to import (Blocker 1).

### Probe 2 — Code-owned policy loader (settings consumed)

- **P2.a (pin enforced).** Construct the policy loader from settings and a fake
  durable/config source that returns a policy whose `policy_id`/`policy_version`/
  `policy_hash` match `autonomy_policy_id/version/hash`. Assert it returns the
  verified policy body. Then return a policy whose hash differs by one byte and
  assert the loader **raises / fails closed** (no dispatch).
- **P2.b (no model authority).** Assert the loaded policy is used verbatim and no
  field is overridable by `event` or by any observation payload.
- **P2.c (loader wired into the handler).** End-to-end through P1.a's injected
  handler, assert the dispatched `AutonomyRuntimeInputs.policy` is exactly the
  loader's verified output.
- **Fails at `6267d84`:** the `autonomy_policy_*` settings exist but are consumed
  by no loader; there is no fetch-and-verify path (Blockers 2, 3).

### Probe 3 — Durable requested/dispatched audit

- **P3.a (requested record before StartExecution).** Drive `dispatch` with a fake
  `start_execution` that raises. Assert a durable audit record for the
  `operation_id` exists in the **`requested`** (pre-dispatch) state and that no
  record claims **`dispatched`**. Assert the in-flight reservation is released
  (fail closed) — current behavior already releases (`handler.py` step 5), so the
  new assertion is the audit state.
- **P3.b (dispatched record only after success).** With a `start_execution` that
  succeeds, assert the audit record transitions to **`dispatched`** exactly once
  and carries the `operation_id` + `logical_action_id` (no policy body, no
  credential — mirror `store.py:430`'s "carries no policy body, no credential").
- **P3.c (idempotent audit under replay).** Re-invoke with the same
  `operation_id`; assert the audit does not create a second `dispatched` record.
- **Fails at `6267d84`:** the dispatch layer persists only the reservation
  lifecycle; there is no requested/dispatched transition record (Blocker 4/audit).

### Probe 4 — Step Functions idempotency (stable name + `ExecutionAlreadyExists`)

- **P4.a (stable, deterministic name).** Call `StepFunctionsStartExecution` twice
  with the same `{"operation_id": X}` against a fake SFN client; assert **both
  calls pass the same `name=`** and that the name is a deterministic function of
  `operation_id` (e.g. derived id), not random, and conforms to SFN name
  constraints (≤80 chars, allowed charset).
- **P4.b (`ExecutionAlreadyExists` is success, not failure).** Fake client raises
  `ExecutionAlreadyExists` on the second call; assert `__call__` treats it as an
  idempotent success (returns normally / does not raise), so a replay does not
  fail-close a legitimately-already-started execution.
- **P4.c (other client errors still fail closed).** Fake client raises a generic
  `ClientError`; assert it propagates so `dispatch` records `start_execution_failed`
  and releases the reservation.
- **P4.d (still identifier-only).** Assert the serialized `input` remains
  `{"operation_id": ...}` and nothing else — the name change must not widen the
  boundary.
- **Fails at `6267d84`:** `__call__` passes no `name=` and catches nothing
  (`evaluator_entry.py:191-200`), so P4.a and P4.b are red (Blocker 5).

### Probe 5 — Strict timestamps (reject naive)

- **P5.a (naive rejected in the switch contract).** Feed
  `validate_autonomy_switch_document` / the freshness path a document whose
  `issued_at` or `not_after` is a **naive** ISO-8601 value (no offset, no `Z`);
  assert it raises the strict contract error and autonomy is **not** allowed.
- **P5.b (naive rejected in the evaluator entry parser).** Same assertion for the
  `_instant` used in `evaluator_entry.py`.
- **P5.c (offset/`Z` still accepted).** A well-formed `...Z` and a `+00:00`
  value are accepted and normalized to UTC; a non-UTC offset is either rejected
  or normalized per the intended contract (assert the chosen behavior explicitly).
- **Fails at `6267d84`:** `_instant` coerces naive → UTC
  (`autonomy_switch.py:256-257`) instead of rejecting, so P5.a/P5.b are red
  (Blocker 6).

### Probe 6 — Preserved v1 branch (regression)

- **P6.a (v1 verifier untouched).** `git diff 6267d84 <final> --
  backend/src/operations/execution_verifier.py backend/src/operations/execute/executor_service.py`
  shows **no change to the v1 approval verifier semantics**; the v1 human path
  still calls the unchanged verifier.
- **P6.b (autonomous path never runs v1 verifier).** Assert the autonomous
  `ExecutorService`'s injected verifier still raises "the autonomous executor
  service does not run the v1 approval verifier" if `execute`/`verify` is reached
  (guard from `executor_entry.py`).
- **P6.c (sole provider writer).** Assert the existing `ExecutorService` is the
  only code that issues the GameLift write on the autonomous path (no write in
  `evaluator_entry.py`/`handler.py`; grep for GameLift write verbs returns only
  the executor adapter).
- **P6.d (existing suite green).** The full `backend` operations unit suite that
  was green at `6267d84` remains green at the final commit (no regressions), and
  the executor-routing test added in `6267d84`
  (`test_operations_executor_entry_routing_unit.py`) still passes.
- **Fails at `6267d84`:** N/A — this probe defines the regression bar the final
  wiring must not break.

## Other source-level blockers found (beyond the six named)

- **B7 — Reservation port shape at the composite gate.** `evaluator_entry.py`
  injects `_UnusedReservationPort` (`evaluator_entry.py:215`) into the composite
  `AutonomyRuntimeGate` on the belief that the handler's own
  `DynamoDbReservationStore` performs the pre-dispatch reservation. The final
  wiring must include a probe asserting the gate's `reservation_port` is **never
  invoked** on the dispatch path (so the `fail-closed` `reserve()->False` can
  never deny a legitimate operation), because the earlier `87e8a52` review found
  two incompatible `reserve` shapes. Probe: spy on `_UnusedReservationPort.reserve`
  and assert it is never called during a successful dispatch.
- **B8 — Single-flip stops dispatch and write.** Default-off + E4 + separate
  switch must be provable with **one** switch document flip that stops **both**
  the pre-dispatch gate (`AutonomySwitchGate`) and, on reload, the executor.
  Probe: with the switch disabled, assert `dispatch` refuses AND the reloaded
  executor path refuses, from the same document.
- **B9 — Window-state loader freshness.** `DynamoDbWindowStateLoader.load` returns
  `reservation_store.current(state_id)` with no staleness assertion at the entry
  boundary; the reservation `reserve` enforces `expected_revision`, but a probe
  should assert that a stale `state_revision` at reserve time yields a `CONFLICT`
  refusal (not a silent second dispatch).

## What was verified vs. not

Verified by reading source at `6267d84`: the presence/absence of each symbol and
control flow cited above (evaluator entry, handler dispatch, switch parser,
executor v1/v2 split, dispatch/store audit surface). Not run: no live AWS, no
Step Functions execution, no DynamoDB. The probes above are specifications; they
have not been executed against a final commit because none is in scope for this
read-only review.
