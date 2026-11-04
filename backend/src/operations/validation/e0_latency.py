"""E0 latency validation harness for the intended E1 synchronous observation.

Issue #412 requires *measured* evidence that the E1 synchronous GameLift
observation completes with explicit headroom below the API Gateway HTTP API
integration timeout, before ADR 0005 can move from Proposed to Accepted.

The intended E1 observation, as described in ADR 0005
("Durable observation state without queues"), performs, inside one request:

1. exactly three bounded, read-only GameLift provider reads, and
2. representative persistence of the observation state transition and its
   ledger events, plus canonical (RFC 8785) serialization of the persisted
   records.

This module models that request path deterministically so it can be measured
repeatedly. It is intentionally provider-agnostic about *where* the three reads
come from: a caller supplies three zero-argument, read-only callables (for a
live measurement these wrap ``boto3`` GameLift ``describe_*`` calls; for unit
tests they are in-process fakes). The harness never issues a write.

Design constraints enforced here (issue #412 / ADR 0005):

* Explicit per-call, persistence/serialization, total, and cancellation-margin
  budgets whose components sum to strictly less than the 30-second gateway
  ceiling.
* Every provider read is issued with its own **wall-clock** deadline: the read
  runs in a worker thread and the request thread waits at most the smaller of
  the per-read budget and the remaining total budget. A read that blocks past
  that deadline is abandoned and a typed *retryable* error is raised **before**
  the read returns, so the request fails closed ahead of the gateway timeout
  rather than merely detecting the overrun after a blocking call returns. A read
  that *does* return but took longer than its budget (measured on the injected
  clock) is also rejected, so the two checks together cover both a hung read and
  a merely-slow one.
* On any deadline overrun the request **fails closed**: in-flight work is
  abandoned (the runner's executor is shut down without waiting), a typed
  *retryable* error is raised, and no partial observation is ever returned as
  success.
* Correct percentile reporting (nearest-rank) over the collected sample, with
  sample size, p50/p95/p99/max, and separate failure, timeout, and partial
  denial counts.

This is a disposable spike. It adds no production tables, buckets, API routes,
queues, workers, or provider write permissions, and does not enable operations.
All measurement output is public-safe: it carries latency statistics and
declared assumptions only — never account identifiers, resource names, ARNs, or
provider payloads.
"""

from __future__ import annotations

# Standard library
import concurrent.futures
import math
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# Local modules
from operations.contracts.canonical import canonical_sha256, canonicalize

# The API Gateway HTTP API integration timeout has a documented, non-raisable
# ceiling of 30 seconds. See the Amazon API Gateway quotas documentation cited
# in ADR 0005. Every budget below must sum to strictly less than this value.
GATEWAY_INTEGRATION_TIMEOUT_S: float = 30.0

# The number of bounded provider reads the E1 observation performs, fixed by
# ADR 0005 and the GameLift specialist's read surface.
PROVIDER_READ_COUNT: int = 3


class DeadlineExceededError(TimeoutError):
    """A per-call or persistence deadline was exceeded.

    Raised when the observation cannot complete within its budget. It maps to a
    typed, *retryable* application error (``PROVIDER_UNAVAILABLE``) so the
    request fails closed before the gateway itself times out, rather than
    returning a partial or late result.
    """

    #: Stable, public-safe application error code for adapters to map onto.
    error_code: str = "PROVIDER_UNAVAILABLE"
    #: This outcome is safe to retry: no write occurred and no state changed.
    retryable: bool = True

    def __init__(self, phase: str, elapsed_s: float, budget_s: float) -> None:
        self.phase = phase
        self.elapsed_s = elapsed_s
        self.budget_s = budget_s
        super().__init__(
            # Public-safe: names only the phase and the numeric budget, never a
            # resource, account, or provider payload.
            f"E0 observation exceeded {phase} deadline: "
            f"{elapsed_s * 1000:.1f}ms > {budget_s * 1000:.1f}ms budget"
        )


class PartialObservationError(RuntimeError):
    """A provider read returned no usable result within budget.

    Fail-closed guard: an observation that could not gather all three reads is
    never returned as success. Retryable for the same reason as
    :class:`DeadlineExceededError`. Reported separately from timeouts because it
    is a distinct provider condition (a read that answered, but with no usable
    result) rather than a latency overrun.
    """

    error_code: str = "PROVIDER_UNAVAILABLE"
    retryable: bool = True


@dataclass(frozen=True)
class LatencyBudget:
    """Explicit sub-budgets for one synchronous observation request.

    All values are in seconds. ``per_read_s`` bounds each individual provider
    read; ``persistence_s`` bounds persistence plus canonical serialization;
    ``cancellation_margin_s`` is reserved headroom so the request aborts and
    returns a typed error *before* the gateway timeout. The components must sum
    to strictly less than :data:`GATEWAY_INTEGRATION_TIMEOUT_S`.
    """

    per_read_s: float
    persistence_s: float
    cancellation_margin_s: float
    read_count: int = PROVIDER_READ_COUNT
    ceiling_s: float = GATEWAY_INTEGRATION_TIMEOUT_S

    def __post_init__(self) -> None:
        for name, value in (
            ("per_read_s", self.per_read_s),
            ("persistence_s", self.persistence_s),
            ("cancellation_margin_s", self.cancellation_margin_s),
            ("ceiling_s", self.ceiling_s),
        ):
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a positive, finite number of seconds")
        if self.read_count < 1:
            raise ValueError("read_count must be at least 1")
        if self.reads_and_persistence_s >= self.ceiling_s:
            raise ValueError(
                "sub-budgets must sum to less than the gateway ceiling: "
                f"{self.reads_and_persistence_s}s >= {self.ceiling_s}s"
            )
        if self.total_deadline_s >= self.ceiling_s:
            raise ValueError(
                "total deadline (including cancellation margin) must be below the ceiling: "
                f"{self.total_deadline_s}s >= {self.ceiling_s}s"
            )

    @property
    def reads_and_persistence_s(self) -> float:
        """Sum of the three read budgets plus persistence/serialization."""
        return self.per_read_s * self.read_count + self.persistence_s

    @property
    def total_deadline_s(self) -> float:
        """The wall-clock deadline for the whole request.

        Set to reads + persistence + the reserved cancellation margin. The
        request must complete before this deadline; the margin guarantees the
        request returns a typed error before the gateway's own ``ceiling_s``.
        """
        return self.reads_and_persistence_s + self.cancellation_margin_s

    @property
    def acceptance_ceiling_s(self) -> float:
        """The value a measured p99 must beat: ceiling minus cancellation margin."""
        return self.ceiling_s - self.cancellation_margin_s


# Conservative default budget. Three reads at 3.0s each (9.0s) + 3.0s for
# persistence and canonical serialization = 12.0s of work, + a 3.0s cancellation
# margin => a 15.0s total request deadline, well under the 30.0s ceiling and
# leaving the acceptance ceiling (ceiling - margin) at 27.0s. The implementing
# issue may tighten these; they are recorded as assumptions in the evidence.
DEFAULT_BUDGET = LatencyBudget(per_read_s=3.0, persistence_s=3.0, cancellation_margin_s=3.0)


def percentile_nearest_rank(samples: Sequence[float], percentile: float) -> float:
    """Return the ``percentile`` value using the nearest-rank method.

    The nearest-rank method (ISO 3534 / NIST) is exact for reporting an observed
    latency percentile: for a sorted sample of size ``n`` and percentile ``p`` in
    ``(0, 100]``, the rank is ``ceil(p/100 * n)`` (1-based), and the result is the
    value at that rank. This never interpolates between samples, so a reported
    p99 is always an actually-observed measurement — the correct choice for a
    latency acceptance gate.

    Raises ``ValueError`` for an empty sample or an out-of-range percentile.
    """
    if not samples:
        raise ValueError("cannot compute a percentile of an empty sample")
    if not (0 < percentile <= 100):
        raise ValueError("percentile must be in the interval (0, 100]")
    ordered = sorted(samples)
    n = len(ordered)
    rank = math.ceil(percentile / 100.0 * n)
    # Guard the boundaries: ceil can yield 0 only for percentile 0 (excluded);
    # clamp defensively so the index is always valid.
    index = min(max(rank, 1), n) - 1
    return ordered[index]


@dataclass(frozen=True)
class LatencySummary:
    """Public-safe latency statistics for a batch of observation runs.

    ``failures``, ``timeouts``, and ``partial_denials`` are three distinct
    fail-closed buckets: a provider error, a wall-clock deadline overrun, and a
    read that answered with no usable result, respectively. Keeping them apart
    lets a reader see *why* the run was not clean, not just that it was not.
    """

    sample_size: int
    successes: int
    failures: int
    timeouts: int
    partial_denials: int
    percentile_method: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @property
    def clean_run(self) -> bool:
        """True iff every attempted sample succeeded (no non-success outcome).

        This is the strict acceptance predicate: a measured p99 is only
        meaningful evidence for the synchronous design if the whole sample
        completed without any failure, timeout, or partial denial.
        """
        return self.sample_size > 0 and self.successes == self.sample_size

    def as_public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict. Contains statistics only, no identifiers."""
        return {
            "sample_size": self.sample_size,
            "successes": self.successes,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "partial_denials": self.partial_denials,
            "percentile_method": self.percentile_method,
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "max_ms": round(self.max_ms, 3),
        }


def summarize_latencies(
    success_latencies_s: Sequence[float],
    *,
    failures: int = 0,
    timeouts: int = 0,
    partial_denials: int = 0,
) -> LatencySummary:
    """Summarize successful-run latencies plus fail-closed counts.

    ``success_latencies_s`` holds the wall-clock duration (seconds) of every run
    that completed successfully. Percentiles are computed over successful runs
    only, using the nearest-rank method; failures, timeouts, and partial denials
    are reported as separate counts so a reader can see the fail-closed rate and
    its cause alongside the latency distribution. Sample size is the total
    number of runs attempted.
    """
    if failures < 0 or timeouts < 0 or partial_denials < 0:
        raise ValueError("failure, timeout, and partial-denial counts must be non-negative")
    successes = len(success_latencies_s)
    sample_size = successes + failures + timeouts + partial_denials
    if successes == 0:
        # No successful runs: percentiles are undefined; report zeros so the
        # summary is still emitted and the fail-closed counts tell the story.
        return LatencySummary(
            sample_size=sample_size,
            successes=0,
            failures=failures,
            timeouts=timeouts,
            partial_denials=partial_denials,
            percentile_method="nearest_rank",
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
        )
    return LatencySummary(
        sample_size=sample_size,
        successes=successes,
        failures=failures,
        timeouts=timeouts,
        partial_denials=partial_denials,
        percentile_method="nearest_rank",
        p50_ms=percentile_nearest_rank(success_latencies_s, 50) * 1000.0,
        p95_ms=percentile_nearest_rank(success_latencies_s, 95) * 1000.0,
        p99_ms=percentile_nearest_rank(success_latencies_s, 99) * 1000.0,
        max_ms=max(success_latencies_s) * 1000.0,
    )


@dataclass
class ObservationOutcome:
    """Result of one successful synchronous observation run.

    Carries only bounded, public-safe values: the per-phase durations and the
    canonical hash of the persisted records. It never carries provider payloads,
    resource names, or account identifiers.
    """

    read_durations_s: list[float]
    persistence_s: float
    total_s: float
    record_hash: str


# A representative, read-only provider read. Zero-argument so the harness never
# supplies (and can never leak) resource identifiers; the callable closes over
# its own bounded target. It returns an opaque, small result that stands in for
# a describe_* response shape without exposing provider data.
ProviderRead = Callable[[], Any]


class _PersistenceSink(Protocol):
    """Structured persistence hook invoked once per observation sample.

    Distinct from the legacy byte-sink ``persist`` callable: a sink receives
    the built observation ``record`` (already stamped with a per-sample-unique
    ``operation_id``) and its RFC 8785 canonical bytes, so a real backend can
    persist uniquely-keyed items. Implementations run inside the measured
    persistence phase and must fail closed on any deadline overrun.
    """

    def persist(self, record: dict[str, Any], canonical_bytes: bytes) -> None: ...


class ObservationRunner:
    """Executes the deterministic E1 observation request path under budget.

    The runner is constructed with a :class:`LatencyBudget`, a clock used to
    *measure* durations, and an optional persistence sink. Each :meth:`run`
    performs the three supplied read-only reads under their per-call **wall-clock**
    deadlines, then persists and canonically serializes a representative
    observation record under the persistence deadline, enforcing the total
    deadline throughout and failing closed on any overrun.

    Wall-clock enforcement is real: each read is executed in a worker thread and
    the request waits at most the remaining budget for it. If the read does not
    return in time it is abandoned — the request raises immediately and does not
    join the still-running worker, so a hung provider read can never make the
    request (or the harness) wait past its deadline. A read that returns within
    the wait but whose measured elapsed time still exceeds the per-read budget
    is likewise rejected.
    """

    def __init__(
        self,
        budget: LatencyBudget = DEFAULT_BUDGET,
        *,
        clock: Callable[[], float] = time.monotonic,
        persist: Callable[[bytes], None] | None = None,
        sink: _PersistenceSink | None = None,
    ) -> None:
        self._budget = budget
        self._clock = clock
        # Two mutually exclusive persistence hooks:
        #  * ``sink`` (preferred) is the structured persistence sink: it receives
        #    the built record and its canonical bytes and can persist uniquely
        #    keyed items (e.g. a real transactional DynamoDB write).
        #  * ``persist`` is the legacy byte sink retained for existing unit tests.
        # When neither is supplied the sink is a no-op *and the run is flagged as
        # not durably persisted*: a no-op measures only canonical serialization and
        # is never acceptable as live durable evidence (issue #412 finding).
        if sink is not None and persist is not None:
            raise ValueError("pass either a structured sink or a legacy persist callable, not both")
        self._sink = sink
        self._persist = persist
        self._durably_persisted = sink is not None or persist is not None

    @property
    def budget(self) -> LatencyBudget:
        return self._budget

    @property
    def durably_persisted(self) -> bool:
        """True iff this runner writes through a real persistence hook.

        A runner with neither a structured ``sink`` nor a legacy ``persist``
        callable measures only canonical serialization; such a run must never be
        reported as durable, live acceptance evidence.
        """
        return self._durably_persisted

    def run(self, reads: Sequence[ProviderRead]) -> ObservationOutcome:
        """Run one observation. Raises on deadline overrun or partial result.

        Raises :class:`DeadlineExceededError` (retryable) when a read exceeds its
        per-read budget — enforced both as a real wall-clock wait (a hung read is
        pre-empted before it returns) and as a post-return elapsed check — or
        when persistence exceeds its budget, and :class:`PartialObservationError`
        (retryable) if a read yields no usable result. The whole-request (total)
        deadline is enforced *compositionally* by these per-phase bounds plus the
        positive cancellation margin, not by a separate late check. Never returns
        a partial observation as success.
        """
        if len(reads) != self._budget.read_count:
            raise ValueError(f"observation requires exactly {self._budget.read_count} reads, got {len(reads)}")

        start = self._clock()
        deadline_s = self._budget.total_deadline_s
        read_durations: list[float] = []

        # One dedicated single-worker executor per request. On any overrun we
        # shut it down with wait=False and cancel_futures=True so the request
        # never blocks on abandoned work; a still-running read thread is left to
        # finish on its own (it holds no lock and performs only a read).
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="e0-read")
        abandoned = False
        try:
            for i, read in enumerate(reads):
                call_start = self._clock()
                # Wall-clock budget for this read: never wait longer than either
                # the per-read budget or whatever remains of the total deadline.
                # remaining_total is a defensive floor; the budget invariant
                # (read_count*per_read + persistence + margin == total, margin > 0)
                # makes per_read_s the binding bound during reads.
                remaining_total = deadline_s - (call_start - start)
                read_budget = min(self._budget.per_read_s, remaining_total)
                future = executor.submit(read)
                try:
                    result = future.result(timeout=read_budget)
                except concurrent.futures.TimeoutError:
                    # The read is still running; abandon it and fail closed
                    # before it returns rather than waiting for the worker.
                    abandoned = True
                    future.cancel()
                    call_elapsed = self._clock() - call_start
                    raise DeadlineExceededError(f"read[{i}]", call_elapsed, self._budget.per_read_s)

                call_elapsed = self._clock() - call_start
                # Post-return per-call check on the measured clock: a read that
                # returned but took longer than its budget still fails closed.
                if call_elapsed > self._budget.per_read_s:
                    raise DeadlineExceededError(f"read[{i}]", call_elapsed, self._budget.per_read_s)
                if result is None:
                    # A read that produced nothing cannot yield a complete
                    # observation: fail closed rather than persist a partial one.
                    raise PartialObservationError("provider read returned no result")
                read_durations.append(call_elapsed)

            # Persistence + canonical serialization phase, bounded on its own.
            # A per-sample-unique operation id is stamped into the record so the
            # canonical bytes differ per sample and a real sink can key items
            # uniquely with conditional no-replacement semantics.
            persist_start = self._clock()
            record = self._build_record(read_durations)
            canonical_bytes = canonicalize(record)
            record_hash = canonical_sha256(record)
            if self._sink is not None:
                self._sink.persist(record, canonical_bytes)
            elif self._persist is not None:
                self._persist(canonical_bytes)
            persist_elapsed = self._clock() - persist_start
            if persist_elapsed > self._budget.persistence_s:
                raise DeadlineExceededError("persistence", persist_elapsed, self._budget.persistence_s)

            # The whole-request deadline is enforced *compositionally*, not by a
            # separate late check: each read is bounded by per_read_s and
            # persistence by persistence_s, and the budget guarantees
            # read_count*per_read_s + persistence_s + cancellation_margin_s ==
            # total_deadline_s with a strictly positive margin. A late "total"
            # check after these bounds could therefore never fire for any valid
            # budget, so it is intentionally omitted rather than left as dead,
            # misleading code. total_s below is reported for the evidence record.
            total_elapsed = self._clock() - start

            return ObservationOutcome(
                read_durations_s=read_durations,
                persistence_s=persist_elapsed,
                total_s=total_elapsed,
                record_hash=record_hash,
            )
        finally:
            # Never block the request on abandoned work. When a read timed out we
            # cannot wait for the worker; when the run completed cleanly the
            # worker is already idle and a non-blocking shutdown is still safe.
            executor.shutdown(wait=not abandoned, cancel_futures=True)

    def _build_record(self, read_durations: Sequence[float]) -> dict[str, Any]:
        """Build a representative, public-safe observation record to serialize.

        The record shape stands in for the observation state transition and its
        ledger events (ADR 0005) at representative size, using only synthetic,
        bounded, canonicalizable values. It intentionally contains no resource
        names, account identifiers, or provider payloads.
        """
        return {
            "observation_contract_version": "1.0",
            "phase": "observe",
            "provider": "gamelift",
            # Per-sample-unique synthetic operation id: makes each persisted
            # item uniquely keyed and each sample's canonical bytes distinct. It
            # is a random uuid4, never derived from any fleet or account id.
            "operation_id": str(uuid.uuid4()),
            "read_count": len(read_durations),
            # Bounded synthetic ledger-event stand-ins; sequence is strictly
            # increasing to mirror the append-only ledger.
            "ledger_events": [
                {"sequence": index, "event_type": "provider_read_completed"} for index in range(len(read_durations))
            ],
        }
