"""Bounded list/detail/ledger projection service tests (issue #416, E4).

:class:`~operations.control.projections.OperationsProjectionService` produces the
read-only E4 projections a caller sees:

* ``list_operations`` — a bounded page (max 50) of public-safe operation
  summaries from the workspace catalog Query, plus an opaque, HMAC-signed
  ``next_cursor`` a client echoes verbatim. The cursor is bound to the caller's
  workspace and page size, so a cursor minted for one workspace cannot be
  replayed against another, and any tamper/truncation is rejected.
* ``get_detail`` — a bounded, public-safe detail projection of one operation
  showing the lifecycle phase timeline, current state, verification and rollback
  visibility, and a bounded, ordered evidence ledger. It excludes email, display
  name, token, ARN, account id, fleet id, and any raw provider payload.

Both are workspace-scoped: an operation owned by another workspace is invisible
(``None`` / not present), never a cross-workspace leak. Every emitted document
validates against its immutable control-plane contract.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.approval_store import CatalogPage
from operations.contracts.capacity import CAPABILITY_ID
from operations.contracts.control_plane import (
    DETAIL_PROJECTION_SCHEMA_NAME,
    LIST_RESPONSE_SCHEMA_NAME,
    CursorError,
    decode_cursor,
    validate_control_contract,
)
from operations.control.projections import OperationsProjectionService, ProjectionError
from operations.evidence import OperationEvidence

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_KEY = b"cursor-signing-key-please-keep-secret"
_WORKSPACE = "ws-alpha"


def _catalog_row(oid: str, state: str = "pending_approval") -> dict[str, Any]:
    return {
        "operation_id": oid,
        "capability_id": CAPABILITY_ID,
        "state": state,
        "created_at": "2026-01-01T12:00:00Z",
        "updated_at": "2026-01-01T12:00:00Z",
        "workspace_id": _WORKSPACE,
    }


class _FakeCatalogStore:
    def __init__(self, page: CatalogPage) -> None:
        self._page = page
        self.calls: list[dict[str, Any]] = []

    def query_workspace_catalog(self, *, workspace_id: str, limit: int, exclusive_start_key: Any = None) -> CatalogPage:
        self.calls.append({"workspace_id": workspace_id, "limit": limit, "exclusive_start_key": exclusive_start_key})
        return self._page


class _FakeEvidenceStore:
    def __init__(self, evidence: OperationEvidence | None) -> None:
        self._evidence = evidence

    def load_operation_evidence(self, operation_id: str) -> OperationEvidence | None:
        return self._evidence


def _service(catalog: Any, evidence: Any = None) -> OperationsProjectionService:
    return OperationsProjectionService(
        catalog_store=catalog,
        evidence_store=evidence or _FakeEvidenceStore(None),
        cursor_key=_KEY,
    )


def _full_operation(oid: str, workspace_id: str = _WORKSPACE) -> dict[str, Any]:
    """A prepared-operation-shaped document with SECRET fields the projection must drop."""
    return {
        "operation_id": oid,
        "capability": {"capability_id": CAPABILITY_ID, "capability_version": "1.0"},
        "created_at": "2026-01-01T12:00:00Z",
        "expires_at": "2026-01-01T12:15:00Z",
        "requester": {
            "subject_id": "user-1",
            "client_id": "client-1",
            "tenant_id": "tenant-1",
            "workspace_id": workspace_id,
            "email": "secret@example.com",
            "display_name": "Secret Person",
        },
        "target": {"fleet_id": "fleet-SECRET-1234", "location": "us-west-2"},
        "resource_enrollment": {"fleet_arn": "arn:aws:gamelift:us-west-2:111122223333:fleet/x"},
        "authority": {"effective_authority": "advise", "decision": "approval_required", "reason_codes": ["X"]},
        "raw_provider_payload": {"AccountId": "111122223333", "token": "TOKEN-SECRET"},
    }


# -- list_operations --------------------------------------------------------


def test_list_returns_bounded_public_safe_summaries() -> None:
    page = CatalogPage(rows=[_catalog_row("op_" + "a" * 26)], last_evaluated_key=None)
    service = _service(_FakeCatalogStore(page))
    response = service.list_operations(workspace_id=_WORKSPACE, page_size=25)
    validate_control_contract(LIST_RESPONSE_SCHEMA_NAME, response)
    assert response["page_size"] == 25
    assert len(response["operations"]) == 1
    summary = response["operations"][0]
    assert set(summary) == {"operation_id", "capability_id", "state", "created_at", "updated_at"}
    assert "next_cursor" not in response


def test_list_page_size_is_clamped_to_50() -> None:
    page = CatalogPage(rows=[], last_evaluated_key=None)
    catalog = _FakeCatalogStore(page)
    service = _service(catalog)
    response = service.list_operations(workspace_id=_WORKSPACE, page_size=10_000)
    assert response["page_size"] <= 50
    assert catalog.calls[0]["limit"] <= 50


def test_list_mints_opaque_next_cursor_bound_to_workspace() -> None:
    # Standard library
    import base64

    last_key = {"PK": {"S": f"WS#{_WORKSPACE}#CATALOG"}, "SK": {"S": "OP#op_" + "a" * 26}}
    page = CatalogPage(rows=[_catalog_row("op_" + "a" * 26)], last_evaluated_key=last_key)
    service = _service(_FakeCatalogStore(page))
    response = service.list_operations(workspace_id=_WORKSPACE, page_size=1)
    validate_control_contract(LIST_RESPONSE_SCHEMA_NAME, response)
    cursor = response["next_cursor"]
    # next_cursor is a pattern-safe base64url wrapper over the HMAC codec token.
    # Unwrap once, then verify the signed position is bound to the workspace.
    padding = "=" * (-len(cursor) % 4)
    token = base64.urlsafe_b64decode(cursor + padding).decode("ascii")
    position = decode_cursor(token, key=_KEY)
    assert position["workspace_id"] == _WORKSPACE
    assert position["start_key"] == last_key


def test_list_accepts_own_cursor_and_passes_start_key() -> None:
    last_key = {"PK": {"S": f"WS#{_WORKSPACE}#CATALOG"}, "SK": {"S": "OP#op_" + "a" * 26}}
    page1 = CatalogPage(rows=[_catalog_row("op_" + "a" * 26)], last_evaluated_key=last_key)
    catalog = _FakeCatalogStore(page1)
    service = _service(catalog)
    first = service.list_operations(workspace_id=_WORKSPACE, page_size=1)
    cursor = first["next_cursor"]

    catalog2 = _FakeCatalogStore(CatalogPage(rows=[], last_evaluated_key=None))
    service2 = _service(catalog2)
    service2.list_operations(workspace_id=_WORKSPACE, page_size=1, cursor=cursor)
    assert catalog2.calls[0]["exclusive_start_key"] == last_key


def test_list_rejects_tampered_cursor() -> None:
    last_key = {"PK": {"S": f"WS#{_WORKSPACE}#CATALOG"}, "SK": {"S": "OP#op_" + "a" * 26}}
    page = CatalogPage(rows=[_catalog_row("op_" + "a" * 26)], last_evaluated_key=last_key)
    service = _service(_FakeCatalogStore(page))
    cursor = service.list_operations(workspace_id=_WORKSPACE, page_size=1)["next_cursor"]
    tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    with pytest.raises(ProjectionError):
        service.list_operations(workspace_id=_WORKSPACE, page_size=1, cursor=tampered)


def test_list_rejects_cursor_minted_for_another_workspace() -> None:
    last_key = {"PK": {"S": "WS#ws-other#CATALOG"}, "SK": {"S": "OP#op_" + "a" * 26}}
    page = CatalogPage(rows=[_catalog_row("op_" + "a" * 26)], last_evaluated_key=last_key)
    service = _service(_FakeCatalogStore(page))
    other_cursor = service.list_operations(workspace_id="ws-other", page_size=1)["next_cursor"]
    # Replaying ws-other's cursor against ws-alpha must be refused.
    with pytest.raises(ProjectionError):
        service.list_operations(workspace_id=_WORKSPACE, page_size=1, cursor=other_cursor)


# -- get_detail -------------------------------------------------------------


def test_detail_is_public_safe_and_valid() -> None:
    oid = "op_" + "a" * 26
    evidence = OperationEvidence(
        operation=_full_operation(oid),
        prepared_hash="sha256:" + "0" * 64,
        state="approved",
        approval={"approval_id": "apr_x", "decision": "granted", "decided_at": "2026-01-01T12:05:00Z"},
        ledger=[
            {"sequence": 0, "event_type": "operation.prepared", "occurred_at": "2026-01-01T12:00:00Z"},
            {"sequence": 1, "event_type": "operation.approved", "occurred_at": "2026-01-01T12:05:00Z"},
        ],
    )
    service = _service(_FakeCatalogStore(CatalogPage([], None)), _FakeEvidenceStore(evidence))
    detail = service.get_detail(operation_id=oid, workspace_id=_WORKSPACE)
    assert detail is not None
    validate_control_contract(DETAIL_PROJECTION_SCHEMA_NAME, detail)
    blob = repr(detail)
    for secret in (
        "secret@example.com",
        "Secret Person",
        "fleet-SECRET-1234",
        "111122223333",
        "TOKEN-SECRET",
        "arn:aws:gamelift",
    ):
        assert secret not in blob


def test_detail_ledger_is_ordered_by_sequence() -> None:
    oid = "op_" + "a" * 26
    evidence = OperationEvidence(
        operation=_full_operation(oid),
        prepared_hash="sha256:" + "0" * 64,
        state="approved",
        approval=None,
        ledger=[
            {"sequence": 1, "event_type": "operation.approved", "occurred_at": "2026-01-01T12:05:00Z"},
            {"sequence": 0, "event_type": "operation.prepared", "occurred_at": "2026-01-01T12:00:00Z"},
        ],
    )
    service = _service(_FakeCatalogStore(CatalogPage([], None)), _FakeEvidenceStore(evidence))
    detail = service.get_detail(operation_id=oid, workspace_id=_WORKSPACE)
    assert detail is not None
    recorded = [e.get("recorded_at") for e in detail["evidence"] if e.get("recorded_at")]
    assert recorded == sorted(recorded)


def test_detail_cross_workspace_is_invisible() -> None:
    oid = "op_" + "a" * 26
    evidence = OperationEvidence(
        operation=_full_operation(oid, workspace_id="ws-other"),
        prepared_hash="sha256:" + "0" * 64,
        state="approved",
        approval=None,
        ledger=[],
    )
    service = _service(_FakeCatalogStore(CatalogPage([], None)), _FakeEvidenceStore(evidence))
    assert service.get_detail(operation_id=oid, workspace_id=_WORKSPACE) is None


def test_detail_missing_operation_is_none() -> None:
    service = _service(_FakeCatalogStore(CatalogPage([], None)), _FakeEvidenceStore(None))
    assert service.get_detail(operation_id="op_" + "z" * 26, workspace_id=_WORKSPACE) is None
