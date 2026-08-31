#!/usr/bin/env python3
"""Property-based test for re-draft identity.

Feature: source-control-connector-executor, Property 4 (design → Correctness Properties).
Re-drafting a change produces a new, distinct ``Operation_ID`` and leaves the prior
:class:`PreparedOperation` record byte-for-byte unchanged (Req 1.8). Because every ``prepare``
mints a fresh operation id and inserts write-once, re-preparing the *same* draft never mutates
the earlier record — it only adds a second, independent operation.

The test prepares the same draft twice through one ``PreparationService`` + store, snapshots the
first stored operation before the second prepare, and asserts the ids differ and the first
record is identical afterward.

Validates: Requirements 1.8
"""

# Standard library
import json

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DefaultOperationContracts277
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.models import DraftedChange, RequesterIdentity, RiskLevel, TargetSelector
from connector.executor.preparation import PreparationService
from connector.executor.store import InMemoryOperationStore
from connector.models import ProposedFile
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"


def _policy() -> AuthorizationPolicy:
    return AuthorizationPolicy(
        entries=(
            AllowlistEntry(
                repo=_REPO,
                target_branches=(_BRANCH,),
                path_prefixes=("infra/",),
                extensions=(".yaml",),
            ),
        )
    )


def _layers() -> tuple[PolicyLayer, ...]:
    return (PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),)


def _prep_service(provider: FakeProvider, store: InMemoryOperationStore) -> PreparationService:
    return PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=True, capability_maximum=RiskLevel.HIGH),
        policy_layers=_layers(),
    )


@st.composite
def _cfn_draft(draw: st.DrawFn) -> DraftedChange:
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    files = tuple(
        ProposedFile(
            path=f"infra/{name}.yaml",
            content=json.dumps({"Resources": {f"Res{index}": {"Type": "AWS::S3::Bucket"}}}),
            iac_format="cloudformation",
        )
        for index, name in enumerate(names)
    )
    return DraftedChange(
        files=files,
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )


# Feature: source-control-connector-executor, Property 4: Re-drafting creates a new operation id and never mutates an existing operation
@settings(max_examples=100)
@given(draft=_cfn_draft())
def test_property4_redraft_mints_new_id_and_preserves_prior(draft: DraftedChange) -> None:
    """Re-drafting yields a distinct id and leaves the prior operation record unchanged."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    service = _prep_service(provider, store)
    requester = RequesterIdentity(subject="user-1", groups=("infra",))

    first = service.prepare(draft, requester=requester)
    assert first.status == "prepared" and first.operation_id
    first_snapshot = store.get_operation(first.operation_id)
    assert first_snapshot is not None

    second = service.prepare(draft, requester=requester)
    assert second.status == "prepared" and second.operation_id

    # A new, distinct operation id is minted for the re-draft.
    assert second.operation_id != first.operation_id
    # The prior operation record is unchanged (never mutated by the re-draft).
    assert store.get_operation(first.operation_id) == first_snapshot
    # The re-draft is a separate, independent operation record.
    assert store.get_operation(second.operation_id) is not None
    assert store.get_operation(second.operation_id).operation_id == second.operation_id
