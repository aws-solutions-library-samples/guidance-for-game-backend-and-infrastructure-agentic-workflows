"""Identifier-only dispatch boundary + emergency disablement for E5 autonomy (#439).

This is the *only* boundary between the deterministic autonomy runtime and the
existing narrow executor, and it is deliberately thin and identifier-only.

Key invariants (ADR 0001 / AGENTS.md):

* **Identifier-only dispatch.** The dispatch envelope carries the
  ``operation_id`` and nothing else — no policy, no limits, no observation, no
  window state, no credential, and no model or request-body input. The executor
  re-reads the durable prepared operation by id and independently re-verifies
  ``operate`` authority before the single provider write.
* **The runtime holds no provider-write permission or executor credential.**
  This module produces an envelope; it does not invoke the executor. Only a
  durable, authenticated Step Functions state machine (provisioned outside the
  default read-only chat runtime) is permitted to invoke the executor with the
  identifier.
* **Emergency disablement is fail-closed and checked twice.** It is consulted
  *before* dispatch (here) and again *immediately before* the provider write
  (:meth:`DispatchEnvelope.authorize_provider_write`). When engaged, dispatch is
  refused and no envelope is produced; a pre-write re-check that finds it engaged
  refuses the write.
* **Default disabled.** An unauthorized (denied) decision can never be
  dispatched.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

# Local modules
from operations.autonomy_runtime.service import PreparedAutonomousOperation


class DispatchOutcome(str, Enum):
    """The closed outcome set of a dispatch attempt."""

    DISPATCHED = "dispatched"
    REFUSED = "refused"


class DispatchRefused(str, Enum):
    """The closed reason set for a refused dispatch (fail closed)."""

    NOT_AUTHORIZED = "not_authorized"
    EMERGENCY_DISABLED = "emergency_disabled"


@runtime_checkable
class EmergencyDisablement(Protocol):
    """A hard, durable emergency kill switch consulted at every gate.

    ``is_engaged`` returns ``True`` when autonomous dispatch and provider writes
    must be refused. Implementations MUST fail closed — an implementation that
    cannot determine its state SHOULD raise or return ``True``.
    """

    def is_engaged(self) -> bool:
        """Return whether the emergency disablement is currently engaged."""
        ...


@dataclass(frozen=True, slots=True)
class DispatchEnvelope:
    """The identifier-only intent handed to the durable Step Functions dispatcher.

    It carries the ``operation_id`` and nothing else. It holds no policy, no
    limits, no observation, no window state, and no credential — the executor
    re-reads the durable prepared operation by id and re-verifies authority.
    """

    operation_id: str

    def payload(self) -> dict[str, str]:
        """Return the identifier-only dispatch payload."""
        return {"operation_id": self.operation_id}

    def authorize_provider_write(self, *, emergency: EmergencyDisablement) -> bool:
        """Re-check emergency disablement immediately before the provider write.

        Returns ``True`` only when the emergency switch is not engaged. This is
        the second, fail-closed consultation of the switch; the executor MUST
        call it immediately before the single ``UpdateFleetCapacity`` write.
        """
        return not emergency.is_engaged()


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """The outcome of a dispatch attempt and, when dispatched, the envelope."""

    outcome: DispatchOutcome
    envelope: DispatchEnvelope | None = None
    reason: DispatchRefused | None = None


def build_dispatch_envelope(
    prepared: PreparedAutonomousOperation,
    *,
    emergency: EmergencyDisablement,
) -> DispatchResult:
    """Build the identifier-only dispatch envelope, fail-closed at every gate.

    Order of gates (all fail closed):

    1. Emergency disablement is consulted *first*; if engaged, dispatch is
       refused with ``EMERGENCY_DISABLED`` and no envelope is produced — even for
       an authorized decision.
    2. Only an ``authorized`` decision may be dispatched; a denied decision is
       refused with ``NOT_AUTHORIZED``.

    The returned envelope carries the ``operation_id`` only.
    """
    if emergency.is_engaged():
        return DispatchResult(DispatchOutcome.REFUSED, reason=DispatchRefused.EMERGENCY_DISABLED)
    if not prepared.authorized:
        return DispatchResult(DispatchOutcome.REFUSED, reason=DispatchRefused.NOT_AUTHORIZED)
    envelope = DispatchEnvelope(operation_id=prepared.operation["operation_id"])
    return DispatchResult(DispatchOutcome.DISPATCHED, envelope=envelope)
