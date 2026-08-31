#!/usr/bin/env python3
"""Property-based test for no provider write during preparation.

Feature: source-control-connector-executor, Property 5 (design → Correctness Properties).
No preparation run ever performs a provider mutation: ``create_branch`` / ``commit_files`` /
``open_change_proposal`` (and any merge/approve/close/delete/force-push) are never invoked
(Req 1.7). Preparation's only provider interaction is a *read* — ``latest_commit_sha`` — to
fetch the server-side base revision.

The test drives both the accepted (valid) and rejected paths — valid drafts, schema-invalid
drafts, and a disabled capability posture — and asserts the reader+writer ``FakeProvider``
records zero mutation calls in every case.

Validates: Requirements 1.7
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

# The provider mutation operations that preparation must NEVER invoke.
_MUTATION_OPS = frozenset({"create_branch", "commit_files", "open_change_proposal"})


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


def _prep_service(
    provider: FakeProvider, store: InMemoryOperationStore, *, posture_enabled: bool
) -> PreparationService:
    return PreparationService(
        provider=provider,
        store=store,
        contracts=DefaultOperationContracts277(),
        policy=_policy(),
        authorized_groups=("infra",),
        capability_posture=CapabilityPosture(enabled=posture_enabled, capability_maximum=RiskLevel.HIGH),
        policy_layers=_layers(),
    )


@st.composite
def _draft_and_flags(draw: st.DrawFn) -> tuple[DraftedChange, bool]:
    """Generate a (draft, posture_enabled) bundle exercising accept + reject paths."""
    posture_enabled = draw(st.booleans())
    valid = draw(st.booleans())
    name = draw(st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True))
    if valid:
        content = json.dumps({"Resources": {"Res0": {"Type": "AWS::S3::Bucket"}}})
    else:
        content = draw(st.sampled_from(["", json.dumps({"NoResources": True}), "not : valid : ["]))
    draft = DraftedChange(
        files=(ProposedFile(path=f"infra/{name}.yaml", content=content, iac_format="cloudformation"),),
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    return draft, posture_enabled


# Feature: source-control-connector-executor, Property 5: Preparation performs no provider write
@settings(max_examples=100)
@given(bundle=_draft_and_flags())
def test_property5_preparation_performs_no_provider_write(bundle: tuple[DraftedChange, bool]) -> None:
    """Across accepted and rejected preparation paths, no provider mutation is ever invoked."""
    draft, posture_enabled = bundle
    provider = FakeProvider()
    store = InMemoryOperationStore()

    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    _prep_service(provider, store, posture_enabled=posture_enabled).prepare(draft, requester=requester)

    # No provider mutation artifacts were produced.
    assert not provider.created_branches
    assert not provider.commits
    assert not provider.pull_requests
    # No provider mutation operation was ever called.
    assert _MUTATION_OPS.isdisjoint(provider.call_operations)
