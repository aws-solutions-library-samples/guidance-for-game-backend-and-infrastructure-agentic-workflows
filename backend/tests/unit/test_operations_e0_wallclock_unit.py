"""Adversarial wall-clock enforcement tests for the E0 observation runner (#412).

These tests use *real* threads and *real* (short) blocking reads to prove the
runner enforces its deadline in wall-clock time: a read that blocks past its
budget is abandoned and a typed retryable ``DeadlineExceededError`` is raised
*before* the read returns, and the runner never waits for the abandoned work to
finish. The budgets used here are deliberately tiny (tens of milliseconds) so
the suite stays fast while still exercising true concurrency and timeouts.
"""

from __future__ import annotations

# Standard library
import threading
import time

# Third-party packages
import pytest

# Local modules
from operations.validation import (
    DeadlineExceededError,
    LatencyBudget,
    ObservationRunner,
)

pytestmark = pytest.mark.unit


# A tiny real-time budget: 50 ms per read, 50 ms persistence, 50 ms margin.
FAST_BUDGET = LatencyBudget(per_read_s=0.05, persistence_s=0.05, cancellation_margin_s=0.05, ceiling_s=1.0)


def test_blocking_read_past_budget_fails_closed_before_it_returns():
    """A read that sleeps well past its per-read budget must time out.

    The runner must raise a retryable DeadlineExceededError in roughly the
    per-read budget, NOT after the full (much longer) sleep completes.
    """
    released = threading.Event()

    def hung_read():
        # Sleeps far longer than the whole request budget.
        released.wait(timeout=5.0)
        return {"ok": True}

    runner = ObservationRunner(budget=FAST_BUDGET)
    started = time.monotonic()
    with pytest.raises(DeadlineExceededError) as exc:
        runner.run([hung_read, lambda: {"ok": True}, lambda: {"ok": True}])
    elapsed = time.monotonic() - started

    assert exc.value.retryable is True
    assert exc.value.error_code == "PROVIDER_UNAVAILABLE"
    # The first read is the one that blocked.
    assert exc.value.phase == "read[0]"
    # Returned promptly (well under the 5s sleep), proving pre-emption.
    assert elapsed < 1.0
    # Let the abandoned worker exit cleanly.
    released.set()


def test_runner_does_not_wait_for_abandoned_work_after_timeout():
    """After a deadline overrun, run() returns without joining the hung read."""
    release = threading.Event()
    read_finished = threading.Event()

    def hung_read():
        release.wait(timeout=5.0)
        read_finished.set()
        return {"ok": True}

    runner = ObservationRunner(budget=FAST_BUDGET)
    with pytest.raises(DeadlineExceededError):
        runner.run([hung_read, lambda: {"ok": True}, lambda: {"ok": True}])

    # The read is still blocked; run() did not wait for it.
    assert not read_finished.is_set()
    release.set()


def test_total_deadline_enforced_across_multiple_slow_reads():
    """Reads that each exceed their budget must fail closed on a real deadline.

    Each read sleeps clearly longer than the per-read budget, so the runner must
    pre-empt on a per-read or total deadline rather than completing the batch.
    """

    def slow_read():
        time.sleep(0.2)  # 4x the 50ms per-read budget
        return {"ok": True}

    runner = ObservationRunner(budget=FAST_BUDGET)
    started = time.monotonic()
    with pytest.raises(DeadlineExceededError) as exc:
        runner.run([slow_read, slow_read, slow_read])
    elapsed = time.monotonic() - started
    # Any per-read or total phase is a correct fail-closed outcome.
    assert exc.value.phase == "read[0]" or exc.value.phase.startswith("read[") or exc.value.phase == "total"
    # Must not have waited for all three 0.2s sleeps to finish.
    assert elapsed < 0.5


def test_fast_reads_succeed_under_real_time_budget():
    """A genuinely fast run completes successfully under the real-time budget."""
    runner = ObservationRunner(budget=FAST_BUDGET)
    outcome = runner.run([lambda: {"ok": 1}, lambda: {"ok": 2}, lambda: {"ok": 3}])
    assert len(outcome.read_durations_s) == 3
    assert outcome.total_s < FAST_BUDGET.total_deadline_s
    assert outcome.record_hash.startswith("sha256:")
