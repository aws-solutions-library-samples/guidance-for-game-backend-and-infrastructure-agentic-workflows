# Issue #439 E5 — Last Two Blockers (read-only review at base `f5fc25d`)

Read-only analysis. **No source was edited.** Every fact below was re-derived by
reading the exact blobs at `f5fc25d`
(`fix(operations): Close final E5 autonomy semantic-review findings`). File/line
references are to the blobs at that commit.

The base already contains the four prior fixes reviewed at `05c13e5`
(reservation `sweep_state` owner, mandatory dispatch audit, pre-write re-verify,
policy id/version binding). This review covers only the **two remaining
blockers**, states the exploit/replay probes for each, and specifies fix shapes
that preserve every E5 invariant: identifier-only action binding, immutable
bundle, the unchanged v1 human-approval path, and no provider writes on replay.

---

## Blocker 1 — retry after a settled success is refused by the live reservation check *before* the E3 execution-store replay

### Confirmed behavior at `f5fc25d`

The v2 autonomous path runs in this order (`execute/executor_entry.py`
`execute_autonomous`, ~L123-127):

```
plan   = verifier.verify(operation_id=..., evidence=<reloaded bundle>)   # step 1
result = service.execute_verified(invocation, plan=plan, ...)            # step 2
```

Step 1 (`autonomy_execution_verifier.py` `AutonomyExecutionVerifier.verify`)
ends with a **live** reservation-ownership call, confirmed *last, immediately
before returning the plan* (L438-445):

```python
# 8. Atomic reservation ownership, confirmed LAST — immediately before the write.
self._reservation.require_reservation(operation_id=operation_id, logical_action_id=action_id)
```

Step 2 (`execute/executor_service.py` `_run`, L261-270) is where the **E3
execution-store replay** lives:

```python
acquisition = self._store.acquire_execution_lease(...)   # -> execution_store.py
if acquisition.recorded_result is not None:
    return acquisition.recorded_result                   # terminal-result replay
```

and `execution_store.py` `acquire_execution_lease` (L120-122) replays first:

```python
recorded = self.load_recorded_result(logical_action_id)
if recorded is not None:
    return LeaseAcquisition(generation=_INITIAL_GENERATION, recorded_result=recorded)
```

**The defect:** on a first, successful v2 execution the executor settles the
in-flight reservation in `execute_autonomous`'s `finally`
(`_settle_reservation(..., terminal="succeeded", generation=1)`), which sets the
reservation record `settled=True` (`autonomy_runtime/store.py`
`InMemoryReservationStore.settle`, L432 / `require`, L398-399). A **retry** of
the same operation (Step Functions re-invokes the executor on the same
`operation_id`; the execution name is deterministic, `_execution_name`) re-enters
`execute_reloaded` → `execute_autonomous` → **`verifier.verify` first**. That
verify calls `require_reservation`, the reservation is already `settled`, so
`require` raises → `RESERVATION_NOT_OWNED`. `verify` raises **before**
`execute_verified`/`_run` is ever reached, so `load_recorded_result` — which
would return the recorded terminal `SUCCEEDED` and make the retry idempotent —
**never runs**.

This is exactly the stated blocker: *"a retry currently verifies live reservation
before E3 execution-store replay and fails."* It is a **liveness/idempotency**
defect (a legitimate retry of an already-succeeded operation is refused), not a
false-success. The generation fence (settle at generation 1, reclaim bumps to 2)
is correct and must be preserved.

### Fix shape

Permit **only** an exact, validated terminal-result replay to short-circuit
*before* the live reservation check on the v2 path — and nowhere else:

1. In `execute_reloaded`/`execute_autonomous`, before `verifier.verify`, consult
   the **E3 execution store's own** recorded terminal result for this exact
   `logical_action_id` (derived, as today, from the immutable
   `operation_id` + `prepared_hash` via `contracts.execution.logical_action_id`).
   Reuse `ExecutionStore.load_recorded_result(logical_action_id)` — do **not**
   introduce a second result source.
2. If — and only if — that store returns a non-`None`, schema-valid **terminal**
   result document, return it unchanged and skip the live reservation `require`
   and the provider path entirely (this is a pure read; no write, no Describe).
3. Otherwise (result is `None`, or the recorded document is nonterminal /
   malformed) fall through to the **unchanged** `verifier.verify` →
   `execute_verified` path, which still performs the live reservation check, the
   pre-write re-verify, and generation fencing exactly as today.

Why this is safe against forgery and cannot bypass live checks for nonterminal
attempts:

- **No forge-able input.** The replayed document is read only from
  `EXEC#<logical_action_id>/RESULT`, which is written **exclusively** by
  `record_execution_result` inside the single generation-fenced, immutable
  (`attribute_not_exists(SK)`) transaction under a held lease. There is no other
  writer and no request-body/model path to that row, so a replay can only return
  a result the executor itself already committed for this exact identifier.
- **Identifier-only binding preserved.** The lookup key is the derived
  `logical_action_id`; nothing from the invocation payload other than
  `operation_id` participates, and the payload still carries only `operation_id`.
- **Nonterminal attempts are unaffected.** A first attempt (no RESULT row) and
  any in-flight/failed-without-record attempt see `recorded_result is None` and
  take the full live path — the live reservation `require`, the pre-write
  re-verify at the current clock, and generation fencing all still fire. The
  shortcut is reachable only when a terminal result already exists.
- **No provider write on replay.** The replay returns the stored document; it
  issues no `Describe` and no `UpdateFleetCapacity`.
- **Immutable bundle / v1 path untouched.** The bundle is still loaded immutably;
  the v1 human-approval branch (`reload_store.load_for_execution`) is not on this
  code path and is unchanged.

Note: today's replay at the lease layer (`_run`, L269) is correct *once reached*;
the fix simply moves an **equivalent, read-only** terminal-result check to before
the verifier's live reservation call, so a settled-then-retried success replays
instead of being refused.

### Exploit / replay probes for Blocker 1

- **P1a — regression (the blocker):** reserve → execute v2 to `SUCCEEDED`
  (records RESULT, settles reservation at gen 1) → re-invoke `execute_reloaded`
  with the same `operation_id`. *Before fix:* raises `RESERVATION_NOT_OWNED` from
  `verify`. *After fix:* returns the byte-identical recorded `SUCCEEDED` result;
  asserts no second `UpdateFleetCapacity`, no `Describe`, and the reservation
  stays settled at gen 1 (no re-settle side effect).
- **P1b — forged-result replay is impossible:** attempt to make the shortcut fire
  with a fabricated result by (i) supplying a result-shaped field in the
  invocation payload and (ii) writing an arbitrary item at a non-`RESULT` SK.
  Assert the shortcut reads only `EXEC#<action_id>/RESULT`, that payload fields
  never reach it, and that with no committed RESULT row the call takes the full
  live path (no replay).
- **P1c — nonterminal attempt is not shortcut:** first attempt with no RESULT row
  must run the full `verify` (live `require_reservation`) and pre-write hook;
  assert the live reservation check still executes and a lost/superseded
  reservation still fails closed.
- **P1d — reclaimed-generation fence preserved:** after an expired-lease reclaim
  bumps the reservation to generation 2 with terminal `reclaimed` (not a recorded
  execution RESULT), a retry must **not** replay a success — `load_recorded_result`
  returns `None`, so the full path runs and the gen-1 ownership check fails
  closed. Confirms the shortcut keys on the execution RESULT, never on the
  reservation terminal token.
- **P1e — malformed/nonterminal recorded document:** seed a `RESULT` row whose
  document is nonterminal or unparseable; assert the shortcut declines it
  (`load_recorded_result` returns `None` / the terminal-check rejects) and the
  full live path runs rather than replaying a partial result.

---

## Blocker 2 — `dispatched` audit is an independent Put and the executor checks only it

### Confirmed behavior at `f5fc25d`

The evaluator writes two audit rows as **independent** conditional puts
(`autonomy_runtime/store.py` `DynamoDbAutonomyBundleStore`):

- `record_dispatch_requested` → SK `AUTZDISPATCH#dispatch_requested`
- `record_dispatched`         → SK `AUTZDISPATCH#dispatched`

Both go through `_record_dispatch_audit` (L1183-1215), each a single `PutItem`
with **`ConditionExpression="attribute_not_exists(SK)"`** on *its own* SK. There
is **no condition tying `dispatched` to the existence of a matching
`dispatch_requested`** (same `operation_id`, same `execution_name`).

The executor's guard `_require_dispatched_audit` (`execute/executor_entry.py`
L419-439) loads **only** the `dispatched` phase and checks
`record.get("phase") == "dispatched"`. It never loads or cross-checks
`dispatch_requested`.

**The defect:** the "requested → dispatched" bracket is only enforced by the
evaluator's in-process control flow (`_dispatch_with_audit` writes requested
before `dispatch`, dispatched after). At the **store layer** the two rows are
independent, and the **executor** trusts a lone `dispatched` row. Any path that
lands a `dispatched` row without a matching `dispatch_requested` — a partial
/ out-of-order write, a store-level replay that re-creates only the `dispatched`
row, or a future caller that skips the requested step — is accepted by the
executor as a proven dispatch predecessor. This is the false-success surface:
a v2 execution proceeding to verify/Describe/write on an unproven dispatch.

### Fix shape

Make `dispatched` **atomically conditional on a matching `dispatch_requested`**,
and require **both** rows at the executor:

1. **Store — atomic predecessor condition.** Write `record_dispatched` in a
   `transact_write_items` with two items:
   - a `ConditionCheck` on `AUTZDISPATCH#dispatch_requested` requiring
     `attribute_exists(SK)` **and** the stored `execution_name` equal to the one
     being dispatched (`#en = :en`), and
   - the `Put` of `AUTZDISPATCH#dispatched` conditioned on
     `attribute_not_exists(SK)` (unchanged immutability of its own row).

   Both succeed or neither is written. A `dispatched` row can then exist only
   atop a matching `dispatch_requested`. Keep the existing idempotent
   reconciliation: a byte-identical re-`record_dispatched` whose predecessor
   still matches resolves cleanly (re-read on conditional failure and return when
   the stored `dispatched` bytes equal the canonical form), so a legitimate
   evaluator retry after an ambiguous StartExecution is idempotent, not a
   hard failure.

2. **Executor — require both.** In `_require_dispatched_audit`, after confirming
   the `dispatched` row, also load `dispatch_requested` and require it to exist
   with the **same `execution_name`** (and `operation_id`). A missing or
   mismatched `dispatch_requested` fails closed with no verify, no Describe, no
   provider write — the same fail-closed shape the function already uses.

This keeps `record_dispatch_requested` first and unconditioned (it legitimately
exists before any `dispatched`), and adds the ordering constraint precisely where
it was missing: the `dispatched` write and the executor's precondition.

### Race-safety and idempotency of the predecessor condition (DynamoDB)

- **Race-safe by construction.** DynamoDB evaluates all conditions in a
  `transact_write_items` atomically against a single committed view: the
  `dispatched` `Put` and the `dispatch_requested` `ConditionCheck` cannot observe
  a torn state. A concurrent writer that has not yet committed
  `dispatch_requested` causes the transaction to fail (predecessor
  `attribute_exists` unmet) rather than silently creating an orphan `dispatched`.
  Two concurrent `record_dispatched` calls contend on the `dispatched`
  `attribute_not_exists(SK)` put; exactly one wins, the loser gets a conditional
  failure and reconciles idempotently against the identical stored bytes.
- **Idempotent.** Both audit rows are immutable single-writer records keyed by a
  deterministic `execution_name` (`operation_id[:80]`). A replay of the same
  intent re-derives the same `execution_name`, so re-`record_dispatched` sees its
  own byte-identical row (reconcile → return) and the predecessor still matches.
  A **differing** `execution_name` for the same `operation_id` fails the
  predecessor `#en = :en` check and fails closed — it never overwrites and never
  orphans.
- **Classification unchanged.** Reuse the existing `_is_conditional_failure`
  classifier (bare `ConditionalCheckFailedException` and
  `TransactionCanceledException` with a `ConditionalCheckFailed` reason and no
  transient reason) so a genuine conditional refusal is distinguished from a
  transient/unavailable error, which still raises `AutonomyBundleStoreError` and
  refuses+releases upstream.

### Exploit / replay probes for Blocker 2

- **P2a — orphan `dispatched` is refused (store):** attempt `record_dispatched`
  with **no** prior `dispatch_requested`. *Before fix:* the lone `Put` succeeds.
  *After fix:* the transaction fails the predecessor `ConditionCheck`, nothing is
  written, and the caller refuses.
- **P2b — orphan `dispatched` is refused (executor):** seed only a `dispatched`
  row (simulating a partial/forged write) and invoke `execute_reloaded`.
  *Before fix:* `_require_dispatched_audit` accepts it and proceeds to verify.
  *After fix:* the missing `dispatch_requested` fails closed — no verify, no
  Describe, no `UpdateFleetCapacity`.
- **P2c — execution-name mismatch is refused:** seed `dispatch_requested` with
  `execution_name=A` and attempt `record_dispatched` / executor check with
  `execution_name=B`. Assert both the store predecessor condition and the
  executor cross-check fail closed.
- **P2d — idempotent replay succeeds:** with a matching `dispatch_requested`
  present, call `record_dispatched` twice with the identical `execution_name`;
  assert the second call reconciles to success (no raise, no duplicate) and the
  executor accepts both-rows-present.
- **P2e — concurrent dispatched contention:** two `record_dispatched` calls for
  the same `(operation_id, execution_name)` over a matching predecessor; assert
  exactly one commit, the other reconciles idempotently, and never an orphan or
  overwrite.
- **P2f — nonterminal / denied path unaffected:** a denied decision records
  neither row and never dispatches; assert the executor still fails closed for
  that `operation_id` (no `dispatched`, no `dispatch_requested`), i.e. the fix
  does not create any new accept path for unproven dispatches.

---

## Invariants preserved by both fixes

| Invariant | How preserved |
|---|---|
| Identifier-only action binding | Replay keys on derived `logical_action_id`; audit keys on deterministic `execution_name`; invocation still carries only `operation_id`. |
| Immutable bundle | Neither fix writes the bundle; both are reads / append-only immutable audit rows. |
| v1 human path | Untouched — the v1 branch (`reload_store.load_for_execution`) is not on either changed path. |
| No provider writes on replay | Blocker-1 replay returns a stored document with no `Describe`/`UpdateFleetCapacity`; Blocker-2 fix only gates dispatch evidence. |
| Generation fencing | Blocker-1 shortcut keys on the execution RESULT (not the reservation terminal), so a reclaimed (gen-2) operation is not replayed as success; the full path still fences at gen 1. |
| Fail-closed on nonterminal / missing evidence | Both fixes fall through to the existing fail-closed live path when the terminal result is absent or the dispatch predecessor is missing/mismatched. |

## Probe harness note

Both probe sets can be reproduced with an independent, no-AWS harness in the
style of `semantic-review/probe_439_last_findings.py` (in-memory fakes for the
execution store, reservation store, and bundle/audit store; no runtime import of
AWS clients; no network). P2a/P2d/P2e are expressible against an in-memory store
that models `transact_write_items` all-or-nothing semantics and per-SK
`attribute_not_exists`/`attribute_exists` conditions.
