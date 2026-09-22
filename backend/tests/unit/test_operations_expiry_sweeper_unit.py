"""Periodic expiry-sweeper tests (issue #416, E4).

:class:`~operations.control.expiry_sweeper.ExpirySweeper` periodically transitions
due prepared operations to ``expired``. It:

* enumerates candidate operations by paging the bounded workspace catalog
  ``Query`` (never a ``Scan``), across the configured workspaces;
* considers only non-terminal candidate states (a terminal operation is skipped
  cheaply, never re-expired);
* reuses the SAME fenced, atomic expiry writer as the E2 lifecycle
  (``expire_if_due``), so a not-yet-due operation is a no-op and a due one is
  transitioned under the existing conditional/transactional guarantees; and
* is bounded — it processes at most a bounded number of pages/operations per run
  and swallows a per-operation failure so one bad operation cannot stall the
  sweep.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.approval_store import CatalogPage
from operations.control.expiry_sweeper import ExpirySweeper, SweepResult

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_WORKSPACE = "ws-alpha"


def _row(oid: str, state: str) -> dict[str, Any]:
    return {"operation_id": oid, "state": state, "workspace_id": _WORKSPACE}


class _FakeCatalog:
    def __init__(self, pages: list[CatalogPage]) -> None:
        self._pages = pages
        self.scans = 0
        self.queries = 0

    def query_workspace_catalog(self, *, workspace_id: str, limit: int, exclusive_start_key: Any = None) -> CatalogPage:
        self.queries += 1
        index = 0 if exclusive_start_key is None else int(exclusive_start_key["i"])
        return self._pages[index] if index < len(self._pages) else CatalogPage([], None)

    def scan(self, **kwargs: Any) -> Any:  # pragma: no cover
        self.scans += 1
        raise AssertionError("expiry sweeper must never Scan")


class _FakeDecisionService:
    def __init__(self, *, due: set[str]) -> None:
        self._due = due
        self.expired: list[str] = []
        self.raise_for: set[str] = set()

    def expire_if_due(self, request: Any) -> dict[str, Any] | None:
        oid = request.operation_id
        if oid in self.raise_for:
            raise RuntimeError("transient")
        if oid in self._due:
            self.expired.append(oid)
            return {"operation_id": oid, "new_state": "expired"}
        return None  # not due: no-op


def _sweeper(catalog: Any, decisions: Any, **overrides: Any) -> ExpirySweeper:
    kwargs: dict[str, Any] = {
        "catalog_store": catalog,
        "decision_service": decisions,
        "workspaces": [_WORKSPACE],
        "page_size": 50,
        "max_operations": 500,
    }
    kwargs.update(overrides)
    return ExpirySweeper(**kwargs)


def test_expires_due_and_skips_not_due() -> None:
    page = CatalogPage(
        rows=[_row("op_" + "a" * 26, "pending_approval"), _row("op_" + "b" * 26, "approved")],
        last_evaluated_key=None,
    )
    catalog = _FakeCatalog([page])
    decisions = _FakeDecisionService(due={"op_" + "a" * 26})
    result = _sweeper(catalog, decisions).run()
    assert isinstance(result, SweepResult)
    assert result.expired == 1
    assert result.considered == 2
    assert decisions.expired == ["op_" + "a" * 26]
    assert catalog.scans == 0


def test_terminal_states_are_skipped_cheaply() -> None:
    page = CatalogPage(
        rows=[_row("op_" + "c" * 26, "succeeded"), _row("op_" + "d" * 26, "cancelled")],
        last_evaluated_key=None,
    )
    catalog = _FakeCatalog([page])
    decisions = _FakeDecisionService(due=set())
    result = _sweeper(catalog, decisions).run()
    # Terminal operations are never handed to expire_if_due.
    assert result.considered == 0
    assert decisions.expired == []


def test_pages_through_catalog_via_query() -> None:
    page0 = CatalogPage(rows=[_row("op_" + "a" * 26, "pending_approval")], last_evaluated_key={"i": "1"})
    page1 = CatalogPage(rows=[_row("op_" + "b" * 26, "pending_approval")], last_evaluated_key=None)
    catalog = _FakeCatalog([page0, page1])
    decisions = _FakeDecisionService(due={"op_" + "a" * 26, "op_" + "b" * 26})
    result = _sweeper(catalog, decisions).run()
    assert result.expired == 2
    assert catalog.queries == 2
    assert catalog.scans == 0


def test_per_operation_failure_does_not_stall_sweep() -> None:
    page = CatalogPage(
        rows=[_row("op_" + "a" * 26, "pending_approval"), _row("op_" + "b" * 26, "pending_approval")],
        last_evaluated_key=None,
    )
    catalog = _FakeCatalog([page])
    decisions = _FakeDecisionService(due={"op_" + "a" * 26, "op_" + "b" * 26})
    decisions.raise_for = {"op_" + "a" * 26}
    result = _sweeper(catalog, decisions).run()
    # The failing op is counted as an error; the other still expires.
    assert result.errors == 1
    assert "op_" + "b" * 26 in decisions.expired


def test_bounded_by_max_operations() -> None:
    rows = [_row(f"op_{'a' * 25}{i % 10}", "pending_approval") for i in range(100)]
    page = CatalogPage(rows=rows, last_evaluated_key={"i": "1"})
    page2 = CatalogPage(rows=rows, last_evaluated_key={"i": "1"})  # would loop forever if unbounded
    catalog = _FakeCatalog([page, page2, page2, page2])
    decisions = _FakeDecisionService(due=set())
    result = _sweeper(catalog, decisions, max_operations=100).run()
    assert result.considered <= 100
