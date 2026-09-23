"""Independent E5 bounded-autonomy execution precondition verifier (issue #439).

This module is the additive, **v2** sibling of the v1
:mod:`operations.execution_verifier`. It is the pure, no-provider-I/O gate that a
future autonomous executor consults before a single ``UpdateFleetCapacity``
write when there is **no human in the loop**. It re-verifies EVERY server-owned
precondition against a freshly *reloaded* bundle — the exact autonomy policy, the
canonical E1 observation (and its bound evidence projection), the deterministic
autonomous decision, the immutable autonomous prepared operation, and the
current durable rolling-window state — plus the code-owned deployment authority
context, a fresh *separate* autonomy switch supplied via a port, and atomic
reservation ownership confirmed immediately before the write.

The v1 :class:`~operations.execution_verifier.ExecutionVerifier` and its
granted-human-approval branch are untouched. This layer never authorizes,
alters policy or limits, dispatches, or reaches the executor from model output:
the only grant it accepts is the deterministic, hash-bound
``authorized`` / ``APPROVED_AUTONOMOUS`` decision at ``operate`` authority
produced by the #438 evaluator. It holds no provider-write permission and no
executor credential.

Order of gates (each is a discriminating fail-closed check):

1. **Operation identity** — the reloaded operation's ``operation_id`` equals the
   ``operation_id`` the workflow was invoked with (the only untrusted input).
2. **#438 binding** — the operation binds the exact policy, canonical E1
   observation evidence, durable window-state reference, and decision, and the
   decision re-derives from the deterministic evaluator at the decision's own
   ``evaluated_at`` (:func:`validate_autonomous_operation_binding`). Any drift,
   forgery, stale/mismatched state, or bounds/guardrail breach fails here.
3. **Authorization** — the decision is ``authorized`` with the single reason
   code ``APPROVED_AUTONOMOUS`` at ``operate`` effective authority, and carries
   ``required_execution_authority == operate``. There is no human-approval
   field anywhere; a smuggled one is ignored, never honored.
4. **Registered identities** — the operation's policy id/version/hash,
   playbook hash, executor id/version, and required execution authority equal
   the code-owned deployment values; the target fleet id/location equal the
   enrolled fleet, whose ARN is server-owned and never taken from the operation;
   and the requester tenant/workspace equal the deployment.
5. **Deployed authority** — the *deployed* mode AND capability maximum are both
   at or above ``operate``.
6. **Decision freshness** — ``decision_expires_at`` is strictly in the future.
7. **Separate autonomy switch** — a fresh, deployment-owned autonomy switch,
   read through a port, explicitly enables autonomous execution (fail closed on
   any denial/unavailability). This is separate from the E4 kill switch the
   executor itself re-checks twice.
8. **Atomic reservation** — the caller owns the atomic budget/cooldown/frequency/
   concurrency reservation for this exact ``logical_action_id``, confirmed
   *last*, immediately before the write.

On success it returns a :class:`~operations.execution_verifier.VerifiedExecutionPlan`
— the same bounded, safe structure the existing ``ExecutorService`` already
consumes — carrying only the deterministic write intent, the server-owned fleet
ARN, the logical action id, and the hard ``max_writes == 1`` bound. Every failure
raises :class:`AutonomyExecutionVerificationError` with a stable, bounded,
public-safe reason and a safe message that never echoes an ARN, fleet id, hash,
or identity.
"""

from __future__ import annotations

# Standard library
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

# Local modules
from operations.contracts.autonomy import (
    AUTONOMY_MINIMUM_AUTHORITY,
    REQUIRED_EXECUTION_AUTHORITY,
    AutonomyContractError,
    autonomous_prepared_hash,
    validate_autonomous_operation_binding,
)
from operations.contracts.execution import ACTION as EXECUTION_ACTION
from operations.contracts.execution import CONTRACT_VERSION as EXECUTION_CONTRACT_VERSION
from operations.contracts.execution import PROVIDER as EXECUTION_PROVIDER
from operations.contracts.execution import logical_action_id
from operations.execution_verifier import VerifiedExecutionPlan

# ADR 0001 authority lattice ordering; lower is more restrictive.
_AUTHORITY_ORDER = {"disabled": 0, "observe": 1, "advise": 2, "remediate": 3, "operate": 4}
_FLEET_ARN_PREFIX = "arn:aws:gamelift:"

# The single reason code a non-denied autonomous decision must carry.
_AUTHORIZED_REASON = "APPROVED_AUTONOMOUS"
_AUTHORIZED_DECISION = "authorized"

# The hard, server-owned execution bound: one operation → at most one write.
_MAX_WRITES = 1


class AutonomyExecutionVerificationReason(str, Enum):
    """Stable, bounded, public-safe autonomy execution precondition failures."""

    OPERATION_IDENTITY_MISMATCH = "operation_identity_mismatch"
    BINDING_INVALID = "binding_invalid"
    DECISION_NOT_AUTHORIZED = "decision_not_authorized"
    INSUFFICIENT_EXECUTION_AUTHORITY = "insufficient_execution_authority"
    POLICY_MISMATCH = "policy_mismatch"
    PLAYBOOK_HASH_MISMATCH = "playbook_hash_mismatch"
    EXECUTOR_BINDING_MISMATCH = "executor_binding_mismatch"
    TENANT_WORKSPACE_MISMATCH = "tenant_workspace_mismatch"
    FLEET_BINDING_MISMATCH = "fleet_binding_mismatch"
    DECISION_EXPIRED = "decision_expired"
    AUTONOMY_SWITCH_DENIED = "autonomy_switch_denied"
    RESERVATION_NOT_OWNED = "reservation_not_owned"
    CONTEXT_INVALID = "context_invalid"


class AutonomyExecutionVerificationError(RuntimeError):
    """A precondition failed; no autonomous provider write may proceed."""

    def __init__(self, reason: AutonomyExecutionVerificationReason, safe_message: str) -> None:
        self.reason = reason
        self.safe_message = safe_message
        super().__init__(safe_message)


class AutonomySwitchDenied(RuntimeError):
    """The separate autonomy switch does not enable autonomous execution.

    Raised by an :class:`AutonomySwitchPort` implementation on any denial —
    an explicitly-disabled switch, an unavailable/malformed/stale document — so
    the verifier has exactly one deny signal to fail closed on.
    """


class AutonomySwitchPort(Protocol):
    """Read-through port to the fresh, separate autonomy switch.

    Implementations MUST read the switch fresh on every call (no caching) and
    raise :class:`AutonomySwitchDenied` on any denial or unavailability so a
    flipped switch takes effect on the very next verification.
    """

    def require_autonomy(self) -> None: ...


class ReservationPort(Protocol):
    """Port confirming atomic reservation ownership immediately before write.

    Implementations MUST fail (raise) unless the caller currently, atomically
    owns the budget/cooldown/frequency/concurrency reservation for this exact
    ``logical_action_id`` — the same fencing the durable window state was
    reserved under.
    """

    def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AutonomyExecutionAuthorityContext:
    """Code-owned deployment authority the autonomous executor re-verifies against.

    Every field is server/deployment-owned; none is taken from the request, the
    model, the operation, the decision, or the policy document. The fleet ARN in
    particular is resolved from the deployment's enrollment record, never from
    the operation.
    """

    deployment_mode: str
    capability_maximum: str
    tenant_id: str
    workspace_id: str
    expected_policy_id: str
    expected_policy_version: str
    expected_policy_hash: str
    expected_playbook_hash: str
    expected_executor_id: str
    expected_executor_binding_version: str
    enrolled_fleet_id: str
    enrolled_fleet_arn: str
    enrolled_location: str

    def __post_init__(self) -> None:
        if self.deployment_mode not in _AUTHORITY_ORDER or self.capability_maximum not in _AUTHORITY_ORDER:
            raise ValueError("deployment_mode and capability_maximum must be valid authority levels")
        for name in (
            "tenant_id",
            "workspace_id",
            "expected_policy_id",
            "expected_policy_version",
            "expected_policy_hash",
            "expected_playbook_hash",
            "expected_executor_id",
            "expected_executor_binding_version",
            "enrolled_fleet_id",
            "enrolled_fleet_arn",
            "enrolled_location",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not self.enrolled_fleet_arn.startswith(_FLEET_ARN_PREFIX):
            raise ValueError("enrolled_fleet_arn must be a GameLift fleet ARN")


@dataclass(frozen=True, slots=True, kw_only=True)
class AutonomyExecutionEvidence:
    """The freshly-reloaded, server-owned document bundle the verifier re-checks.

    None of these is model or request-body input: the policy, decision,
    operation, observation, and durable window state are all reloaded from the
    trusted store keyed by the operation id.
    """

    policy: dict[str, Any]
    observation: dict[str, Any]
    decision: dict[str, Any]
    operation: dict[str, Any]
    window_state: dict[str, Any]


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


class AutonomyExecutionVerifier:
    """Pure re-verification of every E5 bounded-autonomy execution precondition."""

    def __init__(
        self,
        *,
        context: AutonomyExecutionAuthorityContext,
        autonomy_switch: AutonomySwitchPort,
        reservation: ReservationPort,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        if not isinstance(context, AutonomyExecutionAuthorityContext):
            raise ValueError("context must be an AutonomyExecutionAuthorityContext")
        self._context = context
        self._autonomy_switch = autonomy_switch
        self._reservation = reservation
        self._clock = clock

    def verify(self, *, operation_id: str, evidence: AutonomyExecutionEvidence) -> VerifiedExecutionPlan:
        """Re-verify all preconditions and return the bounded execution plan."""
        ctx = self._context
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.CONTEXT_INVALID, "execution clock is invalid"
            )
        now = now.astimezone(timezone.utc)

        if not isinstance(evidence, AutonomyExecutionEvidence):
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.CONTEXT_INVALID, "evidence bundle is invalid"
            )

        policy = evidence.policy
        observation = evidence.observation
        decision = evidence.decision
        operation = evidence.operation
        window_state = evidence.window_state

        # 1. Operation identity: the only untrusted input is the operation id.
        if not isinstance(operation, dict) or operation.get("operation_id") != operation_id:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.OPERATION_IDENTITY_MISMATCH,
                "operation identity does not match the invocation",
            )

        # 2. #438 binding: policy/observation/decision/operation/window all bind
        # and the decision re-derives from the deterministic evaluator. This also
        # catches forged/tampered documents, stale/mismatched window state, and
        # any bounds/guardrail breach.
        try:
            validate_autonomous_operation_binding(operation, decision, policy, observation, window_state)
        except AutonomyContractError as exc:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.BINDING_INVALID,
                "autonomous operation binding is invalid",
            ) from exc

        prepared_hash = autonomous_prepared_hash(operation)
        if operation.get("prepared_hash") != prepared_hash:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.BINDING_INVALID,
                "operation prepared hash does not bind the document",
            )

        # 3. Authorization: only the deterministic operate-authority decision is a
        # grant. There is no human-approval branch; a smuggled approval field is
        # never consulted.
        authority = operation["authority"]
        if (
            decision["decision"] != _AUTHORIZED_DECISION
            or authority["decision"] != _AUTHORIZED_DECISION
            or decision["reason_codes"] != [_AUTHORIZED_REASON]
            or authority["reason_codes"] != [_AUTHORIZED_REASON]
        ):
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.DECISION_NOT_AUTHORIZED,
                "decision is not an authorized autonomous grant",
            )
        if (
            decision["effective_authority"] != AUTONOMY_MINIMUM_AUTHORITY
            or authority["effective_authority"] != AUTONOMY_MINIMUM_AUTHORITY
        ):
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.DECISION_NOT_AUTHORIZED,
                "decision effective authority is not operate",
            )
        if (
            decision["required_execution_authority"] != REQUIRED_EXECUTION_AUTHORITY
            or operation["required_execution_authority"] != REQUIRED_EXECUTION_AUTHORITY
        ):
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.INSUFFICIENT_EXECUTION_AUTHORITY,
                "operation does not require operate execution authority",
            )

        # 4. Registered identities against the code-owned deployment.
        op_policy = operation["autonomy_policy"]
        if (
            op_policy["policy_id"] != ctx.expected_policy_id
            or op_policy["policy_version"] != ctx.expected_policy_version
            or op_policy["policy_hash"] != ctx.expected_policy_hash
        ):
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.POLICY_MISMATCH,
                "operation policy does not match the deployed autonomy policy",
            )
        if operation["playbook"]["playbook_hash"] != ctx.expected_playbook_hash:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.PLAYBOOK_HASH_MISMATCH,
                "operation playbook hash does not match the deployed playbook",
            )
        binding = operation["executor_binding"]
        if (
            binding["executor_id"] != ctx.expected_executor_id
            or binding["executor_binding_version"] != ctx.expected_executor_binding_version
        ):
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.EXECUTOR_BINDING_MISMATCH,
                "operation executor binding does not match this executor",
            )
        principal = operation["automation_principal"]
        if principal["tenant_id"] != ctx.tenant_id or principal["workspace_id"] != ctx.workspace_id:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.TENANT_WORKSPACE_MISMATCH,
                "operation tenant/workspace does not match the deployment",
            )
        target = operation["target"]
        if target["fleet_id"] != ctx.enrolled_fleet_id or target["location"] != ctx.enrolled_location:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.FLEET_BINDING_MISMATCH,
                "operation target does not match the enrolled fleet",
            )

        # 5. Deployed authority: mode AND capability >= operate.
        required = _AUTHORITY_ORDER[AUTONOMY_MINIMUM_AUTHORITY]
        if _AUTHORITY_ORDER[ctx.deployment_mode] < required or _AUTHORITY_ORDER[ctx.capability_maximum] < required:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.INSUFFICIENT_EXECUTION_AUTHORITY,
                "deployment does not grant operate execution authority",
            )

        # 6. Decision freshness: strictly unexpired.
        decision_expires_at = _parse_timestamp(operation.get("decision_expires_at"))
        if decision_expires_at is None:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.BINDING_INVALID,
                "decision expiry is invalid",
            )
        if decision_expires_at <= now:
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.DECISION_EXPIRED,
                "autonomous decision is no longer eligible for execution",
            )

        # Build the deterministic, attempt-independent write intent now that the
        # document is fully bound. The logical action id follows the same stable
        # (operation_id, prepared_hash) derivation as the v1 path.
        action_id = logical_action_id(operation_id, prepared_hash)
        requested = operation["parameters"]["requested"]
        expected_current = operation["current_state"]["capacity"]
        intent = {
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "operation_id": operation_id,
            "prepared_hash": prepared_hash,
            "logical_action_id": action_id,
            "provider": EXECUTION_PROVIDER,
            "action": EXECUTION_ACTION,
            "target": {
                "provider": target["provider"],
                "fleet_id": target["fleet_id"],
                "location": target["location"],
            },
            "parameters": dict(requested),
            "expected_current_capacity": dict(expected_current),
        }

        # 7. Separate autonomy switch (read fresh through the port), before write.
        try:
            self._autonomy_switch.require_autonomy()
        except Exception as exc:  # noqa: BLE001 - any switch denial fails closed
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.AUTONOMY_SWITCH_DENIED,
                "autonomous execution is disabled by the autonomy switch",
            ) from exc

        # 8. Atomic reservation ownership, confirmed LAST — immediately before
        # the write. A lost or superseded reservation fails closed with no plan.
        try:
            self._reservation.require_reservation(operation_id=operation_id, logical_action_id=action_id)
        except Exception as exc:  # noqa: BLE001 - any reservation loss fails closed
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.RESERVATION_NOT_OWNED,
                "atomic autonomy reservation is not owned",
            ) from exc

        return VerifiedExecutionPlan(
            intent=intent,
            logical_action_id=action_id,
            fleet_arn=ctx.enrolled_fleet_arn,
            fleet_id=ctx.enrolled_fleet_id,
            location=ctx.enrolled_location,
            max_writes=_MAX_WRITES,
        )
