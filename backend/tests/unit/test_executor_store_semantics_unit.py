#!/usr/bin/env python3
"""Unit tests for the executor store's write-once / append-only / conditional-transition semantics.

These cover the core conditional-write guarantees of
:class:`connector.executor.store.InMemoryOperationStore` (the default #279 adapter) that the
preparation, approval, and executor logic depend on:

- **write-once** operation insert (a second insert for the same id is rejected and the stored
  record is left unchanged),
- **conditional approval transition** (recorded as a separate record that never modifies the
  operation record, and rejected when the operation is absent), and
- **append-only** ledger (a write to an existing ledger key never overwrites and reports an
  unconfirmed write).

These are focused example tests; the universal property tests for these semantics
(Properties 23, 24, 25) are separate later tasks.
"""

# Standard library
from dataclasses import replace

# Third-party packages
import pytest

# Local modules
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION, DefaultOperationContracts277
from connector.executor.models import (
    ApprovalRecord,
    ApproverIdentity,
    EffectiveAuthority,
    LedgerEntry,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
)
from connector.executor.store import ConditionalWriteError, InMemoryOperationStore
from connector.models import ProposedFile

pytestmark = pytest.mark.unit


def _make_operation(operation_id: str = "op-1") -> PreparedOperation:
    """Build a minimal, fully-populated :class:`PreparedOperation` for the store tests."""
    files = (ProposedFile(path="infra/main.tf", content="resource {}", iac_format="terraform"),)
    authority = EffectiveAuthority(
        decision="authorized", inputs=("deployment_mode", "principal"), risk_ceiling=RiskLevel.MEDIUM
    )
    op = PreparedOperation(
        operation_id=operation_id,
        canonical_hash="",
        operation_contract_version=DEFAULT_CONTRACT_VERSION,
        files=files,
        target_repo="org/iac",
        target_branch="main",
        base_revision="abc123",
        effective_authority=authority,
        risk=RiskLevel.LOW,
        requester_identity=RequesterIdentity(subject="user-1", groups=("infra",)),
        duplicate_content_key="dup-key",
        created_at="2026-01-01T00:00:00+00:00",
    )
    return replace(op, canonical_hash=DefaultOperationContracts277().canonical_hash(op))


def _make_approval(op: PreparedOperation, *, approved_at: str = "2026-01-01T00:05:00+00:00") -> ApprovalRecord:
    return ApprovalRecord(
        operation_id=op.operation_id,
        approver_identity=ApproverIdentity(subject="approver-1", groups=("infra",)),
        bound_canonical_hash=op.canonical_hash,
        approved_at=approved_at,
        expires_at="2026-01-01T01:00:00+00:00",
        separation_of_duties_ok=True,
    )


def test_insert_operation_is_write_once() -> None:
    """A second insert for the same operation id is rejected; the first record is unchanged."""
    store = InMemoryOperationStore()
    op = _make_operation()
    store.insert_operation(op)

    # A second insert for the same id fails the write-once condition.
    tampered = replace(op, base_revision="tampered", canonical_hash="tampered-hash")
    with pytest.raises(ConditionalWriteError):
        store.insert_operation(tampered)

    # The originally stored record is immutable (unchanged by the rejected insert).
    stored = store.get_operation(op.operation_id)
    assert stored is not None
    assert stored.base_revision == "abc123"
    assert stored.canonical_hash == op.canonical_hash


def test_approval_transition_is_conditional_and_never_mutates_operation() -> None:
    """An approval is a separate record; it never modifies OP#META and needs the op present."""
    store = InMemoryOperationStore()
    op = _make_operation()

    # Approving a non-existent operation fails closed.
    with pytest.raises(ConditionalWriteError):
        store.apply_approval_transition(op.operation_id, _make_approval(op))

    store.insert_operation(op)
    before = store.get_operation(op.operation_id)

    store.apply_approval_transition(op.operation_id, _make_approval(op))

    # The operation (prepared content) is byte-for-byte unchanged after the transition.
    assert store.get_operation(op.operation_id) == before
    # The approval is recorded and retrievable, and a stream dispatch event was emitted.
    latest = store.get_latest_approval(op.operation_id)
    assert latest is not None and latest.bound_canonical_hash == op.canonical_hash
    assert store.stream_events and store.stream_events[-1]["event_kind"] == "APPROVAL_TRANSITION"


def test_ledger_is_append_only() -> None:
    """A ledger write to an existing sequence key never overwrites and reports unconfirmed."""
    store = InMemoryOperationStore()
    op = _make_operation()
    store.insert_operation(op)

    first = LedgerEntry(operation_id=op.operation_id, sequence=0, event="intent", outcome="recorded")
    assert store.append_ledger(first) is True

    # Re-writing the same sequence key is refused (append-only) and leaves the entry intact.
    overwrite = LedgerEntry(operation_id=op.operation_id, sequence=0, event="attempt", outcome="TAMPERED")
    assert store.append_ledger(overwrite) is False

    entries = store.list_ledger(op.operation_id)
    assert len(entries) == 1
    assert entries[0].event == "intent" and entries[0].outcome == "recorded"

    # A new sequence appends normally, preserving order.
    assert store.append_ledger(LedgerEntry(operation_id=op.operation_id, sequence=1, event="outcome")) is True
    assert [e.sequence for e in store.list_ledger(op.operation_id)] == [0, 1]
