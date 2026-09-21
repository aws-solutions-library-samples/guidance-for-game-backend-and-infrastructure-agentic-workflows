"""Unit tests for the E0 synchronous-observation latency validation harness.

Covers issue #412 acceptance behaviors that do not require live AWS:

* explicit sub-budgets that sum below the 30s gateway ceiling and reserve a
  cancellation margin;
* the deterministic three-read + persistence + canonical-serialization path;
* correct nearest-rank percentile reporting with sample/failure/timeout counts;
* fail-closed behavior on per-call, persistence, and total deadline overrun; and
* denial of partial results as success.
"""

from __future__ import annotations

# Standard library
import json

# Third-party packages
import pytest

# Local modules
from operations.validation import (
    DEFAULT_BUDGET,
    GATEWAY_INTEGRATION_TIMEOUT_S,
    DeadlineExceededError,
    LatencyBudget,
    ObservationRunner,
    PartialObservationError,
    percentile_nearest_rank,
    summarize_latencies,
)

pytestmark = pytest.mark.unit


class FakeClock:
    """Deterministic monotonic clock advanced explicitly by the test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reads(count: int = 3):
    """Return ``count`` zero-argument reads returning opaque, non-None results."""
    return [lambda i=i: {"ok": i} for i in range(count)]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_default_budget_components_sum_below_gateway_ceiling():
    b = DEFAULT_BUDGET
    assert b.reads_and_persistence_s < GATEWAY_INTEGRATION_TIMEOUT_S
    assert b.total_deadline_s < GATEWAY_INTEGRATION_TIMEOUT_S
    # Total deadline includes the reserved cancellation margin.
    assert b.total_deadline_s == pytest.approx(b.reads_and_persistence_s + b.cancellation_margin_s)
    # Acceptance ceiling is the gateway ceiling minus the cancellation margin.
    assert b.acceptance_ceiling_s == pytest.approx(GATEWAY_INTEGRATION_TIMEOUT_S - b.cancellation_margin_s)


def test_budget_rejects_components_that_exceed_ceiling():
    with pytest.raises(ValueError):
        LatencyBudget(per_read_s=10.0, persistence_s=5.0, cancellation_margin_s=3.0)


def test_budget_rejects_nonpositive_and_nonfinite_values():
    with pytest.raises(ValueError):
        LatencyBudget(per_read_s=0.0, persistence_s=1.0, cancellation_margin_s=1.0)
    with pytest.raises(ValueError):
        LatencyBudget(per_read_s=float("inf"), persistence_s=1.0, cancellation_margin_s=1.0)


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentile_nearest_rank_matches_known_values():
    # 1..100 -> nearest-rank p50=50, p95=95, p99=99, p100(max)=100.
    samples = [float(x) for x in range(1, 101)]
    assert percentile_nearest_rank(samples, 50) == 50.0
    assert percentile_nearest_rank(samples, 95) == 95.0
    assert percentile_nearest_rank(samples, 99) == 99.0
    assert percentile_nearest_rank(samples, 100) == 100.0


def test_percentile_never_interpolates_returns_observed_value():
    samples = [10.0, 20.0, 30.0]
    # p99 of 3 samples -> rank ceil(0.99*3)=3 -> the 3rd value, an observed one.
    assert percentile_nearest_rank(samples, 99) in samples
    assert percentile_nearest_rank(samples, 99) == 30.0


def test_percentile_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        percentile_nearest_rank([], 50)
    with pytest.raises(ValueError):
        percentile_nearest_rank([1.0], 0)
    with pytest.raises(ValueError):
        percentile_nearest_rank([1.0], 101)


def test_summary_reports_counts_and_public_safe_dict():
    latencies = [x / 1000.0 for x in range(1, 101)]  # 1ms..100ms
    summary = summarize_latencies(latencies, failures=2, timeouts=1)
    assert summary.sample_size == 103
    assert summary.successes == 100
    assert summary.failures == 2
    assert summary.timeouts == 1
    assert summary.percentile_method == "nearest_rank"
    assert summary.p50_ms == pytest.approx(50.0)
    assert summary.p99_ms == pytest.approx(99.0)
    assert summary.max_ms == pytest.approx(100.0)
    public = summary.as_public_dict()
    # Round-trips as JSON and contains statistics only (no identifiers).
    text = json.dumps(public)
    assert "sample_size" in text
    assert set(public) == {
        "sample_size",
        "successes",
        "failures",
        "timeouts",
        "percentile_method",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
    }


def test_summary_with_no_successes_reports_zero_percentiles():
    summary = summarize_latencies([], failures=3, timeouts=2)
    assert summary.sample_size == 5
    assert summary.successes == 0
    assert summary.p99_ms == 0.0


# ---------------------------------------------------------------------------
# Observation runner: happy path
# ---------------------------------------------------------------------------


def test_run_completes_three_reads_and_returns_hashed_record():
    clock = FakeClock()
    persisted: list[bytes] = []
    runner = ObservationRunner(clock=clock, persist=persisted.append)

    # Each read advances the clock by 0.5s; persistence by 0.2s.
    def timed_read(i):
        def _read():
            clock.advance(0.5)
            return {"read": i}

        return _read

    reads = [timed_read(i) for i in range(3)]
    outcome = runner.run(reads)

    assert len(outcome.read_durations_s) == 3
    assert all(d == pytest.approx(0.5) for d in outcome.read_durations_s)
    assert outcome.total_s == pytest.approx(1.5)
    assert outcome.record_hash.startswith("sha256:")
    # Persistence sink received canonical bytes.
    assert persisted and isinstance(persisted[0], bytes)


def test_run_requires_exactly_three_reads():
    runner = ObservationRunner(clock=FakeClock())
    with pytest.raises(ValueError):
        runner.run(_reads(2))


# ---------------------------------------------------------------------------
# Fail-closed: deadlines and partial results
# ---------------------------------------------------------------------------


def test_slow_single_read_fails_closed_with_retryable_error():
    clock = FakeClock()
    runner = ObservationRunner(clock=clock)

    def slow_read():
        clock.advance(DEFAULT_BUDGET.per_read_s + 1.0)  # over the per-read budget
        return {"ok": True}

    fast = [lambda: {"ok": True}, lambda: {"ok": True}]
    with pytest.raises(DeadlineExceededError) as exc:
        runner.run([slow_read, *fast])
    assert exc.value.retryable is True
    assert exc.value.error_code == "PROVIDER_UNAVAILABLE"
    assert exc.value.phase.startswith("read[")


def test_persistence_overrun_fails_closed():
    clock = FakeClock()

    def slow_persist(_b: bytes) -> None:
        clock.advance(DEFAULT_BUDGET.persistence_s + 1.0)

    runner = ObservationRunner(clock=clock, persist=slow_persist)
    with pytest.raises(DeadlineExceededError) as exc:
        runner.run(_reads(3))
    assert exc.value.phase == "persistence"
    assert exc.value.retryable is True


def test_total_deadline_overrun_before_persistence_fails_closed():
    clock = FakeClock()
    runner = ObservationRunner(clock=clock)

    # Each read consumes exactly the per-read budget (allowed individually), but
    # together they exhaust the total deadline before persistence can start.
    per = DEFAULT_BUDGET.per_read_s

    def edge_read():
        clock.advance(per)
        return {"ok": True}

    # Push the total past the deadline by making the last read land beyond it.
    def last_read():
        clock.advance(DEFAULT_BUDGET.total_deadline_s)
        return {"ok": True}

    with pytest.raises(DeadlineExceededError):
        runner.run([edge_read, edge_read, last_read])


def test_partial_result_never_returned_as_success():
    clock = FakeClock()
    runner = ObservationRunner(clock=clock)
    reads = [lambda: {"ok": True}, lambda: None, lambda: {"ok": True}]
    with pytest.raises(PartialObservationError) as exc:
        runner.run(reads)
    assert exc.value.retryable is True
