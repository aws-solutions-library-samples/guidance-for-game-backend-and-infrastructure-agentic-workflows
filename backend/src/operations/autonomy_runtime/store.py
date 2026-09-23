"""Atomic, policy-bound reservation port + reference store for E5 autonomy (#439).

The reservation store is the durable mechanism that makes the pure #438
evaluator's reading safe under concurrency. Before an autonomous write can be
dispatched, its budget, frequency, concurrency, and anti-oscillation footprint
must be **atomically reserved** against the policy-bound rolling window state,
fenced on the exact ``state_revision``. A reservation either advances the
window-state revision by exactly one and returns the new hash-bound snapshot, or
it fails closed — never a false success and never a double count.

This module defines:

* :class:`ReservationRequest` — the identifier-and-integer-only reservation
  input. It carries the policy reference, the fenced ``state_id`` /
  ``expected_revision``, the bound ``operation_id``, the integer micro-USD action
  cost, the evaluated epoch, and the proposed change direction. It carries **no**
  policy body, no limits, no observation, no credential, and no model or
  request-body input.
* :class:`ReservationOutcome` / :class:`ReservationResult` — the closed outcome
  set. ``RESERVED`` returns the advanced snapshot; ``CONFLICT`` and
  ``UNAVAILABLE`` return ``None`` and fail closed.
* :class:`ReservationStore` — the DynamoDB-shaped port a runtime depends on. The
  real implementation is a single conditional ``UpdateItem`` / ``TransactWrite``
  fenced on ``state_revision`` (no ``Scan``, no unconditional ``PutItem``); it is
  intentionally not created here because default deployments provision no
  autonomy control plane and grant no provider-write permission.
* :class:`InMemoryReservationStore` — a deterministic reference implementation
  that models the DynamoDB conditional-write fencing so the fail-closed contract
  is provable without any AWS dependency.

Nothing here reads the environment, holds a credential, or performs a provider
write. The store advances *durable accounting state only*; the single provider
write is performed elsewhere, by the existing narrow executor, invoked solely by
a durable authenticated Step Functions state machine carrying the operation id.
"""

from __future__ import annotations

# Standard library
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# Local modules
from operations.contracts.autonomy import autonomy_window_state_hash, validate_autonomy_contract

_WINDOW_STATE_SCHEMA = "gamelift-capacity-autonomy-window-state"
_DIRECTIONS = frozenset({"none", "increase", "decrease"})
_MAX_REVISION = 9007199254740991


class ReservationStoreError(RuntimeError):
    """A structural precondition of a reservation write was violated / store failure."""


class ReservationOutcome(str, Enum):
    """The closed, authoritative outcome set of a reservation attempt."""

    RESERVED = "reserved"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    """One atomic reservation intent — identifiers and integers only.

    ``slots=True`` makes any unknown keyword argument a ``TypeError``, so a caller
    cannot smuggle a policy body, limits, observation, or credential through the
    reservation boundary.
    """

    policy_ref: dict[str, str]
    state_id: str
    expected_revision: int
    operation_id: str
    action_micro_usd: int
    now_epoch_seconds: int
    change_direction: str

    def __post_init__(self) -> None:
        if isinstance(self.action_micro_usd, bool) or not isinstance(self.action_micro_usd, int):
            raise ValueError("action_micro_usd must be an integer")
        if self.action_micro_usd < 0:
            raise ValueError("action_micro_usd must be non-negative")
        if isinstance(self.expected_revision, bool) or not isinstance(self.expected_revision, int):
            raise ValueError("expected_revision must be an integer")
        if self.expected_revision < 0 or self.expected_revision > _MAX_REVISION:
            raise ValueError("expected_revision is out of range")
        if isinstance(self.now_epoch_seconds, bool) or not isinstance(self.now_epoch_seconds, int):
            raise ValueError("now_epoch_seconds must be an integer")
        if self.now_epoch_seconds < 0:
            raise ValueError("now_epoch_seconds must be non-negative")
        if self.change_direction not in _DIRECTIONS:
            raise ValueError("change_direction must be one of none, increase, decrease")
        if set(self.policy_ref) != {"policy_id", "policy_version", "policy_hash"}:
            raise ValueError("policy_ref must carry exactly policy_id, policy_version, policy_hash")


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """The outcome of a reservation attempt and, when reserved, the new snapshot."""

    outcome: ReservationOutcome
    window_state: dict[str, Any] | None = None

    @property
    def reserved(self) -> bool:
        return self.outcome is ReservationOutcome.RESERVED


@runtime_checkable
class ReservationStore(Protocol):
    """The DynamoDB-shaped port for atomic, revision-fenced reservation.

    An implementation MUST advance the window-state revision by exactly one when
    (and only when) the stored revision equals ``request.expected_revision`` and
    the stored policy reference equals ``request.policy_ref``; otherwise it MUST
    return ``CONFLICT``. Any store failure MUST return ``UNAVAILABLE``. It MUST
    never return ``RESERVED`` without a durably advanced revision.
    """

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        """Atomically reserve one write's footprint, fenced on state revision."""
        ...

    def current(self, state_id: str) -> dict[str, Any]:
        """Return the current durable window-state snapshot for ``state_id``."""
        ...


class InMemoryReservationStore:
    """Deterministic reference store modelling DynamoDB conditional-write fencing.

    Holds exactly one durable window-state snapshot keyed by ``state_id``. A
    reservation is accepted only when the stored revision and policy reference
    match the request; the commit advances the revision by exactly one, adds the
    action cost to the window spend, increments the write count and in-flight
    concurrency, updates the last-write time and change direction, and re-binds
    the ``state_hash``. Everything else fails closed.
    """

    __slots__ = ("_by_state_id",)

    def __init__(self, *, initial_state: dict[str, Any]) -> None:
        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, initial_state)
        self._by_state_id: dict[str, dict[str, Any]] = {initial_state["state_id"]: deepcopy(initial_state)}

    def current(self, state_id: str) -> dict[str, Any]:
        try:
            return deepcopy(self._by_state_id[state_id])
        except KeyError as exc:
            raise ReservationStoreError(f"unknown state_id: {state_id}") from exc

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        stored = self._by_state_id.get(request.state_id)
        if stored is None:
            # An unknown state is a conflict, not a silent success.
            return ReservationResult(ReservationOutcome.CONFLICT)

        # Revision + policy fence. A stale revision or a different policy loses.
        if stored["state_revision"] != request.expected_revision:
            return ReservationResult(ReservationOutcome.CONFLICT)
        expected_ref = {
            "policy_id": stored["policy"]["policy_id"],
            "policy_version": stored["policy"]["policy_version"],
            "policy_hash": stored["policy"]["policy_hash"],
        }
        if request.policy_ref != expected_ref:
            return ReservationResult(ReservationOutcome.CONFLICT)

        try:
            advanced = self._commit(stored, request)
        except ReservationStoreError:
            # Any store failure fails closed as retryable-unavailable, never as a
            # false precondition failure and never as a false success.
            return ReservationResult(ReservationOutcome.UNAVAILABLE)

        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, advanced)
        self._by_state_id[request.state_id] = deepcopy(advanced)
        return ReservationResult(ReservationOutcome.RESERVED, window_state=advanced)

    def _commit(self, stored: dict[str, Any], request: ReservationRequest) -> dict[str, Any]:
        """Return the next window-state snapshot with the reservation applied.

        Overridable seam so a subclass can model store unavailability. The real
        DynamoDB store performs this as one conditional ``UpdateItem`` fenced on
        ``state_revision``.
        """
        advanced = deepcopy(stored)
        advanced["state_revision"] = stored["state_revision"] + 1
        advanced["window_micro_usd"] = stored["window_micro_usd"] + request.action_micro_usd
        advanced["action_micro_usd"] = request.action_micro_usd
        advanced["writes_in_window"] = stored["writes_in_window"] + 1
        advanced["in_flight"] = stored["in_flight"] + 1
        advanced["as_of_epoch_seconds"] = request.now_epoch_seconds
        advanced["last_write_epoch_seconds"] = request.now_epoch_seconds

        previous = stored["last_change_direction"]
        proposed = request.change_direction
        if proposed != "none" and previous != "none" and proposed != previous:
            advanced["direction_flips_in_window"] = stored["direction_flips_in_window"] + 1
        if proposed != "none":
            advanced["last_change_direction"] = proposed

        advanced["state_hash"] = autonomy_window_state_hash(advanced)
        return advanced
