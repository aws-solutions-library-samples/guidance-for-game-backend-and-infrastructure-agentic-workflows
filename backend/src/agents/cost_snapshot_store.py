"""Shared snapshot store abstraction for deterministic cost report reuse.

A :class:`CostSnapshotStore` persists immutable, validated cost report snapshots
so a report ID created by one AgentCore worker resolves consistently from any
healthy worker for the configured TTL, without re-issuing a Cost Explorer query.

Two implementations are provided:

* :class:`~agents.cost_snapshot_memory.InMemoryCostSnapshotStore` — a bounded,
  scope-aware TTL cache used for local development and tests, and
* :class:`~agents.cost_snapshot_dynamodb.DynamoDbCostSnapshotStore` — an
  encrypted, TTL-managed Amazon DynamoDB table used in shared cloud runtimes.

Both enforce the same contract: a snapshot is only returned when the caller's
scope hash matches the one stored with it; every other outcome (unknown,
expired, malformed, or wrong scope) resolves to ``None`` so the caller can fail
closed with a single generic response.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Imported lazily at runtime to avoid a circular import with agents.cost_report.
    # Local modules
    from agents.cost_report import CostReportSnapshot


@dataclass(frozen=True)
class SnapshotRecord:
    """A snapshot plus the minimum metadata needed for scoped, bounded reuse."""

    report_id: str
    scope_hash: str
    snapshot: "CostReportSnapshot"
    expires_at: int  # Unix epoch seconds after which the snapshot must not be reused.


class CostSnapshotStoreError(RuntimeError):
    """A store-level infrastructure failure (write or read could not complete).

    Raised so the caller can fail closed: a create must not return a report ID
    that was never durably stored, and a reuse must resolve to a generic
    not-found rather than silently succeeding on partial data.
    """


class SnapshotCollisionError(CostSnapshotStoreError):
    """A report ID already exists in the store with a different (or identical) item.

    Snapshots are immutable: a conditional write that fails because the key is
    already present is surfaced as a typed collision so the caller fails closed
    instead of overwriting a previously stored snapshot. A collision must never
    trigger a re-query of Cost Explorer.
    """


@runtime_checkable
class CostSnapshotStore(Protocol):
    """Persistence contract for scoped, TTL-bounded report snapshots."""

    # Whether this store requires a trusted (authenticated, non-anonymous) scope
    # before a snapshot may be written or resolved. Shared, cross-tenant stores
    # set this True; the process-local in-memory store used for local/tests
    # leaves it False so unscoped local flows stay deterministic.
    requires_trusted_scope: bool

    def put(self, record: SnapshotRecord) -> None:
        """Durably store a snapshot record.

        Raises:
            SnapshotCollisionError: if a record already exists for the report ID
                (snapshots are immutable and must not be overwritten).
            CostSnapshotStoreError: if the record could not be durably written.
        """

    def get(self, report_id: str, scope_hash: str) -> "CostReportSnapshot | None":
        """Return the snapshot for ``report_id`` only if ``scope_hash`` matches.

        Returns ``None`` for unknown, expired, malformed, or wrong-scope
        identifiers so callers can present a single generic not-found response.

        Raises:
            CostSnapshotStoreError: if the read could not complete due to an
                infrastructure failure (distinct from a clean not-found).
        """


def build_default_snapshot_store() -> CostSnapshotStore:
    """Build the snapshot store implied by the current configuration.

    When a DynamoDB table name is configured the shared, encrypted store is
    used. Otherwise, if the shared store is *required* (hosted deployments set
    ``GBAW_COST_SNAPSHOT_REQUIRED=true``), this fails closed rather than silently
    degrading to a process-local cache that cannot satisfy cross-worker reuse.
    Only when the shared store is not required does it fall back to an in-memory
    TTL store for local development and tests.

    Implementations are imported lazily to keep this module free of a boto3
    import and of a circular dependency on :mod:`agents.cost_report`.
    """
    # Local modules
    from config.settings import (
        COST_SNAPSHOT_STORE_REQUIRED,
        COST_SNAPSHOT_TABLE_NAME,
        COST_SNAPSHOT_TTL_SECONDS,
    )

    if COST_SNAPSHOT_TABLE_NAME:
        # Local modules
        from agents.cost_snapshot_dynamodb import DynamoDbCostSnapshotStore

        return DynamoDbCostSnapshotStore(COST_SNAPSHOT_TABLE_NAME)

    if COST_SNAPSHOT_STORE_REQUIRED:
        raise CostSnapshotStoreError(
            "shared cost report snapshot store is required but no table name is configured; "
            "the deployment must provide GBAW_COST_SNAPSHOT_TABLE_NAME"
        )

    # Local modules
    from agents.cost_snapshot_memory import InMemoryCostSnapshotStore

    return InMemoryCostSnapshotStore(ttl_seconds=COST_SNAPSHOT_TTL_SECONDS)
