#!/usr/bin/env python3
"""Property-based test for hash + contract-version gating (SECURITY GATE — non-optional).

Feature: source-control-connector-executor, Property 11 (design → Correctness Properties).
The executor performs a provider write **iff** the stored content hashes to the stored
Canonical_Hash **and** the Operation_Contract_Version is supported. A hash mismatch
(Requirement 14.6) OR an unsupported contract version (Requirement 14.7) rejects the operation
with no ``create_branch`` / ``commit_files`` / ``open_change_proposal`` provider write.

The 2x2 matrix of (hash valid?) x (version supported?) is generated and, for each cell, the
executor is run against a directly-built store record (so the stored hash and version are
controlled exactly), the in-memory #279 store, the default #277 contract adapter, and the
reader+writer ``FakeProvider`` double. The single invariant asserted is the biconditional:
a provider write happened **iff** both gates hold.

Validates: Requirements 4.5, 4.6, 6.9, 14.6, 14.7
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
_UNSUPPORTED_VERSION = "9.9-unsupported"


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


def _build_stored_operation(files: tuple[ProposedFile, ...], *, hash_ok: bool, version_ok: bool) -> PreparedOperation:
    """Build a stored operation with a valid/tampered hash and a supported/unsupported version."""
    contracts = DefaultOperationContracts277()
    version = DEFAULT_CONTRACT_VERSION if version_ok else _UNSUPPORTED_VERSION
    base = PreparedOperation(
        operation_id="op-1",
        canonical_hash="",
        operation_contract_version=version,
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
    real_hash = contracts.canonical_hash(base)
    stored_hash = real_hash if hash_ok else (real_hash[::-1] + "tampered")
    return replace(base, canonical_hash=stored_hash)


# Feature: source-control-connector-executor, Property 11: The executor writes only with a verified hash and a supported contract version
@settings(max_examples=100)
@given(files=_files(), hash_ok=st.booleans(), version_ok=st.booleans())
def test_property11_write_iff_hash_and_version_valid(
    files: tuple[ProposedFile, ...], hash_ok: bool, version_ok: bool
) -> None:
    """A provider write occurs iff the stored hash verifies AND the contract version is
    supported; a hash mismatch or unsupported version rejects with no write (Req 4.5, 4.6,
    6.9, 14.6, 14.7)."""
    operation = _build_stored_operation(files, hash_ok=hash_ok, version_ok=version_ok)

    store = InMemoryOperationStore()
    store.insert_operation(operation)
    # An approval bound to the stored hash exists, so the hash+version gate is the deciding gate.
    store.apply_approval_transition(
        operation.operation_id,
        ApprovalRecord(
            operation_id=operation.operation_id,
            approver_identity=ApproverIdentity(subject="approver-1", groups=("infra",)),
            bound_canonical_hash=operation.canonical_hash,
            approved_at=_T0.isoformat(),
            expires_at=(_T0 + timedelta(hours=1)).isoformat(),
            separation_of_duties_ok=True,
        ),
    )

    provider = FakeProvider()
    outcome = Executor(_deps(provider, store)).handle(
        ExecutorEvent(operation_id=operation.operation_id),
        {"caller_identity": _WORKFLOW_ROLE},
    )

    wrote = bool(provider.created_branches or provider.commits or provider.pull_requests)
    should_write = hash_ok and version_ok

    # The core biconditional: a provider write happened iff both gates held.
    assert wrote == should_write

    if should_write:
        assert outcome.status == "executed"
        assert len(provider.pull_requests) == 1
    else:
        assert outcome.status == "rejected"
        assert outcome.reason in {"hash_mismatch", "unsupported_contract_version"}
        assert not provider.created_branches
        assert not provider.commits
        assert not provider.pull_requests
