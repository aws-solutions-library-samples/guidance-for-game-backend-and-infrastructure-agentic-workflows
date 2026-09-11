"""Policy and runtime enforcement for trusted operation approvals."""

from __future__ import annotations

# Standard library
import re
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

# Local modules
from operations.contracts import (
    CONTRACT_VERSION,
    ContractValidationError,
    canonical_sha256,
    validate_approval_binding,
    validate_prepared_operation,
)
from operations.identity import ApprovalIdentityBoundary, IdentityBoundaryError, VerifiedPrincipal

_OPERATION_ID_PATTERN = re.compile(r"^op_[a-z0-9]{26}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a valid operations identifier")
    return value


def _freeze_values(values: Iterable[str], field_name: str) -> frozenset[str]:
    if isinstance(values, str):
        raise ValueError(f"{field_name} must be a collection of strings")
    frozen = frozenset(values)
    if any(not isinstance(value, str) or not value or value != value.strip() or len(value) > 256 for value in frozen):
        raise ValueError(f"{field_name} contains an invalid value")
    return frozen


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
    return _utc(parsed, field_name)


def _format_timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _new_approval_id() -> str:
    return f"approval:{uuid4().hex}"


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalPolicyReason(str, Enum):
    """Stable internal policy outcomes; adapters expose only safe error codes."""

    POLICY_MISMATCH = "policy_mismatch"
    APPROVER_NOT_AUTHORIZED = "approver_not_authorized"
    SELF_APPROVAL_NOT_ALLOWED = "self_approval_not_allowed"
    SELF_APPROVAL_RISK_NOT_ELIGIBLE = "self_approval_risk_not_eligible"


class ApprovalPolicyError(PermissionError):
    """The current approval policy denied an otherwise valid request."""

    def __init__(self, reason: ApprovalPolicyReason) -> None:
        self.reason = reason
        super().__init__("approval policy denied the request")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalPolicy:
    """Versioned approver authorization and separation-of-duties policy."""

    policy_id: str
    policy_version: str
    approver_groups: frozenset[str] = field(default_factory=frozenset)
    approver_scopes: frozenset[str] = field(default_factory=frozenset)
    low_risk_self_approval_actions: frozenset[str] = field(default_factory=frozenset)
    approval_ttl: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _require_identifier(self.policy_id, "policy_id"))
        if (
            not isinstance(self.policy_version, str)
            or not self.policy_version
            or self.policy_version != self.policy_version.strip()
            or len(self.policy_version) > 128
        ):
            raise ValueError("policy_version must be a nonempty normalized string")
        object.__setattr__(self, "approver_groups", _freeze_values(self.approver_groups, "approver_groups"))
        object.__setattr__(self, "approver_scopes", _freeze_values(self.approver_scopes, "approver_scopes"))
        object.__setattr__(
            self,
            "low_risk_self_approval_actions",
            _freeze_values(self.low_risk_self_approval_actions, "low_risk_self_approval_actions"),
        )
        if not self.approver_groups and not self.approver_scopes:
            raise ValueError("approval policy must require at least one approver group or scope")
        if not isinstance(self.approval_ttl, timedelta) or not timedelta(0) < self.approval_ttl <= timedelta(days=1):
            raise ValueError("approval_ttl must be greater than zero and no more than one day")

    def authorize(self, prepared_operation: Mapping[str, Any], approver: VerifiedPrincipal) -> None:
        """Authorize an approver for one validated prepared operation."""
        binding = prepared_operation["policy"]
        if binding["policy_id"] != self.policy_id or binding["policy_version"] != self.policy_version:
            raise ApprovalPolicyError(ApprovalPolicyReason.POLICY_MISMATCH)

        has_group = bool(self.approver_groups.intersection(approver.groups))
        has_scope = bool(self.approver_scopes.intersection(approver.scopes))
        if not has_group and not has_scope:
            raise ApprovalPolicyError(ApprovalPolicyReason.APPROVER_NOT_AUTHORIZED)

        requester = prepared_operation["requester"]
        if requester["subject_id"] != approver.subject_id:
            return

        if prepared_operation["calculated_risk"]["level"] != "low":
            raise ApprovalPolicyError(ApprovalPolicyReason.SELF_APPROVAL_RISK_NOT_ELIGIBLE)
        if prepared_operation["action"] not in self.low_risk_self_approval_actions:
            raise ApprovalPolicyError(ApprovalPolicyReason.SELF_APPROVAL_NOT_ALLOWED)


class ApprovalErrorCode(str, Enum):
    """Approval service errors aligned with the operations application contract."""

    IDENTITY_CONTEXT_INVALID = "IDENTITY_CONTEXT_INVALID"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    OPERATION_HASH_MISMATCH = "OPERATION_HASH_MISMATCH"
    POLICY_STALE = "POLICY_STALE"
    STATE_CONFLICT = "STATE_CONFLICT"


class ApprovalBoundaryError(RuntimeError):
    """A safe, protocol-neutral failure returned by the approval service."""

    def __init__(self, error_code: ApprovalErrorCode, safe_message: str, *, retryable: bool = False) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        self.retryable = retryable
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Untrusted approval input. Identity and operation content are excluded."""

    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(self.operation_id):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "approval request is invalid")

    @classmethod
    def from_payload(cls, payload: object) -> ApprovalRequest:
        """Parse the complete untrusted action payload and reject identity injection."""
        if not isinstance(payload, dict) or set(payload) != {"operation_id"}:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "approval request is invalid")
        return cls(operation_id=payload["operation_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequestContext:
    """Trusted adapter context supplied separately from the action payload."""

    approver: VerifiedPrincipal
    request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.approver, VerifiedPrincipal):
            raise ValueError("approver must be a VerifiedPrincipal")
        object.__setattr__(self, "request_id", _require_identifier(self.request_id, "request_id"))


class StoredPreparedOperation:
    """Defensive snapshot returned by the durable operation store."""

    __slots__ = ("_prepared_operation", "prepared_operation_hash", "state")

    _prepared_operation: dict[str, Any]
    prepared_operation_hash: str
    state: str

    def __init__(self, prepared_operation: dict[str, Any], prepared_operation_hash: str, state: str) -> None:
        if not isinstance(prepared_operation, dict):
            raise ValueError("prepared_operation must be a JSON object")
        self._prepared_operation = deepcopy(prepared_operation)
        self.prepared_operation_hash = prepared_operation_hash
        self.state = state

    def copy_prepared_operation(self) -> dict[str, Any]:
        """Return a copy so policy evaluation cannot mutate the stored snapshot."""
        return deepcopy(self._prepared_operation)


class ApprovalCommitOutcome(str, Enum):
    """Authoritative result of the store's conditional approval transaction."""

    RECORDED = "recorded"
    PRECONDITION_FAILED = "precondition_failed"
    DEADLINE_EXPIRED = "deadline_expired"


class ApprovalStore(Protocol):
    """Persistence port; implementations must use a conditional atomic commit."""

    def load_for_approval(self, operation_id: str) -> StoredPreparedOperation | None:
        """Load the immutable operation, its stored hash, and current state."""
        ...

    def record_granted_approval(
        self,
        *,
        operation_id: str,
        expected_prepared_operation_hash: str,
        expected_state: str,
        commit_not_after: datetime,
        approval: Mapping[str, Any],
    ) -> ApprovalCommitOutcome:
        """Conditionally record before the deadline and return the authoritative outcome."""
        ...


class ApprovalService:
    """Grant approvals only from fresh authenticated identity and stored content."""

    def __init__(
        self,
        *,
        identity_boundary: ApprovalIdentityBoundary,
        policy: ApprovalPolicy,
        store: ApprovalStore,
        clock: Callable[[], datetime] = _system_clock,
        approval_id_factory: Callable[[], str] = _new_approval_id,
    ) -> None:
        self._identity_boundary = identity_boundary
        self._policy = policy
        self._store = store
        self._clock = clock
        self._approval_id_factory = approval_id_factory

    def grant(self, request: ApprovalRequest, context: ApprovalRequestContext) -> dict[str, Any]:
        """Validate, bind, and conditionally record one operation approval."""
        try:
            evaluated_at = _utc(self._clock(), "clock")
            self._identity_boundary.bind_approver(context.approver, now=evaluated_at)
        except (IdentityBoundaryError, ValueError) as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.IDENTITY_CONTEXT_INVALID,
                "authenticated approval identity is invalid",
            ) from exc

        stored = self._store.load_for_approval(request.operation_id)
        if stored is None:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_NOT_FOUND,
                "operation is unavailable for approval",
            )

        prepared_operation = stored.copy_prepared_operation()
        try:
            validate_prepared_operation(prepared_operation)
        except ContractValidationError as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.APPROVAL_INVALID,
                "stored operation is invalid",
            ) from exc

        if prepared_operation["operation_id"] != request.operation_id:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH,
                "stored operation integrity check failed",
            )

        prepared_operation_hash = canonical_sha256(prepared_operation)
        if stored.prepared_operation_hash != prepared_operation_hash:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH,
                "stored operation integrity check failed",
            )

        if stored.state != "pending_approval":
            raise ApprovalBoundaryError(
                ApprovalErrorCode.STATE_CONFLICT,
                "operation is not pending approval",
            )

        try:
            operation_expires_at = _parse_timestamp(prepared_operation["expires_at"], "operation expires_at")
        except ValueError as exc:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "stored operation is invalid") from exc
        if operation_expires_at <= evaluated_at:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.APPROVAL_EXPIRED,
                "operation is no longer eligible for approval",
            )

        try:
            self._identity_boundary.validate_stored_requester(prepared_operation["requester"])
        except IdentityBoundaryError as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.AUTHORIZATION_DENIED,
                "approval is not authorized for this operation",
            ) from exc

        try:
            self._policy.authorize(prepared_operation, context.approver)
        except ApprovalPolicyError as exc:
            error_code = (
                ApprovalErrorCode.POLICY_STALE
                if exc.reason == ApprovalPolicyReason.POLICY_MISMATCH
                else ApprovalErrorCode.AUTHORIZATION_DENIED
            )
            raise ApprovalBoundaryError(error_code, "approval is not authorized for this operation") from exc

        try:
            decided_at = _utc(self._clock(), "clock")
            if decided_at < evaluated_at:
                raise ValueError("approval clock moved backwards")
            approver_identity = self._identity_boundary.bind_approver(context.approver, now=decided_at)
        except (IdentityBoundaryError, ValueError) as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.IDENTITY_CONTEXT_INVALID,
                "authenticated approval identity expired before commit",
            ) from exc
        if operation_expires_at <= decided_at:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.APPROVAL_EXPIRED,
                "operation expired before approval could be recorded",
            )

        approval_expires_at = min(operation_expires_at, decided_at + self._policy.approval_ttl)
        commit_not_after = min(operation_expires_at, context.approver.expires_at, approval_expires_at)
        if approval_expires_at <= decided_at:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.APPROVAL_EXPIRED,
                "approval has no positive validity interval",
            )

        approval = {
            "approval_contract_version": CONTRACT_VERSION,
            "approval_id": self._approval_id_factory(),
            "operation_id": request.operation_id,
            "prepared_operation_hash": prepared_operation_hash,
            "approver": approver_identity,
            "decision": "granted",
            "policy_version": self._policy.policy_version,
            "decided_at": _format_timestamp(decided_at),
            "expires_at": _format_timestamp(approval_expires_at),
            "correlation": {
                "correlation_id": prepared_operation["correlation"]["correlation_id"],
                "request_id": context.request_id,
            },
        }

        try:
            validate_approval_binding(approval, prepared_operation)
        except (ContractValidationError, ValueError) as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.APPROVAL_INVALID,
                "approval record is invalid",
            ) from exc

        outcome = self._store.record_granted_approval(
            operation_id=request.operation_id,
            expected_prepared_operation_hash=prepared_operation_hash,
            expected_state="pending_approval",
            commit_not_after=commit_not_after,
            approval=deepcopy(approval),
        )
        if outcome is ApprovalCommitOutcome.DEADLINE_EXPIRED:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.APPROVAL_EXPIRED,
                "approval deadline elapsed before the record was committed",
            )
        if outcome is not ApprovalCommitOutcome.RECORDED:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.STATE_CONFLICT,
                "operation changed before approval could be recorded",
            )

        return deepcopy(approval)
