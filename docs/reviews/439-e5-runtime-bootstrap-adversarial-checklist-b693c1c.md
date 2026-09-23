# Issue #439 — E5 evaluator/executor bootstrap: adversarial checklist

Read-only review of commit `b693c1c` (`feat(operations): Wire identifier-only E5
runtime dispatch handler`). No source, infrastructure, or AWS mutation; no push.
Evidence is cited as `path:line` at that commit. Verification: 526 unit tests
green (16 runtime handler/store + 510 autonomy/executor/dispatcher/verifier/
kill-switch/durable), zero AWS calls, run from a detached worktree at `b693c1c`.

## What b693c1c actually changed, and what it did not

`b693c1c` adds exactly three files and 457 lines: `autonomy_runtime/handler.py`
(the `AutonomyRuntimeHandler` composing prepare → reserve → gate → persist →
identifier-only `StartExecution`), its `__init__` re-export, and a unit test.
The **executor-side** v2 machinery it hands off to was already built in the five
preceding commits (`73a1a99`..`c35ffee`): the unified `ReservationStore`
(reserve/require/settle), the `bridges.py` switch/reservation adapters, the
additive `AutonomyExecutionVerifier`, `ExecutorService.execute_verified` + the
immediate `pre_write_hook`, `executor_entry.execute_autonomous` with
always-settle, and the execute-time window-freshness gate.

**The prior review's blockers (`87e8a52`) are genuinely fixed in code:** the two
reservation shapes are collapsed onto one `ReservationStore`
(`bridges.StoreReservationGatePort` builds the strict `ReservationRequest` from
trusted inputs — `bridges.py:104`), the two switch shapes are unified through
`SwitchAutonomyPort` (`bridges.py:40`), the v2 verifier is additive so v1
`ExecutionVerifier.verify` is untouched, `settle` releases the in-flight slot on
every terminal branch (`executor_entry.py:134`), and freshness is enforced at
execute time. All are seam-compatible and independently green.

### The residual "future infrastructure will wire it" gap — NARROWED, NOT CLOSED

The acceptance bar "no residual 'future infrastructure will wire it' gap is
acceptable" is **not met**. The v2 blocks are complete and composable, but
**nothing composes them at either running entrypoint**:

1. **No production caller of `AutonomyRuntimeHandler` / `.dispatch()`.** The only
   references are its own `__init__.py` re-export and the unit test
   (`grep AutonomyRuntimeHandler` → `handler.py`, `__init__.py`, test only). No
   lambda entry, no bootstrap function, no `_build_runtime`-equivalent constructs
   it.
2. **No concrete `BundleStorePort`.** `persist_bundle` is defined only as a
   `Protocol` (`handler.py:78`) and implemented only by the test's
   `_FakeBundleStore` (`test...handler_unit.py:94`). The durable bundle the
   executor must reload by `operation_id` has **no** production persistence
   adapter — the DynamoDb reservation store persists the reservation, but nothing
   persists policy/observation/decision/operation/window_state.
3. **The production executor entrypoint runs only v1.** `executor_entry.handler`
   calls `runtime.service.execute(..., prepared_operation=, approval=)` and
   requires `state == "approved"` (`executor_entry.py:284-291`). It never calls
   `select_execution_path` or `execute_autonomous`; `_build_runtime` builds no v2
   verifier, no reservation store, and no `pre_write_hook`
   (grep in `executor_entry.py` → none). So `execute_autonomous` and
   `select_execution_path` also have **no production caller** (tests only).
4. **No Step Functions ASL / IaC** for the autonomous `operation_id`-only invoke
   exists under `infrastructure/` (`ls-tree ... | grep -i asl|statemachine|step`
   → empty). The existing E3 dispatcher (`dispatcher_handler.py`) starts the
   Standard workflow for the **v1 human-approval** path only (loads `approved`
   operation — `dispatcher_handler.py:1-46`).

Net: the gap moved from "dead code + mutually incompatible seams" (`87e8a52`) to
"correct, composable, unit-proven code that is not yet composed" (`b693c1c`).
Any claim that the runtime is *reachable* at `b693c1c` is false; it is reachable
only from tests. The checklist below is written against the **concrete bootstrap
that still must be authored**, and against the wiring that already exists so the
bootstrap does not regress it.

---

## Adversarial checklist for the evaluator/executor bootstrap

Each item is a failure mode to prove impossible (or fix) before the bootstrap is
declared done. `[b693c1c]` = already enforced at this commit; `[GAP]` = not yet
wired, must be enforced by the bootstrap.

### A. IAM / client separation (evaluator vs executor)

- **A1 `[GAP]` Evaluator role has no `gamelift:*` and no `lambda:InvokeFunction`.**
  The evaluator/dispatcher bootstrap (the thing that will construct
  `AutonomyRuntimeHandler`) may hold only DynamoDB (reservation + bundle table),
  AppConfig (switch read), and `states:StartExecution` on the one autonomous
  state-machine ARN. Assert its role policy denies GameLift and Lambda invoke.
  There is no such role today because there is no bootstrap.
- **A2 `[GAP]` Executor role remains the *sole* GameLift writer.** The only
  principal with `gamelift:UpdateFleetCapacity` must be the executor Lambda
  (`_build_runtime` builds the single `gamelift_client` —
  `executor_entry.py:214`). Prove the autonomous state machine can only
  `lambda:InvokeFunction` the executor, never call GameLift directly.
- **A3 `[GAP]` `start_execution` is a bounded Sfn client, not a boto3 catch-all.**
  The handler injects `start_execution: Any` (`handler.py:103`). The bootstrap
  must inject a client scoped to `StartExecution` on exactly the one state-machine
  ARN (mirror `dispatcher_entry.py:81` `session.client("stepfunctions", ...)`),
  never a generic session that could reach GameLift/PassRole.
- **A4 `[b693c1c]` Handler holds no provider-write surface.** Enforced by
  `test_handler_holds_no_provider_write_surface` (`update_capacity`,
  `provider_client`, `execute`, `boto3` all absent). Keep this assertion; extend
  it to the bootstrap object once it exists.
- **A5 `[GAP]` No shared client between evaluator and executor.** Two separate
  `boto3.Session`/role assumptions; a leaked executor credential into the
  evaluator would let the evaluator write. Prove construction paths are disjoint.

### B. Env fail-closed behavior (default disabled)

- **B1 `[b693c1c]` Static floor is *exactly* `operate`, not `>=`.**
  `_REQUIRED_STATIC_MODE = "operate"` and `_require_static_authority` denies any
  other mode (`autonomy_gate.py:60,256-259`). `AutonomyRuntimeSettings`
  `__post_init__` rejects flag-on-but-mode-not-operate (`autonomy_gate.py:118-131`).
- **B2 `[GAP]` Missing/blank env fails closed at bootstrap, not at first write.**
  `settings.state_machine_arn` is a `_required_str("GBAW_OPERATIONS_STATE_MACHINE_ARN")`
  (`settings.py:381`); the executor `_build_runtime` raises when
  `not settings.execute_enabled` (`executor_entry.py:207`). The **new** evaluator
  bootstrap must do the same: absent autonomy flag / switch target / bundle table
  / state-machine ARN → refuse to construct, never construct a half-wired handler
  that dispatches.
- **B3 `[b693c1c]` No implicit env reads in the hot path.** Service/store/handler
  read no env (`service.py` docstring + `__slots__ = ()`; store constructs no
  client until called). `_region()` is the only `os.environ` read
  (`executor_entry.py:167`) and defaults safely. Keep the evaluator bootstrap's
  env reads at construction only.
- **B4 `[GAP]` Default deployment provisions no autonomy control plane.** Assert
  the default CloudFormation path creates no autonomous state machine, no bundle
  table wiring, and grants no provider-write to any chat runtime (AGENTS.md
  boundary). The bootstrap must be reachable ONLY in an explicitly
  operate-enabled deployment.
- **B5 `[b693c1c]` Switch/kill-switch/durable unavailability = denial.**
  `_require_emergency_enabled` maps any switch/E4 exception to `AutonomyGateDenied`
  (`autonomy_gate.py:268-281`); `SwitchAutonomyPort` maps any switch failure to
  `AutonomySwitchDenied` (`bridges.py:47-49`).

### C. Bundle conditional persistence

- **C1 `[b693c1c]` Persist happens AFTER reserve, BEFORE StartExecution.** Order
  in `handler.dispatch`: prepare → reserve → gate → persist → dispatch
  (`handler.py:114-168`). A persist failure releases in-flight and refuses
  (`handler.py:157-161`).
- **C2 `[GAP]` The persisted bundle is exactly the reload contract.** The handler
  persists `policy, observation, decision, operation, window_state, reservation`
  (`handler.py:141-156`); the executor reloads `policy, observation, decision,
  operation, window_state` into `AutonomyExecutionEvidence`
  (`executor_entry.py:122-128`). The concrete `BundleStorePort` must round-trip
  these byte-identically (the operation's `prepared_hash` and the decision hash
  must survive marshal/unmarshal). No concrete store exists to prove this yet —
  this is the single most important missing artifact.
- **C3 `[GAP]` Bundle persistence is idempotent on `operation_id`.** A replay with
  the same deterministic `operation_id` (`service._operation_id`,
  `service.py:213-243`) must not create a divergent second bundle. Require
  `attribute_not_exists`-style conditional put, matching the reservation store's
  idempotent pattern (`store.py` reservation `attribute_not_exists(SK)`).
- **C4 `[GAP]` Persist and reserve are not atomic — prove the ordering is safe.**
  They are two separate writes. Because reserve precedes persist and any persist
  failure settles the reservation (`handler.py:157-161`), a crash between them is
  covered by C-crash items below; but a *successful* reserve + *successful*
  persist + failed dispatch must leave a reloadable bundle with a settled
  in-flight slot (see E-replay). Assert no orphan bundle without a reservation
  record and vice versa is ever *dispatched*.
- **C5 `[b693c1c]` Bundle carries no credential / no model input.**
  `AutonomyRuntimeInputs` is `slots=True` with no identity/credential field
  (`service.py:76-96`); the persisted dict is built only from those trusted
  fields. Keep the concrete store from adding any client-derived attribute.

### D. Crash at each step (before durable effect)

- **D1 `[b693c1c]` Crash before reserve → no effect.** Prepare is pure
  (`service.prepare`), no durable write; a crash leaves window state untouched.
- **D2 `[b693c1c]` Crash after reserve, before persist → in-flight leaked but
  fenced.** The reservation advanced revision +1 and took the single in-flight
  slot (`store._next_reserved`). A distinct later operation CONFLICTs on
  `in_flight != 0` (`store.reserve` guard). Recovery is by replay of the SAME
  operation_id (idempotent) then settle — not by a new operation. **`[GAP]`**: a
  crash-recovery sweeper/timeout that settles an abandoned in-flight slot does
  not exist; without it a hard crash between reserve and a never-retried dispatch
  wedges the single concurrency slot until manual settle. Prove a bounded
  recovery path (TTL/heartbeat or replay-driven settle).
- **D3 `[b693c1c]` Crash after gate, before persist → in-flight settled.** Gate
  denial and persist failure both call `_release_in_flight`
  (`handler.py:135-137,157-161`); a settle failure is swallowed and never masks
  the refusal (`handler.py:200-210`). But a *process crash* (not an exception)
  between gate and persist skips the release → same wedge as D2.
- **D4 `[GAP]` Crash after persist, before StartExecution.** A reloadable bundle
  exists but nothing was dispatched. Replay of the same operation_id must
  re-reach StartExecution (idempotent reserve returns RESERVED —
  `store.reserve` existing-record branch) and the stable execution name makes the
  Sfn start idempotent (the v1 dispatcher derives a deterministic name —
  `dispatcher_handler.py:38-45`); the autonomous bootstrap MUST use the same
  stable-name discipline. Currently the handler calls
  `start_execution({"operation_id": ...})` with no name (`handler.py:165`) — the
  injected client must supply the deterministic name, or a replay double-starts.

### E. Replay after StartExecution uncertainty

- **E1 `[GAP]` StartExecution timeout/unknown-response fails closed AND is
  replay-safe.** `handler.py:164-168` treats ANY `start_execution` exception as
  `start_execution_failed` and releases in-flight. If the start actually
  succeeded server-side but the response was lost, releasing in-flight + a later
  replay could double-dispatch unless the Sfn execution name is deterministic and
  a same-name start is `ExecutionAlreadyExists` (no-op). Require: (a) deterministic
  execution name bound to `operation_id`; (b) treat `ExecutionAlreadyExists` as
  success, not failure — the current blanket `except Exception` would wrongly
  release and refuse on a benign already-exists. This is a real bug risk in the
  bootstrap if the injected client raises `ExecutionAlreadyExists`.
- **E2 `[b693c1c]` Replay of a settled reservation does not re-reserve.**
  `store.reserve` returns RESERVED for an existing record without double-counting;
  `settle` is idempotent (`store.settle` settled-branch). A replay after a
  completed execution re-returns the existing reservation, and the executor's
  verify/execute is idempotent on already-reconciled state
  (`executor_service` `OUTCOME_RECONCILED` when already at target).
- **E3 `[GAP]` The bundle store replay matches the reservation replay.** C3 must
  hold or a replay could persist a second bundle while the reservation is
  idempotent — divergent state. Prove both idempotency keys are the same
  `operation_id`.

### F. Reservation concurrent replay

- **F1 `[b693c1c]` Concurrent identical operation_id → exactly one reserve.**
  DynamoDb store uses one `TransactWriteItems`: conditional
  `attribute_not_exists(SK)` on the reservation record + conditional window update
  fenced on `state_revision AND in_flight == 0` (`store.py` reserve). The loser
  gets `ConditionalCheckFailed` → classified as CONFLICT (not a false success,
  not UNAVAILABLE) via `_is_conditional_failure` (`store.py`), and a concurrent
  replay resolves to the recorded reservation.
- **F2 `[b693c1c]` Two DIFFERENT operations cannot both reserve.** `in_flight`
  bounded to 1 by schema and the `in_flight != 0` guard; the second CONFLICTs.
- **F3 `[b693c1c]` Transient errors are UNAVAILABLE, not CONFLICT.**
  `_TRANSIENT_REASONS` (TransactionConflict/Throttling/ProvisionedThroughput/
  Validation) are excluded from the conditional-failure classification
  (`store.py`), so a retryable fault does not masquerade as a permanent conflict.
- **F4 `[b693c1c]` Same operation_id, different action/state = forgery → CONFLICT.**
  Both stores reject a mismatched `logical_action_id`/`state_id` for an existing
  record (`InMemory` reserve existing-branch; `DynamoDb` reserve record-branch).
- **F5 `[GAP]` The direction derivation feeding anti-oscillation is trusted.** The
  handler derives `change_direction` from the prepared desired-capacity delta
  (`handler.py:178-187`), and the gate-path adapter derives it from
  `decision["change"]["direction"]` (`bridges.py:66-72`). Prove these two derivation
  sites agree for the same operation, or the flip counter can differ between the
  dispatch-time reserve and any gate-time reserve.

### G. Settle failure

- **G1 `[b693c1c]` Settle failure never masks the handoff outcome.**
  `execute_autonomous` swallows a settle exception in `finally` after a
  best-effort call (`executor_entry.py:134-145`); `handler._release_in_flight`
  swallows likewise (`handler.py:200-210`).
- **G2 `[b693c1c]` Settle retains budget/frequency, releases only concurrency.**
  `_next_released` advances revision +1, sets `in_flight = 0`, and leaves
  `window_micro_usd`/`writes_in_window` untouched (`store.py`). A failed write
  still costs its footprint — no rollback that would permit budget evasion by
  forcing failures.
- **G3 `[b693c1c]` Settle is idempotent and conditional.** DynamoDb settle is a
  `TransactWriteItems` fenced on `state_revision AND in_flight == 1` plus a
  `settled = false` guard on the reservation record; a concurrent double-settle
  resolves to settled (`store.py`).
- **G4 `[GAP]` A settle that never runs (crash) is recoverable.** Same wedge as
  D2/D3: the always-settle is in a `finally`, but a process kill skips it. The
  bootstrap needs a bounded reconciliation for a stranded `in_flight == 1`.

### H. Switch cache / durable fence (dispatch vs execute phase)

- **H1 `[b693c1c]` Two independent switch consultations.** Pre-dispatch:
  `require_pre_dispatch` → static + `_require_emergency_enabled("dispatch")`
  (`autonomy_gate.py:184-187`). Pre-write: `require_pre_provider_write` re-checks
  static + `_require_emergency_enabled("execute")` + evaluator + reserve
  (`autonomy_gate.py:214-249`). A switch flipped between the two denies the write.
- **H2 `[GAP]` The dispatch-phase check uses the E4 *cached* kill-switch, the
  write-phase uses the durable intent.** `_require_emergency_enabled` calls
  `kill_switch_gate.require_phase(phase)` then
  `durable_control_gate.require_phase(phase, deployed_decision=decision)`
  (`autonomy_gate.py:273-280`). The handler receives a bare `_PreDispatchGate`
  (`handler.py:83`, `require_pre_dispatch` only); it does NOT build the composite.
  The bootstrap must construct `AutonomyRuntimeGate` with BOTH the cached
  kill-switch gate (dispatch phase) and the durable control gate, mirroring the
  executor's `_build_runtime` (`executor_entry.py:234-253`). If the bootstrap
  omits either source, "default disabled" and the double-fence silently weaken —
  the exact risk the prior review flagged.
- **H3 `[b693c1c]` The switch is the *separate* autonomy switch, distinct from the
  E4 kill switch.** `bridges.SwitchAutonomyPort` wraps `AutonomySwitchGate`
  (`autonomy_switch.py`), folded independently of `kill_switch_gate`
  (`autonomy_gate.py:268-280`). Prove the bootstrap resolves a *fresh* switch read
  each dispatch/write (no long-lived cached "enabled").
- **H4 `[GAP]` Emergency `is_engaged` (dispatch.py) vs the composite gate.** The
  prior review's "two switch shapes" fix routes everything through the composite
  gate; `dispatch.EmergencyDisablement.is_engaged` still exists as a separate
  protocol (`dispatch.py:60-70`). Confirm the bootstrap uses the composite gate
  (H2) and does NOT reintroduce a second, independently-toggled emergency source
  via `build_dispatch_envelope`, or a single flip again fails to stop both paths.

### I. v2 / v1 route confusion

- **I1 `[b693c1c]` Route selection is by contract version, deterministic.**
  `select_execution_path` returns `AUTONOMOUS` for the v2 operation and
  `HUMAN_APPROVAL` for v1 (`test_operations_autonomy_execution_selection_unit.py`
  47-93). `[GAP]`: the production `executor_entry.handler` never calls it — it
  hard-codes the v1 path (`executor_entry.py:284-291`). The bootstrap must branch
  on `select_execution_path(reloaded_operation)` and dispatch to
  `execute_autonomous` vs the v1 `service.execute`.
- **I2 `[b693c1c]` v1 verifier is untouched by v2.** `ExecutionVerifier.verify`
  still requires `remediate` authority + a granted human approval
  (v1 semantics preserved); the v2 verifier is the *additive*
  `AutonomyExecutionVerifier`. 510 v1/executor regression tests remain green.
- **I3 `[b693c1c]` A v2 operation cannot traverse the v1 path.** The v1 entry
  requires `state == "approved"` and a human approval bound by
  `prepared_operation_hash`; a v2 operation has `APPROVED_AUTONOMOUS` and no human
  approval, so it is refused, not silently executed under v1. **But** this means
  today a dispatched v2 operation would be *rejected* by the live executor entry
  (I1) — not mis-executed. Prove the bootstrap routes v2 to `execute_autonomous`
  so it is neither rejected nor run under v1.
- **I4 `[GAP]` A v1 operation cannot traverse the v2 path.** Symmetric: the
  autonomous state machine must only be started for v2 operations
  (`operation_contract_version` v2). The evaluator handler only ever prepares v2
  (`service.prepare`), so the dispatch side is safe by construction; assert the
  Sfn/executor rejects a v1 operation_id arriving on the autonomous machine.
- **I5 `[b693c1c]` `required_execution_authority` differs by path.** v2 sets
  `operate` (`service._build_operation` → `REQUIRED_EXECUTION_AUTHORITY`); v1
  requires `remediate`. The additive verifier keys on the v2 value; confirm no
  cross-contamination.

### J. Secrets / logging

- **J1 `[b693c1c]` No secret material in the runtime objects.** No credential is
  captured by service/store/handler (`__slots__` audited; no client until called).
- **J2 `[b693c1c]` Store logs type only, never body.** `DynamoDbReservationStore`
  logs `exception_type=%s` on UNAVAILABLE, never the item/policy/observation
  (`store.py` reserve/settle warnings). Keep the concrete bundle store to the same
  discipline (log `operation_id` + exception type, never policy/observation).
- **J3 `[b693c1c]` Identifier-only dispatch — no payload leakage.** `StartExecution`
  receives `{"operation_id": ...}` alone (`handler.py:165`,
  `dispatch.DispatchEnvelope.payload`); no policy/limits/observation/window/
  credential crosses the boundary. `test_happy_path` asserts
  `set(start.calls[0]) == {"operation_id"}`.
- **J4 `[b693c1c]` No model/request-body input anywhere on the path.**
  `AutonomyRuntimeInputs`/`ReservationRequest` are `slots=True`; unknown kwargs
  raise `TypeError` (service.py, store.py docstrings + dataclasses). Prove the
  bootstrap feeds only server-owned trusted inputs (policy resolved server-side,
  one canonical E1 observation, the six authority inputs, the automation
  principal, strict window state) and never a browser/model value.
- **J5 `[GAP]` Bootstrap error surfaces are safe.** The v1 entry re-raises
  `ExecutorServiceError` with a sanitized message (`executor_entry.py:294-299`).
  The new evaluator handler must not surface policy/limits/ARNs in the error it
  returns to its caller or emits to logs.

---

## Highest-leverage findings (fix before declaring #439 done)

1. **Author the concrete `BundleStorePort` (C2/C3)** and prove byte-identical
   round-trip of the six documents plus idempotency on `operation_id`. It is the
   only load-bearing artifact with zero implementation today.
2. **Author the two bootstraps and route by contract version (I1)** — the
   evaluator/dispatcher composition (handler + real gate + DynamoDb reservation +
   bundle store + bounded Sfn client) and the executor entry's v2 branch
   (`select_execution_path` → `execute_autonomous`). Until then the whole v2 path
   is test-only and a dispatched v2 op would be *rejected* by the live executor.
3. **Deterministic Sfn execution name + `ExecutionAlreadyExists` handling
   (D4/E1).** The handler's blanket `except Exception` on `start_execution` will
   wrongly release-and-refuse on a benign already-exists and can double-dispatch
   without a stable name.
4. **Bounded recovery for a stranded `in_flight == 1` (D2/D3/G4).** Always-settle
   lives in `finally`/exception handlers, which a process crash skips; the single
   concurrency slot then wedges until manual settle.
5. **Compose the full double-fence in the dispatch gate (H2/H4)** — cached
   kill-switch (dispatch phase) + durable intent + separate autonomy switch. The
   handler takes a bare `_PreDispatchGate`; a bootstrap that omits a source
   silently weakens "default disabled."
