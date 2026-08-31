#!/usr/bin/env python3
"""Property-based test for the append-only ledger.

Feature: source-control-connector-executor, Property 24 (design → Correctness Properties).
For any sequence of ledger writes, an entry is only ever appended and never overwritten: a
conditional write to an existing ``LEDGER#<seq>`` key leaves the existing entry intact and is
reported as an unconfirmed write (``append_ledger`` returns ``False``), while a write to a
fresh sequence appends (returns ``True``) and preserves append order (Req 8.3, 8.5).

The store double is the in-memory #279 adapter
(:class:`connector.executor.store.InMemoryOperationStore`), whose ``append_ledger`` mirrors a
conditional DynamoDB ``PutItem`` with ``attribute_not_exists`` and the never-raise discipline
of :class:`connector.audit.AuditSink`. For every generated set of appended sequences the test
then attempts to overwrite each existing key with tampered content and asserts the original
entry survives unchanged.

Validates: Requirements 8.3, 8.5
"""

# Standard library
from dataclasses import replace
from datetime import datetime, timezone

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION, DefaultOperationContracts277
from connector.executor.models import (
    EffectiveAuthority,
    LedgerEntry,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
)
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile

pytestmark = pytest.mark.unit

_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
_EVENTS = ("intent", "attempt", "provider_result", "recovery", "outcome")


def _make_operation() -> PreparedOperation:
    contracts = DefaultOperationContracts277()
    base = PreparedOperation(
        operation_id="op-1",
        canonical_hash="",
        operation_contract_version=DEFAULT_CONTRACT_VERSION,
        files=(ProposedFile(path="infra/main.yaml", content="body", iac_format="cloudformation"),),
        target_repo="org/iac",
        target_branch="main",
        base_revision="abc123",
        effective_authority=EffectiveAuthority(decision="authorized", inputs=(), risk_ceiling=RiskLevel.HIGH),
        risk=RiskLevel.LOW,
        requester_identity=RequesterIdentity(subject="user-1", groups=("infra",)),
        duplicate_content_key="dup",
        created_at=_T0.isoformat(),
    )
    return replace(base, canonical_hash=contracts.canonical_hash(base))


# Feature: source-control-connector-executor, Property 24: Ledger entries are append-only
@settings(max_examples=100)
@given(
    events=st.lists(st.sampled_from(_EVENTS), min_size=1, max_size=8),
    overwrite_event=st.sampled_from(_EVENTS),
)
def test_property24_ledger_is_append_only(events: list[str], overwrite_event: str) -> None:
    """Fresh sequences append (True) in order; a write to an existing sequence never overwrites
    (False) and leaves the original entry intact (Req 8.3, 8.5)."""
    store = InMemoryOperationStore()
    operation = _make_operation()
    store.insert_operation(operation)

    # Append one entry per generated event at monotonically increasing sequences.
    for sequence, event in enumerate(events):
        entry = LedgerEntry(
            operation_id=operation.operation_id,
            sequence=sequence,
            event=event,
            outcome=f"recorded:{sequence}",
        )
        assert store.append_ledger(entry) is True

    appended = store.list_ledger(operation.operation_id)
    assert [e.sequence for e in appended] == list(range(len(events)))
    assert [e.event for e in appended] == events
    snapshot = list(appended)

    # Any write to an already-present sequence key is refused and changes nothing.
    for sequence in range(len(events)):
        overwrite = LedgerEntry(
            operation_id=operation.operation_id,
            sequence=sequence,
            event=overwrite_event,
            outcome="TAMPERED",
        )
        assert store.append_ledger(overwrite) is False

    after = store.list_ledger(operation.operation_id)
    assert after == snapshot
    assert all(e.outcome != "TAMPERED" for e in after)

    # A brand-new sequence still appends normally, preserving append order.
    fresh_sequence = len(events)
    assert (
        store.append_ledger(LedgerEntry(operation_id=operation.operation_id, sequence=fresh_sequence, event="outcome"))
        is True
    )
    assert [e.sequence for e in store.list_ledger(operation.operation_id)] == list(range(len(events) + 1))
