#!/usr/bin/env python3
"""Property-based test for conditional lifecycle transitions.

Feature: source-control-connector-executor, Property 23 (design → Correctness Properties).
Every approval/lifecycle transition is applied as a **separate conditional record** and the
prepared-content record (``OP#META``) is byte-for-byte unchanged afterward (Req 6.4, 8.2). The
transition requires the operation record to exist (fail-closed) and is captured as a durable
dispatch stream event, while the immutable :class:`PreparedOperation` is never mutated no
matter how many transitions are applied.

The store double is the in-memory #279 adapter
(:class:`connector.executor.store.InMemoryOperationStore`) — the established conditional
insert-only / append-only store double used across the executor suite. For every generated
operation and 1..k approval transitions the test snapshots the stored operation before the
first transition and asserts it is identical after each one, and that each transition is a
distinct, separately-recorded approval.

Validates: Requirements 6.4, 8.2
"""

# Standard library
from dataclasses import replace
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION, DefaultOperationContracts277
from connector.executor.models import (
    ApprovalRecord,
    ApproverIdentity,
    EffectiveAuthority,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
)
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile

pytestmark = pytest.mark.unit

_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


@st.composite
def _files(draw: st.DrawFn) -> tuple[ProposedFile, ...]:
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    bodies = draw(st.lists(st.text(min_size=0, max_size=30), min_size=len(names), max_size=len(names)))
    return tuple(
        ProposedFile(path=f"infra/{name}.yaml", content=body, iac_format="cloudformation")
        for name, body in zip(names, bodies)
    )


def _make_operation(files: tuple[ProposedFile, ...]) -> PreparedOperation:
    """Build an operation whose canonical_hash is the correct #277 hash of its content."""
    contracts = DefaultOperationContracts277()
    base = PreparedOperation(
        operation_id="op-1",
        canonical_hash="",
        operation_contract_version=DEFAULT_CONTRACT_VERSION,
        files=files,
        target_repo="org/iac",
        target_branch="main",
        base_revision="abc123",
        effective_authority=EffectiveAuthority(
            decision="authorized", inputs=("deployment_mode",), risk_ceiling=RiskLevel.HIGH
        ),
        risk=RiskLevel.LOW,
        requester_identity=RequesterIdentity(subject="user-1", groups=("infra",)),
        duplicate_content_key="dup",
        created_at=_T0.isoformat(),
    )
    return replace(base, canonical_hash=contracts.canonical_hash(base))


# Feature: source-control-connector-executor, Property 23: A lifecycle transition is a conditional record that never modifies prepared content
@settings(max_examples=100)
@given(files=_files(), transition_count=st.integers(min_value=1, max_value=5))
def test_property23_transition_never_modifies_prepared_content(
    files: tuple[ProposedFile, ...], transition_count: int
) -> None:
    """Applying 1..k approval transitions leaves the OP#META prepared record byte-for-byte
    unchanged, and each transition is a separate, individually-recorded conditional write
    (Req 6.4, 8.2)."""
    store = InMemoryOperationStore()
    operation = _make_operation(files)
    store.insert_operation(operation)

    before = store.get_operation(operation.operation_id)
    assert before == operation

    for index in range(transition_count):
        # Distinct approved_at per transition so each is a separate APPROVAL# record.
        approved_at = (_T0 + timedelta(minutes=index)).isoformat()
        approval = ApprovalRecord(
            operation_id=operation.operation_id,
            approver_identity=ApproverIdentity(subject="approver-1", groups=("infra",)),
            bound_canonical_hash=operation.canonical_hash,
            approved_at=approved_at,
            expires_at=(_T0 + timedelta(hours=1)).isoformat(),
            separation_of_duties_ok=True,
        )
        store.apply_approval_transition(operation.operation_id, approval)

        # The prepared content record is unchanged after every transition.
        assert store.get_operation(operation.operation_id) == before

    # Each transition was captured as a separate durable dispatch stream event.
    assert len(store.stream_events) == transition_count
    assert all(event["event_kind"] == "APPROVAL_TRANSITION" for event in store.stream_events)
    # The latest approval binds the stored hash; the operation itself remains immutable.
    latest = store.get_latest_approval(operation.operation_id)
    assert latest is not None and latest.bound_canonical_hash == operation.canonical_hash
    assert store.get_operation(operation.operation_id) == operation
