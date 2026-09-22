"""Workspace catalog store tests (issue #416, E4).

At E2 prepare the approval store now also writes one **workspace catalog** item
inside the SAME atomic ``TransactWriteItems`` that materializes the prepared
operation, so an operation is discoverable to the bounded E4 list projection the
instant it exists (or not at all). The catalog item is partitioned by workspace
(``PK=WS#<workspace_id>#CATALOG``, ``SK=OP#<operation_id>``) so the list uses a
bounded ``Query`` on the workspace partition — never a ``Scan``. Each item
carries the public-safe summary fields (capability, state, created_at,
updated_at). Terminal decisions (grant/reject/cancel/expire) update the catalog
item's ``state``/``updated_at`` in their own atomic transaction — keyed by the
stable ``OP#<operation_id>`` sort key so no chronological lookup is needed — so
the catalog stays consistent with the authoritative state snapshot.

These tests assert: the catalog item is written transactionally at prepare, the
list is a workspace-partitioned Query (no Scan ever), it is bounded, it
paginates via the DynamoDB LastEvaluatedKey, cross-workspace rows are invisible,
and a terminal decision updates the catalog projection.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.approval_store import DynamoDbApprovalStore, PersistOutcome
from operations.contracts.capacity import CAPABILITY_ID

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TABLE = "operations-table"
_WORKSPACE = "ws-alpha"


class _FakeDynamo:
    """A minimal in-memory DynamoDB double: transact writes, get, and query."""

    def __init__(self) -> None:
        # key: (PK, SK) -> item (marshalled attribute-value map)
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.scans = 0
        self.queries: list[dict[str, Any]] = []

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        staged: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                staged[key] = item
            elif "Update" in entry:
                upd = entry["Update"]
                key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
                base = self.items.get(key) or staged.get(key)
                existing = dict(base) if base else {"PK": upd["Key"]["PK"], "SK": upd["Key"]["SK"]}
                values = upd.get("ExpressionAttributeValues", {})
                names = upd.get("ExpressionAttributeNames", {})
                if ":new" in values:
                    existing[names.get("#s", "state")] = values[":new"]
                if ":seq" in values:
                    existing[names.get("#seq", "sequence")] = values[":seq"]
                if ":cat_state" in values:
                    existing["state"] = values[":cat_state"]
                if ":cat_updated" in values:
                    existing["updated_at"] = values[":cat_updated"]
                staged[key] = existing
        self.items.update(staged)
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        key = (Key["PK"]["S"], Key["SK"]["S"])
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        pk = kwargs["ExpressionAttributeValues"][":pk"]["S"]
        forward = kwargs.get("ScanIndexForward", True)
        limit = kwargs.get("Limit")
        rows = [item for (p, _s), item in self.items.items() if p == pk]
        rows.sort(key=lambda it: it["SK"]["S"], reverse=not forward)
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            start_sk = start["SK"]["S"]
            rows = [r for r in rows if (r["SK"]["S"] > start_sk if forward else r["SK"]["S"] < start_sk)]
        page = rows[:limit] if limit else rows
        response: dict[str, Any] = {"Items": page}
        if limit and len(rows) > limit:
            last = page[-1]
            response["LastEvaluatedKey"] = {"PK": last["PK"], "SK": last["SK"]}
        return response

    def scan(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - must never be called
        self.scans += 1
        raise AssertionError("the catalog list must never issue a Scan")


def _store(dynamo: _FakeDynamo) -> DynamoDbApprovalStore:
    return DynamoDbApprovalStore(client=dynamo, table_name=_TABLE, clock=lambda: _NOW)


def _prepared_operation(operation_id: str, created_at: str) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "prepared_hash": f"sha256:{'0' * 64}",
        "capability": {"capability_id": CAPABILITY_ID, "capability_version": "1.0"},
        "created_at": created_at,
        "expires_at": created_at,
    }


def _persist(store: DynamoDbApprovalStore, dynamo: _FakeDynamo, *, operation_id: str, created_at: str) -> Any:
    operation = _prepared_operation(operation_id, created_at)
    return store.persist_prepared_operation(
        prepared_operation=operation,
        prepared_hash=operation["prepared_hash"],
        workspace_id=_WORKSPACE,
        idempotency_token=f"tok-{operation_id}",
        idempotency_fingerprint=f"fp-{operation_id}",
        state_change={"event": "materialized"},
        ledger_event={"event_type": "prepared", "occurred_at": created_at},
        commit_not_after=_NOW + timedelta(minutes=5),
    )


# -- Transactional catalog write at prepare --------------------------------


def test_prepare_writes_catalog_item_in_same_transaction() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    result = _persist(store, dynamo, operation_id="op_" + "a" * 26, created_at="2026-01-01T12:00:00Z")
    assert result.outcome is PersistOutcome.PERSISTED
    catalog_pk = f"WS#{_WORKSPACE}#CATALOG"
    catalog_items = [item for (pk, _sk), item in dynamo.items.items() if pk == catalog_pk]
    assert len(catalog_items) == 1
    item = catalog_items[0]
    assert item["operation_id"]["S"] == "op_" + "a" * 26
    assert item["state"]["S"] == "pending_approval"
    assert item["capability_id"]["S"] == CAPABILITY_ID
    assert item["created_at"]["S"] == "2026-01-01T12:00:00Z"


def test_list_uses_query_never_scan() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    _persist(store, dynamo, operation_id="op_" + "a" * 26, created_at="2026-01-01T12:00:00Z")
    page = store.query_workspace_catalog(workspace_id=_WORKSPACE, limit=10)
    assert dynamo.scans == 0
    assert len(dynamo.queries) == 1
    assert dynamo.queries[0]["ExpressionAttributeValues"][":pk"]["S"] == f"WS#{_WORKSPACE}#CATALOG"
    assert [row["operation_id"] for row in page.rows] == ["op_" + "a" * 26]
    assert page.last_evaluated_key is None


def test_list_paginates_via_last_evaluated_key() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    ids = []
    for i in range(3):
        oid = f"op_{'b' * 25}{i}"
        ids.append(oid)
        _persist(store, dynamo, operation_id=oid, created_at=f"2026-01-01T12:00:0{i}Z")

    first = store.query_workspace_catalog(workspace_id=_WORKSPACE, limit=2)
    assert len(first.rows) == 2
    assert first.last_evaluated_key is not None

    second = store.query_workspace_catalog(
        workspace_id=_WORKSPACE, limit=2, exclusive_start_key=first.last_evaluated_key
    )
    assert len(second.rows) == 1
    assert second.last_evaluated_key is None
    seen = {r["operation_id"] for r in first.rows} | {r["operation_id"] for r in second.rows}
    assert seen == set(ids)


def test_cross_workspace_rows_are_invisible() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    _persist(store, dynamo, operation_id="op_" + "a" * 26, created_at="2026-01-01T12:00:00Z")
    page = store.query_workspace_catalog(workspace_id="ws-other", limit=10)
    assert page.rows == []


def test_terminal_decision_updates_catalog_projection() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    oid = "op_" + "c" * 26
    _persist(store, dynamo, operation_id=oid, created_at="2026-01-01T12:00:00Z")

    # Local modules
    from operations.decisions import DecisionCommitOutcome

    outcome = store.record_terminal_decision(
        operation_id=oid,
        expected_prepared_operation_hash=f"sha256:{'0' * 64}",
        expected_state="pending_approval",
        new_state="cancelled",
        commit_not_after=_NOW + timedelta(minutes=5),
        state_change={"event": "cancelled"},
        ledger_event={"event_type": "cancelled", "occurred_at": "2026-01-01T12:01:00Z"},
        approval=None,
        workspace_id=_WORKSPACE,
    )
    assert outcome is DecisionCommitOutcome.RECORDED
    page = store.query_workspace_catalog(workspace_id=_WORKSPACE, limit=10)
    assert page.rows[0]["state"] == "cancelled"


def test_terminal_decision_without_workspace_skips_catalog_update() -> None:
    # Backward compatible: an existing caller that does not pass workspace_id
    # still commits the authoritative terminal transition (the catalog update is
    # additive and simply omitted).
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    oid = "op_" + "d" * 26
    _persist(store, dynamo, operation_id=oid, created_at="2026-01-01T12:00:00Z")

    # Local modules
    from operations.decisions import DecisionCommitOutcome

    outcome = store.record_terminal_decision(
        operation_id=oid,
        expected_prepared_operation_hash=f"sha256:{'0' * 64}",
        expected_state="pending_approval",
        new_state="rejected",
        commit_not_after=_NOW + timedelta(minutes=5),
        state_change={"event": "rejected"},
        ledger_event={"event_type": "rejected", "occurred_at": "2026-01-01T12:01:00Z"},
        approval=None,
    )
    assert outcome is DecisionCommitOutcome.RECORDED


def test_query_limit_is_bounded_to_max_page_size() -> None:
    dynamo = _FakeDynamo()
    store = _store(dynamo)
    # A caller cannot force an unbounded query: the limit is clamped to 50.
    store.query_workspace_catalog(workspace_id=_WORKSPACE, limit=10_000)
    assert dynamo.queries[0]["Limit"] <= 50
