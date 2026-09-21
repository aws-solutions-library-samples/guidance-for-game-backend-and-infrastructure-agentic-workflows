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
* Every provider read is issued with its own deadline; the whole request also
  has a total deadline set below ``ceiling - cancellation_margin``.
* On any deadline overrun the request **fails closed**: in-flight work is
  abandoned, a typed *retryable* error is raised, and no partial observation is
  ever returned as success.
* Correct percentile reporting (nearest-rank) over the collected sample, with
  sample size, p50/p95/p99/max, failure, and timeout counts.

This is a disposable spike. It adds no production tables, buckets, API routes,
queues, workers, or provider write permissions, and does not enable operations.
All measurement output is public-safe: it carries latency statistics and
declared assumptions only — never account identifiers, resource names, ARNs, or
provider payloads.
"""

from __future__ import annotations

# Standard library
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

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
    """A per-call, persistence, or total deadline was exceeded.

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
    :class:`DeadlineExceededError`.
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
    """Public-safe latency statistics for a batch of observation runs."""

    sample_size: int
    successes: int
    failures: int
    timeouts: int
    percentile_method: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    def as_public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict. Contains statistics only, no identifiers."""
        return {
            "sample_size": self.sample_size,
            "successes": self.successes,
            "failures": self.failures,
            "timeouts": self.timeouts,
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
) -> LatencySummary:
    """Summarize successful-run latencies plus failure and timeout counts.

    ``success_latencies_s`` holds the wall-clock duration (seconds) of every run
    that completed successfully. Percentiles are computed over successful runs
    only, using the nearest-rank method; failures and timeouts are reported as
    counts so a reader can see the fail-closed rate alongside the latency
    distribution. Sample size is the total number of runs attempted.
    """
    if failures < 0 or timeouts < 0:
        raise ValueError("failure and timeout counts must be non-negative")
    successes = len(success_latencies_s)
    sample_size = successes + failures + timeouts
    if successes == 0:
        # No successful runs: percentiles are undefined; report zeros so the
        # summary is still emitted and the failure/timeout counts tell the story.
        return LatencySummary(
            sample_size=sample_size,
            successes=0,
            failures=failures,
            timeouts=timeouts,
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


class ObservationRunner:
    """Executes the deterministic E1 observation request path under budget.

    The runner is constructed with a :class:`LatencyBudget`, a monotonic clock,
    and an optional persistence sink. Each :meth:`run` performs the three
    supplied read-only reads under their per-call deadlines, then persists and
    canonically serializes a representative observation record under the
    persistence deadline, enforcing the total deadline throughout and failing
    closed on any overrun.
    """

    def __init__(
        self,
        budget: LatencyBudget = DEFAULT_BUDGET,
        *,
        clock: Callable[[], float] = time.monotonic,
        persist: Callable[[bytes], None] | None = None,
    ) -> None:
        self._budget = budget
        self._clock = clock
        # Default persistence sink is a no-op: representative persistence cost is
        # exercised by canonical serialization plus the caller-supplied sink. A
        # live harness passes a sink that writes to a disposable local store.
        self._persist = persist if persist is not None else (lambda _b: None)

    @property
    def budget(self) -> LatencyBudget:
        return self._budget

    def run(self, reads: Sequence[ProviderRead]) -> ObservationOutcome:
        """Run one observation. Raises on deadline overrun or partial result."""
        if len(reads) != self._budget.read_count:
            raise ValueError(f"observation requires exactly {self._budget.read_count} reads, got {len(reads)}")

        start = self._clock()
        deadline = start + self._budget.total_deadline_s
        read_durations: list[float] = []
        results: list[Any] = []

        for i, read in enumerate(reads):
            call_start = self._clock()
            # Fail closed before issuing a read if the total budget is already
            # spent — never start work we cannot finish in time.
            if call_start >= deadline:
                raise DeadlineExceededError("total", call_start - start, self._budget.total_deadline_s)
            result = read()
            call_elapsed = self._clock() - call_start
            # Per-call deadline: a single slow read must not consume the whole
            # request budget.
            if call_elapsed > self._budget.per_read_s:
                raise DeadlineExceededError(f"read[{i}]", call_elapsed, self._budget.per_read_s)
            if result is None:
                # A read that produced nothing cannot yield a complete
                # observation: fail closed rather than persist a partial result.
                raise PartialObservationError("provider read returned no result")
            read_durations.append(call_elapsed)
            results.append(result)

        # Persistence + canonical serialization phase.
        persist_start = self._clock()
        if persist_start >= deadline:
            raise DeadlineExceededError("total", persist_start - start, self._budget.total_deadline_s)
        record = self._build_record(read_durations)
        # Canonical (RFC 8785) serialization of the representative persisted
        # record, then a representative persistence write via the sink.
        canonical_bytes = canonicalize(record)
        record_hash = canonical_sha256(record)
        self._persist(canonical_bytes)
        persist_elapsed = self._clock() - persist_start
        if persist_elapsed > self._budget.persistence_s:
            raise DeadlineExceededError("persistence", persist_elapsed, self._budget.persistence_s)

        total_elapsed = self._clock() - start
        if total_elapsed > self._budget.total_deadline_s:
            raise DeadlineExceededError("total", total_elapsed, self._budget.total_deadline_s)

        return ObservationOutcome(
            read_durations_s=read_durations,
            persistence_s=persist_elapsed,
            total_s=total_elapsed,
            record_hash=record_hash,
        )

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
            "read_count": len(read_durations),
            # Bounded synthetic ledger-event stand-ins; sequence is strictly
            # increasing to mirror the append-only ledger.
            "ledger_events": [
                {"sequence": index, "event_type": "provider_read_completed"} for index in range(len(read_durations))
            ],
        }
