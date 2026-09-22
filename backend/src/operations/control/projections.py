"""Bounded, workspace-scoped E4 read projections (issue #416).

:class:`OperationsProjectionService` produces the two read-only E4 projections a
caller sees, each validated against its immutable control-plane contract before
it is returned:

* :meth:`list_operations` — a bounded page (at most 50) of public-safe operation
  summaries drawn from the workspace catalog ``Query`` (never a ``Scan``), plus
  an opaque, HMAC-signed ``next_cursor`` a client echoes verbatim. The cursor
  encodes the DynamoDB continuation key together with the caller's
  ``workspace_id`` and ``page_size``; on the next call the service verifies the
  cursor and refuses one minted for a different workspace, so a cursor can never
  be replayed to page another tenant's operations. Any tamper/truncation is
  rejected by the HMAC.
* :meth:`get_detail` — a bounded, public-safe detail projection of one operation:
  the lifecycle phase timeline, current state, verification/rollback visibility,
  and a bounded, chronologically-ordered evidence ledger. It excludes email,
  display name, token, ARN, account id, fleet id, and any raw provider payload.

Both are strictly workspace-scoped. An operation owned by another workspace is
invisible — ``get_detail`` returns ``None`` rather than revealing existence — and
the service performs no provider read, holds no credential, and issues no write.
"""

from __future__ import annotations

# Standard library
import base64
import binascii
from typing import Any, Protocol

# Local modules
from operations.approval_store import CatalogPage
from operations.contracts import CONTRACT_VERSION
from operations.contracts.control_plane import (
    DETAIL_PROJECTION_SCHEMA_NAME,
    LIST_RESPONSE_SCHEMA_NAME,
    MAX_PAGE_SIZE,
    ControlContractError,
    CursorError,
    decode_cursor,
    encode_cursor,
    validate_control_contract,
)
from operations.evidence import OperationEvidence

# The maximum evidence entries and phase entries the detail projection surfaces;
# aligned with the detail-projection schema bounds (16 evidence, 8 phases).
_MAX_EVIDENCE_ENTRIES = 16

# The lifecycle phases, in canonical order, and the operation states that mark a
# phase reached/succeeded. This is a presentation mapping only; it never grants
# authority. Terminal failure/rejection/cancellation is reflected in phase
# status without exposing provider detail.
_PHASE_ORDER = ("prepare", "approve", "dispatch", "execute", "verify", "rollback")

# Operation states, ordered by lifecycle progress, mapped to the furthest phase
# they imply has completed successfully.
_STATE_REACHED_PHASE = {
    "prepared": "prepare",
    "pending_approval": "prepare",
    "approved": "approve",
    "dispatched": "dispatch",
    "executing": "dispatch",
    "retry_pending": "dispatch",
    "succeeded": "verify",
    "failed": "execute",
    "rejected": "approve",
    "cancelled": "prepare",
    "expired": "prepare",
}

_TERMINAL_FAILURE_STATES = frozenset({"failed", "rejected", "cancelled", "expired"})


class ProjectionError(ValueError):
    """A projection could not be produced from the given inputs (fail closed).

    Raised for an invalid/tampered/cross-workspace cursor or a projection that
    unexpectedly fails its own output contract. It is a bounded, public-safe
    error; it never carries the offending document or provider detail.
    """


class CatalogStorePort(Protocol):
    """Read-only workspace catalog Query port."""

    def query_workspace_catalog(
        self, *, workspace_id: str, limit: int, exclusive_start_key: Any = None
    ) -> CatalogPage: ...


class EvidenceStorePort(Protocol):
    """Read-only durable evidence load port."""

    def load_operation_evidence(self, operation_id: str) -> OperationEvidence | None: ...


class OperationsProjectionService:
    """Bounded, workspace-scoped list and detail projections."""

    def __init__(
        self,
        *,
        catalog_store: CatalogStorePort,
        evidence_store: EvidenceStorePort,
        cursor_key: bytes,
    ) -> None:
        if not cursor_key:
            raise ValueError("cursor_key must be a non-empty secret")
        self._catalog_store = catalog_store
        self._evidence_store = evidence_store
        self._cursor_key = cursor_key

    # -- list ------------------------------------------------------------

    def list_operations(
        self,
        *,
        workspace_id: str,
        page_size: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one bounded, public-safe operation page for the workspace."""
        bounded_page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        exclusive_start_key = self._resolve_cursor(cursor, workspace_id, bounded_page_size)

        page = self._catalog_store.query_workspace_catalog(
            workspace_id=workspace_id,
            limit=bounded_page_size,
            exclusive_start_key=exclusive_start_key,
        )

        summaries = [_operation_summary(row) for row in page.rows]
        response: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "page_size": bounded_page_size,
            "operations": summaries,
        }
        if page.last_evaluated_key is not None:
            response["next_cursor"] = self._mint_cursor(workspace_id, bounded_page_size, page.last_evaluated_key)

        self._validate(LIST_RESPONSE_SCHEMA_NAME, response)
        return response

    def _resolve_cursor(self, cursor: str | None, workspace_id: str, page_size: int) -> Any:
        if cursor is None:
            return None
        try:
            token = _wire_decode(cursor)
            position = decode_cursor(token, key=self._cursor_key)
        except CursorError as exc:
            raise ProjectionError("list cursor is invalid") from exc
        # A cursor is bound to the workspace and page size it was minted for. A
        # cursor minted for another workspace (or a changed page size) is refused
        # rather than silently paging a different scope.
        if position.get("workspace_id") != workspace_id:
            raise ProjectionError("list cursor does not belong to this workspace")
        if position.get("page_size") != page_size:
            raise ProjectionError("list cursor page size does not match the request")
        start_key = position.get("start_key")
        if not isinstance(start_key, dict):
            raise ProjectionError("list cursor is malformed")
        return start_key

    def _mint_cursor(self, workspace_id: str, page_size: int, last_evaluated_key: dict[str, Any]) -> str:
        position = {
            "workspace_id": workspace_id,
            "page_size": page_size,
            "start_key": last_evaluated_key,
        }
        try:
            token = encode_cursor(position, key=self._cursor_key)
        except CursorError as exc:  # pragma: no cover - our own position is canonicalizable
            raise ProjectionError("could not mint a list cursor") from exc
        return _wire_encode(token)

    # -- detail ----------------------------------------------------------

    def get_detail(self, *, operation_id: str, workspace_id: str) -> dict[str, Any] | None:
        """Return the bounded detail projection, or ``None`` if not visible."""
        evidence = self._evidence_store.load_operation_evidence(operation_id)
        if evidence is None:
            return None
        operation = evidence.operation
        if not isinstance(operation, dict):
            return None
        requester = operation.get("requester")
        if not isinstance(requester, dict) or requester.get("workspace_id") != workspace_id:
            # Cross-workspace or malformed ownership: invisible, never a leak.
            return None

        detail = _detail_projection(operation_id, operation, evidence)
        self._validate(DETAIL_PROJECTION_SCHEMA_NAME, detail)
        return detail

    # -- internals -------------------------------------------------------

    def _validate(self, schema_name: str, document: dict[str, Any]) -> None:
        try:
            validate_control_contract(schema_name, document)
        except ControlContractError as exc:  # pragma: no cover - our own output is server-owned
            raise ProjectionError("projection failed its output contract") from exc


# -- Pure projection builders -----------------------------------------------


def _operation_summary(row: dict[str, Any]) -> dict[str, Any]:
    """One public-safe list summary from a catalog row."""
    return {
        "operation_id": row.get("operation_id"),
        "capability_id": row.get("capability_id"),
        "state": row.get("state"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _capability_id(operation: dict[str, Any]) -> str:
    capability = operation.get("capability")
    if isinstance(capability, dict):
        capability_id = capability.get("capability_id")
        if isinstance(capability_id, str) and capability_id:
            return capability_id
    return "unknown"


def _detail_projection(operation_id: str, operation: dict[str, Any], evidence: OperationEvidence) -> dict[str, Any]:
    state = evidence.state
    created_at = operation.get("created_at") or "1970-01-01T00:00:00Z"
    updated_at = _latest_ledger_timestamp(evidence.ledger) or created_at
    return {
        "contract_version": CONTRACT_VERSION,
        "operation_id": operation_id,
        "capability_id": _capability_id(operation),
        "state": state,
        "created_at": created_at,
        "updated_at": updated_at,
        "phases": _phase_timeline(state, created_at),
        "verification": _verification_visibility(state),
        "rollback": _rollback_visibility(state),
        "evidence": _evidence_entries(evidence),
    }


def _phase_timeline(state: str, created_at: str) -> list[dict[str, Any]]:
    reached = _STATE_REACHED_PHASE.get(state, "prepare")
    reached_index = _PHASE_ORDER.index(reached) if reached in _PHASE_ORDER else 0
    is_failure = state in _TERMINAL_FAILURE_STATES
    phases: list[dict[str, Any]] = []
    for index, phase in enumerate(_PHASE_ORDER):
        if index < reached_index:
            status = "succeeded"
        elif index == reached_index:
            status = "failed" if is_failure else ("succeeded" if state == "succeeded" else "in_progress")
        else:
            status = "not_started"
        entry: dict[str, Any] = {"phase": phase, "status": status}
        if index == 0:
            entry["occurred_at"] = created_at
        phases.append(entry)
    return phases


def _verification_visibility(state: str) -> dict[str, Any]:
    if state == "succeeded":
        return {"applicable": True, "outcome": "succeeded"}
    if state == "failed":
        return {"applicable": True, "outcome": "failed"}
    if state in ("dispatched", "executing", "retry_pending"):
        return {"applicable": True, "outcome": "pending"}
    return {"applicable": False, "outcome": "not_applicable"}


def _rollback_visibility(state: str) -> dict[str, Any]:
    # Rollback is a separate inverse operation; the detail view only exposes
    # whether one applies, never provider detail. A failed execution is the only
    # state where rollback is presented as applicable-and-pending.
    if state == "failed":
        return {"applicable": True, "outcome": "pending"}
    return {"applicable": False, "outcome": "not_applicable"}


def _evidence_entries(evidence: OperationEvidence) -> list[dict[str, Any]]:
    ledger = evidence.ledger if isinstance(evidence.ledger, list) else []
    ordered = sorted(
        (entry for entry in ledger if isinstance(entry, dict)),
        key=lambda entry: (_sequence_of(entry), str(entry.get("occurred_at") or "")),
    )
    entries: list[dict[str, Any]] = []
    for entry in ordered[:_MAX_EVIDENCE_ENTRIES]:
        category = _evidence_category(entry.get("event_type"))
        item: dict[str, Any] = {
            "category": category,
            "summary": _evidence_summary(entry.get("event_type")),
        }
        occurred_at = entry.get("occurred_at")
        if isinstance(occurred_at, str) and occurred_at:
            item["recorded_at"] = occurred_at
        entries.append(item)
    return entries


def _sequence_of(entry: dict[str, Any]) -> int:
    sequence = entry.get("sequence")
    return sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else 0


def _evidence_category(event_type: Any) -> str:
    text = event_type if isinstance(event_type, str) else ""
    lowered = text.lower()
    if "approv" in lowered or "reject" in lowered:
        return "approval"
    if "dispatch" in lowered:
        return "dispatch"
    if "verif" in lowered:
        return "verification"
    if "rollback" in lowered:
        return "rollback"
    if "authoriz" in lowered:
        return "authorization"
    return "state_change"


def _evidence_summary(event_type: Any) -> str:
    text = event_type if isinstance(event_type, str) and event_type else "lifecycle event"
    # Bound the summary to the schema max (280) and keep it category-level only:
    # it is a public-safe label, never a raw provider payload.
    return text[:280]


def _latest_ledger_timestamp(ledger: list[dict[str, Any]] | None) -> str | None:
    if not isinstance(ledger, list):
        return None
    timestamps: list[str] = [
        occurred_at
        for entry in ledger
        if isinstance(entry, dict)
        for occurred_at in (entry.get("occurred_at"),)
        if isinstance(occurred_at, str) and occurred_at
    ]
    return max(timestamps) if timestamps else None


# -- Opaque cursor wire wrapping --------------------------------------------
#
# The HMAC cursor codec mints a ``payload.signature`` token whose '.' separator
# is outside the list-response ``opaque_cursor`` pattern (base64url only). We
# wrap the whole token in one more base64url layer for the wire so the emitted
# ``next_cursor`` matches the contract, and reverse it before verification. The
# wrapping is not a security boundary — the HMAC is — it is purely encoding.


def _wire_encode(token: str) -> str:
    return base64.urlsafe_b64encode(token.encode("ascii")).rstrip(b"=").decode("ascii")


def _wire_decode(cursor: str) -> str:
    if not isinstance(cursor, str) or not cursor:
        raise CursorError("cursor is empty")
    padding = "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(cursor + padding).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CursorError(f"cursor is not valid base64url: {exc}") from exc
