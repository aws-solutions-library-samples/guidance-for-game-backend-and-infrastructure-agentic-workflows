"""Protocol-neutral E2 capacity advice service (issue #414, E2 prepare).

``AdviceService`` turns one untrusted capacity proposal plus one trusted current
fleet-capacity observation into one deterministic
``gamelift-capacity-advice`` document. It is the read/reasoning half of the E2
prepare layer: it performs no provider write, holds no executor credential, and
selects the exact capability/playbook in code — never from model text.

Boundaries (consistent with ADR 0001/0002/0003 and the E1 ``ObservationService``
precedent):

* **Verifier-derived identity only.** Tenant, workspace, subject, and client
  come only from the trusted :class:`~operations.identity.VerifiedPrincipal`.
  The untrusted :class:`CapacityProposalRequest` carries capacity intent and an
  idempotency token, nothing else.
* **Trusted current state.** Current fleet capacity is loaded from an injected
  E1 observation/status port at advice time, never from the request. A stale,
  missing, or mismatched observation fails closed.
* **Server-owned bounds and deterministic risk.** Enrollment/policy bounds and
  the risk score are computed by trusted server code, not proposed by a caller.
* **No provider writes, no credentials.** The service only reads.
"""

from __future__ import annotations

# Standard library
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

# Local modules
from operations.contracts import CONTRACT_VERSION
from operations.contracts.capacity import (
    ADVICE_SCHEMA_NAME,
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    PHASE,
    PROVIDER,
    calculate_capacity_risk,
    capacity_bounds_violations,
    capacity_change,
    validate_capacity_contract,
)
from operations.identity import ApprovalIdentityBoundary, IdentityBoundaryError, VerifiedPrincipal
from operations.settings import OperationsSettings

_FLEET_ID_PATTERN = re.compile(r"^fleet-[a-f0-9-]{1,120}$")
_LOCATION_PATTERN = re.compile(r"^[a-z0-9-]+$")
_IDEMPOTENCY_PATTERN = re.compile(r"^idem_[A-Za-z0-9_-]{20,128}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_AUTHORITY_VALUES = frozenset({"disabled", "observe", "advise", "remediate", "operate"})


class AdviceErrorCode(str, Enum):
    """Stable, safe error codes for the advice boundary."""

    CONTRACT_INVALID = "contract_invalid"
    IDENTITY_CONTEXT_INVALID = "identity_context_invalid"
    AUTHORIZATION_DENIED = "authorization_denied"
    CURRENT_STATE_UNAVAILABLE = "current_state_unavailable"
    CURRENT_STATE_STALE = "current_state_stale"
    CURRENT_STATE_MISMATCH = "current_state_mismatch"
    TARGET_NOT_ENROLLED = "target_not_enrolled"
    CONTRACT_OUTPUT_INVALID = "contract_output_invalid"


class AdviceBoundaryError(RuntimeError):
    """A bounded, safe failure raised at the advice boundary."""

    def __init__(self, error_code: AdviceErrorCode, safe_message: str, *, retryable: bool = False) -> None:
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class CapacityValues:
    """Bounded desired/minimum/maximum capacity triple."""

    desired: int
    minimum: int
    maximum: int

    def as_dict(self) -> dict[str, int]:
        return {"desired": self.desired, "minimum": self.minimum, "maximum": self.maximum}


@dataclass(frozen=True, slots=True)
class CapacityProposalRequest:
    """Untrusted capacity proposal. Identity is excluded by construction."""

    fleet_id: str
    location: str
    requested: CapacityValues
    idempotency_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.fleet_id, str) or not _FLEET_ID_PATTERN.fullmatch(self.fleet_id):
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        if not isinstance(self.location, str) or not _LOCATION_PATTERN.fullmatch(self.location):
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        if not isinstance(self.idempotency_token, str) or not _IDEMPOTENCY_PATTERN.fullmatch(self.idempotency_token):
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        if not isinstance(self.requested, CapacityValues):
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        for name in ("desired", "minimum", "maximum"):
            value = getattr(self.requested, name)
            if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 1_000_000):
                raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")

    @classmethod
    def from_payload(cls, payload: object) -> "CapacityProposalRequest":
        """Parse untrusted proposal payload and reject any injected field.

        The accepted shape is exactly the ``gamelift-capacity-proposal-request``
        body: ``request_contract_version``, ``capability_id``,
        ``idempotency_token``, and ``proposal`` (``fleet_id``, ``location``,
        ``requested``). Any extra key — identity, policy, risk, executor,
        current-state, deployment mode — fails closed here.
        """
        if not isinstance(payload, dict):
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        if set(payload) != {"request_contract_version", "capability_id", "idempotency_token", "proposal"}:
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        if payload["request_contract_version"] != CONTRACT_VERSION:
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        if payload["capability_id"] != CAPABILITY_ID:
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        proposal = payload["proposal"]
        if not isinstance(proposal, dict) or set(proposal) != {"fleet_id", "location", "requested"}:
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        requested = proposal["requested"]
        if not isinstance(requested, dict) or set(requested) != {"desired", "minimum", "maximum"}:
            raise AdviceBoundaryError(AdviceErrorCode.CONTRACT_INVALID, "capacity proposal is invalid")
        return cls(
            fleet_id=proposal["fleet_id"],
            location=proposal["location"],
            requested=CapacityValues(
                desired=requested["desired"],
                minimum=requested["minimum"],
                maximum=requested["maximum"],
            ),
            idempotency_token=payload["idempotency_token"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapacityBounds:
    """Server-owned enrollment/policy bounds for one fleet/location.

    These are resolved by trusted server code (enrollment + policy), never
    supplied by the caller.
    """

    floor: int
    ceiling: int
    max_step: int
    enrollment_id: str
    enrollment_version: str
    policy_id: str
    policy_version: str
    target_enrolled: bool

    def limits(self) -> dict[str, int]:
        return {"floor": self.floor, "ceiling": self.ceiling, "max_step": self.max_step}


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentCapacity:
    """Trusted current fleet capacity loaded from the E1 observation port.

    ``observed_at`` and ``expires_at`` are the trusted E1 observation revision's
    own timestamps. They are the deterministic time anchor for advice and the
    prepared operation: the emitted ``created_at``/``advised_at`` are derived
    from ``observed_at`` (never the live clock), and ``expires_at`` bounds how
    long a prepared operation built on this revision may live. The live clock is
    used only to decide whether the revision is still fresh.
    """

    observation_id: str
    observation_hash: str
    capacity: CapacityValues
    observed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        observed = _utc(self.observed_at, "observed_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= observed:
            raise ValueError("observation expires_at must be strictly after observed_at")


class CapacityStatePort(Protocol):
    """Injected, trusted E1 observation/status port for current fleet capacity.

    The implementer owns the E1 :class:`ObservationService`/store and returns a
    bounded, verified current-capacity reading for one fleet/location, or
    ``None`` when no fresh, verified observation exists. It exposes no write.
    """

    def load_current_capacity(
        self, *, requester: VerifiedPrincipal, fleet_id: str, location: str
    ) -> CurrentCapacity | None:
        """Return the trusted current capacity, or ``None`` if unavailable."""
        ...


class CapacityBoundsPort(Protocol):
    """Injected, trusted server-owned enrollment/policy bounds resolver."""

    def resolve_bounds(self, *, requester: VerifiedPrincipal, fleet_id: str, location: str) -> CapacityBounds | None:
        """Return server-owned bounds, or ``None`` when the target is unknown."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityCeilings:
    """The five ADR 0001 request-side authority ceilings. Deployment mode is
    supplied by trusted settings and combined at evaluation time."""

    tenant_policy: str
    workspace_policy: str
    principal_authority: str
    capability_maximum: str
    operation_risk_policy: str

    def __post_init__(self) -> None:
        for name in (
            "tenant_policy",
            "workspace_policy",
            "principal_authority",
            "capability_maximum",
            "operation_risk_policy",
        ):
            if getattr(self, name) not in _AUTHORITY_VALUES:
                raise ValueError(f"{name} must be a valid authority value")


@dataclass(frozen=True, slots=True, kw_only=True)
class AdviceRequestContext:
    """Trusted adapter context supplied separately from the proposal payload."""

    requester: VerifiedPrincipal
    request_id: str
    correlation_id: str
    authority_ceilings: AuthorityCeilings

    def __post_init__(self) -> None:
        if not isinstance(self.requester, VerifiedPrincipal):
            raise ValueError("requester must be a VerifiedPrincipal")
        if not isinstance(self.request_id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.request_id):
            raise ValueError("request_id must be a valid operations identifier")
        if not isinstance(self.correlation_id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.correlation_id):
            raise ValueError("correlation_id must be a valid operations identifier")
        if not isinstance(self.authority_ceilings, AuthorityCeilings):
            raise ValueError("authority_ceilings must be an AuthorityCeilings value")


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AdviceService:
    """Deterministic capacity advice from one proposal and trusted state."""

    def __init__(
        self,
        *,
        settings: OperationsSettings,
        identity_boundary: ApprovalIdentityBoundary,
        state_port: CapacityStatePort,
        bounds_port: CapacityBoundsPort,
        clock: Callable[[], datetime],
        advice_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._identity_boundary = identity_boundary
        self._state_port = state_port
        self._bounds_port = bounds_port
        self._clock = clock
        self._advice_id_factory = advice_id_factory or _default_advice_id

    def advise(self, request: CapacityProposalRequest, context: AdviceRequestContext) -> dict[str, Any]:
        """Produce one deterministic, validated capacity advice document."""
        requester_identity = self._authorize(context)

        current = self._state_port.load_current_capacity(
            requester=context.requester, fleet_id=request.fleet_id, location=request.location
        )
        if current is None:
            raise AdviceBoundaryError(
                AdviceErrorCode.CURRENT_STATE_UNAVAILABLE,
                "no fresh current-capacity observation is available",
                retryable=True,
            )

        # The live clock only decides freshness of the trusted current-state
        # revision; it never contributes to the emitted document bytes. A
        # revision at or past its own expiry is stale and fails closed.
        observed_at = _utc(current.observed_at, "observed_at")
        observation_expiry = _utc(current.expires_at, "expires_at")
        now = _utc(self._clock(), "clock")
        if now >= observation_expiry:
            raise AdviceBoundaryError(
                AdviceErrorCode.CURRENT_STATE_STALE,
                "current-capacity observation is no longer fresh",
                retryable=True,
            )

        bounds = self._bounds_port.resolve_bounds(
            requester=context.requester, fleet_id=request.fleet_id, location=request.location
        )
        if bounds is None:
            raise AdviceBoundaryError(AdviceErrorCode.TARGET_NOT_ENROLLED, "target fleet/location is not enrolled")

        requested = request.requested.as_dict()
        current_capacity = current.capacity.as_dict()
        change = capacity_change(current_capacity, requested)
        violations = capacity_bounds_violations(
            requested=requested,
            change=change,
            limits=bounds.limits(),
            target_enrolled=bounds.target_enrolled,
        )
        risk = calculate_capacity_risk(current=current_capacity, requested=requested, within_bounds=not violations)

        # Advice is a pure function of (proposal, trusted observation revision):
        # its timestamp is the observation anchor, so recomputing it on a later
        # clock is byte-for-byte identical.
        advised_at = _isoformat(observed_at)
        advice = {
            "advice_contract_version": CONTRACT_VERSION,
            "advice_id": self._advice_id_factory(),
            "phase": PHASE,
            "provider": PROVIDER,
            "capability": {"capability_id": CAPABILITY_ID, "capability_version": CAPABILITY_VERSION},
            "target": {"provider": PROVIDER, "fleet_id": request.fleet_id, "location": request.location},
            "current_state": {
                "observation_id": current.observation_id,
                "observation_hash": current.observation_hash,
                "capacity": current_capacity,
                "observed_at": _isoformat(observed_at),
                "expires_at": _isoformat(observation_expiry),
            },
            "requested": requested,
            "change": change,
            "bounds": {
                "within_bounds": not violations,
                "enrollment": {
                    "enrollment_id": bounds.enrollment_id,
                    "enrollment_version": bounds.enrollment_version,
                },
                "policy": {"policy_id": bounds.policy_id, "policy_version": bounds.policy_version},
                "violations": violations,
            },
            "calculated_risk": risk,
            "advised_at": advised_at,
        }
        try:
            validate_capacity_contract(ADVICE_SCHEMA_NAME, advice)
        except Exception as exc:  # Output is server-owned; a failure is our bug, surfaced safely.
            raise AdviceBoundaryError(
                AdviceErrorCode.CONTRACT_OUTPUT_INVALID, "computed advice failed its contract"
            ) from exc
        return advice

    def _authorize(self, context: AdviceRequestContext) -> dict[str, str]:
        if not self._settings.advise_enabled:
            raise AdviceBoundaryError(AdviceErrorCode.AUTHORIZATION_DENIED, "operations advise is not enabled")
        try:
            evaluated_at = _utc(self._clock(), "clock")
            return self._identity_boundary.bind_requester(context.requester, now=evaluated_at)
        except (IdentityBoundaryError, ValueError) as exc:
            raise AdviceBoundaryError(
                AdviceErrorCode.IDENTITY_CONTEXT_INVALID, "authenticated advice identity is invalid"
            ) from exc


def _default_advice_id() -> str:
    return f"adv_{uuid4().hex[:26]}"
