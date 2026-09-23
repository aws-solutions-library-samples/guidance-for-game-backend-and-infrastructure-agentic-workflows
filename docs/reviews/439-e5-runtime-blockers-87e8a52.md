# Issue #439 — E5 bounded-autonomy runtime: combined-review blocker analysis

Read-only review of commit `87e8a52` (`feat(operations): Add E5 bounded-autonomy
runtime gate and settings`). No source, infrastructure, or AWS mutation; no push.
Evidence is cited as `path:line` at that commit.

## Verdict

The v2 bounded-autonomy runtime at `87e8a52` is **not a coherent runtime**: it is
a set of well-formed modules plus per-module tests, but **no code path reaches
any of it**, and the three seams that would have to fit together (gate ↔
reservation store, gate ↔ executor, dispatch ↔ emergency switch) are defined with
**mutually incompatible interfaces** that are each proven only against a private
fake. The acceptance bar ("existing executor reloads and verifies v2 … existing
ExecutorService remains sole GameLift writer and checks E4 + autonomy +
reservation immediately before write") **cannot be met without editing the v1
executor/verifier**, which this commit's own scope note ("Restricts edits to new
gate/settings modules and tests") forbade. That contradiction is the root cause of
every blocker below.

## Reachability: the whole v2 path is dead code

Across `backend/src` at `87e8a52`, nothing constructs or calls the runtime:

- No caller of `AutonomyRuntimeGate(...)`, `require_pre_dispatch`,
  `require_pre_provider_write`.
- No caller of `AutonomyRuntimeService.prepare(...)` — the only `.prepare(` calls
  are the **v1** human path (`approval_handler.py:193`,
  `prepare_orchestrator.py:234`).
- No instantiation of `ReservationStore` / `InMemoryReservationStore`.
- No caller of `build_dispatch_envelope` / `DispatchEnvelope.authorize_provider_write`.
- `executor_entry.py` wires only the E4 kill-switch + durable gate; it does **not**
  resolve autonomy settings, build the autonomy switch, or inject a reservation
  port (grep for `autonomy` in `settings.py`/`executor_entry.py` is empty).
- No Standard Step Functions state machine / ASL exists under `infrastructure/`
  for an `operation_id`-only autonomous invoke.

So "deterministic v2 prepare", "atomic replay-safe reserve before dispatch", and
"operation_id-only Standard Step Functions" exist as **types**, not as a runtime.

## The seams do not fit — three reservation shapes, two switch shapes

### Reservation port — 2 incompatible callable shapes + a 3rd emergency shape

1. `autonomy_runtime/store.py` — the **real** atomic port:
   `ReservationStore.reserve(request: ReservationRequest) -> ReservationResult`.
   `ReservationRequest` is `slots=True` and accepts **identifiers + integers only**
   (`policy_ref`, `state_id`, `expected_revision`, `operation_id`,
   `action_micro_usd`, `now_epoch_seconds`, `change_direction`); it advances the
   window-state revision by exactly one and returns `RESERVED/CONFLICT/UNAVAILABLE`.
2. `autonomy_gate.py` — the port the gate actually calls:
   `ReservationPort.reserve(**kwargs) -> bool`, invoked with **full bodies**
   `policy=, observation=, requested=, window_state=, now_epoch_seconds=, decision=`
   (`autonomy_gate.py:283`+). A real `ReservationRequest` would raise `TypeError`
   on every one of those kwargs, and a `bool` is not a `ReservationResult`.
3. `autonomy_runtime/dispatch.py` — a **third** pre-write control:
   `EmergencyDisablement.is_engaged() -> bool` +
   `DispatchEnvelope.authorize_provider_write(emergency=...)`, unrelated to the
   reservation and unwired to anything.

The gate's own "integration" test never closes this gap: it builds a hand-rolled
`_Reservation.reserve(**kwargs) -> True`
(`test_operations_autonomy_gate_integration_unit.py:104-153`) rather than the real
`InMemoryReservationStore`. The store's test
(`test_operations_autonomy_runtime_store_unit.py`) only ever calls
`store.reserve(ReservationRequest(...))`. **Both suites are green because each
tests a different `reserve`.** They can never be composed.

### Switch / emergency-disablement — 2 divergent interfaces

- Gate path: `_SwitchGatePort.require_enabled()` (raises `AutonomySwitchUnavailable`)
  folded with E4 `require_phase(...)`.
- Dispatch path: `EmergencyDisablement.is_engaged() -> bool`.

Two different contracts for "is autonomy emergency-disabled", neither wired to the
other, so a single flip cannot be proven to stop both dispatch and write.

## Verifier is hard-bound to v1 — "executor verifies v2" is currently impossible

`execution_verifier.py:verify()` (the code `ExecutorService` calls, and the only
thing the acceptance's "existing executor reloads and verifies v2" can mean) is
structurally a **v1 human-approval** verifier:

- `validate_capacity_prepared_operation(prepared)` — v1 schema. A v2
  `gamelift-capacity-autonomous-operation` (`operation_contract_version`,
  `autonomy_policy`, `authority.decision`, `automation_principal`,
  `executor_binding`, `decision_expires_at`, `autonomous_prepared_hash`) fails
  integrity immediately.
- `prepared["required_execution_authority"] != "remediate"` → deny
  (`execution_verifier.py:201`). v2 sets it to **`operate`**
  (`contracts/autonomy.py:124`, fixtures line 106).
- Requires a **granted human approval** bound by `prepared_operation_hash` +
  `policy_version` (`execution_verifier.py:~224`). v2 has **no** human approval
  (`APPROVED_AUTONOMOUS`); `executor_entry.handler` also rejects any state other
  than `approved`.
- Reads v1-only fields `prepared["requester"]`, `["future_executor_binding"]`,
  `["expires_at"]`, `capacity_prepared_hash` — none of which exist on v2.

`ExecutorService.execute` also has **no** autonomy gate and **no** reservation
parameter; its pre-write checks are only the E4 kill-switch + durable gate
(`executor_service.py:_require_execute_phase`). So "checks E4 + autonomy +
reservation immediately before write" is unimplemented in the writer.

## Ownership / settlement / freshness gaps

- **No `approved → dispatched` owner.** `ExecutorService` commits with
  `expected_state="dispatched"` (`executor_service.py:428`) and
  `executor_entry` only admits `approved`. The valid graph
  (`contracts/versions.py:45-46`) is `approved → dispatched → …`, but **no
  component performs `approved → dispatched`** for the autonomous path, and that
  transition is not composed with the atomic reserve. The reserve
  (`store.py._commit`: revision+1, `in_flight += 1`) is durable accounting only;
  nothing settles/releases `in_flight` on failure, timeout, or replay → the
  "atomic replay-safe reserve before dispatch" and "release in-flight" bars are
  unmet.
- **No execute-time window freshness re-check.** `require_pre_provider_write`
  re-runs the evaluator and reserves, but neither it nor the executor re-reads
  the durable window state's freshness/expiry at write time against a fenced
  revision; freshness is only enforced on the AppConfig switch document, not on
  the window-state the reservation fences.

## Smallest additive seam that satisfies acceptance without touching v1

Do **not** widen `ExecutionVerifier`/`ExecutorService` in place (that changes v1
semantics and breaks the scope note). Instead:

1. **One shared reservation contract.** Delete the gate's `ReservationPort`
   `**kwargs->bool`. Make `AutonomyRuntimeGate` depend on the store's
   `ReservationStore` and build a `ReservationRequest` **inside** the gate from
   the hash-bound decision it just computed (derive `action_micro_usd`,
   `change_direction`, `policy_ref`, `state_id`, `expected_revision`,
   `operation_id` from trusted inputs). Treat any non-`RESERVED` as fail-closed.
   This collapses seams (1) and (2) into one and lets the real store be the test
   double.
2. **One emergency shape.** Have `dispatch.build_dispatch_envelope` and
   `DispatchEnvelope.authorize_provider_write` consult the *same* composite gate
   (`require_pre_dispatch` / the switch+E4 fold), removing the separate
   `EmergencyDisablement.is_engaged` protocol — or make the gate implement it.
3. **A thin v2 verify adapter, additive.** Add a separate
   `AutonomousExecutionVerifier` (new module) that validates the v2 operation via
   the frozen `validate_autonomous_operation_binding` and re-derives the same
   `VerifiedExecutionPlan`/`logical_action_id` shape the existing
   `ExecutorService` already consumes. Inject it into `ExecutorService` via a new
   **optional** `verifier`-compatible port (constructor already takes injected
   collaborators), so v1 keeps its exact `remediate`+granted-approval verifier and
   v2 gets an `operate`+`APPROVED_AUTONOMOUS` verifier — **no change to v1
   verify() logic**. The executor stays the sole GameLift writer.
4. **Compose reserve with the state transition.** Perform the atomic
   `approved → dispatched` ownership write in the **same** conditional/transaction
   family as the reservation (fenced on `state_revision` + operation state), and
   settle/release `in_flight` on every terminal/failure/timeout/replay branch.
5. **Execute-time freshness.** In `require_pre_provider_write`, re-read the fenced
   window state and reject on stale revision/expiry immediately before reserve.
6. **Provision the operation_id-only Standard Step Functions** state machine (IaC,
   later track) as the only principal allowed to invoke the executor.

## Exact compatibility tests the acceptance requires (currently missing)

1. **Real gate ↔ real store composition:** build `AutonomyRuntimeGate` with the
   real `InMemoryReservationStore` (not `_Reservation`) and assert
   `require_pre_provider_write` reserves exactly once, advances the revision by 1,
   increments `in_flight`, and that a second identical call **CONFLICTs** (replay
   safety). This is the test that would fail today and expose seams (1)/(2).
2. **Executor verifies a v2 operation:** feed a valid
   `gamelift-capacity-autonomous-operation` fixture through the executor's reload
   + verify + describe-before-write path and assert exactly one
   `UpdateFleetCapacity`, plus fail-closed on `operate`-authority absence. Fails
   today (verifier rejects v2).
3. **Single emergency flip stops both gates:** one switch/emergency source flipped
   → both `require_pre_dispatch` and `authorize_provider_write` (or its
   replacement) deny. Fails today (two shapes).
4. **v1 immutability regression:** the existing
   `test_operations_executor_service_unit`, `_kill_switch_unit`,
   `_execution_verifier` suites must remain byte-for-byte green after the additive
   v2 verifier is injected — proving v1 human path is unchanged.
5. **In-flight release on every failure/timeout/replay branch:** assert `in_flight`
   returns to its prior value on `PROVIDER_ERROR`, `STATE_DRIFT`,
   `RESULT_INCONCLUSIVE`, `VERIFICATION_FAILED`, and replay.
6. **Execute-time window-freshness denial:** a stale/revision-moved window state
   denies at `require_pre_provider_write` before any reserve.

## Unsafe assumptions in the requested implementation

- **"existing executor verifies v2" assumes the v1 verifier is polymorphic — it is
  not.** Forcing v2 through `ExecutionVerifier.verify` requires loosening the
  `remediate` + granted-approval + v1-schema checks, which silently weakens the v1
  human path. The additive-adapter route (seam 3) is the only way to keep v1
  semantics intact.
- **"proven" by the current integration test is an illusion.** The gate's
  integration test's `_Reservation` fake guarantees green while hiding the
  signature/return-type incompatibility with the real store. Any acceptance that
  trusts existing green tests is trusting isolated fakes.
- **Reserve ≠ dispatch ownership.** Advancing window accounting is assumed to be
  the same as taking exclusive `approved → dispatched` ownership; it is not.
  Without composing them atomically, two workers can both reserve-then-dispatch,
  or a crash between reserve and dispatch leaks `in_flight` with no settlement.
- **Freshness on the switch document is assumed to cover the window state.** It
  does not; the reservation fences `state_revision`, but nothing re-checks
  window-state expiry at write time.
- **Default-off is assumed structural but is only enforced by env + `__post_init__`
  today.** Because nothing wires the gate, "default disabled" is currently
  vacuously true; once wired, `AutonomyRuntimeSettings`/switch/E4/durable must all
  intersect (they are designed to) — the risk is wiring that omits one source.
