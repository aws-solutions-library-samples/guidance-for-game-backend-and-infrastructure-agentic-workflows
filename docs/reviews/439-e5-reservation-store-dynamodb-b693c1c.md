# Issue #439 — E5 reservation store: DynamoDB-transaction specialist review

Read-only review of commit `b693c1c` (`feat(operations): Wire identifier-only E5
runtime dispatch handler`). No source, infrastructure, or AWS mutation was made
and nothing was pushed. Scope is the durable DynamoDB reservation lifecycle that
`AutonomyRuntimeHandler` depends on. Evidence is cited as `path:line` at this
commit. Line numbers are from `git show b693c1c:<path>`.

Primary artifact under review:
`backend/src/operations/autonomy_runtime/store.py` (`DynamoDbReservationStore`,
`InMemoryReservationStore`, `_next_reserved`, `_next_released`).
Cross-referenced against the mature E3 store
`backend/src/operations/execute/execution_store.py`
(`DynamoDbExecutionStore`) — the same team's established lease/idempotency
template — and the handler `backend/src/operations/autonomy_runtime/handler.py`.

## What is correct today (so fixes do not regress it)

The store already gets the core conditional-write shape right, and the fixes
below must preserve these properties:

- **`reserve` is a single `TransactWriteItems`** that conditionally `Put`s the
  reservation record (`attribute_not_exists(SK)`) and conditionally `Update`s the
  window fenced on `state_revision = :expected AND in_flight = :zero`
  (`store.py:521`–`556`). No `Scan`, no unconditional `PutItem`.
- **Revision advances by exactly one** and re-binds `state_hash`
  (`_next_reserved`, `store.py:198`–`221`), so a false success cannot desync the
  hash.
- **Fail-closed classification** distinguishes a genuine conditional failure
  (→ `CONFLICT`) from transient errors (`TransactionConflict`, throttling,
  `ProvisionedThroughputExceeded`, `ValidationError` → `UNAVAILABLE`) via
  `_is_conditional_failure` (`store.py:388`–`402`). This is the right default.
- **`settle` retains budget/frequency** and returns only the concurrency slot
  (`_next_released`, `store.py:224`–`236`) — the conservative choice.
- The store holds **no provider-write surface** (asserted by
  `test_store_never_scans`, `test_dynamodb_store.py:266`).

The five defects below are about the *edges*: replay races, crash recovery,
settle ambiguity, the unbuilt bundle store, and audit truthfulness.

---

## Finding 1 — Concurrent idempotent replay: the idempotency read is not part of the fence (TOCTOU)

**Severity: High. Fail-closed, but can spuriously `CONFLICT` a legitimate replay
and — more importantly — the in-memory reference store does not model the real
race, so the contract is "proven" against a model that cannot fail.**

### Observation

`DynamoDbReservationStore.reserve` decides idempotency with a *non-transactional*
`get_item` before the transaction:

```
record = self._get(self._reservation_pk(request.operation_id), _RESERVATION_SK)
if record is not None:
    if record.get("logical_action_id") != request.action_id:
        return ReservationResult(ReservationOutcome.CONFLICT)
    return ReservationResult(ReservationOutcome.RESERVED, window_state=stored)   # store.py:497-501
```

Then it builds `advanced = _next_reserved(stored, request)` and commits the
transaction with the reservation `Put` fenced on `attribute_not_exists(SK)`
(`store.py:543`).

Two replays of the same `operation_id` that arrive concurrently both read
`record is None`, both compute `advanced` from the same `stored` revision, and
both submit the transaction. The `attribute_not_exists(SK)` condition on the
reservation `Put` means exactly one wins; the loser raises a conditional failure
and is classified as `CONFLICT` (`store.py:559`–`565`). That is safe (no double
count) but **wrong for a replay**: the second call is the *same operation* and
the contract says a replay "returns the existing reservation and NEVER double
counts" (`store.py:15`–`17`). The loser instead gets `CONFLICT`, which the
handler surfaces as `reservation_conflict` and refuses
(`handler.py:127`–`130`) — a legitimate retry is dropped.

### Why the tests do not catch it

`test_reserve_is_idempotent_on_operation_id` (`test_dynamodb_store.py:192`)
exercises only the **serial** replay: first `reserve` records the item, the
second `reserve` sees it via `_get` and returns `RESERVED` without a second
transaction. The concurrent interleaving (both read `None`, both commit) is never
constructed. `InMemoryReservationStore.reserve` (`store.py:274`) has the same
check-then-act structure and, being single-threaded, cannot exhibit it either —
so the "reference implementation that makes the fail-closed contract provable"
(`store.py:41`–`44`) does not actually model this concurrency edge.

### Exact fix

Make the transaction itself resolve the replay, and treat the reservation
`Put`'s conditional failure as *the idempotency signal*, not a generic conflict:

1. Keep the pre-read as a fast path only. After a conditional failure on the
   transaction, **re-read the reservation record by `operation_id`** and branch:
   - record exists and `logical_action_id == request.action_id` → return
     `RESERVED` with `self.current(state_id)` (idempotent replay won the race for
     someone; we converge on it);
   - record exists with a different `logical_action_id` → `CONFLICT` (forgery /
     id collision, matching the existing `store.py:499`);
   - record absent → the *window* fence lost (a different operation advanced the
     revision or holds the slot) → `CONFLICT`.

   This mirrors exactly what the E3 store already does on lease contention:
   `acquire_execution_lease` catches the conditional failure and re-reads both
   the recorded result and the existing lease before deciding
   (`execution_store.py:143`–`160`). The reservation store should adopt the same
   "conditional-failure → re-read → converge" pattern.

2. Strengthen the reservation `Put` condition so a replay with a *different
   action* cannot overwrite: `attribute_not_exists(SK) OR logical_action_id =
   :action`. Combined with idempotent `_next_reserved` this keeps a true replay a
   no-op at the item level even if it reaches the transaction.

### Exact tests to add (`test_operations_autonomy_runtime_dynamodb_store_unit.py`)

- `test_reserve_replay_after_concurrent_commit_returns_reserved`: seed window;
  call `reserve` once to record the reservation; then drive a second `reserve`
  whose transaction is forced to raise `_conditional_failure()` on the
  reservation `Put` (extend `_FakeDynamo` with a `fail_put_once` toggle), and
  assert the result is `RESERVED` (re-read convergence) with `in_flight == 1` and
  **no** revision advance beyond the first commit.
- `test_reserve_window_fence_lost_without_reservation_is_conflict`: force the
  transaction conditional failure while the reservation record is absent; assert
  `CONFLICT` and `window_state is None`.
- `test_reserve_same_operation_different_action_is_conflict`: pre-seed a
  reservation record with a different `logical_action_id`; assert `CONFLICT`
  (locks `store.py:499` behaviour against regression once the condition changes).

---

## Finding 2 — Reservation TTL / reclaim: a crash between `reserve` and `settle` wedges the state forever (no fail-safe recovery)

**Severity: Critical for liveness. The concurrency slot is a hard single-flight
lock with no expiry and no reclaim path — a single crashed dispatch permanently
disables autonomy for that `state_id`.**

### Observation

The reservation takes the single in-flight slot (`in_flight -> 1`,
`_next_reserved`, `store.py:205`) and the *only* thing that returns it is an
explicit `settle` that flips `in_flight -> 0` (`_next_released`,
`store.py:224`–`236`). The reservation item (`AUTZRSV`) carries only
`operation_id`, `logical_action_id`, `state_id`, `settled`
(`store.py:517`–`524`) — **no `ttl`, no `lease_not_after`, no `generation`.**

`reserve` also enforces `in_flight != 0 → CONFLICT` (`store.py:507`, and the
DynamoDB fence `in_flight = :zero`, `store.py:530`). So once a reservation is
taken, every future `reserve` for that `state_id` fails until a `settle` runs.

The handler drives `settle` only on the *in-process* failure paths
(gate/persist/StartExecution, `handler.py:137,160,167` via `_release_in_flight`).
If the process crashes after `reserve` succeeds and before any of those — or
after `StartExecution` but the Step Functions execution is lost/never settles —
`in_flight` stays `1` **forever**. There is no sweeper, no TTL, no generation to
fence past. `autonomy_runtime` has zero references to `ttl`/`reclaim`/`expire`
(confirmed by grep across the package). This is a permanent, silent liveness
outage of the whole autonomy feature for that window state.

### Why this is a "future infrastructure will wire it" gap

The docstring claims the store "makes the pure evaluator's reading safe under
concurrency" and that a "lost, superseded, or already-settled reservation fails
closed" (`store.py:20`–`23`) — but there is no mechanism by which a reservation
becomes *lost*. Nothing expires it. The safety story implicitly relies on "some
Step Functions / sweeper will always eventually settle," which is exactly the
residual gap the task forbids.

The correct template already exists in the same codebase: the E3 lease is
**TTL-bounded and generation-fenced**. `acquire_execution_lease` writes `ttl =
lease_epoch_s` on the transient lease item *only* (`execution_store.py:137`–`138`),
and `record_execution_result` fences the commit on the held `generation`
(`execution_store.py:241`–`251`) so a writer whose lease was reclaimed after
expiry fails closed as `PRECONDITION_FAILED`. The reservation slot needs the same
shape.

### Exact fix

Give the in-flight reservation a lease deadline and a reclaim generation, mirror
of the E3 lease:

1. **Add lease fields to the reservation write.** In `_next_reserved`, stamp the
   window with `in_flight_lease_not_after = request.now_epoch_seconds +
   policy.reservation_lease_seconds` and an `in_flight_generation` that increments
   on each *fresh* reservation. Add `reservation_lease_seconds` to the policy /
   window-state contract (`contracts/autonomy.py`) with a validated positive
   bound.
2. **Allow reclaim in `reserve`'s window fence.** Replace the hard
   `in_flight = :zero` condition with a disjunction that also permits taking the
   slot when the current lease has expired:
   `(in_flight = :zero) OR (in_flight = :one AND in_flight_lease_not_after <
   :now)`. On the reclaim branch, advance `in_flight_generation` and rewrite the
   reservation record (or write a superseding one) so the abandoned holder is
   fenced out. This is a durable, deterministic reclaim — not a background
   sweeper — so it needs no new control plane.
3. **Fence `require`/`settle` on the generation.** `require` (called immediately
   before the provider write) must confirm the *current* `in_flight_generation`
   still equals the generation the caller reserved; a reclaimed reservation
   fails closed (`ReservationStoreError`), exactly like E3's
   `PRECONDITION_FAILED`. Today `require` only checks `in_flight == 1`
   (`store.py:562`–`566`), which a reclaiming successor would also satisfy — so a
   stale holder could pass `require` after being reclaimed. This must change with
   the generation fence.
4. **Do NOT put `ttl` on the window snapshot itself.** The window state is the
   durable accounting record and must never be TTL-expired (same rule the E3 store
   states: audit/state records "never carry a `ttl`", `execution_store.py:24`).
   The lease deadline is a *field compared in a condition*, not a DynamoDB
   `TimeToLiveSpecification`. Only genuinely transient items may use the frozen
   `ttl` attribute (`settings.py:80`–`82`).

If a full lease/generation change is deemed out of scope for this commit, the
minimum honest alternative is to **document the liveness dependency explicitly**
and add a deterministic reclaim in `reserve` keyed on `as_of_epoch_seconds`
staleness — but the generation fence on `require` is required either way to keep
the pre-write check truthful.

### Exact tests to add

- `test_reserve_reclaims_expired_in_flight_slot`: seed a window with
  `in_flight == 1` and `in_flight_lease_not_after` in the past; a distinct
  operation's `reserve` succeeds, advancing `in_flight_generation`.
- `test_reserve_does_not_reclaim_live_in_flight_slot`: same but lease in the
  future → `CONFLICT`.
- `test_require_fails_closed_after_reclaim`: reserve as op A; reclaim as op B;
  `require(op_A)` raises `ReservationStoreError` even though `in_flight == 1`.
- `test_settle_by_reclaimed_holder_is_precondition_failed`: op A `settle` after
  op B reclaimed the slot does not release op B's slot (generation fence).

---

## Finding 3 — Settlement conditional ambiguity: a conditional failure is unconditionally read as "already settled"

**Severity: High. `settle` can report success (`RESERVED`) after a fence it did
not actually satisfy, releasing/【mis-reporting】 a slot that a different writer
owns.**

### Observation

`DynamoDbReservationStore.settle` runs a two-item transaction: the window update
fenced on `state_revision = :expected AND in_flight = :one`
(`store.py:589`–`592`) and the reservation update fenced on
`attribute_exists(SK) AND settled = :false` (`store.py:598`–`601`). On any
conditional failure it does:

```
if _is_conditional_failure(exc):
    # A concurrent settle already released the slot; treat as settled.
    return ReservationResult(ReservationOutcome.RESERVED, window_state=self.current(state_id))  # store.py:606-609
```

The comment assumes the *only* way this transaction fails conditionally is "a
concurrent settle already released the slot." That is not the only cause:

- **Window revision moved for another reason.** Between the `self.current(state_id)`
  read (`store.py:582`) and the transaction, any writer that advances
  `state_revision` (e.g. a reclaim per Finding 2, or a future co-located writer)
  fails the `state_revision = :expected` fence — while `settled` is still
  `False`. The code then returns `RESERVED` and re-reads a snapshot that may
  **still show `in_flight == 1`**, i.e. it reports the slot settled when it is
  not. The handler's `_release_in_flight` swallows this and proceeds to report a
  refusal reason unrelated to settlement (`handler.py:205`–`217`), but a direct
  caller (the executor's terminal-handoff settle) would believe the slot was
  released.
- **Split fence outcomes are not distinguished.** The window fence and the
  reservation fence can fail independently. `settled = :false` failing means
  "already settled" (benign, idempotent); `state_revision`/`in_flight` failing
  means "the world moved" (not benign). Collapsing both into one `RESERVED`
  return loses that distinction. `settle` has no return value for "the slot is
  not mine to release" — it only has `RESERVED`/`UNAVAILABLE` (and raises
  `ReservationStoreError` for a missing/mismatched record via `_find_reservation`,
  `store.py:618`–`630`).

The in-memory reference store cannot surface this either: its `settle` re-reads
its own single dict and flips `settled` unconditionally after checking the record
(`store.py:334`–`356`); there is no revision race.

### Exact fix

Disambiguate the conditional failure by re-reading state before declaring
success:

1. After catching `_is_conditional_failure`, re-read **both** the reservation
   record and the window:
   - reservation `settled is True` → genuine idempotent double-settle → return
     `RESERVED` with the current window (preserve today's idempotency,
     `store.py:344`–`346` / `test_dynamodb_store.py:237`).
   - reservation `settled is False` **and** window `in_flight == 0` → someone
     else released it; converge → `RESERVED`.
   - reservation `settled is False` **and** window `in_flight == 1` → **the fence
     lost against a live/other holder** → return a new
     `ReservationOutcome.CONFLICT` (add handling in the handler; a settle conflict
     must not be mistaken for a release). Do **not** return `RESERVED`.
2. Split the transaction's semantics in the classifier if needed: inspect
   `CancellationReasons` positionally (index 0 = window, index 1 = reservation) so
   the code can tell *which* fence failed rather than guessing. `_is_conditional_failure`
   already parses `CancellationReasons` (`store.py:381`–`385`); extend a sibling
   helper to return the per-item reason list.
3. Make `settle` fence on the reservation `generation` from Finding 2 as well, so
   a reclaimed holder's settle is a clean `CONFLICT` rather than an accidental
   release of the successor's slot.

### Exact tests to add

- `test_settle_conflict_when_revision_moved_and_not_settled`: seed reservation
  `settled=False`, then mutate the window `state_revision` in `_FakeDynamo`
  before `settle` so the window fence fails; assert the result is `CONFLICT` (new)
  and the window still shows `in_flight == 1`.
- `test_settle_idempotent_when_already_settled_via_record`: reservation record
  `settled=True`, window `in_flight==0`; conditional failure resolves to
  `RESERVED` (locks current idempotency).
- `test_settle_converges_when_slot_released_by_other`: reservation
  `settled=False` but window `in_flight==0`; resolves to `RESERVED`.

---

## Finding 4 — Immutable bundle storage: the reload bundle is an unimplemented Protocol (a literal "future infrastructure will wire it" gap)

**Severity: High for the stated no-residual-gap bar. There is no concrete,
write-once bundle store — only a `Protocol` and a test fake — so the immutability
and reload contract the executor depends on is unspecified and unenforced.**

### Observation

`BundleStorePort` is declared as a `typing.Protocol` in `handler.py:75`–`89`,
with a single `persist_bundle(...)` method. The handler calls it at step 4
(`handler.py:145`–`158`) and, on failure, releases the in-flight slot and refuses
(`handler.py:159`–`161`). But:

- There is **no `DynamoDbBundleStore`** (or any concrete implementation) anywhere
  in `backend/src` at this commit — grep for `persist_bundle` / `class .*Bundle`
  finds only the Protocol and `handler.py`. The only implementation is
  `_FakeBundleStore` in `test_operations_autonomy_runtime_handler_unit.py:90`,
  which just appends kwargs to a list.
- The port's contract says nothing about **immutability**: no `attribute_not_exists`
  write-once condition, no idempotent-on-`operation_id` requirement, no hash
  binding. The executor "relies on the durable bundle" and reloads strictly by
  `operation_id` (`executor_service.py:52`, `:222`), so if the real bundle store
  ever overwrites or races, the executor could reload a *different* bundle than
  the one that was reserved and gated — defeating the whole hash-bound chain.

This is precisely the residual gap the task prohibits: the dispatch path is
"wired," but the durable object the dispatch promises the executor can reload is
not actually built or specified.

### Exact fix

1. **Add a concrete `DynamoDbBundleStore`** in
   `backend/src/operations/autonomy_runtime/store.py` (or a new
   `bundle_store.py`) that writes the bundle as **write-once** in a single
   `TransactWriteItems` / conditional `PutItem`:
   - key `PK=OP#<operation_id>, SK=AUTZBUNDLE` on the existing operations table
     (`GBAW_OPERATIONS_TABLE_NAME`, `settings.py:312`) — the same "06" operations
     table, not a new one;
   - condition `attribute_not_exists(SK)` so a bundle is never overwritten;
   - store the canonical bundle as one JSON `document` plus a `bundle_hash`
     (reuse the `_canonical` + `hashlib` approach at `store.py:435`);
   - on conditional failure, **re-read and compare `bundle_hash`**: identical hash
     → idempotent replay, treat as success; different hash → raise
     `ReservationStoreError` (a forged/racing bundle must fail closed, never
     overwrite).
   - carry **no `ttl`** — the bundle is a durable audit/reload record, same rule
     as E3 result records (`execution_store.py:24`).
2. **Tighten `BundleStorePort` docstring** to require write-once,
   idempotent-on-`operation_id`, hash-bound, no-overwrite semantics, so the
   contract is explicit rather than implied.
3. **Order persist before reserve-visible dispatch is fine, but** consider making
   the bundle `Put` part of the *reserve* transaction (single atomic write of
   reservation + bundle) to remove the window where a reservation exists without a
   reloadable bundle. If kept separate (as today), the handler's release-on-persist
   -failure (`handler.py:159`) is the correct compensating action and must be kept.

### Exact tests to add (`test_operations_autonomy_runtime_bundle_store_unit.py`, new)

- `test_persist_bundle_is_write_once`: second `persist_bundle` with a *different*
  body for the same `operation_id` raises `ReservationStoreError`; the stored
  document is unchanged.
- `test_persist_bundle_idempotent_same_hash`: second `persist_bundle` with the
  identical body is a no-op success (no second overwrite).
- `test_persist_bundle_never_writes_ttl`: assert the marshalled item has no `ttl`
  attribute.
- `test_bundle_uses_operations_table_and_op_key`: assert `PK == OP#<op>` /
  `SK == AUTZBUNDLE` on the configured table name.

---

## Finding 5 — Truthful state / audit records: the reservation lifecycle emits no ledger/state-change record, and settle records nothing about the terminal reason

**Severity: Medium-High. The reservation store advances accounting state but
writes no durable audit trail of *why* the slot moved, and `settle` accepts a
`terminal` reason it silently discards — so the durable record cannot later prove
whether a slot was released by success, failure, or reclaim.**

### Observation

Contrast with E3, which writes an explicit `state_change_item` and `ledger_item`
in the same fenced transaction as the result
(`execution_store.py:216`–`238`), so the audit log is atomic with the accounting
change and records `previous_state`, `state`, `event_type`, `recorded_at`,
`outcome`.

The reservation store writes **only** the window snapshot and the reservation
record. Specifically:

- `settle` takes a `terminal: str` argument (`store.py:188`, `:334`;
  `handler.py:215` passes `"failed"`, the E3-style callers pass `"succeeded"`)
  but **never persists it**. `_next_released` (`store.py:224`) drops it entirely;
  the reservation record only flips `settled -> True` (`store.py:596`). After the
  fact, the durable state cannot distinguish "slot released because the write
  succeeded" from "released because the gate denied" from "released because the
  dispatch crashed." The `terminal` field is a truthfulness promise the store
  does not keep.
- There is **no ledger event** for `reserve` or `settle`. The window's
  `state_revision` advances, but nothing records the causal event
  (`operation.autonomy_reserved` / `operation.autonomy_settled`) the way E3 emits
  `operation.execution_recorded` (`execution_store.py:230`–`237`). The task
  requires "truthful state/audit records"; a revision counter with no event log is
  not an audit trail.
- The service persists an `APPROVED_AUTONOMOUS` authorization and the docstring is
  careful to "never a human approval" (`store.py`/`service.py:9`, commit message).
  That truthfulness is correct at the *decision* layer — but the *reservation*
  layer, which is where money/frequency is actually consumed, has no matching
  durable event.

### Exact fix

1. **Persist `terminal` on settle.** Extend `_next_released` to stamp the window
   (or better, the reservation record) with `terminal_reason` and
   `settled_at_epoch_seconds`, and include those in the `settle` transaction. This
   is a small, contained change and makes the record truthful about *why* the slot
   returned.
2. **Emit ledger + state-change items atomically**, mirroring E3:
   - in `reserve`'s transaction add a `LEDGER#AUTZ#<operation_id>` item
     (`event_type = operation.autonomy_reserved`, `state_revision`,
     `action_micro_usd`, `recorded_at`) with `attribute_not_exists(SK)`;
   - in `settle`'s transaction add `LEDGER#AUTZSETTLE#<operation_id>`
     (`event_type = operation.autonomy_settled`, `terminal_reason`,
     `state_revision`) with `attribute_not_exists(SK)`.
   Because these are in the *same* `TransactWriteItems` as the fenced window
   update, the ledger cannot diverge from the accounting change — the exact
   property E3 relies on. Use a deterministic, injected clock for `recorded_at`
   (E3 does this precisely for testable audit timestamps,
   `execution_store.py:183`–`184`); the reservation store currently has no clock
   and would otherwise be non-deterministic.
3. **Do not add `ttl`** to any of these audit items (durable records rule).

### Exact tests to add

- `test_settle_persists_terminal_reason`: settle with `terminal="failed"` writes
  a durable `terminal_reason == "failed"`; a later read reflects it.
- `test_reserve_emits_reserved_ledger_event_atomically`: the `reserve`
  transaction includes the ledger `Put`; if the window fence fails, the ledger
  item is **not** written (assert `_FakeDynamo` recorded no partial write — the
  transaction is all-or-nothing).
- `test_settle_emits_settled_ledger_event_with_reason`.
- `test_audit_items_never_carry_ttl`.

---

## Constraint check (task guardrails)

- **Default remains disabled / no AWS calls in tests.** Confirmed: constructing
  `DynamoDbReservationStore` "captures no credential and performs no I/O until a
  method is called" (`store.py:47`–`50`) and the tests inject a `_FakeDynamo`
  (`test_dynamodb_store.py:98`) — no `boto3`/`moto`/network. All fixes above keep
  this: they add conditions/items to existing fake-driven transactions only.
- **Evaluator role: DynamoDB/AppConfig/StepFunctions only, never GameLift or
  Lambda invoke.** All proposed changes stay within DynamoDB transactions and the
  identifier-only `StartExecution`; none touch a provider client. The store has no
  provider surface (`test_dynamodb_store.py:266`) and the fixes add none.
- **Existing executor remains the sole GameLift writer.** No fix moves a write
  into the reservation store; Finding 2's `require` generation-fence strengthens
  the pre-write check the executor already performs, and Finding 4's bundle store
  only makes the executor's reload deterministic.
- **Exactly `operation_id` to Step Functions.** Unchanged; dispatch envelope stays
  identifier-only (`handler.py:170`).
- **06 table, code-owned policy, strict server-owned inputs.** Finding 4 keeps the
  bundle on the same `GBAW_OPERATIONS_TABLE_NAME` operations table; no new table.
  `ReservationRequest` remains identifier-and-integer-only (`slots=True`,
  `store.py:109`–`117`).
- **Never fabricate human approval.** No change to the decision layer; the
  reservation layer records `APPROVED_AUTONOMOUS` provenance only. Finding 5 adds
  autonomy-specific ledger events, not approval records.

## Priority

1. **Finding 2 (Critical liveness)** — no reclaim/TTL means one crash permanently
   disables autonomy for a `state_id`. This is the load-bearing gap.
2. **Finding 3 (settle ambiguity)** and **Finding 1 (replay TOCTOU)** — both can
   mis-report an outcome; both are fixed by the same "conditional-failure →
   re-read → converge, else CONFLICT" pattern E3 already uses.
3. **Finding 4 (bundle store)** — closes the literal unimplemented-Protocol gap.
4. **Finding 5 (audit truthfulness)** — makes the durable record honest about why
   state moved.

No edits were made to any source, test, or infrastructure file. This document is
the deliverable.
