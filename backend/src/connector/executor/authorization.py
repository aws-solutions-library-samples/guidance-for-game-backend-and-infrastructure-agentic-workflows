"""Two-layer authorization for the executor write path (design → Component 7).

This module implements the two authorization layers the write path enforces at every entry
boundary and re-checks at execution time:

1. **Capability posture** (:class:`CapabilityPosture`) — the deployment-mode + trusted
   tenant/workspace configuration that must be *enabled* for an operation to proceed past any
   write-path boundary (preparation, approval, execution) (Req 7.1).
2. **Request-time check** (:func:`request_time_check`) — evaluates the principal's authority,
   the capability maximum, and the operation's risk (Req 7.2).

On top of these, :func:`compute_effective_authority` computes the **Effective_Authority** as
the *intersection* of all applicable policy layers — deployment mode, tenant policy, workspace
policy, principal authority, capability maximum, and operation-risk policy — and an operation
is authorized iff its risk lies within that intersection (Req 7.3). The decision + its inputs
are stamped onto the prepared operation at preparation time (Req 7.4) and re-evaluated by the
executor before any provider write.

Separately, :func:`target_authorization` evaluates **Target_Authorization** (repository ·
branch · normalized path · extension · group) by **reusing the sibling
:class:`connector.config.AuthorizationPolicy`** — the same five-dimension evaluator the
read/propose paths use — so target authorization is identical across the read and write halves
and is re-checked by the executor independently of the preparation-time decision (Req 7.5,
7.6).
"""

from __future__ import annotations

# Standard library
from collections.abc import Sequence
from dataclasses import dataclass

# Local modules
from connector.config import AuthorizationPolicy, Decision
from connector.executor.models import EffectiveAuthority, RiskLevel

__all__ = [
    "CapabilityPosture",
    "PolicyLayer",
    "request_time_check",
    "compute_effective_authority",
    "normalize_path",
    "target_authorization",
]


@dataclass(frozen=True)
class CapabilityPosture:
    """Deployment-mode + trusted tenant/workspace capability posture (Req 7.1).

    ``enabled`` gates whether the write path is permitted at all for this tenant/workspace;
    ``capability_maximum`` is the maximum operation risk the capability posture itself
    permits. The posture is enforced at *every* write-path entry boundary (preparation,
    approval, execution), so an operation only proceeds past a boundary when the posture is
    enabled for its tenant/workspace.
    """

    enabled: bool
    capability_maximum: RiskLevel
    tenant: str | None = None
    workspace: str | None = None

    def is_enabled(self) -> bool:
        """Return ``True`` iff the capability posture permits the write path here."""
        return self.enabled


@dataclass(frozen=True)
class PolicyLayer:
    """One applicable authorization policy layer contributing to the intersection.

    ``name`` identifies the layer (e.g. ``"deployment_mode"``, ``"tenant"``, ``"workspace"``,
    ``"principal"``, ``"capability_maximum"``, ``"risk_policy"``); ``enabled`` is whether the
    layer permits the write path at all; ``max_risk`` is the maximum operation risk the layer
    permits. The Effective_Authority is the intersection over a set of these layers.
    """

    name: str
    enabled: bool
    max_risk: RiskLevel


def request_time_check(
    *,
    principal_authority: RiskLevel,
    capability_maximum: RiskLevel,
    operation_risk: RiskLevel,
) -> bool:
    """Evaluate the request-time check over principal authority, capability max, and risk.

    Returns ``True`` iff the operation's risk is within *both* the principal's authority and
    the capability maximum — i.e. ``operation_risk <= min(principal_authority,
    capability_maximum)`` (Req 7.2).
    """
    return operation_risk <= min(principal_authority, capability_maximum)


def compute_effective_authority(
    layers: Sequence[PolicyLayer],
    operation_risk: RiskLevel,
) -> EffectiveAuthority:
    """Compute Effective_Authority as the intersection of all applicable policy layers.

    The intersection is: every layer must be ``enabled``, and the operation is authorized iff
    its risk is within the *lowest* ceiling any layer imposes
    (``operation_risk <= min(layer.max_risk)``) (Req 7.3). The returned
    :class:`EffectiveAuthority` records the decision, the layer names that formed the
    intersection (its inputs), the intersected ``risk_ceiling``, and — on a denial — the layer
    that caused it (a disabled layer, or the layer whose ceiling bound below the operation
    risk). This decision + inputs are what the preparation stamps onto the operation (Req 7.4).
    """
    inputs = tuple(layer.name for layer in layers)

    # Any disabled layer denies outright; report the first disabled layer.
    for layer in layers:
        if not layer.enabled:
            return EffectiveAuthority(
                decision="denied",
                inputs=inputs,
                risk_ceiling=None,
                failed_layer=layer.name,
            )

    if not layers:
        # No applicable layers => nothing constrains the operation; treat as authorized with
        # no ceiling. (The write path always supplies the standard layers in practice.)
        return EffectiveAuthority(decision="authorized", inputs=inputs, risk_ceiling=None)

    binding_layer = min(layers, key=lambda layer: layer.max_risk)
    ceiling = binding_layer.max_risk
    if operation_risk <= ceiling:
        return EffectiveAuthority(decision="authorized", inputs=inputs, risk_ceiling=ceiling)
    return EffectiveAuthority(
        decision="denied",
        inputs=inputs,
        risk_ceiling=ceiling,
        failed_layer=binding_layer.name,
    )


def normalize_path(path: str) -> str:
    """Return a normalized repo-relative path for target authorization.

    Trims surrounding whitespace and strips a leading ``./`` or ``/`` so a path is matched
    against allowlist prefixes/extensions on a consistent, repo-relative form (Req 7.5).
    """
    normalized = (path or "").strip()
    while normalized.startswith(("./", "/")):
        normalized = normalized[2:] if normalized.startswith("./") else normalized[1:]
    return normalized


def target_authorization(
    policy: AuthorizationPolicy,
    *,
    repo: str,
    branch: str,
    paths: Sequence[str],
    groups: Sequence[str],
    authorized_groups: Sequence[str],
) -> Decision:
    """Evaluate Target_Authorization over repo · branch · normalized path · extension · group.

    Delegates to the sibling :class:`connector.config.AuthorizationPolicy` — the identical
    five-dimension evaluator the read/propose paths use — after normalizing each path, so the
    write half authorizes targets exactly as the read half does (Req 7.5). The executor calls
    this again at execution time to re-check target authorization independently of the
    preparation-time decision (Req 7.6).
    """
    normalized_paths = [normalize_path(p) for p in paths]
    return policy.authorize(
        repo=repo,
        branch=branch,
        paths=normalized_paths,
        groups=list(groups),
        authorized_groups=list(authorized_groups),
    )
