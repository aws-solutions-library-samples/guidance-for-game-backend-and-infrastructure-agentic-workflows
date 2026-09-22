"""Reject / cancel / expiry lifecycle decisions for prepared operations (#414).

The E2 approval domain has three terminal lifecycle decisions beyond the grant
path implemented in :mod:`operations.approval`:

* **reject** — an authorized approver (never the requester, unless an explicit
  server-owned low-risk self-approval policy allows it) records a ``denied``
  approval and transitions ``pending_approval -> rejected``;
* **cancel** — the requester of the operation, or an authorized approver, may
  cancel a not-yet-dispatched operation (``prepared`` / ``pending_approval`` /
  ``approved``), transitioning it to ``cancelled``; and
* **expire** — a due operation (past its ``expires_at``) transitions to
  ``expired``. Expiry is driven by the trusted system clock, not by a caller.

Every decision:

* takes an untrusted :class:`DecisionRequest` that carries **no** executable or
  identity content — only the ``operation_id`` and an optional expected
  ``prepared_hash`` the caller believes it is acting on;
* takes a trusted :class:`DecisionRequestContext` whose principal is a
  :class:`~operations.identity.VerifiedPrincipal` produced by the adapter after
  cryptographic verification;
* binds the exact stored ``prepared_hash`` and validated stored operation;
* builds an ``operation-state-change`` and an ``approval.recorded`` /
  ``operation.state-changed`` ``ledger-event`` from server-owned data; and
* commits through a fenced, conditional, atomic store transaction that fails
  closed under a race (a lost conditional check is a ``STATE_CONFLICT``, never a
  silent success).

The service performs no provider write and holds no executor credential.
"""

from __future__ import annotations

# Standard library
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

# Local modules
from operations.approval import (
    ApprovalBoundaryError,
    ApprovalErrorCode,
    ApprovalPolicy,
    ApprovalPolicyError,
    ApprovalPolicyReason,
    StoredPreparedOperation,
)
from operations.contracts import (
    CONTRACT_VERSION,
    ContractValidationError,
    validate_contract,
)
from operations.contracts.capacity import (
    PREPARED_OPERATION_SCHEMA_NAME,
    CapacityContractError,
    capacity_prepared_hash,
    validate_capacity_contract,
)
from operations.identity import ApprovalIdentityBoundary, IdentityBoundaryError, VerifiedPrincipal

_OPERATION_ID_PATTERN = re.compile(r"^op_[a-z0-9]{26}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")

# Pre-dispatch, non-terminal states a cancel may act on.
_CANCELLABLE_STATES = frozenset({"prepared", "pending_approval", "approved"})

# An expiry sweep is not bound to a caller credential; the operation's own
# expiry has already elapsed by the time expire runs, so there is no separate
# commit deadline to enforce beyond the store's fenced conditional transition.
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}.{uuid4().hex}"


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a valid operations identifier")
    return value


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


class DecisionCommitOutcome(str, Enum):
    """Authoritative result of the store's conditional terminal transaction."""

    RECORDED = "recorded"
    PRECONDITION_FAILED = "precondition_failed"
    DEADLINE_EXPIRED = "deadline_expired"


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Untrusted decision input. Carries no identity or executable content."""

    operation_id: str
    expected_prepared_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(self.operation_id):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "decision request is invalid")
        if self.expected_prepared_hash is not None and (
            not isinstance(self.expected_prepared_hash, str)
            or not _DIGEST_PATTERN.fullmatch(self.expected_prepared_hash)
        ):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "decision request is invalid")

    @classmethod
    def from_payload(cls, payload: object) -> DecisionRequest:
        """Parse the complete untrusted action body and reject any injection."""
        if not isinstance(payload, dict):
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "decision request is invalid")
        allowed = {"operation_id", "expected_prepared_hash"}
        if not set(payload).issubset(allowed) or "operation_id" not in payload:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "decision request is invalid")
        return cls(
            operation_id=payload["operation_id"],
            expected_prepared_hash=payload.get("expected_prepared_hash"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionRequestContext:
    """Trusted adapter context supplied separately from the action payload."""

    principal: VerifiedPrincipal
    request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.principal, VerifiedPrincipal):
            raise ValueError("principal must be a VerifiedPrincipal")
        object.__setattr__(self, "request_id", _require_identifier(self.request_id, "request_id"))


class DecisionStore(Protocol):
    """Persistence port; implementations use a conditional atomic commit."""

    def load_for_decision(self, operation_id: str) -> StoredPreparedOperation | None:
        """Load the immutable operation, its stored hash, and current state."""
        ...

    def record_terminal_decision(
        self,
        *,
        operation_id: str,
        expected_prepared_operation_hash: str,
        expected_state: str,
        new_state: str,
        commit_not_after: datetime,
        state_change: Mapping[str, Any],
        ledger_event: Mapping[str, Any],
        approval: Mapping[str, Any] | None,
        workspace_id: str | None = None,
    ) -> DecisionCommitOutcome:
        """Conditionally record the terminal transition, fenced and atomic."""
        ...


class LifecycleDecisionService:
    """Reject, cancel, or expire one prepared operation, fenced and atomic."""

    def __init__(
        self,
        *,
        identity_boundary: ApprovalIdentityBoundary,
        policy: ApprovalPolicy,
        store: DecisionStore,
        clock: Callable[[], datetime] = _system_clock,
        decision_id_factory: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        state_change_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._identity_boundary = identity_boundary
        self._policy = policy
        self._store = store
        self._clock = clock
        self._decision_id_factory = decision_id_factory or (lambda: _new_id("approval"))
        self._event_id_factory = event_id_factory or (lambda: _new_id("event"))
        self._state_change_id_factory = state_change_id_factory or (lambda: _new_id("state"))

    # -- Public decisions -------------------------------------------------

    def reject(self, request: DecisionRequest, context: DecisionRequestContext) -> dict[str, Any]:
        """An authorized approver denies one pending operation."""
        principal_identity, prepared_operation, prepared_hash, stored_state, evaluated_at = self._load_authenticated(
            request, context
        )
        if prepared_operation["operation_id"] != request.operation_id:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH, "stored operation integrity check failed"
            )
        stored_state = self._require_state(stored_state, frozenset({"pending_approval"}))
        self._authorize_approver(prepared_operation, context.principal)

        approval = self._build_approval_record(
            request.operation_id, prepared_hash, principal_identity, prepared_operation, evaluated_at, "denied"
        )
        state_change = self._build_state_change(
            request.operation_id,
            prepared_hash,
            prepared_operation,
            previous_state=stored_state,
            new_state="rejected",
            reason_code="APPROVAL_DENIED",
            actor=_principal_actor(principal_identity),
            changed_at=evaluated_at,
        )
        ledger_event = self._build_ledger_event(
            request.operation_id,
            prepared_hash,
            prepared_operation,
            event_type="approval.recorded",
            payload={
                "payload_type": "approval_recorded",
                "approval_id": approval["approval_id"],
                "decision": "denied",
            },
            actor=_principal_actor(principal_identity),
            occurred_at=evaluated_at,
        )
        self._commit(
            request,
            stored_state,
            "rejected",
            prepared_hash,
            state_change,
            ledger_event,
            approval,
            commit_not_after=min(context.principal.expires_at, self._operation_expiry(prepared_operation)),
            workspace_id=_operation_workspace_id(prepared_operation),
        )
        return dict(approval)

    def cancel(self, request: DecisionRequest, context: DecisionRequestContext) -> dict[str, Any]:
        """The requester or an authorized approver cancels a pre-dispatch op."""
        principal_identity, prepared_operation, prepared_hash, stored_state, evaluated_at = self._load_authenticated(
            request, context
        )
        if prepared_operation["operation_id"] != request.operation_id:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH, "stored operation integrity check failed"
            )
        stored_state = self._require_state(stored_state, _CANCELLABLE_STATES)
        self._authorize_cancel(prepared_operation, context.principal)

        state_change = self._build_state_change(
            request.operation_id,
            prepared_hash,
            prepared_operation,
            previous_state=stored_state,
            new_state="cancelled",
            reason_code="CANCELLED",
            actor=_principal_actor(principal_identity),
            changed_at=evaluated_at,
        )
        ledger_event = self._build_ledger_event(
            request.operation_id,
            prepared_hash,
            prepared_operation,
            event_type="operation.state-changed",
            payload={
                "payload_type": "state_changed",
                "state_change_id": state_change["state_change_id"],
                "previous_state": stored_state,
                "new_state": "cancelled",
            },
            actor=_principal_actor(principal_identity),
            occurred_at=evaluated_at,
        )
        self._commit(
            request,
            stored_state,
            "cancelled",
            prepared_hash,
            state_change,
            ledger_event,
            None,
            commit_not_after=min(context.principal.expires_at, self._operation_expiry(prepared_operation)),
            workspace_id=_operation_workspace_id(prepared_operation),
        )
        return dict(state_change)

    def expire_if_due(self, request: DecisionRequest) -> dict[str, Any] | None:
        """Transition a due operation to ``expired``; a no-op if not yet due."""
        now = _utc(self._clock(), "clock")
        prepared_operation, prepared_hash, stored_state = self._load_and_validate(request)
        if prepared_operation["operation_id"] != request.operation_id:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH, "stored operation integrity check failed"
            )
        stored_state = self._require_state(stored_state, _CANCELLABLE_STATES)
        if now < self._operation_expiry(prepared_operation):
            return None

        actor = {"actor_type": "system", "actor_id": "operations.expiry-sweeper"}
        state_change = self._build_state_change(
            request.operation_id,
            prepared_hash,
            prepared_operation,
            previous_state=stored_state,
            new_state="expired",
            reason_code="EXPIRED",
            actor=actor,
            changed_at=now,
        )
        ledger_event = self._build_ledger_event(
            request.operation_id,
            prepared_hash,
            prepared_operation,
            event_type="operation.state-changed",
            payload={
                "payload_type": "state_changed",
                "state_change_id": state_change["state_change_id"],
                "previous_state": stored_state,
                "new_state": "expired",
            },
            actor=actor,
            occurred_at=now,
        )
        # An expiry sweep is not bound to a caller credential; the operation's
        # own expiry is the only relevant deadline and it has already elapsed,
        # so the store enforces the fenced conditional transition directly.
        self._commit(
            request,
            stored_state,
            "expired",
            prepared_hash,
            state_change,
            ledger_event,
            None,
            commit_not_after=_FAR_FUTURE,
            workspace_id=_operation_workspace_id(prepared_operation),
        )
        return dict(state_change)

    # -- Internals --------------------------------------------------------

    def _load_authenticated(
        self, request: DecisionRequest, context: DecisionRequestContext
    ) -> tuple[dict[str, str], dict[str, Any], str, str, datetime]:
        try:
            evaluated_at = _utc(self._clock(), "clock")
            principal_identity = self._identity_boundary.bind_approver(context.principal, now=evaluated_at)
        except (IdentityBoundaryError, ValueError) as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.IDENTITY_CONTEXT_INVALID, "authenticated decision identity is invalid"
            ) from exc
        prepared_operation, prepared_hash, stored_state = self._load_and_validate(request)
        try:
            self._identity_boundary.validate_stored_requester(prepared_operation["requester"])
        except IdentityBoundaryError as exc:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.AUTHORIZATION_DENIED, "decision is not authorized for this operation"
            ) from exc
        return principal_identity, prepared_operation, prepared_hash, stored_state, evaluated_at

    def _load_and_validate(self, request: DecisionRequest) -> tuple[dict[str, Any], str, str]:
        stored = self._store.load_for_decision(request.operation_id)
        if stored is None:
            raise ApprovalBoundaryError(ApprovalErrorCode.OPERATION_NOT_FOUND, "operation is unavailable for decision")
        prepared_operation = stored.copy_prepared_operation()
        try:
            validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, prepared_operation)
        except (CapacityContractError, ContractValidationError) as exc:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "stored operation is invalid") from exc

        # The capacity operation carries its own deterministic ``prepared_hash``
        # binding every field except itself; recompute it and require an exact
        # match against both the carried field and the store's bound hash.
        prepared_hash = capacity_prepared_hash(prepared_operation)
        if prepared_operation.get("prepared_hash") != prepared_hash:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH, "stored operation integrity check failed"
            )
        if stored.prepared_operation_hash != prepared_hash:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH, "stored operation integrity check failed"
            )
        if request.expected_prepared_hash is not None and request.expected_prepared_hash != prepared_hash:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.OPERATION_HASH_MISMATCH, "operation changed since it was presented"
            )
        return prepared_operation, prepared_hash, stored.state

    @staticmethod
    def _require_state(state: str, allowed: frozenset[str]) -> str:
        if state not in allowed:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.STATE_CONFLICT, "operation is not in a state that admits this decision"
            )
        return state

    def _authorize_approver(self, prepared_operation: Mapping[str, Any], principal: VerifiedPrincipal) -> None:
        try:
            self._policy.authorize(prepared_operation, principal)
        except ApprovalPolicyError as exc:
            code = (
                ApprovalErrorCode.POLICY_STALE
                if exc.reason == ApprovalPolicyReason.POLICY_MISMATCH
                else ApprovalErrorCode.AUTHORIZATION_DENIED
            )
            raise ApprovalBoundaryError(code, "decision is not authorized for this operation") from exc

    def _authorize_cancel(self, prepared_operation: Mapping[str, Any], principal: VerifiedPrincipal) -> None:
        # The requester of the operation may always cancel their own operation.
        if prepared_operation["requester"]["subject_id"] == principal.subject_id:
            return
        # Otherwise the principal must satisfy the approver authorization policy.
        try:
            self._policy.authorize(prepared_operation, principal)
        except ApprovalPolicyError as exc:
            code = (
                ApprovalErrorCode.POLICY_STALE
                if exc.reason == ApprovalPolicyReason.POLICY_MISMATCH
                else ApprovalErrorCode.AUTHORIZATION_DENIED
            )
            raise ApprovalBoundaryError(code, "decision is not authorized for this operation") from exc

    def _operation_expiry(self, prepared_operation: Mapping[str, Any]) -> datetime:
        try:
            return _parse_timestamp(prepared_operation["expires_at"], "operation expires_at")
        except ValueError as exc:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "stored operation is invalid") from exc

    def _build_approval_record(
        self,
        operation_id: str,
        prepared_hash: str,
        principal_identity: Mapping[str, str],
        prepared_operation: Mapping[str, Any],
        decided_at: datetime,
        decision: str,
    ) -> dict[str, Any]:
        approval = {
            "approval_contract_version": CONTRACT_VERSION,
            "approval_id": self._decision_id_factory(),
            "operation_id": operation_id,
            "prepared_operation_hash": prepared_hash,
            "approver": dict(principal_identity),
            "decision": decision,
            "policy_version": self._policy.policy_version,
            "decided_at": _format_timestamp(decided_at),
            "expires_at": _format_timestamp(decided_at),
            "correlation": {
                "correlation_id": prepared_operation["correlation"]["correlation_id"],
                "request_id": prepared_operation["correlation"]["request_id"],
            },
        }
        try:
            validate_contract("approval-record", approval)
        except ContractValidationError as exc:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "approval record is invalid") from exc
        return approval

    def _build_state_change(
        self,
        operation_id: str,
        prepared_hash: str,
        prepared_operation: Mapping[str, Any],
        *,
        previous_state: str,
        new_state: str,
        reason_code: str,
        actor: Mapping[str, str],
        changed_at: datetime,
    ) -> dict[str, Any]:
        state_change = {
            "state_contract_version": CONTRACT_VERSION,
            "state_change_id": self._state_change_id_factory(),
            "operation_id": operation_id,
            "prepared_operation_hash": prepared_hash,
            "previous_state": previous_state,
            "new_state": new_state,
            "attempt": 0,
            "reason_code": reason_code,
            "changed_at": _format_timestamp(changed_at),
            "actor": dict(actor),
            "correlation": {
                "correlation_id": prepared_operation["correlation"]["correlation_id"],
                "request_id": prepared_operation["correlation"]["request_id"],
            },
        }
        try:
            validate_contract("operation-state-change", state_change)
        except ContractValidationError as exc:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "state change is invalid") from exc
        return state_change

    def _build_ledger_event(
        self,
        operation_id: str,
        prepared_hash: str,
        prepared_operation: Mapping[str, Any],
        *,
        event_type: str,
        payload: Mapping[str, Any],
        actor: Mapping[str, str],
        occurred_at: datetime,
    ) -> dict[str, Any]:
        ledger_event = {
            "ledger_contract_version": CONTRACT_VERSION,
            "event_id": self._event_id_factory(),
            "event_type": event_type,
            "operation_id": operation_id,
            "prepared_operation_hash": prepared_hash,
            "sequence": 1,
            "occurred_at": _format_timestamp(occurred_at),
            "actor": dict(actor),
            "correlation": {
                "correlation_id": prepared_operation["correlation"]["correlation_id"],
                "request_id": prepared_operation["correlation"]["request_id"],
            },
            "payload": dict(payload),
        }
        try:
            validate_contract("ledger-event", ledger_event)
        except ContractValidationError as exc:
            raise ApprovalBoundaryError(ApprovalErrorCode.APPROVAL_INVALID, "ledger event is invalid") from exc
        return ledger_event

    def _commit(
        self,
        request: DecisionRequest,
        expected_state: str,
        new_state: str,
        prepared_hash: str,
        state_change: Mapping[str, Any],
        ledger_event: Mapping[str, Any],
        approval: Mapping[str, Any] | None,
        *,
        commit_not_after: datetime,
        workspace_id: str | None = None,
    ) -> None:
        outcome = self._store.record_terminal_decision(
            operation_id=request.operation_id,
            expected_prepared_operation_hash=prepared_hash,
            expected_state=expected_state,
            new_state=new_state,
            commit_not_after=_utc(commit_not_after, "commit_not_after"),
            state_change=dict(state_change),
            ledger_event=dict(ledger_event),
            approval=dict(approval) if approval is not None else None,
            workspace_id=workspace_id,
        )
        if outcome is DecisionCommitOutcome.DEADLINE_EXPIRED:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.APPROVAL_EXPIRED, "decision deadline elapsed before the record was committed"
            )
        if outcome is not DecisionCommitOutcome.RECORDED:
            raise ApprovalBoundaryError(
                ApprovalErrorCode.STATE_CONFLICT, "operation changed before the decision could be recorded"
            )


def _operation_workspace_id(operation: Mapping[str, Any]) -> str | None:
    """Return the operation's requester workspace id, or None if absent."""
    requester = operation.get("requester")
    if isinstance(requester, Mapping):
        workspace_id = requester.get("workspace_id")
        if isinstance(workspace_id, str) and workspace_id:
            return workspace_id
    return None


def _principal_actor(principal_identity: Mapping[str, str]) -> dict[str, str]:
    return {
        "actor_type": "principal",
        "actor_id": principal_identity["subject_id"],
        "client_id": principal_identity["client_id"],
    }
