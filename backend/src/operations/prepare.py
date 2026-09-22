"""Protocol-neutral E2 capacity prepare service (issue #414, E2 prepare).

``PrepareService`` turns one deterministic
:func:`~operations.advice.AdviceService.advise` result into one immutable,
idempotent ``gamelift-capacity-prepared-operation``. Preparation is the trusted
authorization/binding half of the E2 prepare layer:

* It selects the exact playbook/capability in code, never from model text.
* It resolves the requester scope only from the trusted
  :class:`~operations.identity.VerifiedPrincipal`.
* It computes the six ADR 0001 authority inputs and their deterministic minimum,
  and produces exactly one of ``approval_required`` (a non-denied GameLift
  capacity change always requires a direct human approval) or ``denied``.
* It fails closed when the advice is stale, when the bound current-state
  observation does not match, or when the change is outside server-owned bounds.
* It binds the entire operation with a canonical ``prepared_hash`` that excludes
  only itself, and it holds no executor credential and performs no provider
  write. The future executor binding is an identifier only.

Preparation is a pure function of (advice, trusted context, playbook): the
timestamps are derived from the trusted E1 observation revision anchor carried
in the advice, so the same inputs always yield the same prepared operation and
the same hash — a retry on a later clock is byte-for-byte idempotent by
construction. The live clock only decides whether the anchor/current-state
revision is still fresh; it never alters the emitted bytes for an otherwise
identical retry.
"""

from __future__ import annotations

# Standard library
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

# Local modules
from operations.contracts import CONTRACT_VERSION
from operations.contracts.capacity import (
    ACTION,
    AUTHORIZATION_SCHEMA_NAME,
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    PHASE,
    PREPARE_MINIMUM_AUTHORITY,
    PREPARED_OPERATION_SCHEMA_NAME,
    PROFILE,
    PROVIDER,
    REQUIRED_EXECUTION_AUTHORITY,
    capacity_prepared_hash,
    effective_authority,
    validate_capacity_contract,
    validate_prepared_operation_binding,
)
from operations.identity import ApprovalIdentityBoundary, IdentityBoundaryError, VerifiedPrincipal
from operations.settings import OperationsSettings

_ADVICE_ID_PATTERN = re.compile(r"^adv_[a-z0-9]{26}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_AUTHORITY_ORDER = {"disabled": 0, "observe": 1, "advise": 2, "remediate": 3, "operate": 4}

# E2 prepare/approval is an ``advise``-authority capability: preparing an
# operation and recording a human approval are advisory acts that never touch a
# provider, so a non-denied outcome only requires the six authority inputs to
# clear ``advise`` (``observe``/``disabled`` still deny). A GameLift capacity
# change is still never authorized outright — the strongest non-denied outcome is
# a prepared operation awaiting a direct human approval — and the operation always
# carries the immutable, hash-bound ``required_execution_authority`` (``remediate``)
# that a future E3 executor re-verifies before any write. Approval never elevates
# execution authority.
_MINIMUM_NON_DENIED_AUTHORITY = PREPARE_MINIMUM_AUTHORITY


class PrepareErrorCode(str, Enum):
    """Stable, safe error codes for the prepare boundary."""

    CONTRACT_INVALID = "contract_invalid"
    IDENTITY_CONTEXT_INVALID = "identity_context_invalid"
    AUTHORIZATION_DENIED = "authorization_denied"
    ADVICE_STALE = "advice_stale"
    # Reserved, wire-stable member. Not currently raised (the prepare boundary
    # fences on advice hash/expiry and re-derives the prepared_hash), but its
    # 409 value is owned by the prepare route's status map. Kept for wire
    # compatibility; pinned by the approval-handler unit tests
    # ``test_prepare_status_map_owns_current_state_mismatch_as_409`` and
    # ``test_prepare_handler_maps_current_state_mismatch_error_to_409``.
    # Do not remove without a contract bump.
    CURRENT_STATE_MISMATCH = "current_state_mismatch"
    CONTRACT_OUTPUT_INVALID = "contract_output_invalid"


class PrepareBoundaryError(RuntimeError):
    """A bounded, safe failure raised at the prepare boundary."""

    def __init__(self, error_code: PrepareErrorCode, safe_message: str, *, retryable: bool = False) -> None:
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(safe_message)


class PreparedDecision(str, Enum):
    """The deterministic outcome of preparing a capacity operation."""

    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


@dataclass(frozen=True, slots=True, kw_only=True)
class CapacityPlaybook:
    """Trusted, code-selected playbook for the capacity-adjustment capability."""

    playbook_id: str
    playbook_version: str
    playbook_hash: str
    profile: str
    retry_policy: dict[str, Any]
    future_executor_binding: dict[str, str]

    def __post_init__(self) -> None:
        if self.profile != PROFILE:
            raise ValueError("playbook profile is not the capacity-adjustment profile")


@dataclass(frozen=True, slots=True, kw_only=True)
class PrepareRequestContext:
    """Trusted adapter context for preparing one operation."""

    requester: VerifiedPrincipal
    request_id: str
    correlation_id: str
    deployment_mode: str
    tenant_policy: str
    workspace_policy: str
    principal_authority: str
    capability_maximum: str
    operation_risk_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.requester, VerifiedPrincipal):
            raise ValueError("requester must be a VerifiedPrincipal")
        if not isinstance(self.request_id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.request_id):
            raise ValueError("request_id must be a valid operations identifier")
        if not isinstance(self.correlation_id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.correlation_id):
            raise ValueError("correlation_id must be a valid operations identifier")
        for name in (
            "deployment_mode",
            "tenant_policy",
            "workspace_policy",
            "principal_authority",
            "capability_maximum",
            "operation_risk_policy",
        ):
            if getattr(self, name) not in _AUTHORITY_ORDER:
                raise ValueError(f"{name} must be a valid authority value")

    def authority_inputs(self) -> dict[str, str]:
        return {
            "deployment_mode": self.deployment_mode,
            "tenant_policy": self.tenant_policy,
            "workspace_policy": self.workspace_policy,
            "principal_authority": self.principal_authority,
            "capability_maximum": self.capability_maximum,
            "operation_risk_policy": self.operation_risk_policy,
        }


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    """A prepared operation, its deterministic decision, and its bound hash."""

    decision: PreparedDecision
    operation: dict[str, Any]
    authorization: dict[str, Any]
    prepared_hash: str


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class PrepareService:
    """Prepare one immutable, idempotent capacity operation from advice."""

    def __init__(
        self,
        *,
        settings: OperationsSettings,
        identity_boundary: ApprovalIdentityBoundary,
        playbook: CapacityPlaybook,
        clock: Callable[[], datetime],
        operation_ttl_seconds: int = 900,
    ) -> None:
        self._settings = settings
        self._identity_boundary = identity_boundary
        self._playbook = playbook
        self._clock = clock
        if operation_ttl_seconds <= 0:
            raise ValueError("operation_ttl_seconds must be positive")
        self._operation_ttl = timedelta(seconds=operation_ttl_seconds)

    def prepare(
        self,
        advice: dict[str, Any],
        context: PrepareRequestContext,
        *,
        idempotency_token: str,
    ) -> PreparedOperation:
        """Deterministically prepare one immutable capacity operation."""
        requester_identity = self._authorize(context)
        self._require_valid_advice(advice)

        # The prepared operation is a pure function of (token, intent, trusted
        # current-state revision). Its timestamps are derived from the trusted
        # E1 observation revision anchor carried in the advice, never from the
        # live clock, so a retry of the same intent on a later clock reproduces
        # byte-for-byte identical bytes and prepared_hash. The live clock only
        # decides freshness: it may reject a stale or not-yet-valid anchor, but
        # it never alters the emitted document for an otherwise identical retry.
        observed_at = _parse_timestamp(advice["current_state"]["observed_at"])
        observation_expiry = _parse_timestamp(advice["current_state"]["expires_at"])
        now = _utc(self._clock(), "clock")
        if now < observed_at:
            raise PrepareBoundaryError(PrepareErrorCode.ADVICE_STALE, "advice is not yet valid")
        if now >= observation_expiry:
            raise PrepareBoundaryError(PrepareErrorCode.ADVICE_STALE, "advice current-state is no longer fresh")

        authority_inputs = context.authority_inputs()
        effective = effective_authority(authority_inputs)
        decision, reason_codes = self._decide(advice, authority_inputs, effective)

        # created_at is the trusted observation anchor. expires_at is derived
        # deterministically and is never later than the trusted observation
        # expiry nor the configured preparation TTL.
        created_at_dt = observed_at
        expires_at_dt = min(observation_expiry, created_at_dt + self._operation_ttl)
        created_at = _isoformat(created_at_dt)
        expires_at = _isoformat(expires_at_dt)
        operation_id = self._operation_id(advice, idempotency_token, requester_identity)

        operation: dict[str, Any] = {
            "operation_contract_version": CONTRACT_VERSION,
            "operation_id": operation_id,
            "idempotency_token": idempotency_token,
            "phase": PHASE,
            "profile": PROFILE,
            "action": ACTION,
            "provider": PROVIDER,
            "created_at": created_at,
            "expires_at": expires_at,
            "playbook": {
                "playbook_id": self._playbook.playbook_id,
                "playbook_version": self._playbook.playbook_version,
                "playbook_hash": self._playbook.playbook_hash,
            },
            "capability": {"capability_id": CAPABILITY_ID, "capability_version": CAPABILITY_VERSION},
            "requester": requester_identity,
            "correlation": {"correlation_id": context.correlation_id, "request_id": context.request_id},
            "recommendation": {"source_type": "automation", "source_id": advice["advice_id"]},
            "target": dict(advice["target"]),
            "parameters": {"requested": dict(advice["requested"]), "change": dict(advice["change"])},
            "current_state": {
                "observation_id": advice["current_state"]["observation_id"],
                "observation_hash": advice["current_state"]["observation_hash"],
                "capacity": dict(advice["current_state"]["capacity"]),
                "observed_at": advice["current_state"]["observed_at"],
                "expires_at": advice["current_state"]["expires_at"],
            },
            "resource_enrollment": dict(advice["bounds"]["enrollment"]),
            "policy": dict(advice["bounds"]["policy"]),
            "calculated_risk": {
                "level": advice["calculated_risk"]["level"],
                "score": advice["calculated_risk"]["score"],
                "factors": list(advice["calculated_risk"]["factors"]),
            },
            "authority": {
                "authority_inputs": authority_inputs,
                "effective_authority": effective,
                "decision": decision.value,
                "reason_codes": reason_codes,
            },
            "retry_policy": dict(self._playbook.retry_policy),
            "future_executor_binding": dict(self._playbook.future_executor_binding),
            # Immutable execution authority a future E3 executor independently
            # re-verifies (mode/policy >= remediate) before any write. Bound by
            # prepared_hash; the advise-authority E2 phase never elevates it.
            "required_execution_authority": REQUIRED_EXECUTION_AUTHORITY,
        }
        operation["prepared_hash"] = capacity_prepared_hash(operation)

        authorization = {
            "authorization_contract_version": CONTRACT_VERSION,
            "decision_id": f"authz.{operation_id}",
            "phase": PHASE,
            "prepared_operation_hash": operation["prepared_hash"],
            "principal": requester_identity,
            "authority_inputs": authority_inputs,
            "effective_authority": effective,
            "decision": decision.value,
            "reason_codes": reason_codes,
            "required_execution_authority": REQUIRED_EXECUTION_AUTHORITY,
            "policy_version": advice["bounds"]["policy"]["policy_version"],
            "evaluated_at": created_at,
            "expires_at": expires_at,
            "correlation": {"correlation_id": context.correlation_id, "request_id": context.request_id},
        }

        try:
            validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, operation)
            validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, authorization)
            validate_prepared_operation_binding(operation, authorization)
        except Exception as exc:  # Output is server-owned; a failure is our bug, surfaced safely.
            raise PrepareBoundaryError(
                PrepareErrorCode.CONTRACT_OUTPUT_INVALID, "prepared operation failed its contract"
            ) from exc

        return PreparedOperation(
            decision=decision,
            operation=operation,
            authorization=authorization,
            prepared_hash=operation["prepared_hash"],
        )

    # -- Internals --------------------------------------------------------

    def _authorize(self, context: PrepareRequestContext) -> dict[str, str]:
        if not self._settings.advise_enabled:
            raise PrepareBoundaryError(PrepareErrorCode.AUTHORIZATION_DENIED, "operations advise is not enabled")
        try:
            now = _utc(self._clock(), "clock")
            return self._identity_boundary.bind_requester(context.requester, now=now)
        except (IdentityBoundaryError, ValueError) as exc:
            raise PrepareBoundaryError(
                PrepareErrorCode.IDENTITY_CONTEXT_INVALID, "authenticated prepare identity is invalid"
            ) from exc

    def _require_valid_advice(self, advice: object) -> None:
        if not isinstance(advice, dict):
            raise PrepareBoundaryError(PrepareErrorCode.CONTRACT_INVALID, "advice is invalid")
        try:
            validate_capacity_contract("gamelift-capacity-advice", advice)
        except Exception as exc:
            raise PrepareBoundaryError(PrepareErrorCode.CONTRACT_INVALID, "advice is invalid") from exc
        if advice["capability"]["capability_id"] != CAPABILITY_ID:
            raise PrepareBoundaryError(PrepareErrorCode.CONTRACT_INVALID, "advice is invalid")
        if not _ADVICE_ID_PATTERN.fullmatch(advice["advice_id"]):
            raise PrepareBoundaryError(PrepareErrorCode.CONTRACT_INVALID, "advice is invalid")

    def _decide(
        self,
        advice: dict[str, Any],
        authority_inputs: dict[str, str],
        effective: str,
    ) -> tuple[PreparedDecision, list[str]]:
        reasons: set[str] = set()

        if authority_inputs["deployment_mode"] == "disabled":
            return PreparedDecision.DENIED, ["DEPLOYMENT_DISABLED"]

        if _AUTHORITY_ORDER[effective] < _AUTHORITY_ORDER[_MINIMUM_NON_DENIED_AUTHORITY]:
            reasons.add("INSUFFICIENT_AUTHORITY")
        if not advice["bounds"]["within_bounds"]:
            reasons.add("BOUNDS_EXCEEDED")
            if "TARGET_NOT_ENROLLED" in advice["bounds"]["violations"]:
                reasons.add("TARGET_NOT_ENROLLED")
        if advice["calculated_risk"]["level"] == "critical":
            reasons.add("RISK_LIMIT_EXCEEDED")

        if reasons:
            return PreparedDecision.DENIED, sorted(reasons)
        # E2 GameLift changes always require a direct approval; never authorized.
        return PreparedDecision.APPROVAL_REQUIRED, ["APPROVAL_REQUIRED"]

    def _operation_id(self, advice: dict[str, Any], idempotency_token: str, requester_identity: dict[str, str]) -> str:
        """Derive a deterministic operation id from the idempotent intent.

        The id is a function of the trusted workspace, the idempotency token, and
        the exact target and requested change, so a retry of the same intent
        reproduces the same operation id (and therefore the same prepared hash).
        """
        fingerprint = "\u001f".join(
            [
                requester_identity["workspace_id"],
                idempotency_token,
                advice["target"]["fleet_id"],
                advice["target"]["location"],
                str(advice["requested"]["desired"]),
                str(advice["requested"]["minimum"]),
                str(advice["requested"]["maximum"]),
                advice["current_state"]["observation_hash"],
            ]
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        # Map hex to the base36-ish [a-z0-9]{26} operation-id body.
        return f"op_{_hex_to_id_body(digest)}"


_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _hex_to_id_body(digest: str) -> str:
    value = int(digest, 16)
    body = []
    for _ in range(26):
        value, index = divmod(value, 36)
        body.append(_ID_ALPHABET[index])
    return "".join(reversed(body))
