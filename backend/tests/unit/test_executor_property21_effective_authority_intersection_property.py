#!/usr/bin/env python3
"""Property-based test for the effective-authority intersection.

Feature: source-control-connector-executor, Property 21 (design → Correctness Properties).
For any combination of applicable policy layers (deployment mode, tenant policy, workspace
policy, principal authority, capability maximum, operation-risk policy), the computed
Effective_Authority equals their **intersection**: every layer must be enabled and the
operation is authorized **iff** its risk lies within the lowest ceiling any layer imposes
(``operation_risk <= min(layer.max_risk)``). The reference intersection is recomputed
independently in the test and compared against
:func:`connector.executor.authorization.compute_effective_authority` for every generated
layer set / risk (Req 7.2, 7.3).

Validates: Requirements 7.2, 7.3
"""

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.executor.authorization import PolicyLayer, compute_effective_authority
from connector.executor.models import RiskLevel

pytestmark = pytest.mark.unit

# The canonical policy-layer names the write path intersects (design → Component 7).
_LAYER_NAMES = (
    "deployment_mode",
    "tenant",
    "workspace",
    "principal",
    "capability_maximum",
    "risk_policy",
)
_RISKS = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)


@st.composite
def _layer_sets(draw: st.DrawFn) -> tuple[PolicyLayer, ...]:
    """Generate an ordered set of 0..6 distinct-named policy layers with random enable/ceiling."""
    names = draw(st.lists(st.sampled_from(_LAYER_NAMES), min_size=0, max_size=len(_LAYER_NAMES), unique=True))
    layers: list[PolicyLayer] = []
    for name in names:
        layers.append(
            PolicyLayer(
                name=name,
                enabled=draw(st.booleans()),
                max_risk=draw(st.sampled_from(_RISKS)),
            )
        )
    return tuple(layers)


# Feature: source-control-connector-executor, Property 21: Effective authority equals the intersection of all applicable policy layers
@settings(max_examples=100)
@given(layers=_layer_sets(), operation_risk=st.sampled_from(_RISKS))
def test_property21_effective_authority_is_the_layer_intersection(
    layers: tuple[PolicyLayer, ...], operation_risk: RiskLevel
) -> None:
    """The computed authority equals the independently-recomputed intersection: authorized iff
    every layer is enabled and the risk is within the lowest ceiling (Req 7.2, 7.3)."""
    result = compute_effective_authority(layers, operation_risk)

    # Independent reference intersection.
    all_enabled = all(layer.enabled for layer in layers)
    within_ceiling = (not layers) or operation_risk <= min(layer.max_risk for layer in layers)
    expected_authorized = all_enabled and within_ceiling

    # The core biconditional: authorized iff the operation lies within the intersection.
    assert result.authorized == expected_authorized
    assert result.decision == ("authorized" if expected_authorized else "denied")

    # The inputs always record exactly the applicable layer names, in order.
    assert result.inputs == tuple(layer.name for layer in layers)

    if not all_enabled:
        # A disabled layer denies outright and names the first disabled layer; no ceiling.
        first_disabled = next(layer.name for layer in layers if not layer.enabled)
        assert result.failed_layer == first_disabled
        assert result.risk_ceiling is None
    elif not layers:
        # No applicable layers => nothing constrains the operation (authorized, no ceiling).
        assert result.authorized
        assert result.risk_ceiling is None
    else:
        # All enabled: the ceiling is the lowest max_risk across layers (the binding layer).
        expected_ceiling = min(layer.max_risk for layer in layers)
        assert result.risk_ceiling == expected_ceiling
        if not expected_authorized:
            binding = min(layers, key=lambda layer: layer.max_risk)
            assert result.failed_layer == binding.name
