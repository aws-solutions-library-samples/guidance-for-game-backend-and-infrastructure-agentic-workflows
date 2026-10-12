"""Deployment-configured, server-owned capacity bounds resolver (issue #414).

The E2 advise layer resolves enrollment/policy bounds through the trusted
:class:`~operations.advice.CapacityBoundsPort`. Those bounds are **server-owned**
— the floor/ceiling/max-step limits and the policy/enrollment identifiers come
from the deployment configuration and code, never from the untrusted request.

:class:`DeploymentCapacityBoundsResolver` derives ``target_enrolled`` from the
same trusted signal the advice layer already requires: a fresh, successful E1
observation exists for the fleet/location under the verified workspace (proof
that the target is observable/enrolled for this workspace). When no such
observation exists the resolver still returns a bounds object with
``target_enrolled=False`` so the advice layer *deterministically denies* with
``TARGET_NOT_ENROLLED`` rather than crashing or silently authorizing.

The resolver holds no credential, performs no provider read of its own beyond
the injected read-only state port, and issues no write.
"""

from __future__ import annotations

# Local modules
from operations.advice import CapacityBounds, CapacityStatePort
from operations.identity import VerifiedPrincipal


class DeploymentCapacityBoundsResolver:
    """Resolve server-owned bounds and derive enrollment from a fresh E1 read."""

    def __init__(
        self,
        *,
        state_port: CapacityStatePort,
        floor: int,
        ceiling: int,
        max_step: int,
        enrollment_id: str,
        enrollment_version: str,
        policy_id: str,
        policy_version: str,
    ) -> None:
        if not (0 <= floor <= ceiling):
            raise ValueError("floor must be non-negative and no greater than ceiling")
        if max_step <= 0:
            raise ValueError("max_step must be a positive integer")
        for value, name in (
            (enrollment_id, "enrollment_id"),
            (enrollment_version, "enrollment_version"),
            (policy_id, "policy_id"),
            (policy_version, "policy_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        self._state_port = state_port
        self._floor = floor
        self._ceiling = ceiling
        self._max_step = max_step
        self._enrollment_id = enrollment_id
        self._enrollment_version = enrollment_version
        self._policy_id = policy_id
        self._policy_version = policy_version

    def resolve_bounds(self, *, requester: VerifiedPrincipal, fleet_id: str, location: str) -> CapacityBounds:
        """Return server-owned bounds; ``target_enrolled`` reflects a fresh read."""
        current = self._state_port.load_current_capacity(requester=requester, fleet_id=fleet_id, location=location)
        return CapacityBounds(
            floor=self._floor,
            ceiling=self._ceiling,
            max_step=self._max_step,
            enrollment_id=self._enrollment_id,
            enrollment_version=self._enrollment_version,
            policy_id=self._policy_id,
            policy_version=self._policy_version,
            target_enrolled=current is not None,
        )
