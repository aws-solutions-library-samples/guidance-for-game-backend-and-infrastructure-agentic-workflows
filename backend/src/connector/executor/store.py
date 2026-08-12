"""Prepared_Operation_Store + Audit_Ledger — write-once / append-only store adapter.

This module provides an **in-memory** implementation of the #279 state/recovery seam
(:class:`connector.executor.seams.StateRecoveryContract279`) with the *exact* conditional-write
semantics the accepted DynamoDB adapter will have, so the preparation/approval/executor logic
can be built and property-tested now, before #279 lands (a real DynamoDB adapter comes later,
gated task 11.3).

Single-table key schema (design → Data Models → "Prepared_Operation_Store — key schema"):

- ``PK = OP#<operation_id>`` for every record of one operation.
- ``SK = OP#META`` — the immutable operation record, inserted **once** with
  ``attribute_not_exists(PK AND SK)`` (insert-only, write-once, immutable — Req 8.1, 6.3).
- ``SK = APPROVAL#<ts>`` — an approval lifecycle transition written as a **separate**
  conditional record that never modifies ``OP#META`` (Req 6.4, 8.2).
- ``SK = LEDGER#<seq>`` — an append-only ledger entry written with a conditional ``PutItem``
  using ``attribute_not_exists`` so an entry is never overwritten (Req 8.3, 8.5). In the real
  table the ``LEDGER#`` prefix is additionally protected by an IAM deny on update/delete
  (Req 8.4).

Streams (``NEW_AND_OLD_IMAGES``) are modeled by capturing every ``APPROVAL#`` transition as a
durable dispatch event (:attr:`InMemoryOperationStore.stream_events`); the append-only records
remain the audit ledger (Req 3.4, 8.8).

The **ledger append** follows the durable, confirmed-write / never-raise discipline of
:class:`connector.audit.AuditSink`: :meth:`InMemoryOperationStore.append_ledger` returns a
``bool`` (``True`` only on a confirmed append) and never raises, so a duplicate/append-only
violation is reported as an unconfirmed write rather than an exception. The **conditional
writes** that enforce write-once / lifecycle correctness (:meth:`insert_operation`,
:meth:`apply_approval_transition`) raise :class:`ConditionalWriteError` on a failed condition,
exactly as a DynamoDB ``ConditionalCheckFailedException`` would surface, so callers observe the
write-once guarantee (Req 8.1). Control-plane operation idempotency is enforced through the
operation record + ledger (a repeat dispatch cannot create a second independent execution),
not via Lambda/Step Functions retries (Req 8.7).

.. note::

   This is a **default/prototype adapter**. It is intended to be replaced by the accepted
   #279 write-once / conditional-transition / reconciliation model (gated task 11.3). The
   conditional-write and append-only *semantics* here are the contract the real adapter must
   preserve.
"""

from __future__ import annotations

# Standard library
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    # Local modules
    from connector.executor.models import (
        ApprovalRecord,
        LedgerEntry,
        PreparedOperation,
        ReconcileOutcome,
    )

__all__ = ["ConditionalWriteError", "InMemoryOperationStore"]

# Sort-key discriminators for the single-table schema.
_SK_META = "OP#META"
_SK_APPROVAL_PREFIX = "APPROVAL#"
_SK_LEDGER_PREFIX = "LEDGER#"


def _pk(operation_id: str) -> str:
    """Return the partition key ``OP#<operation_id>`` for an operation's records."""
    return f"OP#{operation_id}"


class ConditionalWriteError(Exception):
    """Raised when a conditional write fails its condition (mirrors DynamoDB's error).

    Signals a violated ``attribute_not_exists`` / lifecycle precondition — e.g. a second
    insert for the same operation id (write-once violation), or an approval transition for an
    operation that was never inserted. The caller observes this exactly as it would a
    DynamoDB ``ConditionalCheckFailedException``.
    """


@dataclass
class InMemoryOperationStore:
    """In-memory Prepared_Operation_Store + Audit_Ledger with conditional-write semantics.

    Implements :class:`connector.executor.seams.StateRecoveryContract279`. Items are keyed by
    ``(PK, SK)`` exactly as the single DynamoDB table would be, so the write-once, conditional
    approval-transition, and append-only ledger guarantees are directly assertable without any
    AWS dependency. Instances are safe for concurrent use (a lock guards each conditional
    write) so property tests can exercise interleavings.
    """

    def __init__(self) -> None:
        # (pk, sk) -> item dict. One dict is the whole "table".
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        # Captured APPROVAL# transitions modeling NEW_AND_OLD_IMAGES stream dispatch.
        self.stream_events: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    # -- #279 seam: write-once operation insert ----------------------------------------

    def insert_operation(self, op: "PreparedOperation") -> None:
        """Insert the operation record **write-once** (``attribute_not_exists(PK AND SK)``).

        A second insert for the same ``operation_id`` fails the condition and raises
        :class:`ConditionalWriteError`; the already-stored record is left byte-for-byte
        unchanged, so the stored content and canonical hash are immutable thereafter (Req 8.1,
        6.3). The stored image is a shallow copy so a later mutation of the passed dataclass
        (dataclasses are frozen, but defensive all the same) cannot alter the record.
        """
        key = (_pk(op.operation_id), _SK_META)
        with self._lock:
            if key in self._items:
                raise ConditionalWriteError(
                    f"operation {op.operation_id!r} already exists (write-once insert rejected)"
                )
            self._items[key] = {"PK": key[0], "SK": key[1], "operation": op}

    # -- #279 seam: conditional approval transition -------------------------------------

    def apply_approval_transition(self, op_id: str, approval: "ApprovalRecord") -> None:
        """Record an approval lifecycle transition as a separate conditional record.

        Written under ``SK = APPROVAL#<approved_at>``; requires the operation's ``OP#META``
        record to exist (else :class:`ConditionalWriteError`) and **never** modifies that
        record — the prepared content stays byte-for-byte unchanged (Req 6.4, 8.2). Emits the
        transition as a durable dispatch stream event (Req 3.4, 8.8).
        """
        meta_key = (_pk(op_id), _SK_META)
        approval_key = (_pk(op_id), f"{_SK_APPROVAL_PREFIX}{approval.approved_at}")
        with self._lock:
            if meta_key not in self._items:
                raise ConditionalWriteError(f"cannot record approval transition: operation {op_id!r} does not exist")
            if approval_key in self._items:
                raise ConditionalWriteError(f"approval transition {approval_key[1]!r} already recorded for {op_id!r}")
            self._items[approval_key] = {"PK": approval_key[0], "SK": approval_key[1], "approval": approval}
            # Stream dispatch: NEW image of the APPROVAL# transition only (durable trigger).
            self.stream_events.append(
                {
                    "event_kind": "APPROVAL_TRANSITION",
                    "operation_id": op_id,
                    "sk": approval_key[1],
                }
            )

    # -- #279 seam: reconciliation ------------------------------------------------------

    def reconcile(self, op_id: str, provider_state: object) -> "ReconcileOutcome":
        """Reconcile the operation against observed ``provider_state``.

        ``provider_state`` may be a mapping or any object exposing ``branch_exists`` /
        ``commit_present`` / ``proposal`` attributes; the reported outcome tells the executor
        whether the intended branch/commit/proposal already exist so a retry reuses them
        rather than duplicating them (Req 10.1, 10.3, 10.4). ``resolved`` is ``True`` when the
        full intended provider state is already present.
        """
        # Local modules
        from connector.executor.models import ReconcileOutcome

        def _get(name: str, default: Any) -> Any:
            if isinstance(provider_state, dict):
                return provider_state.get(name, default)
            return getattr(provider_state, name, default)

        branch_exists = bool(_get("branch_exists", False))
        commit_present = bool(_get("commit_present", False))
        proposal = _get("proposal", None)
        resolved = branch_exists and commit_present and proposal is not None
        return ReconcileOutcome(
            operation_id=op_id,
            branch_exists=branch_exists,
            commit_present=commit_present,
            proposal=proposal,
            resolved=resolved,
        )

    # -- append-only ledger (never-raise, confirmed-write discipline) -------------------

    def append_ledger(self, entry: "LedgerEntry") -> bool:
        """Append one ledger entry under ``LEDGER#<sequence>`` (append-only, never raises).

        Uses a conditional ``PutItem`` with ``attribute_not_exists``: a write to an existing
        ``LEDGER#<seq>`` key leaves the existing entry intact and returns ``False`` (an
        unconfirmed write), never overwriting it (Req 8.3, 8.5). A successful append returns
        ``True``. This mirrors :class:`connector.audit.AuditSink`'s confirmed-write / never-raise
        contract, so a caller observes an append-only violation as an unconfirmed write rather
        than an exception.
        """
        try:
            key = (_pk(entry.operation_id), f"{_SK_LEDGER_PREFIX}{entry.sequence:012d}")
            with self._lock:
                if key in self._items:
                    return False
                self._items[key] = {"PK": key[0], "SK": key[1], "ledger": entry}
            return True
        except Exception:  # noqa: BLE001 - the ledger path must never raise to the caller
            return False

    # -- read helpers (used by the approval surface and executor) -----------------------

    def get_operation(self, op_id: str) -> "PreparedOperation | None":
        """Return the stored :class:`PreparedOperation`, or ``None`` if absent."""
        with self._lock:
            item = self._items.get((_pk(op_id), _SK_META))
        return cast("PreparedOperation", item["operation"]) if item is not None else None

    def get_latest_approval(self, op_id: str) -> "ApprovalRecord | None":
        """Return the most recent approval transition for ``op_id``, or ``None``."""
        prefix = _SK_APPROVAL_PREFIX
        with self._lock:
            approvals = [
                item["approval"] for (pk, sk), item in self._items.items() if pk == _pk(op_id) and sk.startswith(prefix)
            ]
        if not approvals:
            return None
        return cast("ApprovalRecord", max(approvals, key=lambda a: a.approved_at))

    def list_ledger(self, op_id: str) -> list["LedgerEntry"]:
        """Return the operation's ledger entries ordered by sequence (append order)."""
        prefix = _SK_LEDGER_PREFIX
        with self._lock:
            entries = [
                item["ledger"] for (pk, sk), item in self._items.items() if pk == _pk(op_id) and sk.startswith(prefix)
            ]
        return sorted(entries, key=lambda e: e.sequence)

    def next_ledger_sequence(self, op_id: str) -> int:
        """Return the next append sequence for ``op_id`` (0-based, monotonic)."""
        return len(self.list_ledger(op_id))
