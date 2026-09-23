"""Narrow execution-path selection between the v1 and v2 executor gates (#439).

The existing E3 executor is invoked with ``operation_id`` only and reloads the
prepared operation, approval, and state itself. This module is the single,
*narrow* seam that decides — from the freshly reloaded, immutable, server-owned
operation document alone — which precondition gate a reloaded operation belongs
to:

* :attr:`ExecutionPath.HUMAN_APPROVAL` — the v1
  ``gamelift.capacity-adjustment/1.0`` prepared operation, verified by the
  untouched :class:`~operations.execution_verifier.ExecutionVerifier` against a
  stored *granted human approval*.
* :attr:`ExecutionPath.AUTONOMOUS` — the v2
  ``gamelift.capacity-adjustment/2.0`` autonomous operation, verified by
  :class:`~operations.autonomy_execution_verifier.AutonomyExecutionVerifier`
  against the deterministic bounded-autonomy decision (no human in the loop).

The decision is made **only** from immutable, server-owned envelope fields — the
contract version, phase, profile, and capability version — that a prepare-time
server owns and hash-binds. It never consults model output, request-body fields,
or a smuggled approval/bypass field: an operation that does not cleanly and
unambiguously match exactly one known envelope fails closed with
:class:`SelectionError` rather than defaulting to a path. This guarantees a v1
human-approved operation is never routed through the autonomous verifier and a
v2 autonomous operation is never routed through the human-approval branch.
"""

from __future__ import annotations

# Standard library
from collections.abc import Mapping
from enum import Enum
from typing import Any

# Local modules
from operations.contracts.autonomy import (
    AUTONOMY_CONTRACT_VERSION,
)
from operations.contracts.autonomy import CAPABILITY_VERSION as AUTONOMY_CAPABILITY_VERSION
from operations.contracts.autonomy import PHASE as AUTONOMY_PHASE
from operations.contracts.autonomy import PROFILE as AUTONOMY_PROFILE

# The v1 human-approved capacity envelope. These mirror the immutable v1
# prepared-operation contract (issue #414/#415) and are matched exactly.
_V1_CONTRACT_VERSION = "1.0"
_V1_PROFILE = "gamelift.capacity-adjustment/1.0"
_V1_CAPABILITY_VERSION = "1.0"
_V1_PHASE = "advise"


class ExecutionPath(Enum):
    """The one precondition gate a reloaded operation is eligible for."""

    HUMAN_APPROVAL = "human_approval"
    AUTONOMOUS = "autonomous"


class SelectionError(ValueError):
    """The operation does not unambiguously match exactly one known envelope."""


def _envelope(operation: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    capability = operation.get("capability")
    capability_version = capability.get("capability_version") if isinstance(capability, Mapping) else None
    return (
        operation.get("operation_contract_version"),
        operation.get("phase"),
        operation.get("profile"),
        capability_version,
    )


def select_execution_path(operation: Mapping[str, Any]) -> ExecutionPath:
    """Return the single execution path a reloaded operation is eligible for.

    Fails closed (:class:`SelectionError`) unless the immutable, server-owned
    envelope exactly matches one — and only one — known contract. The match is
    made from the contract version, phase, profile, and capability version
    together, so a smuggled field can neither add a path nor cross a v1
    operation onto the autonomous gate.
    """
    if not isinstance(operation, Mapping):
        raise SelectionError("operation must be a mapping")

    contract_version, phase, profile, capability_version = _envelope(operation)
    if contract_version is None or phase is None or profile is None or capability_version is None:
        raise SelectionError("operation is missing a required envelope field")

    v1 = (
        contract_version == _V1_CONTRACT_VERSION
        and phase == _V1_PHASE
        and profile == _V1_PROFILE
        and capability_version == _V1_CAPABILITY_VERSION
    )
    v2 = (
        contract_version == AUTONOMY_CONTRACT_VERSION
        and phase == AUTONOMY_PHASE
        and profile == AUTONOMY_PROFILE
        and capability_version == AUTONOMY_CAPABILITY_VERSION
    )

    if v1 and not v2:
        return ExecutionPath.HUMAN_APPROVAL
    if v2 and not v1:
        return ExecutionPath.AUTONOMOUS
    raise SelectionError("operation envelope does not match exactly one known execution contract")
