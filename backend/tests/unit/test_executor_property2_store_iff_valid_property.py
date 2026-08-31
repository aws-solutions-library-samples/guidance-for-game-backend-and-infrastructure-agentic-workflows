#!/usr/bin/env python3
"""Property-based test for store-iff-valid.

Feature: source-control-connector-executor, Property 2 (design → Correctness Properties).
A :class:`PreparedOperation` is stored **iff** the drafted change passes schema validation: a
valid draft is stored write-once and its opaque ``operation_id`` is returned, while a draft that
fails schema validation is rejected and stores no operation at all (Req 1.3, 1.4).

The generator flips a ``make_valid`` flag to produce either a structurally valid CloudFormation
draft or a schema-invalid one (empty / missing-``Resources`` / no-``Type`` content), and the
test asserts the biconditional against the observable store state: an operation record exists
after preparation iff the draft was valid.

Validates: Requirements 1.3, 1.4
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


def _operation_count(store: InMemoryOperationStore) -> int:
    """Return the number of stored ``OP#META`` operation records (observable store state)."""
    return sum(1 for (_, sk) in store._items if sk == "OP#META")


@st.composite
def _draft_and_validity(draw: st.DrawFn) -> tuple[DraftedChange, bool]:
    """Generate a draft that is either schema-valid or schema-invalid, plus the expected flag."""
    make_valid = draw(st.booleans())
    name = draw(st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True))
    if make_valid:
        content = json.dumps({"Resources": {"Res0": {"Type": "AWS::S3::Bucket"}}})
    else:
        # A selection of contents that all fail CloudFormation structural validation.
        content = draw(
            st.sampled_from(
                [
                    "",  # empty content
                    "   ",  # whitespace only
                    json.dumps({"NoResources": True}),  # missing top-level Resources
                    json.dumps({"Resources": {}}),  # empty Resources mapping
                    json.dumps({"Resources": {"Res0": {"NoType": "x"}}}),  # resource missing Type
                    "{ this is : not valid : yaml : [",  # unparseable
                ]
            )
        )
    draft = DraftedChange(
        files=(ProposedFile(path=f"infra/{name}.yaml", content=content, iac_format="cloudformation"),),
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    return draft, make_valid


# Feature: source-control-connector-executor, Property 2: Preparation stores an operation if and only if the draft is valid
@settings(max_examples=100)
@given(bundle=_draft_and_validity())
def test_property2_stores_operation_iff_draft_valid(bundle: tuple[DraftedChange, bool]) -> None:
    """An operation is stored iff the draft passes schema validation (Req 1.3, 1.4)."""
    draft, valid = bundle
    provider = FakeProvider()
    store = InMemoryOperationStore()

    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    result = _prep_service(provider, store).prepare(draft, requester=requester)

    if valid:
        assert result.status == "prepared" and result.operation_id
        assert store.get_operation(result.operation_id) is not None
        assert _operation_count(store) == 1
    else:
        assert result.status == "rejected"
        assert result.reason is not None and result.reason.startswith("iac_validation_failed")
        assert result.operation_id == ""
        # Nothing was stored on the rejected path.
        assert _operation_count(store) == 0
