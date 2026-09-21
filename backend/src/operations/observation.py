"""Read-only GameLift observation application service (issue #413, E1 Agent A).

This protocol-neutral service performs the exact E1 observation described in
ADR 0005 ("Persist operations state and recover workflows"): three bounded,
read-only GameLift reads for one fleet — utilization, capacity, and scaling
policies — durably bracketed by conditional/idempotent DynamoDB state and an
append-only ledger, all inside one request under explicit wall-clock deadlines.

Lifecycle (ADR 0005, "create before reads"):

1. **Resolve/create idempotency before any provider read.** The canonical
   idempotency fingerprint is a SHA-256 canonical hash over the trusted
   workspace, the idempotency token, and the *request intent* — the capability,
   provider, phase, and target. It is computed from the request and never from
   the observation result, observation id, or a timestamp, so it is stable
   across retries. ``begin_observation`` atomically creates the idempotency
   mapping (carrying the fingerprint), the initial ``observing`` state snapshot,
   the immutable ``STATE#0`` transition, and the initial ledger event — all or
   nothing — *before* any read runs.
2. **A matching completed retry replays.** If the mapping already exists with a
   matching fingerprint and the operation reached a terminal ``succeeded``
   state, the stored bounded observation is returned verbatim, its canonical
   hash re-verified, without any new provider read.
3. **A changed intent under the same token conflicts.** A stored fingerprint
   that differs fails with ``IDEMPOTENCY_CONFLICT`` and mutates nothing.
4. **An in-progress retry is safe, and a stale lease is recovered.** A
   non-terminal existing operation whose single-flight lease is still live
   returns a bounded, retryable in-progress state — it never creates a second
   operation and makes no recovery claim. Once that lease has *expired*, the
   store conditionally advances the fencing generation and lease, appends a
   recovery ledger transition, and reports the operation as reclaimed; the
   service then safely reruns the read-only observation under the reclaimed
   lease. A *terminally failed* operation is distinct: it replays its stored
   bounded failure deterministically and is not retryable under the same token —
   a new observation requires a new idempotency token.
5. **Execute reads, then finalize.** After a fresh create, the three reads run
   under the E0-validated deadlines; on success the bounded, validated,
   canonicalized, hashed observation is persisted while the state transitions
   ``observing`` -> ``succeeded`` and a matching ledger event appends, atomically.
   A read failure or timeout records a bounded ``failed`` transition where
   possible and raises a typed error.

Boundaries enforced here, consistent with ADR 0001/0002/0003 and the
``ApprovalService`` precedent:

* **Verifier-derived identity only.** Tenant, workspace, subject, and client
  come only from the trusted :class:`~operations.identity.VerifiedPrincipal`,
  never from the untrusted :class:`ObservationRequest` (fleet id and idempotency
  token only).
* **Explicit ceilings.** The six ADR 0001 authority inputs are recorded and the
  deterministic minimum is the effective authority. The five caller/deployment
  inputs are recorded exactly as supplied — a higher deployment or principal
  ceiling is never rewritten — while this capability's own maximum is the
  explicit ``observe`` phase ceiling. The effective authority is therefore the
  real ``min(inputs)``, which for this phase can never exceed ``observe``.
* **No provider writes.** Only injected read callables are invoked.
* **Fail closed, no partial success.** Any deadline overrun, empty read,
  malformed or oversized shape, replay/idempotency conflict, or state conflict
  raises a typed :class:`ObservationBoundaryError`.
* **Bounded, sanitized output.** The observation validates against the additive
  ``gamelift-observation`` contract, is canonicalized (RFC 8785) and hashed, and
  only bounded, public-safe metrics are emitted.
"""

from __future__ import annotations

# Standard library
import concurrent.futures
import re
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

# Local modules
from operations.contracts import CONTRACT_VERSION, canonical_sha256
from operations.contracts.observation import ObservationContractError, validate_observation
from operations.identity import ApprovalIdentityBoundary, IdentityBoundaryError, VerifiedPrincipal
from operations.settings import OperationsSettings
from operations.validation.e0_latency import (
    DeadlineExceededError,
    LatencyBudget,
    PartialObservationError,
    ProviderRead,
)

_FLEET_ID_PATTERN = re.compile(r"^fleet-[a-f0-9-]{1,120}$")
_IDEMPOTENCY_PATTERN = re.compile(r"^idem_[A-Za-z0-9_-]{20,128}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_OPERATION_ID_PATTERN = re.compile(r"^obs_[a-z0-9]{26}$")

_OBSERVE_AUTHORITY = "observe"
_DISABLED_AUTHORITY = "disabled"
_AUTHORITY_ORDER = {
    "disabled": 0,
    "observe": 1,
    "advise": 2,
    "remediate": 3,
    "operate": 4,
}

# Bounds mirrored from the observation schema so the service fails closed before
# canonicalization on an oversized provider result.
_MAX_CAPACITY_LOCATIONS = 64
_MAX_SCALING_POLICIES = 50

_SCALING_POLICY_STATUSES = frozenset(
    {"ACTIVE", "UPDATE_REQUESTED", "UPDATING", "DELETE_REQUESTED", "DELETING", "DELETED", "ERROR"}
)

# ADR 0005 observation states used by the two-phase lifecycle.
STATE_OBSERVING = "observing"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
_TERMINAL_STATES = frozenset({STATE_SUCCEEDED, STATE_FAILED})


def _cap_at_observe(authority: str) -> str:
    """Return ``authority`` capped at the observe-phase ceiling."""
    return min((authority, _OBSERVE_AUTHORITY), key=_AUTHORITY_ORDER.__getitem__)


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_operation_id() -> str:
    # 26 lowercase base32-ish characters, matching ^obs_[a-z0-9]{26}$.
    return "obs_" + uuid4().hex[:26]


class ObservationErrorCode(str, Enum):
    """Observation service errors aligned with the operations application-error set."""

    IDENTITY_CONTEXT_INVALID = "IDENTITY_CONTEXT_INVALID"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    CONTRACT_INVALID = "CONTRACT_INVALID"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    STATE_CONFLICT = "STATE_CONFLICT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ObservationBoundaryError(RuntimeError):
    """A safe, protocol-neutral fail-closed failure returned by the service."""

    def __init__(self, error_code: ObservationErrorCode, safe_message: str, *, retryable: bool = False) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        self.retryable = retryable
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    """Untrusted observation input. Identity is excluded by construction."""

    fleet_id: str
    idempotency_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.fleet_id, str) or not _FLEET_ID_PATTERN.fullmatch(self.fleet_id):
            raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "observation request is invalid")
        if not isinstance(self.idempotency_token, str) or not _IDEMPOTENCY_PATTERN.fullmatch(self.idempotency_token):
            raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "observation request is invalid")

    @classmethod
    def from_payload(cls, payload: object) -> "ObservationRequest":
        """Parse the untrusted action payload and reject identity injection."""
        if not isinstance(payload, dict) or set(payload) != {"fleet_id", "idempotency_token"}:
            raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "observation request is invalid")
        return cls(fleet_id=payload["fleet_id"], idempotency_token=payload["idempotency_token"])


@dataclass(frozen=True, slots=True)
class StatusRequest:
    """Untrusted status lookup input. Identity is excluded by construction."""

    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(self.operation_id):
            raise ObservationBoundaryError(ObservationErrorCode.CONTRACT_INVALID, "status request is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityInputs:
    """The five ADR 0001 request-side authority ceilings that bound this observation.

    The deployment mode is the sixth input and is supplied by trusted settings,
    not by the request; it is combined at evaluation time.
    """

    tenant_policy: str
    workspace_policy: str
    principal_authority: str
    capability_maximum: str
    risk_policy: str

    def __post_init__(self) -> None:
        for name in ("tenant_policy", "workspace_policy", "principal_authority", "capability_maximum", "risk_policy"):
            value = getattr(self, name)
            if value not in _AUTHORITY_ORDER:
                raise ValueError(f"{name} must be a valid authority value")


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationRequestContext:
    """Trusted adapter context supplied separately from the action payload."""

    requester: VerifiedPrincipal
    request_id: str
    authority_inputs: AuthorityInputs
    capability_id: str
    capability_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.requester, VerifiedPrincipal):
            raise ValueError("requester must be a VerifiedPrincipal")
        if not isinstance(self.request_id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.request_id):
            raise ValueError("request_id must be a valid operations identifier")
        if not isinstance(self.authority_inputs, AuthorityInputs):
            raise ValueError("authority_inputs must be an AuthorityInputs value")
        if not isinstance(self.capability_id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.capability_id):
            raise ValueError("capability_id must be a valid operations identifier")
        if not isinstance(self.capability_version, str) or not re.fullmatch(
            r"^[1-9][0-9]*\.[0-9]+$", self.capability_version
        ):
            raise ValueError("capability_version must be MAJOR.MINOR")


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusRequestContext:
    """Trusted adapter context for a status lookup (identity only)."""

    requester: VerifiedPrincipal
    request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.requester, VerifiedPrincipal):
            raise ValueError("requester must be a VerifiedPrincipal")
        if not isinstance(self.request_id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.request_id):
            raise ValueError("request_id must be a valid operations identifier")


class GameLiftObservationReader(Protocol):
    """Injected, read-only provider port. It exposes no write method.

    Each method returns a bounded, normalized domain value for one fleet. The
    adapter that implements this port owns the provider SDK and least-privilege
    read-only credentials; it never returns a raw provider response, ARN, or
    account id to the application layer.
    """

    def read_utilization(self, fleet_id: str) -> dict[str, int]:
        """Return normalized fleet utilization counters."""
        ...

    def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
        """Return normalized per-location fleet capacity."""
        ...

    def read_scaling_policies(self, fleet_id: str) -> list[dict[str, str]]:
        """Return normalized fleet scaling policies."""
        ...


class ObservationBeginOutcome(str, Enum):
    """Authoritative result of the store's conditional create transaction."""

    CREATED = "created"
    REPLAY_COMPLETED = "replay_completed"
    REPLAY_FAILED = "replay_failed"
    RECLAIMED = "reclaimed"
    IN_PROGRESS = "in_progress"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STATE_CONFLICT = "state_conflict"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    DEADLINE_EXPIRED = "deadline_expired"


class ObservationCompleteOutcome(str, Enum):
    """Authoritative result of the store's conditional finalize transaction."""

    RECORDED = "recorded"
    ALREADY_TERMINAL = "already_terminal"
    STATE_CONFLICT = "state_conflict"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    DEADLINE_EXPIRED = "deadline_expired"


@dataclass(frozen=True, slots=True)
class ObservationBegin:
    """The store's create result: an outcome, the bound operation id, and,
    depending on the outcome, the stored observation and hash (a completed
    replay), the stored bounded failure reason (a terminal-failed replay), or the
    reclaimed fencing generation (a stale-lease reclaim)."""

    outcome: ObservationBeginOutcome
    operation_id: str | None = None
    observation: dict[str, Any] | None = None
    observation_hash: str | None = None
    lease_holder: str | None = None
    current_state: str | None = None
    generation: int = 1
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationComplete:
    """The store's finalize result: an outcome and, if already terminal, the
    stored observation and hash so a racing writer replays rather than fails."""

    outcome: ObservationCompleteOutcome
    observation: dict[str, Any] | None = None
    observation_hash: str | None = None


class ObservationStatusView(str, Enum):
    """Public status of an operation as seen by a status lookup."""

    OBSERVING = "observing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ObservationStatus:
    """Typed, workspace-scoped status of one operation."""

    operation_id: str
    workspace_id: str
    state: ObservationStatusView
    observation: dict[str, Any] | None = None
    observation_hash: str | None = None


class ObservationStore(Protocol):
    """Persistence port; implementations MUST use conditional atomic commits.

    ``begin_observation`` writes, in one DynamoDB ``TransactWriteItems`` call:
    the idempotency mapping (conditional on absence, carrying the intent
    fingerprint), the current-state snapshot at ``observing``, the immutable
    ``STATE#0`` transition, and the initial append-only ledger event — all or
    nothing, before any provider read. ``complete_observation`` atomically
    persists the bounded result and advances ``observing`` -> ``succeeded`` with
    a matching ledger append. ``fail_observation`` records a bounded ``failed``
    transition where possible. ``load_status`` resolves an operation by id under
    trusted workspace ownership. Every required read is a key lookup; no method
    issues a scan or an unconditional put.
    """

    def begin_observation(
        self,
        *,
        operation_id: str,
        idempotency_fingerprint: str,
        workspace_id: str,
        idempotency_token: str,
        lease_holder: str,
        commit_not_after: datetime,
        lease_not_after: datetime,
        ttl_epoch_s: int,
        intent: Mapping[str, Any],
    ) -> ObservationBegin:
        """Resolve or atomically create the operation before any read."""
        ...

    def complete_observation(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        lease_holder: str,
        commit_not_after: datetime,
        ttl_epoch_s: int,
        observation: Mapping[str, Any],
        generation: int = 1,
    ) -> ObservationComplete:
        """Atomically persist the bounded result and transition to succeeded.

        generation fences the write on the caller's fencing token so a writer
        holding a reclaimed lease (generation > 1) commits while a superseded
        writer's generation-1 commit fails closed.
        """
        ...

    def fail_observation(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        lease_holder: str,
        reason_code: str,
        ttl_epoch_s: int,
        generation: int = 1,
    ) -> None:
        """Record a bounded failed transition where possible (best effort).

        The stored bounded failure is terminal and deterministic: a later retry
        under the same idempotency token replays it verbatim rather than being
        marked retryable. generation fences the write on the caller's lease.
        """
        ...

    def load_status(self, *, operation_id: str, workspace_id: str) -> ObservationStatus | None:
        """Return the typed status if it exists and is owned by the workspace."""
        ...


@dataclass
class NullObservationMetrics:
    """Default sink that drops metrics; a real adapter forwards to CloudWatch."""

    def record(self, name: str, value: float, *, dimensions: Mapping[str, str] | None = None) -> None:
        return None


class ObservationMetrics(Protocol):
    """Structured, bounded metrics sink. Only sanitized dimensions are emitted."""

    def record(self, name: str, value: float, *, dimensions: Mapping[str, str] | None = None) -> None: ...


class ObservationService:
    """Produce one bounded read-only GameLift observation, failing closed."""

    def __init__(
        self,
        *,
        settings: OperationsSettings,
        identity_boundary: ApprovalIdentityBoundary,
        reader: GameLiftObservationReader,
        store: ObservationStore,
        clock: Callable[[], datetime] = _system_clock,
        monotonic: Callable[[], float] | None = None,
        operation_id_factory: Callable[[], str] = _new_operation_id,
        metrics: ObservationMetrics | None = None,
    ) -> None:
        self._settings = settings
        self._identity_boundary = identity_boundary
        self._reader = reader
        self._store = store
        self._clock = clock
        self._operation_id_factory = operation_id_factory
        self._metrics = metrics or NullObservationMetrics()
        # Reuse the E0-validated budget/deadline enforcement for the read phase.
        self._budget = LatencyBudget(
            per_read_s=settings.per_read_budget_s,
            persistence_s=settings.persistence_budget_s,
            cancellation_margin_s=settings.cancellation_margin_s,
        )
        self._monotonic = monotonic or time.monotonic

    # -- Observe ----------------------------------------------------------

    def observe(self, request: ObservationRequest, context: ObservationRequestContext) -> dict[str, Any]:
        """Resolve/create idempotency, then read, canonicalize, and persist."""
        requester_identity, effective_authority, authority_inputs = self._authorize(context)

        # Intent fingerprint: trusted workspace + exact request intent. It is
        # computed BEFORE any read and never depends on the observation result,
        # its id, or a timestamp, so a retry recomputes the same value.
        intent = self._intent(request, context)
        fingerprint = canonical_sha256(
            {
                "workspace_id": requester_identity["workspace_id"],
                "idempotency_token": request.idempotency_token,
                "intent_hash": canonical_sha256(intent),
            }
        )

        now = _utc(self._clock(), "clock")
        expires_at = now + timedelta(seconds=self._settings.observation_ttl_s)
        commit_not_after = min(expires_at, context.requester.expires_at)
        lease_not_after = min(now + timedelta(seconds=self._budget.total_deadline_s), context.requester.expires_at)
        ttl_epoch_s = int(expires_at.timestamp())
        operation_id = self._operation_id_factory()
        lease_holder = context.request_id

        begin = self._store.begin_observation(
            operation_id=operation_id,
            idempotency_fingerprint=fingerprint,
            workspace_id=requester_identity["workspace_id"],
            idempotency_token=request.idempotency_token,
            lease_holder=lease_holder,
            commit_not_after=commit_not_after,
            lease_not_after=lease_not_after,
            ttl_epoch_s=ttl_epoch_s,
            intent=intent,
        )
        begin = self._require_begin(begin)

        if begin.outcome is ObservationBeginOutcome.REPLAY_COMPLETED:
            # A matching completed retry returns the stored bounded observation
            # verbatim with no new provider read; its hash is re-verified.
            return self._replay(begin)

        if begin.outcome is ObservationBeginOutcome.REPLAY_FAILED:
            # A terminal failed operation is distinct from an in-progress one: it
            # returns its stored bounded failure deterministically and is NOT
            # retryable, because the same idempotency token can never make
            # progress. A caller that wants to try again must start a new
            # operation with a fresh idempotency token.
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "terminal"})
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "a prior observation for this idempotency token failed terminally; "
                "start a new operation with a new idempotency token",
                retryable=False,
            )

        if begin.outcome is ObservationBeginOutcome.IN_PROGRESS:
            return self._resume_in_progress(begin)

        if begin.outcome is ObservationBeginOutcome.IDEMPOTENCY_CONFLICT:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "idempotency"})
            raise ObservationBoundaryError(
                ObservationErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency token was reused with different intent",
            )

        if begin.outcome is ObservationBeginOutcome.PROVIDER_UNAVAILABLE:
            # A non-conditional store error (throttle, transaction conflict,
            # validation, or provider fault) is a retryable unavailable state,
            # never a false 409 idempotency/state conflict.
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "store"})
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "observation state store is temporarily unavailable",
                retryable=True,
            )

        if begin.outcome is ObservationBeginOutcome.DEADLINE_EXPIRED:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "deadline"})
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "observation deadline elapsed before the operation was created",
                retryable=True,
            )

        if begin.outcome is ObservationBeginOutcome.RECLAIMED:
            # A stale in-progress operation whose lease expired was reclaimed:
            # the store advanced the fencing generation, took the lease, and
            # appended a recovery ledger transition. We now safely rerun the
            # read-only observation and finalize under the reclaimed generation.
            reclaimed_id = begin.operation_id
            if reclaimed_id is None:
                self._metrics.record("observation.failed", 1.0, dimensions={"reason": "state"})
                raise ObservationBoundaryError(
                    ObservationErrorCode.STATE_CONFLICT, "observation could not be reclaimed"
                )
            self._metrics.record("observation.reclaimed", 1.0)
            return self._read_and_finalize(
                request=request,
                context=context,
                operation_id=reclaimed_id,
                lease_holder=lease_holder,
                requester_identity=requester_identity,
                effective_authority=effective_authority,
                authority_inputs=authority_inputs,
                expires_at=expires_at,
                commit_not_after=commit_not_after,
                ttl_epoch_s=ttl_epoch_s,
                generation=begin.generation,
            )

        if begin.outcome is not ObservationBeginOutcome.CREATED or begin.operation_id is None:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "state"})
            raise ObservationBoundaryError(ObservationErrorCode.STATE_CONFLICT, "observation could not be created")

        # A fresh operation now exists at ``observing`` under our lease. Execute
        # the reads and finalize. Any failure records a bounded failed transition.
        created_id = begin.operation_id
        return self._read_and_finalize(
            request=request,
            context=context,
            operation_id=created_id,
            lease_holder=lease_holder,
            requester_identity=requester_identity,
            effective_authority=effective_authority,
            authority_inputs=authority_inputs,
            expires_at=expires_at,
            commit_not_after=commit_not_after,
            ttl_epoch_s=ttl_epoch_s,
            generation=1,
        )

    # -- Status -----------------------------------------------------------

    def get_status(self, request: StatusRequest, context: StatusRequestContext) -> ObservationStatus:
        """Return the typed status of one operation, enforcing workspace ownership."""
        if not self._settings.observe_enabled:
            raise ObservationBoundaryError(
                ObservationErrorCode.AUTHORIZATION_DENIED, "operations observe is not enabled"
            )
        try:
            evaluated_at = _utc(self._clock(), "clock")
            requester_identity = self._identity_boundary.bind_requester(context.requester, now=evaluated_at)
        except (IdentityBoundaryError, ValueError) as exc:
            raise ObservationBoundaryError(
                ObservationErrorCode.IDENTITY_CONTEXT_INVALID, "authenticated observation identity is invalid"
            ) from exc

        status = self._store.load_status(
            operation_id=request.operation_id, workspace_id=requester_identity["workspace_id"]
        )
        # A missing record, or one owned by a different workspace, is NOT_FOUND:
        # ownership is enforced by the store's workspace-scoped key, and a
        # cross-workspace id is never disclosed as existing.
        if not isinstance(status, ObservationStatus):
            raise ObservationBoundaryError(ObservationErrorCode.NOT_FOUND, "observation was not found")
        if status.workspace_id != requester_identity["workspace_id"]:
            raise ObservationBoundaryError(ObservationErrorCode.NOT_FOUND, "observation was not found")
        if status.state is ObservationStatusView.SUCCEEDED and isinstance(status.observation, dict):
            self._verify_stored_hash(status.observation, status.observation_hash)
        return status

    # -- Internals --------------------------------------------------------

    def _authorize(self, context: ObservationRequestContext) -> tuple[dict[str, str], str, dict[str, str]]:
        # 1. Deployment ceiling. A disabled deployment denies before any read.
        if not self._settings.observe_enabled:
            self._metrics.record("observation.denied", 1.0, dimensions={"reason": "deployment_disabled"})
            raise ObservationBoundaryError(
                ObservationErrorCode.AUTHORIZATION_DENIED, "operations observe is not enabled"
            )

        # 2. Verifier-derived identity, re-bound to the configured boundary.
        try:
            evaluated_at = _utc(self._clock(), "clock")
            requester_identity = self._identity_boundary.bind_requester(context.requester, now=evaluated_at)
        except (IdentityBoundaryError, ValueError) as exc:
            self._metrics.record("observation.denied", 1.0, dimensions={"reason": "identity"})
            raise ObservationBoundaryError(
                ObservationErrorCode.IDENTITY_CONTEXT_INVALID, "authenticated observation identity is invalid"
            ) from exc

        # 3. Effective authority: the real deterministic minimum of the six ADR
        #    0001 inputs. The five caller/deployment inputs are recorded exactly
        #    as supplied — a higher deployment or principal ceiling is NOT capped
        #    or rewritten, so the recorded ceilings stay truthful. Only this
        #    capability's own maximum is the explicit observe-phase ceiling, so
        #    the observation can never carry more than observe authority while the
        #    contract's ``effective == min(inputs)`` invariant still holds.
        authority_inputs = {
            "deployment_mode": self._settings.mode,
            "tenant_policy": context.authority_inputs.tenant_policy,
            "workspace_policy": context.authority_inputs.workspace_policy,
            "principal_authority": context.authority_inputs.principal_authority,
            "capability_maximum": _cap_at_observe(context.authority_inputs.capability_maximum),
            "risk_policy": context.authority_inputs.risk_policy,
        }
        effective_authority = min(authority_inputs.values(), key=_AUTHORITY_ORDER.__getitem__)
        if _AUTHORITY_ORDER[effective_authority] < _AUTHORITY_ORDER[_OBSERVE_AUTHORITY]:
            self._metrics.record("observation.denied", 1.0, dimensions={"reason": "authority"})
            raise ObservationBoundaryError(
                ObservationErrorCode.AUTHORIZATION_DENIED, "effective authority does not permit observation"
            )
        return requester_identity, effective_authority, authority_inputs

    @staticmethod
    def _intent(request: ObservationRequest, context: ObservationRequestContext) -> dict[str, Any]:
        """The exact request intent that fingerprints an operation.

        It carries the capability (playbook), provider, phase, and target only —
        never the observation result, its id, or a timestamp — so a retry with
        the same request reproduces it exactly.
        """
        return {
            "phase": "observe",
            "provider": "gamelift",
            "capability_id": context.capability_id,
            "capability_version": context.capability_version,
            "target": {"provider": "gamelift", "fleet_id": request.fleet_id},
        }

    def _read_and_finalize(
        self,
        *,
        request: ObservationRequest,
        context: ObservationRequestContext,
        operation_id: str,
        lease_holder: str,
        requester_identity: dict[str, str],
        effective_authority: str,
        authority_inputs: dict[str, str],
        expires_at: datetime,
        commit_not_after: datetime,
        ttl_epoch_s: int,
        generation: int = 1,
    ) -> dict[str, Any]:
        reads: list[ProviderRead] = [
            lambda: self._reader.read_utilization(request.fleet_id),
            lambda: self._reader.read_capacity(request.fleet_id),
            lambda: self._reader.read_scaling_policies(request.fleet_id),
        ]
        try:
            utilization, capacity, scaling_policies = self._run_reads(reads)
        except (DeadlineExceededError, PartialObservationError) as exc:
            # A deadline overrun is a timeout; any other provider condition is a
            # failure. The two are reported through distinct metric events.
            if isinstance(exc, DeadlineExceededError):
                self._metrics.record("observation.timeout", 1.0, dimensions={"reason": "provider"})
            else:
                self._metrics.record("observation.failed", 1.0, dimensions={"reason": "provider"})
            self._store.fail_observation(
                operation_id=operation_id,
                workspace_id=requester_identity["workspace_id"],
                lease_holder=lease_holder,
                reason_code="provider_unavailable",
                ttl_epoch_s=ttl_epoch_s,
                generation=generation,
            )
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "provider observation could not be completed",
                retryable=True,
            ) from exc

        observed_at = _utc(self._clock(), "clock")
        observation: dict[str, Any] = {
            "observation_contract_version": CONTRACT_VERSION,
            "observation_id": operation_id,
            "idempotency_token": request.idempotency_token,
            "phase": "observe",
            "provider": "gamelift",
            "capability": {
                "capability_id": context.capability_id,
                "capability_version": context.capability_version,
            },
            "effective_authority": effective_authority,
            "authority_inputs": authority_inputs,
            "requester": requester_identity,
            "correlation": {
                "correlation_id": f"obs.{request.idempotency_token[5:25]}",
                "request_id": context.request_id,
            },
            "target": {"provider": "gamelift", "fleet_id": request.fleet_id},
            "observed_at": _format_timestamp(observed_at),
            "expires_at": _format_timestamp(expires_at),
            "results": {
                "utilization": utilization,
                "capacity": capacity,
                "scaling_policies": scaling_policies,
            },
        }
        try:
            validate_observation(observation)
        except ObservationContractError as exc:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "contract"})
            self._store.fail_observation(
                operation_id=operation_id,
                workspace_id=requester_identity["workspace_id"],
                lease_holder=lease_holder,
                reason_code="contract_invalid",
                ttl_epoch_s=ttl_epoch_s,
                generation=generation,
            )
            raise ObservationBoundaryError(
                ObservationErrorCode.CONTRACT_INVALID, "observation result is invalid"
            ) from exc

        outcome = self._store.complete_observation(
            operation_id=operation_id,
            workspace_id=requester_identity["workspace_id"],
            lease_holder=lease_holder,
            commit_not_after=commit_not_after,
            ttl_epoch_s=ttl_epoch_s,
            observation=deepcopy(observation),
            generation=generation,
        )
        return self._resolve_complete(outcome, observation)

    def _resolve_complete(self, complete: object, observation: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(complete, ObservationComplete):
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "store"})
            raise ObservationBoundaryError(ObservationErrorCode.STATE_CONFLICT, "observation could not be recorded")

        if complete.outcome is ObservationCompleteOutcome.RECORDED:
            self._metrics.record(
                "observation.recorded", 1.0, dimensions={"authority": observation["effective_authority"]}
            )
            return deepcopy(observation)

        if complete.outcome is ObservationCompleteOutcome.ALREADY_TERMINAL:
            # A concurrent writer finalized first: replay the stored result.
            if not isinstance(complete.observation, dict):
                raise ObservationBoundaryError(ObservationErrorCode.STATE_CONFLICT, "observation replay is unavailable")
            self._verify_stored_hash(complete.observation, complete.observation_hash)
            self._metrics.record("observation.replay", 1.0)
            return deepcopy(complete.observation)

        if complete.outcome is ObservationCompleteOutcome.DEADLINE_EXPIRED:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "deadline"})
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "observation deadline elapsed before the record was committed",
                retryable=True,
            )

        if complete.outcome is ObservationCompleteOutcome.PROVIDER_UNAVAILABLE:
            # A non-conditional store fault on finalize is retryable/unavailable,
            # never a false state conflict.
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "store"})
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "observation state store is temporarily unavailable",
                retryable=True,
            )

        self._metrics.record("observation.failed", 1.0, dimensions={"reason": "state"})
        raise ObservationBoundaryError(
            ObservationErrorCode.STATE_CONFLICT, "observation changed before it could be recorded"
        )

    def _require_begin(self, begin: object) -> ObservationBegin:
        if not isinstance(begin, ObservationBegin):
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "store"})
            raise ObservationBoundaryError(ObservationErrorCode.STATE_CONFLICT, "observation could not be created")
        return begin

    def _replay(self, begin: ObservationBegin) -> dict[str, Any]:
        if not isinstance(begin.observation, dict):
            raise ObservationBoundaryError(ObservationErrorCode.STATE_CONFLICT, "observation replay is unavailable")
        self._verify_stored_hash(begin.observation, begin.observation_hash)
        self._metrics.record("observation.replay", 1.0)
        return deepcopy(begin.observation)

    def _resume_in_progress(self, begin: ObservationBegin) -> dict[str, Any]:
        # An in-progress retry under an ACTIVE (unexpired) lease held by another
        # attempt is a bounded, retryable state conflict, not a second operation.
        # This path makes no recovery claim: the store only reports IN_PROGRESS
        # while the lease is live, and returns RECLAIMED (handled in observe)
        # once the lease has expired. Before expiry the caller retries and
        # eventually replays the winner's completed result or triggers a reclaim.
        self._metrics.record("observation.in_progress", 1.0)
        raise ObservationBoundaryError(
            ObservationErrorCode.STATE_CONFLICT,
            "an observation for this idempotency token is already in progress",
            retryable=True,
        )

    def _verify_stored_hash(self, observation: dict[str, Any], recorded_hash: str | None) -> None:
        """Re-verify the stored observation's canonical hash before returning it."""
        actual = canonical_sha256(observation)
        if not isinstance(recorded_hash, str) or actual != recorded_hash:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "hash"})
            raise ObservationBoundaryError(
                ObservationErrorCode.STATE_CONFLICT, "stored observation failed hash verification"
            )

    def _run_reads(
        self, reads: list[ProviderRead]
    ) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, str]]]:
        """Run the three reads under per-call wall-clock deadlines, failing closed."""
        start = self._monotonic()
        deadline_s = self._budget.total_deadline_s
        results: list[Any] = []
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="obs-read")
        abandoned = False
        try:
            for index, read in enumerate(reads):
                call_start = self._monotonic()
                remaining_total = deadline_s - (call_start - start)
                read_budget = min(self._budget.per_read_s, remaining_total)
                future = executor.submit(read)
                try:
                    value = future.result(timeout=read_budget)
                except concurrent.futures.TimeoutError as exc:
                    abandoned = True
                    future.cancel()
                    raise DeadlineExceededError(
                        f"read[{index}]", self._monotonic() - call_start, self._budget.per_read_s
                    ) from exc
                except Exception as exc:  # a provider error is a fail-closed condition
                    raise PartialObservationError("provider read failed") from exc

                call_elapsed = self._monotonic() - call_start
                if call_elapsed > self._budget.per_read_s:
                    raise DeadlineExceededError(f"read[{index}]", call_elapsed, self._budget.per_read_s)
                if value is None:
                    raise PartialObservationError("provider read returned no result")
                results.append(value)
        finally:
            executor.shutdown(wait=not abandoned, cancel_futures=True)

        utilization = self._normalize_utilization(results[0])
        capacity = self._normalize_capacity(results[1])
        scaling_policies = self._normalize_scaling_policies(results[2])
        return utilization, capacity, scaling_policies

    @staticmethod
    def _normalize_utilization(raw: object) -> dict[str, int]:
        if not isinstance(raw, dict):
            raise PartialObservationError("utilization read returned an unexpected shape")
        keys = ("active_server_processes", "active_game_sessions", "current_player_sessions", "maximum_player_sessions")
        result: dict[str, int] = {}
        for key in keys:
            value = raw.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PartialObservationError("utilization read returned an invalid counter")
            result[key] = value
        return result

    @staticmethod
    def _normalize_capacity(raw: object) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or len(raw) > _MAX_CAPACITY_LOCATIONS:
            raise PartialObservationError("capacity read returned an unexpected or oversized shape")
        result: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise PartialObservationError("capacity read returned an unexpected entry")
            location = entry.get("location")
            if not isinstance(location, str) or not location:
                raise PartialObservationError("capacity read returned an invalid location")
            normalized: dict[str, Any] = {"location": location}
            for key in ("desired", "minimum", "maximum", "active", "idle"):
                value = entry.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise PartialObservationError("capacity read returned an invalid counter")
                normalized[key] = value
            result.append(normalized)
        return result

    @staticmethod
    def _normalize_scaling_policies(raw: object) -> list[dict[str, str]]:
        if not isinstance(raw, list) or len(raw) > _MAX_SCALING_POLICIES:
            raise PartialObservationError("scaling policies read returned an unexpected or oversized shape")
        result: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise PartialObservationError("scaling policies read returned an unexpected entry")
            name = entry.get("name")
            status = entry.get("status")
            metric_name = entry.get("metric_name")
            if not isinstance(name, str) or not name:
                raise PartialObservationError("scaling policy read returned an invalid name")
            if status not in _SCALING_POLICY_STATUSES:
                raise PartialObservationError("scaling policy read returned an invalid status")
            if not isinstance(metric_name, str) or not metric_name:
                raise PartialObservationError("scaling policy read returned an invalid metric name")
            result.append({"name": name, "status": status, "metric_name": metric_name})
        return result
