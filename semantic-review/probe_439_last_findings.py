"""Independent, no-AWS probe logic for issue #439 E5 final semantic-review findings.

Runtime base under review: 05c13e5
    ``feat(operations): Wire E5 module Lambda handler, policy loader, dispatch audit``
    (on branch feat/439-e5-runtime, forked from mainline at 21f8935).

PURPOSE
-------
This module is a *read-only review artifact*. It does NOT import the operations
runtime and does NOT touch AWS. Each probe re-implements — from scratch and by
hand — the exact decision logic that the runtime executes at 05c13e5 for one of
the four findings, then asserts the observable defect the finding names. The
point is to demonstrate the defect is real (or, for finding 4, to show where the
literal wording does *not* hold and where a residual edge does) without trusting
the runtime's own code or tests.

Every probe is a plain function returning a ``ProbeResult``. ``main`` runs them
all and prints a table. No pytest, no network, no boto3, no clock other than an
injected integer/float. Pure Python stdlib.

HOW THE LOGIC WAS TRANSCRIBED
-----------------------------
The reconstructed snippets below mirror the following source spans at 05c13e5:

  * Finding 1  operations/autonomy_runtime/store.py
                 InMemoryReservationStore.sweep_expired  (~L446)
                 DynamoDbReservationStore.sweep_expired   (~L799)
               plus the whole-tree caller scan (only control/ E4 sweeper names
               ``sweep_expired``; the reservation method has NO production caller).
  * Finding 2  operations/autonomy_runtime/evaluator_entry.py
                 _dispatch_with_audit (~L644) + _best_effort_audit (~L691)
               operations/execute/executor_service.py
                 record_execution_result(expected_state="dispatched")  (~L508)
               operations/execute/execution_store.py
                 record_execution_result state_change_item put uses only
                 ``attribute_not_exists(SK)`` — ``expected_state`` is stored as a
                 data field, never enforced as a precondition  (~L164-L260)
               operations/execute/dispatcher_handler.py
                 the v1 dispatcher starts the workflow but never durably writes
                 state="dispatched"  (~L138, L217-L230)
  * Finding 3  operations/autonomy_execution_verifier.py
                 verify(): single ``now = self._clock()`` at entry, decision +
                 window expiry checked against it (steps 6/6b), switch +
                 reservation last; NO expiry re-check in the final pre-write
                 hook  (~L253-L456)
               operations/execute/executor_service.py
                 _run(): Describe-before-write happens AFTER verification and
                 BEFORE the pre_write_hook; the hook re-checks switch+reservation
                 only  (~L255-L320)
  * Finding 4  operations/autonomy_runtime/evaluator_entry.py
                 DynamoDbAutonomyPolicyLoader.load(policy_id, policy_version,
                 policy_hash): fetches by PK(id,version) AND enforces stored
                 policy_hash == configured hash; caller passes server-owned
                 settings  (~L372-L413, L512-L516)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------- #
# Probe harness
# --------------------------------------------------------------------------- #
@dataclass
class ProbeResult:
    finding: str
    name: str
    # True  => the runtime is SAFE on this exact point (no defect reproduced).
    # False => the probe REPRODUCED the defect / race the finding names.
    safe: bool
    detail: str


# --------------------------------------------------------------------------- #
# Finding 1: reservation sweep_expired has no production owner; a crash between
#            reserve and settle wedges in_flight forever.
# --------------------------------------------------------------------------- #
class _ReservationWindowModel:
    """Minimal transcription of the in-memory reservation window fencing.

    Mirrors InMemoryReservationStore: a window carries ``in_flight`` (0/1) and a
    monotonically advancing ``state_revision``; a reservation carries a bounded
    ``lease_not_after`` and a ``generation``. ``reserve`` sets in_flight=1;
    ``settle`` (or ``sweep_expired``) releases it. There is NO scheduled task in
    the runtime that calls ``sweep_expired``.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.state_revision = 0
        self.settled = False
        self.lease_not_after: Optional[int] = None
        self.generation = 1

    def reserve(self, *, lease_not_after: int) -> None:
        if self.in_flight != 0:
            raise AssertionError("double reserve (should be fenced)")
        self.in_flight = 1
        self.state_revision += 1
        self.settled = False
        self.lease_not_after = lease_not_after

    def settle(self) -> None:
        # Normal terminal path (executor finally-settle).
        self.in_flight = 0
        self.state_revision += 1
        self.settled = True

    def sweep_expired(self, *, now_epoch_seconds: int) -> bool:
        # Faithful to store.py: only reclaims a bounded, expired, still-held lease.
        if self.in_flight == 0 or self.settled:
            return False
        if self.lease_not_after is None or self.lease_not_after > now_epoch_seconds:
            return False
        self.in_flight = 0
        self.state_revision += 1
        self.settled = True
        self.generation += 1
        return True


def probe_f1_no_sweeper_wedges_state(sweeper_callers: int) -> ProbeResult:
    """Reproduce: crash after reserve, no caller ever invokes sweep_expired.

    ``sweeper_callers`` models the number of PRODUCTION owners that invoke the
    reservation store's ``sweep_expired``. At 05c13e5 a whole-tree scan finds
    ZERO (only the unrelated E4 control-plane freshness sweeper shares the
    *name*). We pass 0 to reflect the runtime as-is.
    """
    w = _ReservationWindowModel()
    w.reserve(lease_not_after=100)  # evaluator reserves the slot
    # ---- process crashes here, between reserve and the executor's finally-settle ----
    # A recovery sweeper WOULD reclaim once the lease passes, but nothing calls it:
    now = 10_000  # long past lease_not_after=100
    reclaimed_by_someone = False
    for _ in range(sweeper_callers):
        reclaimed_by_someone = w.sweep_expired(now_epoch_seconds=now) or reclaimed_by_someone

    still_wedged = (w.in_flight == 1 and not w.settled)
    if sweeper_callers == 0 and still_wedged:
        return ProbeResult(
            "F1", "no-owner sweep => permanent in_flight wedge", safe=False,
            detail=(
                "sweep_expired is correct in isolation but has NO production "
                "caller at 05c13e5; after a reserve/settle crash in_flight stays "
                "1 forever, so every future reserve for this state_id is fenced "
                "out (concurrency=1) and autonomy is permanently wedged. The "
                "method exists as dead recovery code."
            ),
        )
    # Control case: had even one owner existed, the wedge would clear.
    return ProbeResult(
        "F1", "no-owner sweep => permanent in_flight wedge", safe=True,
        detail=f"With {sweeper_callers} owner(s) the slot cleared (in_flight={w.in_flight}).",
    )


def probe_f1_sweep_is_otherwise_correct() -> ProbeResult:
    """Confirm the method itself is safe: never reclaims a LIVE lease (no false reclaim)."""
    w = _ReservationWindowModel()
    w.reserve(lease_not_after=100)
    live_reclaim = w.sweep_expired(now_epoch_seconds=50)  # before deadline
    expired_reclaim = w.sweep_expired(now_epoch_seconds=150)  # after deadline
    ok = (live_reclaim is False) and (expired_reclaim is True) and w.generation == 2
    return ProbeResult(
        "F1", "sweep_expired body is fail-closed (fencing correct)", safe=ok,
        detail=(
            "Live lease not reclaimed; expired lease reclaimed once and generation "
            "bumped to fence the stale holder. The defect is the MISSING CALLER, "
            "not the body."
        ) if ok else "Unexpected: sweep body mis-fenced.",
    )


# --------------------------------------------------------------------------- #
# Finding 2: _best_effort_audit swallows requested/dispatched failures, and the
#            executor does not enforce dispatch evidence as a precondition.
# --------------------------------------------------------------------------- #
@dataclass
class _AuditStore:
    """Models the bundle store's dispatch-audit writes. May be flaky."""
    records: set[str] = field(default_factory=set)
    fail_methods: set[str] = field(default_factory=set)

    def record_dispatch_requested(self, *, operation_id: str, execution_name: str) -> None:
        if "record_dispatch_requested" in self.fail_methods:
            raise RuntimeError("audit store hiccup")
        self.records.add(f"requested:{operation_id}")

    def record_dispatched(self, *, operation_id: str, execution_name: str) -> None:
        if "record_dispatched" in self.fail_methods:
            raise RuntimeError("audit store hiccup")
        self.records.add(f"dispatched:{operation_id}")


def _best_effort_audit(store: Any, method_name: str, operation_id: str, execution_name: str) -> None:
    # Faithful transcription of evaluator_entry._best_effort_audit at 05c13e5.
    method = getattr(store, method_name, None)
    if method is None:
        return
    try:
        method(operation_id=operation_id, execution_name=execution_name)
    except Exception:  # noqa: BLE001 -- audit never masks dispatch outcome
        pass


def _record_execution_result_state_put(
    persisted_states: set[str],
    *,
    operation_id: str,
    logical_action_id: str,
    expected_state: str,
    new_state: str,
) -> str:
    """Transcription of execution_store.record_execution_result state write.

    CRITICAL: the state_change_item is put with ``attribute_not_exists(SK)`` on
    the NEW state SK only. ``expected_state`` (="dispatched") is written as a
    plain data field ``previous_state`` and is NEVER used as a conditional
    precondition. So the commit does NOT require any prior durable "dispatched"
    state record to exist.
    """
    new_sk = f"STATE#{new_state}#{logical_action_id}"
    if new_sk in persisted_states:
        return "PRECONDITION_FAILED"  # attribute_not_exists(SK) on the NEW sk
    # NB: no check like  f"STATE#dispatched#..." in persisted_states
    persisted_states.add(new_sk)
    return "RECORDED"


def probe_f2_audit_swallow_and_no_evidence_gate() -> ProbeResult:
    """Reproduce: requested-audit write fails silently, yet execution still
    commits a SUCCEEDED terminal because the executor never requires a durable
    dispatch record.
    """
    op = "op_probe000000000000000000f2"
    lai = "lai_" + hashlib.sha256(op.encode()).hexdigest()[:24]

    # 1. Dispatch path: the "requested" audit write fails and is swallowed.
    store = _AuditStore(fail_methods={"record_dispatch_requested"})
    _best_effort_audit(store, "record_dispatch_requested", op, op[:80])
    requested_written = f"requested:{op}" in store.records  # False (swallowed)

    # 2. Handler reports DISPATCHED; "dispatched" audit succeeds.
    _best_effort_audit(store, "record_dispatched", op, op[:80])
    dispatched_written = f"dispatched:{op}" in store.records  # True

    # 3. Executor commits a terminal result. The state store was NEVER seeded
    #    with a durable STATE#dispatched#... record (the v1 dispatcher only calls
    #    sfn.start_execution; the audit is best-effort). Yet the commit succeeds
    #    because expected_state is not a precondition.
    persisted_states: set[str] = set()  # no STATE#dispatched#... exists
    outcome = _record_execution_result_state_put(
        persisted_states,
        operation_id=op,
        logical_action_id=lai,
        expected_state="dispatched",
        new_state="succeeded",
    )

    committed_without_dispatch_evidence = (
        outcome == "RECORDED" and not requested_written
        and not any(s.startswith("STATE#dispatched#") for s in persisted_states)
    )
    return ProbeResult(
        "F2", "audit swallowed + no enforced dispatch-evidence precondition",
        safe=not committed_without_dispatch_evidence,
        detail=(
            "record_dispatch_requested failed and was swallowed (requested "
            f"evidence written={requested_written}); handler-level dispatched "
            f"audit written={dispatched_written}; executor committed "
            f"new_state=succeeded (outcome={outcome}) with NO durable "
            "STATE#dispatched# record and no requested-audit predecessor. "
            "expected_state='dispatched' is stored as data, not enforced as a "
            "conditional precondition, so a SUCCEEDED terminal can exist with no "
            "provable dispatch predecessor. FALSE-SUCCESS edge confirmed."
        ) if committed_without_dispatch_evidence else "Executor blocked the commit.",
    )


def probe_f2_expected_state_is_not_a_gate() -> ProbeResult:
    """Isolate: even with the new-SK idempotency guard, expected_state is inert."""
    persisted: set[str] = set()
    first = _record_execution_result_state_put(
        persisted, operation_id="op_x", logical_action_id="lai_x",
        expected_state="dispatched", new_state="succeeded",
    )
    # Replay is idempotently blocked on the NEW sk (good) ...
    replay = _record_execution_result_state_put(
        persisted, operation_id="op_x", logical_action_id="lai_x",
        expected_state="dispatched", new_state="succeeded",
    )
    # ... but the FIRST commit needed no dispatched predecessor at all.
    inert = first == "RECORDED" and replay == "PRECONDITION_FAILED"
    return ProbeResult(
        "F2", "expected_state='dispatched' is descriptive metadata, not a fence",
        safe=not inert,
        detail=(
            "The only real precondition is attribute_not_exists on the NEW state "
            "SK (idempotency of the terminal write). No condition asserts a prior "
            "STATE#dispatched# row exists, so the 'dispatched' expectation cannot "
            "block a write that skipped dispatch."
        ) if inert else "expected_state enforced as a precondition.",
    )


# --------------------------------------------------------------------------- #
# Finding 3: verifier expiry runs before Describe; the final pre-write hook
#            checks no decision/window time, so the clock can cross the deadline
#            before the provider write.
# --------------------------------------------------------------------------- #
class _MutableClock:
    def __init__(self, start: int) -> None:
        self.t = start

    def now(self) -> int:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += seconds


def _verify_expiry_snapshot(clock: _MutableClock, *, decision_expires_at: int, window_expires_at: int) -> None:
    """Transcription of AutonomyExecutionVerifier.verify expiry checks.

    ``now`` is snapshotted ONCE at entry; decision (step 6) and window (step 6b)
    freshness are both checked against that single snapshot. Raises on expiry.
    """
    now = clock.now()  # single snapshot at verify() entry
    if decision_expires_at <= now:
        raise RuntimeError("DECISION_EXPIRED")
    if now >= window_expires_at:
        raise RuntimeError("WINDOW_STATE_EXPIRED")


def _pre_write_hook(*, switch_ok: bool, reservation_owned: bool) -> None:
    """Transcription of the E5 pre_write_hook: switch + reservation ONLY.

    Note the absence of any decision_expires_at / window_expires_at re-check.
    """
    if not switch_ok:
        raise RuntimeError("AUTONOMY_SWITCH_DENIED")
    if not reservation_owned:
        raise RuntimeError("RESERVATION_NOT_OWNED")


def probe_f3_deadline_crossed_during_describe() -> ProbeResult:
    """Reproduce: decision valid at verify(); Describe latency pushes the clock
    past decision_expires_at; the pre-write hook still passes; the write fires
    after the deadline (false success against a stale decision).
    """
    clock = _MutableClock(start=1000)
    decision_expires_at = 1010  # 10s of validity left at verify time
    window_expires_at = 1010

    # 1. verify() succeeds: now=1000 < 1010.
    verified = True
    try:
        _verify_expiry_snapshot(clock, decision_expires_at=decision_expires_at, window_expires_at=window_expires_at)
    except RuntimeError:
        verified = False

    # 2. Executor: entry kill-switch, lease acquire, then the pre-write DESCRIBE
    #    round-trip. Model a slow (but plausible) Describe that outlasts the
    #    remaining decision validity.
    clock.advance(30)  # Describe latency > 10s remaining

    # 3. Final pre-write hook: switch + reservation both still fine.
    hook_ok = True
    try:
        _pre_write_hook(switch_ok=True, reservation_owned=True)
    except RuntimeError:
        hook_ok = False

    # 4. Would the write fire? Yes: nothing re-checks the deadline here.
    write_would_fire = verified and hook_ok
    deadline_crossed = clock.now() >= decision_expires_at
    false_success = write_would_fire and deadline_crossed
    return ProbeResult(
        "F3", "decision/window deadline crossed before write (no pre-write re-check)",
        safe=not false_success,
        detail=(
            f"verify() passed at t=1000 (deadline {decision_expires_at}); Describe "
            f"advanced the clock to t={clock.now()}; pre-write hook (switch+"
            "reservation) passed; NO expiry re-check exists before the write, so "
            "UpdateFleetCapacity would issue against an EXPIRED decision/window. "
            "The reservation lease being still-live does not encode the decision "
            "deadline (and per F1 is never swept), so it cannot catch this. "
            "RACE / false-success edge confirmed."
        ) if false_success else "A deadline re-check blocked the stale write.",
    )


def probe_f3_fix_shape_reads_clock_once_more() -> ProbeResult:
    """Show the minimal correct fix and that it closes the edge: re-snapshot the
    clock and re-assert decision/window expiry inside the pre-write hook.
    """
    clock = _MutableClock(start=1000)
    decision_expires_at = 1010
    window_expires_at = 1010
    _verify_expiry_snapshot(clock, decision_expires_at=decision_expires_at, window_expires_at=window_expires_at)
    clock.advance(30)

    def _pre_write_hook_fixed() -> None:
        _pre_write_hook(switch_ok=True, reservation_owned=True)
        now2 = clock.now()  # FIX: fresh snapshot immediately before write
        if decision_expires_at <= now2 or now2 >= window_expires_at:
            raise RuntimeError("DECISION_EXPIRED_AT_WRITE")

    blocked = False
    try:
        _pre_write_hook_fixed()
    except RuntimeError:
        blocked = True
    return ProbeResult(
        "F3", "candidate fix (re-check expiry in pre-write hook) closes the edge",
        safe=blocked,
        detail=(
            "Re-snapshotting the clock and re-asserting decision/window expiry "
            "inside the pre-write hook (after Describe, immediately before the "
            "write) blocks the stale write. This is race-free ONLY because it is "
            "the last gate before the single write and reads a fresh clock; it "
            "introduces no false-failure for an unexpired decision."
        ) if blocked else "Fix did not block the stale write (unexpected).",
    )


# --------------------------------------------------------------------------- #
# Finding 4: policy loader checks hash but not configured id/version.
#            => Against 05c13e5 this literal claim does NOT hold. The probe
#            proves the loader keys on (id, version) AND enforces the configured
#            hash, then isolates the one residual edge worth a check.
# --------------------------------------------------------------------------- #
def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


class _PolicyTableModel:
    """Transcription of DynamoDbAutonomyPolicyLoader over an in-memory table.

    Table key: PK = f"AUTZPOLICY#{policy_id}#{policy_version}", SK = "AUTZPOLICY".
    load(policy_id, policy_version, policy_hash):
      * fetch by PK(id,version)  -> a wrong id/version cannot return a policy
      * enforce stored policy_hash == configured hash (fail closed on drift)
      * re-validate the document binds its own hash (contract) [modelled]
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _pk(policy_id: str, policy_version: str) -> str:
        return f"AUTZPOLICY#{policy_id}#{policy_version}"

    def seed(self, policy: dict[str, Any]) -> None:
        pk = self._pk(policy["policy_id"], policy["policy_version"])
        self._items[pk] = {"policy": _canonical(policy), "policy_hash": policy["policy_hash"]}

    def load(self, *, policy_id: str, policy_version: str, policy_hash: str) -> dict[str, Any]:
        pk = self._pk(policy_id, policy_version)
        item = self._items.get(pk)
        if item is None:
            raise RuntimeError("no policy is sealed for the configured id/version")
        document = json.loads(item["policy"])
        if document.get("policy_hash") != policy_hash:
            raise RuntimeError("stored policy_hash does not match the configured policy hash")
        # Contract re-validation would also assert policy_hash binds canonical bytes.
        return document


def _hash_of(body: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()


def _policy(pid: str, pver: str) -> dict[str, Any]:
    body = {"policy_id": pid, "policy_version": pver, "rules": ["r1"]}
    body["policy_hash"] = _hash_of(body)
    return body


def probe_f4_id_version_are_enforced_via_pk() -> ProbeResult:
    """Refute the literal finding: a wrong configured id/version cannot load a
    policy, and a hash mismatch fails closed.
    """
    table = _PolicyTableModel()
    good = _policy("cap-scale", "v3")
    table.seed(good)

    # (a) correct id/version/hash loads.
    loaded = table.load(policy_id="cap-scale", policy_version="v3", policy_hash=good["policy_hash"])
    ok_load = loaded["policy_id"] == "cap-scale" and loaded["policy_version"] == "v3"

    # (b) wrong version -> no policy (id/version ARE part of the fetch key).
    wrong_version_blocked = False
    try:
        table.load(policy_id="cap-scale", policy_version="v2", policy_hash=good["policy_hash"])
    except RuntimeError:
        wrong_version_blocked = True

    # (c) right id/version but wrong configured hash -> fail closed.
    wrong_hash_blocked = False
    try:
        table.load(policy_id="cap-scale", policy_version="v3", policy_hash="sha256:deadbeef")
    except RuntimeError:
        wrong_hash_blocked = True

    all_enforced = ok_load and wrong_version_blocked and wrong_hash_blocked
    return ProbeResult(
        "F4", "loader enforces configured id/version (via PK) AND hash",
        safe=all_enforced,
        detail=(
            "At 05c13e5 load() fetches by PK=AUTZPOLICY#{id}#{version}, so a wrong "
            "configured id/version returns nothing (blocked), and a stored hash "
            "!= configured hash fails closed. The literal finding ('checks hash "
            "but not configured id/version') does NOT hold against this base; the "
            "caller passes server-owned settings.autonomy_policy_{id,version,hash}."
        ) if all_enforced else "Unexpected: an id/version/hash path was not enforced.",
    )


def probe_f4_residual_pk_body_consistency_edge() -> ProbeResult:
    """The one real residual edge: load() re-validates that policy_hash binds the
    canonical BODY, but does not separately assert the stored body's own
    policy_id/policy_version equal the PK components used to fetch it.

    In practice seed() derives the PK from the same body, so a divergent record
    cannot arise through the sealed write path. This probe shows the edge is
    THEORETICAL (requires an out-of-band write) and therefore low-risk, not a
    false-success in the normal flow.
    """
    table = _PolicyTableModel()
    # A hand-crafted, self-consistent, correctly-hashed policy body whose declared
    # id/version differ from the PK it is stored under would require bypassing
    # seed(). seed() forbids that by construction (PK derived from the body).
    body = {"policy_id": "cap-scale", "policy_version": "v3", "rules": ["r1"]}
    body["policy_hash"] = _hash_of(body)
    # Simulate an out-of-band tamper: same PK, but body claims a different version.
    tampered = dict(body)
    tampered["policy_version"] = "v9"
    tampered["policy_hash"] = _hash_of(tampered)  # self-consistent hash
    table._items[_PolicyTableModel._pk("cap-scale", "v3")] = {
        "policy": _canonical(tampered),
        "policy_hash": tampered["policy_hash"],
    }
    # Configured hash must equal the tampered self-consistent hash for load to pass.
    doc = table.load(policy_id="cap-scale", policy_version="v3", policy_hash=tampered["policy_hash"])
    body_version_diverges = doc["policy_version"] != "v3"
    return ProbeResult(
        "F4", "residual: PK components not re-asserted against loaded body",
        # Reported as SAFE-in-practice: only reachable via an out-of-band write
        # that bypasses seed(); recommend a cheap defensive assert, not a blocker.
        safe=True,
        detail=(
            "Only reachable by writing a record out-of-band (seed() derives the "
            "PK from the body, so it cannot create divergence). If such a record "
            "existed, load() would return a body whose policy_version (v9) differs "
            f"from the fetch key (v3): observed diverges={body_version_diverges}. "
            "Recommendation: add a cheap defensive check that the loaded body's "
            "policy_id/policy_version equal the requested ones. Low-risk hardening, "
            "NOT the finding as stated."
        ),
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def all_probes() -> list[ProbeResult]:
    return [
        probe_f1_no_sweeper_wedges_state(sweeper_callers=0),
        probe_f1_sweep_is_otherwise_correct(),
        probe_f2_audit_swallow_and_no_evidence_gate(),
        probe_f2_expected_state_is_not_a_gate(),
        probe_f3_deadline_crossed_during_describe(),
        probe_f3_fix_shape_reads_clock_once_more(),
        probe_f4_id_version_are_enforced_via_pk(),
        probe_f4_residual_pk_body_consistency_edge(),
    ]


def main() -> int:
    results = all_probes()
    width = max(len(r.name) for r in results)
    print(f"Issue #439 E5 final-findings probe @ runtime base 05c13e5")
    print("=" * (width + 30))
    reproduced = 0
    for r in results:
        verdict = "SAFE " if r.safe else "DEFECT"
        if not r.safe:
            reproduced += 1
        print(f"[{r.finding}] {verdict}  {r.name:<{width}}")
        print(f"        {r.detail}")
    print("=" * (width + 30))
    print(f"Probes reproducing a defect/edge: {reproduced}/{len(results)}")
    # Exit non-zero when any defect was reproduced (useful as a CI signal).
    return 1 if reproduced else 0


if __name__ == "__main__":
    raise SystemExit(main())
