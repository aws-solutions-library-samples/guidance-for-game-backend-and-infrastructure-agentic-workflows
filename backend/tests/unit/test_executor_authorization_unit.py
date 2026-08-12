#!/usr/bin/env python3
"""Unit tests for the executor two-layer authorization and the #277 default adapter shapes.

Focused example tests that lock in the foundation shapes built for the executor:

- :func:`connector.executor.authorization.compute_effective_authority` computes the layer
  intersection (a disabled layer denies; risk must be within the lowest ceiling),
- :func:`connector.executor.authorization.request_time_check` bounds risk by principal and
  capability maximum,
- :func:`connector.executor.authorization.target_authorization` reuses the sibling
  five-dimension :class:`connector.config.AuthorizationPolicy`, and
- :class:`connector.executor.adapters.DefaultOperationContracts277` derives a deterministic,
  operation-id-only branch name and a content-addressed canonical hash.

The universal property tests (Properties 18, 19, 21, 22) are separate later tasks.
"""

# Standard library
from dataclasses import replace

# Third-party packages
import pytest

# Local modules
from connector.config import AllowlistEntry, AuthorizationPolicy
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION, DefaultOperationContracts277
from connector.executor.authorization import (
    CapabilityPosture,
    PolicyLayer,
    compute_effective_authority,
    normalize_path,
    request_time_check,
    target_authorization,
)
from connector.executor.models import (
    EffectiveAuthority,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
)
from connector.models import ProposedFile

pytestmark = pytest.mark.unit


def _make_operation(operation_id: str = "op-1") -> PreparedOperation:
    """Build a minimal, fully-populated :class:`PreparedOperation` for these tests."""
    files = (ProposedFile(path="infra/main.tf", content="resource {}", iac_format="terraform"),)
    authority = EffectiveAuthority(decision="authorized", inputs=("deployment_mode",), risk_ceiling=RiskLevel.MEDIUM)
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


def _layer(name: str, enabled: bool = True, max_risk: RiskLevel = RiskLevel.HIGH) -> PolicyLayer:
    return PolicyLayer(name=name, enabled=enabled, max_risk=max_risk)


def test_effective_authority_is_the_layer_intersection() -> None:
    """Authorized iff every layer is enabled and risk is within the lowest ceiling."""
    layers = [
        _layer("deployment_mode", max_risk=RiskLevel.HIGH),
        _layer("tenant", max_risk=RiskLevel.MEDIUM),
        _layer("workspace", max_risk=RiskLevel.HIGH),
    ]
    # Risk MEDIUM is within the intersected ceiling (MEDIUM).
    authorized = compute_effective_authority(layers, RiskLevel.MEDIUM)
    assert authorized.decision == "authorized"
    assert authorized.risk_ceiling == RiskLevel.MEDIUM

    # Risk HIGH exceeds the intersected ceiling -> denied, naming the binding layer.
    denied = compute_effective_authority(layers, RiskLevel.HIGH)
    assert denied.decision == "denied"
    assert denied.failed_layer == "tenant"


def test_effective_authority_disabled_layer_denies() -> None:
    """A single disabled layer denies regardless of risk."""
    layers = [_layer("deployment_mode"), _layer("tenant", enabled=False)]
    result = compute_effective_authority(layers, RiskLevel.LOW)
    assert result.decision == "denied"
    assert result.failed_layer == "tenant"


def test_request_time_check_bounds_by_principal_and_capability() -> None:
    """Operation risk must be within both principal authority and the capability maximum."""
    assert request_time_check(
        principal_authority=RiskLevel.HIGH,
        capability_maximum=RiskLevel.MEDIUM,
        operation_risk=RiskLevel.MEDIUM,
    )
    assert not request_time_check(
        principal_authority=RiskLevel.HIGH,
        capability_maximum=RiskLevel.MEDIUM,
        operation_risk=RiskLevel.HIGH,
    )


def test_capability_posture_enabled_flag() -> None:
    posture = CapabilityPosture(enabled=True, capability_maximum=RiskLevel.MEDIUM, tenant="t", workspace="w")
    assert posture.is_enabled()
    assert not CapabilityPosture(enabled=False, capability_maximum=RiskLevel.LOW).is_enabled()


def test_target_authorization_reuses_five_dimension_policy() -> None:
    """target_authorization delegates to AuthorizationPolicy over normalized paths."""
    policy = AuthorizationPolicy(
        entries=(
            AllowlistEntry(
                repo="org/iac",
                target_branches=("main",),
                path_prefixes=("infra/",),
                extensions=(".tf",),
            ),
        )
    )
    ok = target_authorization(
        policy,
        repo="org/iac",
        branch="main",
        paths=["./infra/main.tf"],  # normalized to infra/main.tf before the check
        groups=["infra"],
        authorized_groups=["infra"],
    )
    assert ok.allowed and ok.repo == "org/iac" and ok.branch == "main"

    denied = target_authorization(
        policy,
        repo="org/iac",
        branch="main",
        paths=["secrets/creds.tf"],
        groups=["infra"],
        authorized_groups=["infra"],
    )
    assert not denied.allowed and denied.failed_dimension == "path"


def test_normalize_path_strips_leading_markers() -> None:
    assert normalize_path("  ./infra/main.tf ") == "infra/main.tf"
    assert normalize_path("/infra/main.tf") == "infra/main.tf"
    assert normalize_path("infra/main.tf") == "infra/main.tf"


def test_branch_name_is_operation_id_only_and_deterministic() -> None:
    """Same id -> same branch; identical content under different ids -> different branches."""
    contracts = DefaultOperationContracts277()
    assert contracts.branch_name("op-abc") == contracts.branch_name("op-abc")
    assert contracts.branch_name("op-abc").startswith("gbaw/")
    assert contracts.branch_name("op-abc") != contracts.branch_name("op-def")


def test_canonical_hash_is_content_addressed() -> None:
    """A change to the bound content yields a different canonical hash."""
    contracts = DefaultOperationContracts277()
    op = _make_operation()
    same = contracts.canonical_hash(op)
    assert same == contracts.canonical_hash(op)

    changed = replace(op, files=(ProposedFile(path="infra/main.tf", content="CHANGED", iac_format="terraform"),))
    assert contracts.canonical_hash(changed) != same
