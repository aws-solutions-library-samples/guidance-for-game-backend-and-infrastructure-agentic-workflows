# Issue #439 E5 — Final Semantic Review (runtime base `05c13e5`)

Read-only review of the four final findings. No source was edited. Each finding
was re-derived by reading the exact blobs at `05c13e5`
(`feat(operations): Wire E5 module Lambda handler, policy loader, dispatch audit`,
on `feat/439-e5-runtime`, forked from mainline at `21f8935`), and reproduced by
an independent, no-AWS probe: `semantic-review/probe_439_last_findings.py`
(`python3 semantic-review/probe_439_last_findings.py`; exits non-zero and prints
one line per probe). The probe imports nothing from the runtime and makes no AWS
or network call.

Invariants respected throughout: identifier-only workflow, static + AppConfig +
E4 gates, generation fencing, immutable evidence, v1 human path, sole executor
writer.

## Verdicts

| # | Finding (as stated) | Verdict | Race / false-success edge |
|---|---------------------|---------|----------------------------|
| 1 | reservation `sweep_expired` has no production owner/caller; a crash permanently wedges state | **Confirmed** | Permanent `in_flight=1` wedge (liveness), not a false success |
| 2 | `_best_effort_audit` swallows requested/dispatched failures; executor does not require dispatch evidence | **Confirmed** | **False-success**: SUCCEEDED terminal with no provable dispatch predecessor |
| 3 | verifier expiry runs before Describe; final hook checks no decision/window time, clock can cross deadline before write | **Confirmed** | **Race → false-success**: write issued against an expired decision/window |
| 4 | policy loader checks hash but not configured id/version | **Does not hold at `05c13e5`** | No live edge; one theoretical, low-risk hardening only |

## Finding 1 — reservation `sweep_expired` has no owner

`operations/autonomy_runtime/store.py`: both `InMemoryReservationStore.sweep_expired`
(~L446) and `DynamoDbReservationStore.sweep_expired` (~L799) are correct in
isolation — they never reclaim a live lease, release only budget/frequency-neutral
concurrency, bump the generation to fence the stale holder, and fail closed on any
conditional failure or unavailability.

The defect is that **nothing in production calls them.** A whole-tree scan at
`05c13e5` finds `sweep_expired` referenced only at its two definition sites; the
only similarly named production symbol is the unrelated E4 control-plane freshness
sweeper (`control/…`, `put_expiry_sweep_expired`), which operates on the durable
gate, not the reservation window. So a crash between `reserve` and the executor's
`finally`-settle leaves `in_flight = 1` forever. With concurrency bounded to 1,
every subsequent reservation for that `state_id` is fenced out and bounded
autonomy is permanently wedged. `sweep_expired` is dead recovery code.

Probes: `probe_f1_no_sweeper_wedges_state` (reproduces the wedge with 0 owners),
`probe_f1_sweep_is_otherwise_correct` (shows the body is fail-closed).

**Fix shape:** give the method a production owner — a scheduled/entry sweeper that
enumerates expired-lease reservations and calls `sweep_expired` before a new
reserve, or an opportunistic reclaim on the reserve path guarded by
`lease_not_after <= now`. Any owner must keep the existing generation fence so a
returning stale holder cannot settle. No new false-success risk: the body already
refuses live leases.

## Finding 2 — swallowed audit + no enforced dispatch-evidence

`operations/autonomy_runtime/evaluator_entry.py`: `_dispatch_with_audit` (~L644)
writes `record_dispatch_requested` **before** StartExecution and `record_dispatched`
**after** a confirmed dispatch, both through `_best_effort_audit` (~L691), which is
`try/except Exception: pass`. A silently-dropped `record_dispatch_requested` leaves
a dispatched operation with no durable "requested" predecessor.

The compounding problem is on the executor side. `execute/executor_service.py`
`record_execution_result(…, expected_state="dispatched", …)` (~L508) *reads* as if
it gates on a prior dispatched state, but in `execute/execution_store.py` the
`state_change_item` is written with **`attribute_not_exists(SK)` only** (~L257);
`expected_state` is persisted as the data field `previous_state` and is **never a
transaction condition**. The only real fences in that transaction are the lease
generation (`lease_fence`) and new-SK idempotency. The single durable writer of a
`dispatched` *state* is the same best-effort dispatch **audit** (`store.py` ~L1105,
phase `"dispatched"`); the v1 dispatcher (`dispatcher_handler.py` ~L140) only puts
`"state":"dispatched"` in the HTTP 202 body and never persists a state row.

Net: a SUCCEEDED terminal can be committed with **no provable dispatch
predecessor** — a false success. Probes: `probe_f2_audit_swallow_and_no_evidence_gate`,
`probe_f2_expected_state_is_not_a_gate`.

**Fix shape (two parts):**
1. Make the `approved → dispatched` transition a durable, fenced write and make the
   executor's terminal commit *conditional* on that dispatched-state row existing
   (compound `ConditionExpression`, not a data field), so the executor genuinely
   requires dispatch evidence.
2. Keep the audit best-effort **only** for the truly non-gating record; do not let
   `record_dispatch_requested` be the sole proof of a state transition. Note the
   edge: tightening the executor precondition must not deadlock the legitimate
   retry/idempotent-replay path — the condition should accept both `dispatched`
   and the modeled `retry_pending → dispatched` re-entry from `versions.py`.

## Finding 3 — expiry checked before Describe, not before the write

`operations/autonomy_execution_verifier.py` `verify()` snapshots
`now = self._clock()` **once** at entry (~L253) and checks decision freshness
(step 6, `decision_expires_at <= now`, ~L376) and window freshness
(step 6b, `now >= window_expires_at`, ~L397) against that single snapshot, then
checks the autonomy switch and reservation ownership and returns the plan.

The executor (`executor_service._run`, ~L255) then performs the
**Describe-before-write** round-trip and only afterward runs the `pre_write_hook`,
which re-checks the autonomy switch and reservation ownership **but not
decision/window time**. Between the verifier's snapshot and the actual
`UpdateFleetCapacity`, the wall clock advances across the entry kill-switch check,
lease acquisition, the unbounded-latency Describe, the second kill-switch check,
and the hook. A decision/window `expires_at` that was valid at `verify()` can be
crossed during Describe, and nothing re-asserts it before the write fires — a race
that yields a false success against a stale decision. The still-live reservation
lease does not encode the decision deadline (and per Finding 1 is never swept), so
it cannot catch this.

Probes: `probe_f3_deadline_crossed_during_describe` (reproduces the stale write),
`probe_f3_fix_shape_reads_clock_once_more` (fix closes it).

**Fix shape:** in the `pre_write_hook` (the last gate, after Describe, immediately
before the single write), re-snapshot the clock and re-assert both
`decision_expires_at` and `window_expires_at`. This is race-free precisely because
it is the last gate and reads a fresh clock; it adds no false-failure for an
unexpired decision. This does not relax the sole-writer or generation-fence
invariants.

## Finding 4 — policy loader id/version enforcement

**The literal finding does not hold at `05c13e5`.** `DynamoDbAutonomyPolicyLoader.load`
(`evaluator_entry.py` ~L372) fetches by `PK = AUTZPOLICY#{policy_id}#{policy_version}`,
so a wrong configured id/version simply returns nothing ("no policy is sealed for
the configured id/version"). It then enforces `document.policy_hash == configured
hash` (fail closed on drift) and re-validates the frozen contract (which binds the
hash to the canonical bytes). The caller (`evaluate`, ~L512) passes the server-owned
`settings.autonomy_policy_{id,version,hash}` — never event/model input. So id and
version *are* enforced (as the fetch key) in addition to the hash.

Probe `probe_f4_id_version_are_enforced_via_pk` confirms correct load, wrong-version
block, and wrong-hash block.

**One residual, low-risk edge** (`probe_f4_residual_pk_body_consistency_edge`):
`load` re-validates that the stored body's `policy_hash` binds its own canonical
bytes but does not separately assert that the stored body's `policy_id`/`policy_version`
equal the PK components used to fetch it. In the normal flow this cannot diverge —
`seed` derives the PK from the same body — so it is only reachable via an
out-of-band write. Recommendation: add a cheap defensive check that the loaded
body's `policy_id`/`policy_version` equal the requested ones. This is hardening,
not the finding as stated, and introduces no false-success.

## Summary of race / false-success analysis

- **Findings 2 and 3 are genuine false-success edges** (a terminal SUCCEEDED write
  with no dispatch predecessor; a provider write against an expired decision/window)
  and the proposed fixes close them without weakening the sole-writer, generation-
  fence, or gate invariants — provided the Finding-2 executor precondition still
  admits the modeled `retry_pending → dispatched` re-entry so it does not create a
  new false-failure/deadlock.
- **Finding 1 is a liveness (permanent-wedge) defect**, not a false success; its fix
  is additive (a caller) and reuses the existing fail-closed body.
- **Finding 4, as worded, is already mitigated**; only a minor defensive assertion is
  advisable.
