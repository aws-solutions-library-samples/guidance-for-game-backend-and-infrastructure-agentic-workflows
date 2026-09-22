"""Independent E3 execution precondition verifier (issue #415, E3 execute).

The executor trusts nothing it is handed — not the dispatcher, not the Step
Functions workflow, not the intent, not even the operation id beyond using it to
reload. This module is the pure, no-I/O gate that, given a freshly *reloaded*
prepared operation and its stored granted approval plus the code-owned
deployment authority context, re-verifies EVERY server-owned precondition before
a single provider write is allowed:

1. **Operation integrity** — the reloaded prepared operation is contract-valid
   and its ``prepared_hash`` still binds every field (tamper/drift fail closed).
2. **Operation freshness** — ``expires_at`` is strictly in the future.
3. **Granted, bound, unexpired human approval** — the stored approval is
   ``granted``, its ``prepared_operation_hash`` equals the canonical prepared
   hash, its ``operation_id`` and ``policy_version`` match, and its ``expires_at``
   is strictly in the future.
4. **Policy / tenant / workspace** — the approval and operation policy versions
   agree and the operation's requester tenant/workspace equals the deployment.
5. **Real playbook hash** — the operation's bound ``playbook_hash`` equals the
   deployment's code-owned playbook hash (never an all-zero placeholder).
6. **Executor binding** — the operation's ``future_executor_binding`` equals the
   deployment's executor id/version.
7. **Execution authority** — the operation's ``required_execution_authority`` is
   ``remediate`` AND the *deployed* mode and capability maximum are both at or
   above ``remediate``. The advise-authority E2 phase never satisfies this.
8. **Enrolled fleet binding** — the operation's target fleet id and location
   equal the deployment's enrolled fleet id/location, and the deployment binds a
   real fleet ARN. The ARN is server-owned and never taken from the operation.
9. **Hard server-owned bounds** — at most one logical update per operation
   (``max_writes == 1``); the deterministic ``logical_action_id`` enforces it.

Every failure raises :class:`ExecutionVerificationError` with a stable, bounded
:class:`ExecutionVerificationReason` and a safe message that never echoes an
ARN, fleet id, hash, or identity. On success it returns a
:class:`VerifiedExecutionPlan` carrying only the deterministic write intent, the
server-owned fleet ARN, the logical action id, and the hard write bound.
"""

from __future__ import annotations

# Standard library
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Local modules
from operations.contracts.capacity import (
    CapacityContractError,
    capacity_prepared_hash,
    validate_capacity_prepared_operation,
)
from operations.contracts.execution import (
    ExecutionContractError,
    build_execution_intent,
    logical_action_id,
)

# ADR 0001 authority lattice ordering; lower is more restrictive.
_AUTHORITY_ORDER = {"disabled": 0, "observe": 1, "advise": 2, "remediate": 3, "operate": 4}
_REQUIRED_EXECUTION_AUTHORITY = "remediate"
_FLEET_ARN_PREFIX = "arn:aws:gamelift:"

# The hard, server-owned execution bound: one operation produces at most one
# logical update.
_MAX_WRITES = 1


class ExecutionVerificationReason(str, Enum):
    """Stable, bounded, public-safe execution precondition failures."""

    OPERATION_INTEGRITY_INVALID = "operation_integrity_invalid"
    OPERATION_EXPIRED = "operation_expired"
    APPROVAL_NOT_BOUND = "approval_not_bound"
    APPROVAL_EXPIRED = "approval_expired"
    TENANT_WORKSPACE_MISMATCH = "tenant_workspace_mismatch"
    PLAYBOOK_HASH_MISMATCH = "playbook_hash_mismatch"
    EXECUTOR_BINDING_MISMATCH = "executor_binding_mismatch"
    INSUFFICIENT_EXECUTION_AUTHORITY = "insufficient_execution_authority"
    FLEET_BINDING_MISMATCH = "fleet_binding_mismatch"
    CONTEXT_INVALID = "context_invalid"


class ExecutionVerificationError(RuntimeError):
    """A precondition failed; no provider write may proceed."""

    def __init__(self, reason: ExecutionVerificationReason, safe_message: str) -> None:
        self.reason = reason
        self.safe_message = safe_message
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionAuthorityContext:
    """Code-owned deployment authority the executor re-verifies against.

    Every field is server/deployment-owned; none is taken from the request, the
    intent, the operation, or the approval. The fleet ARN in particular is
    resolved from the deployment's enrollment record, never from the operation.
    """

    deployment_mode: str
    capability_maximum: str
    tenant_id: str
    workspace_id: str
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
class VerifiedExecutionPlan:
    """The bounded, safe output of a fully-verified execution precondition set."""

    intent: dict[str, Any]
    logical_action_id: str
    fleet_arn: str
    fleet_id: str
    location: str
    max_writes: int


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


class ExecutionVerifier:
    """Pure, no-I/O re-verification of every E3 execution precondition."""

    def __init__(self, *, context: ExecutionAuthorityContext, clock: Callable[[], datetime] = _system_clock) -> None:
        if not isinstance(context, ExecutionAuthorityContext):
            raise ValueError("context must be an ExecutionAuthorityContext")
        self._context = context
        self._clock = clock

    def verify(
        self,
        *,
        prepared_operation: Mapping[str, Any],
        approval: Mapping[str, Any],
    ) -> VerifiedExecutionPlan:
        """Re-verify all preconditions and return the bounded execution plan."""
        ctx = self._context
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ExecutionVerificationError(ExecutionVerificationReason.CONTEXT_INVALID, "execution clock is invalid")
        now = now.astimezone(timezone.utc)

        prepared = dict(prepared_operation)

        # 1. Operation integrity: contract-valid and prepared_hash binds all.
        try:
            validate_capacity_prepared_operation(prepared)
        except CapacityContractError as exc:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.OPERATION_INTEGRITY_INVALID,
                "prepared operation failed its integrity contract",
            ) from exc

        prepared_hash = capacity_prepared_hash(prepared)
        if prepared["prepared_hash"] != prepared_hash:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.OPERATION_INTEGRITY_INVALID,
                "prepared operation hash does not bind the document",
            )
        if prepared["required_execution_authority"] != _REQUIRED_EXECUTION_AUTHORITY:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.INSUFFICIENT_EXECUTION_AUTHORITY,
                "operation does not require remediate execution authority",
            )

        # 2. Operation freshness.
        operation_expires_at = _parse_timestamp(prepared.get("expires_at"))
        if operation_expires_at is None:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.OPERATION_INTEGRITY_INVALID,
                "operation expiry is invalid",
            )
        if operation_expires_at <= now:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.OPERATION_EXPIRED,
                "operation is no longer eligible for execution",
            )

        # 3. Granted, bound, unexpired human approval.
        if not isinstance(approval, Mapping):
            raise ExecutionVerificationError(ExecutionVerificationReason.APPROVAL_NOT_BOUND, "approval is missing")
        if (
            approval.get("decision") != "granted"
            or approval.get("operation_id") != prepared["operation_id"]
            or approval.get("prepared_operation_hash") != prepared_hash
            or approval.get("policy_version") != prepared["policy"]["policy_version"]
        ):
            raise ExecutionVerificationError(
                ExecutionVerificationReason.APPROVAL_NOT_BOUND,
                "approval does not bind this operation",
            )
        approval_expires_at = _parse_timestamp(approval.get("expires_at"))
        if approval_expires_at is None:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.APPROVAL_NOT_BOUND, "approval expiry is invalid"
            )
        if approval_expires_at <= now:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.APPROVAL_EXPIRED,
                "granted approval has expired",
            )

        # 4. Tenant / workspace binding against the deployment.
        requester = prepared["requester"]
        if requester["tenant_id"] != ctx.tenant_id or requester["workspace_id"] != ctx.workspace_id:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.TENANT_WORKSPACE_MISMATCH,
                "operation tenant/workspace does not match the deployment",
            )

        # 5. Real playbook hash.
        if prepared["playbook"]["playbook_hash"] != ctx.expected_playbook_hash:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.PLAYBOOK_HASH_MISMATCH,
                "operation playbook hash does not match the deployed playbook",
            )

        # 6. Executor binding.
        binding = prepared["future_executor_binding"]
        if (
            binding["executor_id"] != ctx.expected_executor_id
            or binding["executor_binding_version"] != ctx.expected_executor_binding_version
        ):
            raise ExecutionVerificationError(
                ExecutionVerificationReason.EXECUTOR_BINDING_MISMATCH,
                "operation executor binding does not match this executor",
            )

        # 7. Execution authority: deployed mode AND capability >= remediate.
        required = _AUTHORITY_ORDER[_REQUIRED_EXECUTION_AUTHORITY]
        if _AUTHORITY_ORDER[ctx.deployment_mode] < required or _AUTHORITY_ORDER[ctx.capability_maximum] < required:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.INSUFFICIENT_EXECUTION_AUTHORITY,
                "deployment does not grant remediate execution authority",
            )

        # 8. Enrolled fleet binding (ARN is server-owned, never from the op).
        target = prepared["target"]
        if target["fleet_id"] != ctx.enrolled_fleet_id or target["location"] != ctx.enrolled_location:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.FLEET_BINDING_MISMATCH,
                "operation target does not match the enrolled fleet",
            )

        # 9. Build the deterministic write intent and confirm its binding.
        intent = build_execution_intent(prepared)
        try:
            # Local modules
            from operations.contracts.execution import validate_execution_intent_binding

            validate_execution_intent_binding(intent, prepared)
        except ExecutionContractError as exc:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.OPERATION_INTEGRITY_INVALID,
                "execution intent does not bind the operation",
            ) from exc

        expected_action_id = logical_action_id(prepared["operation_id"], prepared_hash)
        if intent["logical_action_id"] != expected_action_id:
            raise ExecutionVerificationError(
                ExecutionVerificationReason.OPERATION_INTEGRITY_INVALID,
                "logical action id is not deterministic",
            )

        return VerifiedExecutionPlan(
            intent=intent,
            logical_action_id=intent["logical_action_id"],
            fleet_arn=ctx.enrolled_fleet_arn,
            fleet_id=ctx.enrolled_fleet_id,
            location=ctx.enrolled_location,
            max_writes=_MAX_WRITES,
        )
