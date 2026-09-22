"""Deployable AWS Lambda entry point for the E3 executor (issue #415).

The executor is the ONLY component in the whole solution that performs the
bounded GameLift capacity provider write, and only on the exact enrolled fleet.
It is invoked by the Step Functions state machine with a payload of
``operation_id`` ONLY; it re-loads the approved operation from the operations
table under that id, re-checks authority, and performs the pre-approved capacity
change.

This slice DEFINES the frozen entrypoint and its FAIL-CLOSED kill-switch
contract (lever 2 of the reversible emergency disable): when the injected
``GBAW_OPERATIONS_EXECUTION_MODE`` is not ``remediate`` the executor denies
before constructing any boto3 client or touching any AWS resource. It also
enforces the ``operation_id``-only wire contract: a payload carrying any other
field is rejected, because the fleet id and capacity numbers are re-loaded from
the table and never cross the wire.

The E3 core wires the full remediation service behind this entrypoint in a later
slice; until then the enabled path returns a conservative ``denied`` outcome
rather than performing an unverified write.
"""

from __future__ import annotations

# Standard library
from collections.abc import Mapping
from typing import Any

# Local modules
from operations.execute.settings import (
    ExecutionDeploymentSettings,
    resolve_execution_settings,
)

# The single field the state machine threads to the executor. Nothing else may
# appear on the wire; the executor re-loads everything under this id.
_ALLOWED_PAYLOAD_KEYS = frozenset({"operation_id"})


def _denied(operation_id: str, reason: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "outcome": "denied", "reason": reason}


def _rejected(operation_id: str, reason: str) -> dict[str, Any]:
    return {"operation_id": operation_id, "outcome": "rejected", "reason": reason}


def handle_event(event: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Pure, injectable core of the executor handler.

    Fails closed on the disabled kill switch (lever 2) and on any payload that
    is not exactly ``{"operation_id": ...}``. Returns a structured outcome the
    state machine records; never raises for the ordinary denial paths.
    """
    settings = resolve_execution_settings(env)

    payload = event.get("Payload", event) if isinstance(event, Mapping) else {}
    if not isinstance(payload, Mapping):
        return _rejected("", "payload must be a mapping")
    operation_id = str(payload.get("operation_id") or "")

    # Lever 2: fail closed before any AWS work when the kill switch is off.
    if not settings.enabled:
        return _denied(operation_id, "execution disabled (GBAW_OPERATIONS_EXECUTION_MODE is not remediate)")

    # operation_id-only wire contract.
    extra = set(payload.keys()) - _ALLOWED_PAYLOAD_KEYS
    if extra:
        return _rejected(operation_id, f"payload must carry operation_id only; unexpected fields: {sorted(extra)}")
    if not operation_id:
        return _rejected("", "operation_id is required")

    # The enabled remediation path is wired by the E3 core in a later slice.
    # Until then we fail closed rather than perform an unverified provider write.
    return _execute_bounded_remediation(operation_id=operation_id, settings=settings)


def _execute_bounded_remediation(*, operation_id: str, settings: ExecutionDeploymentSettings) -> dict[str, Any]:
    """Perform the bounded capacity remediation for ``operation_id``.

    Placeholder for the E3 core remediation service. It is intentionally
    fail-closed: with no verified remediation service wired, the executor denies
    rather than writing. When the core lands it binds the read-back of the
    approved operation, the authority re-check, and the single fleet-scoped
    ``update_fleet_capacity`` call behind this seam.
    """
    return _denied(operation_id, "remediation service not wired (E3 core pending); failing closed")


def handler(event: Any, context: Any = None) -> dict[str, Any]:  # noqa: ANN401 - Lambda contract
    """AWS Lambda entry point invoked by the execution state machine."""
    return handle_event(event if isinstance(event, Mapping) else {})
