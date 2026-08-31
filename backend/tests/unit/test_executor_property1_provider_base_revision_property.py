#!/usr/bin/env python3
"""Property-based test for provider-sourced base revision.

Feature: source-control-connector-executor, Property 1 (design → Correctness Properties).
For every drafted change — including ones that carry an attacker-planted revision-like value in
their free-form fields — the base revision stored on the resulting :class:`PreparedOperation`
equals the revision the Preparation_Service fetched **directly from the provider**
(``latest_commit_sha``), and never a client- or model-supplied value (Req 1.1, 1.2).

:class:`DraftedChange` intentionally has no ``base_revision`` field, so a revision can only ever
be *planted* in the untrusted client/model surface (``intent`` / ``title`` / ``description`` /
file content). The generator seeds the provider head to a known SHA and plants a *different*
SHA throughout the draft's free-form fields; the invariant asserted is that the stored
``base_revision`` equals the provider head and is never the planted value.

Validates: Requirements 1.1, 1.2
"""

# Standard library
import json

# Third-party packages
import pytest
from hypothesis import assume, given, settings
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

_SHA = st.from_regex(r"[0-9a-f]{40}", fullmatch=True)


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
def _planted_draft(draw: st.DrawFn) -> tuple[DraftedChange, str, str]:
    """Generate a valid draft that plants ``planted_sha`` in its free-form fields.

    Returns ``(draft, provider_head_sha, planted_sha)`` where the two SHAs differ, so the test
    can prove the stored base revision came from the provider and not from the draft.
    """
    provider_head = draw(_SHA)
    planted = draw(_SHA)
    assume(provider_head != planted)

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
            # The planted revision is smuggled into otherwise-valid CFN content, too.
            path=f"infra/{name}.yaml",
            content=json.dumps({"Resources": {f"Res{index}": {"Type": "AWS::S3::Bucket"}}, "PlantedRev": planted}),
            iac_format="cloudformation",
        )
        for index, name in enumerate(names)
    )
    draft = DraftedChange(
        files=files,
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent=f"apply against base {planted}",
        title=f"change @ {planted}",
        description=f"base_revision={planted} sha={planted}",
    )
    return draft, provider_head, planted


# Feature: source-control-connector-executor, Property 1: Base revision is sourced only from the provider
@settings(max_examples=100)
@given(bundle=_planted_draft())
def test_property1_base_revision_comes_only_from_provider(bundle: tuple[DraftedChange, str, str]) -> None:
    """The stored base revision equals the provider head and never a planted draft value."""
    draft, provider_head, planted = bundle

    provider = FakeProvider()
    provider.set_head(_REPO, _BRANCH, provider_head)
    store = InMemoryOperationStore()

    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    result = _prep_service(provider, store).prepare(draft, requester=requester)
    assert result.status == "prepared" and result.operation_id

    operation = store.get_operation(result.operation_id)
    assert operation is not None
    # The base revision is exactly what the provider reported, never the planted value.
    assert operation.base_revision == provider_head
    assert operation.base_revision != planted
