"""Periodic expiry sweeper over the bounded workspace catalog (issue #416, E4).

:class:`ExpirySweeper` periodically transitions due prepared operations to
``expired``. It is the batch counterpart of the on-request E2 expiry: instead of
waiting for a caller to touch a stale operation, a scheduled invocation walks the
workspace catalog and expires everything that is due.

It is deliberately built on the two existing, already-verified primitives:

* the bounded workspace catalog ``Query`` (``query_workspace_catalog``) — so
  enumeration is a paged, workspace-partitioned read and NEVER a ``Scan``; and
* the fenced, atomic E2 expiry writer (``LifecycleDecisionService.expire_if_due``)
  — so each transition keeps the exact conditional/transactional
  one-terminal-transition guarantee the lifecycle already enforces. A not-yet-due
  operation is a no-op; a due one is transitioned; a racing writer loses the CAS
  safely.

The sweep is bounded (at most ``max_operations`` per run across at most a derived
page budget) and resilient: a per-operation failure is counted and skipped so one
bad operation cannot stall the whole run. Terminal operations are skipped cheaply
without touching the writer.
"""

from __future__ import annotations

# Standard library
import logging
from dataclasses import dataclass
from typing import Any, Protocol

# Local modules
from operations.approval_store import CatalogPage
from operations.decisions import DecisionRequest

_LOGGER = logging.getLogger(__name__)

# States that are already terminal and are never re-expired.
_TERMINAL_STATES = frozenset({"succeeded", "failed", "rejected", "cancelled", "expired"})

_MAX_PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class SweepResult:
    """The bounded outcome of one sweep run."""

    considered: int
    expired: int
    errors: int
    pages: int


class CatalogStorePort(Protocol):
    def query_workspace_catalog(
        self, *, workspace_id: str, limit: int, exclusive_start_key: Any = None
    ) -> CatalogPage: ...


class DecisionServicePort(Protocol):
    def expire_if_due(self, request: DecisionRequest) -> dict[str, Any] | None: ...


class ExpirySweeper:
    """Expire due operations by paging the catalog and reusing the fenced writer."""

    def __init__(
        self,
        *,
        catalog_store: CatalogStorePort,
        decision_service: DecisionServicePort,
        workspaces: list[str],
        page_size: int = _MAX_PAGE_SIZE,
        max_operations: int = 5000,
    ) -> None:
        if not workspaces:
            raise ValueError("workspaces must be a non-empty list")
        if max_operations <= 0:
            raise ValueError("max_operations must be positive")
        self._catalog_store = catalog_store
        self._decision_service = decision_service
        self._workspaces = list(workspaces)
        self._page_size = max(1, min(int(page_size), _MAX_PAGE_SIZE))
        self._max_operations = int(max_operations)

    def run(self) -> SweepResult:
        """Sweep the configured workspaces once, bounded and resilient."""
        considered = 0
        expired = 0
        errors = 0
        pages = 0

        for workspace_id in self._workspaces:
            exclusive_start_key: Any = None
            while considered < self._max_operations:
                page = self._catalog_store.query_workspace_catalog(
                    workspace_id=workspace_id,
                    limit=self._page_size,
                    exclusive_start_key=exclusive_start_key,
                )
                pages += 1
                for row in page.rows:
                    if considered >= self._max_operations:
                        break
                    state = row.get("state")
                    operation_id = row.get("operation_id")
                    if not isinstance(operation_id, str) or state in _TERMINAL_STATES:
                        # Terminal (or malformed) rows are skipped without touching
                        # the writer — cheap and never re-expiring a terminal op.
                        continue
                    considered += 1
                    try:
                        result = self._decision_service.expire_if_due(DecisionRequest(operation_id=operation_id))
                    except Exception:  # noqa: BLE001 - one bad op must not stall the sweep
                        _LOGGER.warning("expiry sweep skipped an operation after a transient failure")
                        errors += 1
                        continue
                    if result is not None:
                        expired += 1

                exclusive_start_key = page.last_evaluated_key
                if exclusive_start_key is None:
                    break

        return SweepResult(considered=considered, expired=expired, errors=errors, pages=pages)
