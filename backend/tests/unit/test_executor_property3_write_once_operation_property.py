#!/usr/bin/env python3
"""Property-based test for write-once stored operation.

Feature: source-control-connector-executor, Property 3 (design → Correctness Properties).
Every stored operation carries a unique ``Operation_ID``, a ``Canonical_Hash``, a supported
``Operation_Contract_Version``, and the stamped ``Effective_Authority`` decision (with inputs)
and risk; a second insert for the same ``Operation_ID`` is rejected and the stored content and
hash are immutable thereafter (Req 1.5, 1.6, 6.1, 6.3, 7.4, 8.1).

The test prepares a valid draft through ``PreparationService.prepare`` against the in-memory
#279 store and the default #277 contract adapter, asserts every stored field is present and
consistent (the canonical hash equals the #277 hash of the stored content, the version is in
the supported set, authority is stamped ``authorized`` and risk is a :class:`RiskLevel`), then
attempts a second insert for the same id — asserting it raises :class:`ConditionalWriteError`
and leaves the stored record byte-for-byte unchanged.

Validates: Requirements 1.5, 1.6, 6.1, 6.3, 7.4, 8.1
"""

# Standard library
import json
from dataclasses import replace

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION, DefaultOperationContracts277
from connector.executor.authorization import CapabilityPosture, PolicyLayer
from connector.executor.models import DraftedChange, RequesterIdentity, RiskLevel, TargetSelector
from connector.executor.preparation import PreparationService
from connector.executor.store import ConditionalWriteError, InMemoryOperationStore
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
    return (
        PolicyLayer(name="deployment_mode", enabled=True, max_risk=RiskLevel.HIGH),
        PolicyLayer(name="tenant", enabled=True, max_risk=RiskLevel.HIGH),
    )


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
def _cfn_files(draw: st.DrawFn) -> tuple[ProposedFile, ...]:
    names = draw(
        st.lists(
            st.from_regex(r"[a-z][a-z0-9]{2,10}", fullmatch=True),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    files: list[ProposedFile] = []
    for index, name in enumerate(names):
        resource_type = draw(st.sampled_from(["AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::SNS::Topic"]))
        content = json.dumps({"Resources": {f"Res{index}": {"Type": resource_type}}})
        files.append(ProposedFile(path=f"infra/{name}.yaml", content=content, iac_format="cloudformation"))
    return tuple(files)


# Feature: source-control-connector-executor, Property 3: Every stored operation is write-once with an id, a canonical hash, and stamped authority and risk
@settings(max_examples=100)
@given(files=_cfn_files())
def test_property3_stored_operation_is_write_once_and_stamped(files: tuple[ProposedFile, ...]) -> None:
    """A stored operation carries id/hash/version/authority/risk and is write-once immutable."""
    provider = FakeProvider()
    store = InMemoryOperationStore()
    contracts = DefaultOperationContracts277()

    draft = DraftedChange(
        files=files,
        iac_format="cloudformation",
        target=TargetSelector(repository=_REPO, branch=_BRANCH),
        intent="prepare change",
        title="Prepared change",
        description="A prepared change.",
    )
    requester = RequesterIdentity(subject="user-1", groups=("infra",))
    result = _prep_service(provider, store).prepare(draft, requester=requester)
    assert result.status == "prepared" and result.operation_id

    operation = store.get_operation(result.operation_id)
    assert operation is not None

    # Unique id + binding canonical hash consistent with the #277 hash of the stored content.
    assert operation.operation_id == result.operation_id
    assert operation.canonical_hash and operation.canonical_hash == contracts.canonical_hash(operation)
    # Supported operation-contract version.
    assert operation.operation_contract_version == DEFAULT_CONTRACT_VERSION
    assert operation.operation_contract_version in contracts.supported_contract_versions()
    # Stamped effective authority (decision + inputs) and a risk level.
    assert operation.effective_authority.decision == "authorized"
    assert operation.effective_authority.inputs == ("deployment_mode", "tenant")
    assert isinstance(operation.risk, RiskLevel)

    # Write-once: a second insert for the same id is rejected and leaves the record unchanged.
    tampered = replace(operation, base_revision="deadbeef", canonical_hash="tampered")
    with pytest.raises(ConditionalWriteError):
        store.insert_operation(tampered)
    assert store.get_operation(result.operation_id) == operation
