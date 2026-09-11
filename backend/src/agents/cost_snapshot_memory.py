"""In-memory, scope-aware snapshot store for local development and tests.

This mirrors the bounded, TTL behavior the runtime historically relied on, while
adding the same scope enforcement and the same *logical* expiry semantics the
shared store applies. It is process-local: it does not solve cross-worker reuse
(that is the DynamoDB store's job), but it keeps local and test flows
deterministic and free of any AWS dependency.

Expiry is driven by each record's ``expires_at`` (an absolute Unix epoch second)
compared against an injectable clock, exactly like the DynamoDB store — so a
snapshot's logical lifetime is identical regardless of backend, and tests can
assert exact expiry boundaries with a fixed clock.
"""

from __future__ import annotations

# Standard library
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Callable

# Local modules
from agents.cost_snapshot_store import SnapshotRecord

if TYPE_CHECKING:
    # Local modules
    from agents.cost_report import CostReportSnapshot


class InMemoryCostSnapshotStore:
    """Thread-safe, bounded store that enforces scope and exact logical expiry."""

    # Process-local store does not require a trusted scope: local development and
    # unit tests run unscoped and must remain deterministic. The shared DynamoDB
    # store is the one that enforces trusted scope.
    requires_trusted_scope = False

    def __init__(
        self,
        *,
        maxsize: int = 128,
        ttl_seconds: int = 1800,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._cache: "OrderedDict[str, SnapshotRecord]" = OrderedDict()
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._lock = threading.RLock()

    def put(self, record: SnapshotRecord) -> None:
        with self._lock:
            self._cache[record.report_id] = record
            self._cache.move_to_end(record.report_id)
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def get(self, report_id: str, scope_hash: str) -> "CostReportSnapshot | None":
        with self._lock:
            record = self._cache.get(report_id)
            # Honor the record's absolute expiry against the injected clock, the
            # same way the DynamoDB store does; drop the entry once expired.
            if record is not None and record.expires_at <= int(self._now()):
                del self._cache[report_id]
                record = None
        # A scope mismatch is indistinguishable from a miss to the caller, which
        # fails closed with a generic not-found.
        if record is None or record.scope_hash != scope_hash:
            return None
        return record.snapshot

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
