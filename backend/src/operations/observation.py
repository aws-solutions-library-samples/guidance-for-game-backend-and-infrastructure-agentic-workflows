"""Read-only GameLift observation application service (issue #413, E1 Agent A).

This protocol-neutral service performs the exact E1 observation described in
ADR 0005 ("Durable observation state without queues"): three bounded, read-only
GameLift reads for one fleet — utilization, capacity, and scaling policies —
plus a conditional/idempotent DynamoDB state write and an append-only ledger
transaction, all inside one request under explicit wall-clock deadlines.

Boundaries enforced here, consistent with ADR 0001/0002/0003 and the
``ApprovalService`` precedent:

* **Verifier-derived identity only.** The service accepts an untrusted
  :class:`ObservationRequest` (fleet id and idempotency token only) plus a
  trusted :class:`ObservationRequestContext` carrying a
  :class:`~operations.identity.VerifiedPrincipal`. Tenant, workspace, subject,
  and client are taken from the verified principal, never from the request.
* **Explicit ceilings.** The six ADR 0001 authority inputs are recorded and the
  deterministic minimum is the effective authority, additionally capped at
  ``observe``. A ``disabled`` deployment denies before any provider read.
* **No provider writes.** Only injected read callables are invoked. There is no
  write method on this service or its ports.
* **Fail closed, no partial success.** Any per-read or persistence deadline
  overrun, a read with no usable result, a replay/idempotency conflict, or a
  state conflict raises a typed :class:`ObservationBoundaryError`; a partial
  observation is never returned as success.
* **Bounded, sanitized output.** The observation validates against the additive
  ``gamelift-observation`` contract (bounded sizes), is canonicalized (RFC 8785)
  and hashed, and only bounded, public-safe metrics are emitted — never an
  account id, ARN, or raw provider payload.
"""

from __future__ import annotations

# Standard library
import concurrent.futures
import re
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
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
_OBSERVATION_ID_PATTERN = re.compile(r"^obs_[a-z0-9]{26}$")

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


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_observation_id() -> str:
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


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityInputs:
    """The six ADR 0001 authority ceilings that bound this observation."""

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


class ObservationCommitOutcome(str, Enum):
    """Authoritative result of the store's conditional observation transaction."""

    RECORDED = "recorded"
    REPLAY = "replay"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STATE_CONFLICT = "state_conflict"
    DEADLINE_EXPIRED = "deadline_expired"


@dataclass(frozen=True, slots=True)
class ObservationCommit:
    """The store's result: an outcome and, on replay, the stored observation."""

    outcome: ObservationCommitOutcome
    observation: dict[str, Any] | None = None


class ObservationStore(Protocol):
    """Persistence port; implementations MUST use a conditional atomic commit.

    ``record_observation`` writes, in one DynamoDB ``TransactWriteItems`` call:
    the idempotency mapping (conditional on absence, carrying the fingerprint),
    the current-state snapshot, the immutable initial state transition, and the
    initial append-only ledger event — all or nothing. It enforces the exclusive
    ``commit_not_after`` deadline in the same transaction and returns a typed
    :class:`ObservationCommit`; a raw string lookalike fails closed at the caller.
    """

    def record_observation(
        self,
        *,
        observation_id: str,
        idempotency_fingerprint: str,
        workspace_id: str,
        idempotency_token: str,
        commit_not_after: datetime,
        ttl_epoch_s: int,
        observation: Mapping[str, Any],
    ) -> ObservationCommit:
        """Conditionally record before the deadline; return the authoritative outcome."""
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
        observation_id_factory: Callable[[], str] = _new_observation_id,
        metrics: ObservationMetrics | None = None,
    ) -> None:
        self._settings = settings
        self._identity_boundary = identity_boundary
        self._reader = reader
        self._store = store
        self._clock = clock
        self._observation_id_factory = observation_id_factory
        self._metrics = metrics or NullObservationMetrics()
        # Reuse the E0-validated budget/deadline enforcement for the read phase.
        self._budget = LatencyBudget(
            per_read_s=settings.per_read_budget_s,
            persistence_s=settings.persistence_budget_s,
            cancellation_margin_s=settings.cancellation_margin_s,
        )
        self._monotonic = monotonic or time.monotonic

    def observe(self, request: ObservationRequest, context: ObservationRequestContext) -> dict[str, Any]:
        """Validate, read, canonicalize, and conditionally persist one observation."""
        # 1. Deployment ceiling. A disabled deployment denies before any read.
        if not self._settings.observe_enabled:
            self._metrics.record("observation.denied", 1.0, dimensions={"reason": "deployment_disabled"})
            raise ObservationBoundaryError(
                ObservationErrorCode.AUTHORIZATION_DENIED, "operations observe is not enabled"
            )

        # 2. Verifier-derived identity. Tenant/workspace/subject/client come from
        #    the verified principal, re-bound to the configured deployment boundary.
        try:
            evaluated_at = _utc(self._clock(), "clock")
            requester_identity = self._identity_boundary.bind_requester(context.requester, now=evaluated_at)
        except (IdentityBoundaryError, ValueError) as exc:
            self._metrics.record("observation.denied", 1.0, dimensions={"reason": "identity"})
            raise ObservationBoundaryError(
                ObservationErrorCode.IDENTITY_CONTEXT_INVALID, "authenticated observation identity is invalid"
            ) from exc

        # 3. Effective authority: deterministic minimum of the six inputs, then
        #    capped at the deployment ceiling and the observe phase ceiling.
        authority_inputs = {
            "deployment_mode": self._settings.mode,
            "tenant_policy": context.authority_inputs.tenant_policy,
            "workspace_policy": context.authority_inputs.workspace_policy,
            "principal_authority": context.authority_inputs.principal_authority,
            "capability_maximum": context.authority_inputs.capability_maximum,
            "risk_policy": context.authority_inputs.risk_policy,
        }
        effective_authority = min(authority_inputs.values(), key=_AUTHORITY_ORDER.__getitem__)
        if _AUTHORITY_ORDER[effective_authority] < _AUTHORITY_ORDER[_OBSERVE_AUTHORITY]:
            self._metrics.record("observation.denied", 1.0, dimensions={"reason": "authority"})
            raise ObservationBoundaryError(
                ObservationErrorCode.AUTHORIZATION_DENIED, "effective authority does not permit observation"
            )
        # The observe phase never carries more than observe authority.
        effective_authority = min((effective_authority, _OBSERVE_AUTHORITY), key=_AUTHORITY_ORDER.__getitem__)

        # 4. Three bounded, read-only provider reads under wall-clock deadlines.
        reads: list[ProviderRead] = [
            lambda: self._reader.read_utilization(request.fleet_id),
            lambda: self._reader.read_capacity(request.fleet_id),
            lambda: self._reader.read_scaling_policies(request.fleet_id),
        ]
        try:
            utilization, capacity, scaling_policies = self._run_reads(reads)
        except (DeadlineExceededError, PartialObservationError) as exc:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "provider"})
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "provider observation could not be completed",
                retryable=True,
            ) from exc

        # 5. Build, validate, and hash the bounded observation document.
        observed_at = _utc(self._clock(), "clock")
        expires_at = observed_at + timedelta(seconds=self._settings.observation_ttl_s)
        observation_id = self._observation_id_factory()
        observation: dict[str, Any] = {
            "observation_contract_version": CONTRACT_VERSION,
            "observation_id": observation_id,
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
            raise ObservationBoundaryError(
                ObservationErrorCode.CONTRACT_INVALID, "observation result is invalid"
            ) from exc

        observation_hash = canonical_sha256(observation)
        idempotency_fingerprint = canonical_sha256(
            {
                "workspace_id": requester_identity["workspace_id"],
                "idempotency_token": request.idempotency_token,
                "observation_hash": observation_hash,
            }
        )

        # 6. Conditional/idempotent transactional persistence with a deadline.
        commit_not_after = min(expires_at, context.requester.expires_at)
        ttl_epoch_s = int(expires_at.timestamp())
        outcome = self._store.record_observation(
            observation_id=observation_id,
            idempotency_fingerprint=idempotency_fingerprint,
            workspace_id=requester_identity["workspace_id"],
            idempotency_token=request.idempotency_token,
            commit_not_after=commit_not_after,
            ttl_epoch_s=ttl_epoch_s,
            observation=deepcopy(observation),
        )
        return self._resolve_commit(outcome, observation)

    def _resolve_commit(self, commit: object, observation: dict[str, Any]) -> dict[str, Any]:
        # A raw string lookalike (not an ObservationCommit) fails closed.
        if not isinstance(commit, ObservationCommit):
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "store"})
            raise ObservationBoundaryError(ObservationErrorCode.STATE_CONFLICT, "observation could not be recorded")

        if commit.outcome is ObservationCommitOutcome.RECORDED:
            self._metrics.record(
                "observation.recorded", 1.0, dimensions={"authority": observation["effective_authority"]}
            )
            return deepcopy(observation)

        if commit.outcome is ObservationCommitOutcome.REPLAY:
            # A replay returns the stored observation without repeating work.
            if not isinstance(commit.observation, dict):
                raise ObservationBoundaryError(ObservationErrorCode.STATE_CONFLICT, "observation replay is unavailable")
            self._metrics.record("observation.replay", 1.0)
            return deepcopy(commit.observation)

        if commit.outcome is ObservationCommitOutcome.IDEMPOTENCY_CONFLICT:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "idempotency"})
            raise ObservationBoundaryError(
                ObservationErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency token was reused with different content",
            )

        if commit.outcome is ObservationCommitOutcome.DEADLINE_EXPIRED:
            self._metrics.record("observation.failed", 1.0, dimensions={"reason": "deadline"})
            raise ObservationBoundaryError(
                ObservationErrorCode.PROVIDER_UNAVAILABLE,
                "observation deadline elapsed before the record was committed",
                retryable=True,
            )

        # STATE_CONFLICT or any other non-success outcome.
        self._metrics.record("observation.failed", 1.0, dimensions={"reason": "state"})
        raise ObservationBoundaryError(
            ObservationErrorCode.STATE_CONFLICT, "observation changed before it could be recorded"
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
