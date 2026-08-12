#!/usr/bin/env python3
"""Property-based test for changed-content invalidation (SECURITY GATE — non-optional).

Feature: source-control-connector-executor, Property 7 (design → Correctness Properties).
If the bound content changes, the changed content has a different Canonical_Hash (and a new
Operation_ID), so a prior Approval_Record — bound to the *original* content's hash — does not
authorize it and the executor rejects it on hash mismatch with no provider write. This is the
"approval reuse cannot authorize changed content" guarantee (Requirement 14.5).

Two facets are asserted for every generated content change:

1. **Hash inequality** — the #277 canonical hash of the changed content differs from that of
   the original content, so the two are distinct operations.
2. **Executor rejection** — presenting the *changed* operation together with the prior
   approval (which binds the original hash) causes the executor to reject with
   ``hash_mismatch`` and perform no ``create_branch`` / ``commit_files`` /
   ``open_change_proposal`` provider write.

The test builds the store state directly through the in-memory #279 store and the default #277
contract adapter so the binding hash is controlled exactly; the reader+writer ``FakeProvider``
records that no mutation ever occurs on the rejected path.

Validates: Requirements 2.5, 14.5
"""

# Standard library
from dataclasses import replace
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION, DefaultOperationContracts277
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.handler import Executor, ExecutorDependencies
from connector.executor.models import (
    ApprovalRecord,
    ApproverIdentity,
    EffectiveAuthority,
    ExecutorEvent,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
)
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import DEFAULT_HEAD_SHA, FakeProvider

pytestmark = pytest.mark.unit

_WORKFLOW_ROLE = "arn:aws:iam::123456789012:role/ScmWorkflowRole"
_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"
_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _policy() -> AuthorizationPolicy:
    return AuthorizationPolicy(
        entries=(
            AllowlistEntry(
                repo="org/iac",
                target_branches=("main",),
                path_prefixes=("infra/",),
                extensions=(".yaml",),
            ),
        )
    )


def _layers() -> tuple[PolicyLayer, ...]:
    return (PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),)


def _acquirer(secret_arn: str, *, source: str) -> str:
    return "write-token"


def _deps(provider: FakeProvider, store: InMemoryOperationStore) -> ExecutorDependencies:
    return ExecutorDependencies(
        store=store,
        contracts=DefaultOperationContracts277(),
        provider=provider,
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        workflow_role_arn=_WORKFLOW_ROLE,
        write_secret_arn=_WRITE_SECRET,
        policy_layers=_layers(),
        credential_acquirer=_acquirer,
        clock=lambda: _T0,
    )


def _make_operation(operation_id: str, files: tuple[ProposedFile, ...]) -> PreparedOperation:
    """Build a stored operation whose canonical_hash is the correct #277 hash of its content."""
    contracts = DefaultOperationContracts277()
    base = PreparedOperation(
        operation_id=operation_id,
        canonical_hash="",
        operation_contract_version=DEFAULT_CONTRACT_VERSION,
        files=files,
        target_repo="org/iac",
        target_branch="main",
        base_revision=DEFAULT_HEAD_SHA,
        effective_authority=EffectiveAuthority(decision="authorized", inputs=(), risk_ceiling=RiskLevel.HIGH),
        risk=RiskLevel.LOW,
        requester_identity=RequesterIdentity(subject="user-1", groups=("infra",)),
        duplicate_content_key="dup",
        created_at=_T0.isoformat(),
    )
    return replace(base, canonical_hash=contracts.canonical_hash(base))


@st.composite
def _content_change(draw: st.DrawFn) -> tuple[tuple[ProposedFile, ...], tuple[ProposedFile, ...]]:
    """Generate an (original, changed) pair of file sets whose content differs."""
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    bodies = draw(st.lists(st.text(min_size=0, max_size=30), min_size=len(names), max_size=len(names)))
    suffix = draw(st.text(min_size=1, max_size=8))
    original = tuple(
        ProposedFile(path=f"infra/{name}.yaml", content=body, iac_format="cloudformation")
        for name, body in zip(names, bodies)
    )
    # The changed set alters at least one file body, guaranteeing different content.
    changed = tuple(
        ProposedFile(path=f.path, content=f.content + "\n# changed:" + suffix, iac_format=f.iac_format)
        for f in original
    )
    return original, changed


# Feature: source-control-connector-executor, Property 7: Changed content invalidates a prior approval
@settings(max_examples=100)
@given(pair=_content_change())
def test_property7_changed_content_is_not_authorized_by_prior_approval(
    pair: tuple[tuple[ProposedFile, ...], tuple[ProposedFile, ...]],
) -> None:
    """Changed content hashes differently, so a prior approval never authorizes it: the
    executor rejects on hash mismatch and performs no provider write (Req 2.5, 14.5)."""
    original_files, changed_files = pair
    contracts = DefaultOperationContracts277()

    original_op = _make_operation("op-original", original_files)
    changed_op = _make_operation("op-changed", changed_files)

    # Facet 1: changed content yields a different canonical hash (a distinct operation).
    assert original_op.canonical_hash != changed_op.canonical_hash

    # Store the *changed* operation, but present the PRIOR approval that binds the ORIGINAL
    # content's hash — modeling approval reuse against changed content.
    store = InMemoryOperationStore()
    store.insert_operation(changed_op)
    prior_approval = ApprovalRecord(
        operation_id=changed_op.operation_id,
        approver_identity=ApproverIdentity(subject="approver-1", groups=("infra",)),
        bound_canonical_hash=original_op.canonical_hash,  # bound to the ORIGINAL content
        approved_at=_T0.isoformat(),
        expires_at=(_T0 + timedelta(hours=1)).isoformat(),
        separation_of_duties_ok=True,
    )
    store.apply_approval_transition(changed_op.operation_id, prior_approval)

    provider = FakeProvider()
    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=changed_op.operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    # Facet 2: the executor rejects on hash mismatch and performs no provider write.
    assert outcome.status == "rejected"
    assert outcome.reason == "hash_mismatch"
    assert not provider.created_branches
    assert not provider.commits
    assert not provider.pull_requests
