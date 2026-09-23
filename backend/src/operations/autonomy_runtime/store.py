"""Atomic, policy-bound reservation lifecycle for E5 autonomy (#439).

The reservation store is the durable mechanism that makes the pure #438
evaluator's reading safe under concurrency. Before an autonomous write can be
dispatched, its budget, frequency, concurrency, and anti-oscillation footprint
must be **atomically reserved** against the policy-bound rolling window state,
fenced on the exact ``state_revision``. A reservation either advances the
window-state revision by exactly one and returns the new hash-bound snapshot, or
it fails closed — never a false success and never a double count.

This module defines the single, unified reservation lifecycle the runtime, the
gate, and the autonomous execution verifier all depend on:

* :meth:`ReservationStore.reserve` — atomically reserve one write's footprint,
  fenced on ``state_revision`` and the policy reference, and — crucially —
  **idempotent on ``operation_id``**: a replay of the same operation returns the
  existing reservation and never double counts. A reservation takes the single
  in-flight concurrency slot (the window schema bounds ``in_flight`` to 1), so a
  distinct operation cannot reserve while one write is in flight.
* :meth:`ReservationStore.require` — confirm, immediately before the provider
  write, that the caller currently, atomically owns the live in-flight
  reservation for this exact ``(operation_id, logical_action_id)``. A lost,
  superseded, or already-settled reservation fails closed.
* :meth:`ReservationStore.settle` — release the in-flight slot on every
  terminal / failed handoff while **conservatively retaining** the consumed
  budget (``window_micro_usd``) and frequency (``writes_in_window``). Settlement
  never rolls those counters back, so a failed write still costs its budget and
  frequency footprint; only the concurrency slot is returned so a later
  operation may proceed. Settlement is idempotent.
* :meth:`ReservationStore.current` — read the current durable window snapshot.

``ReservationRequest`` is the identifier-and-integer-only reservation input. It
carries the policy reference, the fenced ``state_id`` / ``expected_revision``,
the bound ``operation_id`` and ``logical_action_id``, the integer micro-USD
action cost, the evaluated epoch, and the proposed change direction. It carries
**no** policy body, no limits, no observation, no credential, and no model or
request-body input.

Two implementations are provided:

* :class:`InMemoryReservationStore` — a deterministic reference implementation
  that models the DynamoDB conditional-write fencing so the fail-closed contract
  is provable without any AWS dependency.
* :class:`DynamoDbReservationStore` — the real durable store: a single
  conditional ``UpdateItem`` / ``TransactWriteItems`` fenced on ``state_revision``
  and the in-flight reservation record (no ``Scan``, no unconditional
  ``PutItem``). Constructing it captures no credential and performs no I/O until a
  method is called; default deployments provision no autonomy control plane and
  grant no provider-write permission, so it is never wired in the read-only chat
  runtime.

Nothing here reads the environment implicitly, holds a provider-write
credential, or performs a provider write. The store advances *durable accounting
state only*; the single provider write is performed elsewhere, by the existing
narrow executor, invoked solely by a durable authenticated Step Functions state
machine carrying the operation id.
"""

from __future__ import annotations

# Standard library
import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# Local modules
from operations.contracts.autonomy import autonomy_window_state_hash, validate_autonomy_contract

_WINDOW_STATE_SCHEMA = "gamelift-capacity-autonomy-window-state"
_DIRECTIONS = frozenset({"none", "increase", "decrease"})
_MAX_REVISION = 9007199254740991
_MAX_EPOCH_SECONDS = 4102444800  # matches the frozen window-state schema epoch ceiling
_MAX_TERMINAL_LENGTH = 64
_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


class ReservationStoreError(RuntimeError):
    """A structural precondition of a reservation write was violated / store failure."""


class ReservationOutcome(str, Enum):
    """The closed, authoritative outcome set of a reservation attempt."""

    RESERVED = "reserved"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


def _derive_action_id(operation_id: str) -> str:
    """Derive a stable placeholder logical action id from the operation id.

    Used only when a caller does not supply one (e.g. the locked reserve-only
    tests). The real runtime always supplies the executor's own
    ``logical_action_id`` so ``require`` and ``settle`` bind the exact write.
    """
    digest = hashlib.sha256(("act::" + operation_id).encode("utf-8")).hexdigest()
    value = int(digest, 16)
    body: list[str] = []
    for _ in range(64):
        value, index = divmod(value, 36)
        body.append(_ID_ALPHABET[index])
    return "act_" + "".join(reversed(body))


_ALLOWED_TERMINALS = frozenset({"succeeded", "failed", "reclaimed"})


def _validate_terminal(terminal: str) -> str:
    """Validate the bounded terminal reason recorded on settlement.

    The terminal reason is a short, closed token (``succeeded`` / ``failed`` /
    ``reclaimed``) persisted on the reservation record and the immutable audit
    row. An unknown or oversized value fails closed rather than being stored.
    """
    if not isinstance(terminal, str) or terminal not in _ALLOWED_TERMINALS:
        raise ReservationStoreError("terminal reason must be one of succeeded, failed, reclaimed")
    if len(terminal) > _MAX_TERMINAL_LENGTH:
        raise ReservationStoreError("terminal reason is too long")
    return terminal


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
    logical_action_id: str | None = None
    lease_not_after: int | None = None
    generation: int = 1

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
        if not isinstance(self.operation_id, str) or not self.operation_id.startswith("op_"):
            raise ValueError("operation_id must be an op_ identifier")
        if self.logical_action_id is not None and (
            not isinstance(self.logical_action_id, str) or not self.logical_action_id.startswith("act_")
        ):
            raise ValueError("logical_action_id must be an act_ identifier")
        # A bounded lease deadline, when supplied, MUST be a non-boolean integer
        # strictly after ``now`` and no later than the schema epoch ceiling. The
        # caller derives it no later than the decision expiry, so a crash leaves a
        # bounded, reclaimable lease instead of a wedged in-flight slot.
        if self.lease_not_after is not None:
            if isinstance(self.lease_not_after, bool) or not isinstance(self.lease_not_after, int):
                raise ValueError("lease_not_after must be an integer")
            if self.lease_not_after <= self.now_epoch_seconds:
                raise ValueError("lease_not_after must be strictly after now_epoch_seconds")
            if self.lease_not_after > _MAX_EPOCH_SECONDS:
                raise ValueError("lease_not_after is out of range")
        # The monotonic generation fences a stale holder once an expired lease is
        # reclaimed. It MUST be a non-boolean positive integer.
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("generation must be an integer")
        if self.generation < 1 or self.generation > _MAX_REVISION:
            raise ValueError("generation must be a positive integer")

    @property
    def action_id(self) -> str:
        """The bound logical action id (derived from the operation id if absent)."""
        return self.logical_action_id or _derive_action_id(self.operation_id)


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
    """The DynamoDB-shaped port for the atomic, revision-fenced reservation lifecycle.

    An implementation MUST advance the window-state revision by exactly one when
    (and only when) the stored revision equals ``request.expected_revision``, the
    stored policy reference equals ``request.policy_ref``, and no other write is
    in flight; otherwise it MUST return ``CONFLICT``. A replay of the same
    ``operation_id`` MUST be idempotent. Any store failure MUST return
    ``UNAVAILABLE``. It MUST never return ``RESERVED`` without a durably advanced
    revision.
    """

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        """Atomically reserve one write's footprint, fenced on state revision."""
        ...

    def require(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
        """Confirm live in-flight ownership of the reservation; fail closed otherwise.

        When ``generation`` is supplied it MUST equal the record's current
        generation, so a holder whose expired lease was reclaimed (which bumps
        the generation) is fenced.
        """
        ...

    def settle(
        self, *, operation_id: str, logical_action_id: str, terminal: str, generation: int | None = None
    ) -> ReservationResult:
        """Release the in-flight slot on a terminal handoff, retaining budget/frequency.

        Persists the terminal reason and requires the exact ``generation`` when
        supplied. On a conditional failure the durable store re-reads and returns
        success only if the slot is truly released; otherwise it fails closed.
        """
        ...

    def current(self, state_id: str) -> dict[str, Any]:
        """Return the current durable window-state snapshot for ``state_id``."""
        ...


def _next_reserved(stored: dict[str, Any], request: ReservationRequest) -> dict[str, Any]:
    """Return the next window snapshot with one reservation applied.

    Shared by both store implementations. The real DynamoDB store applies this as
    one conditional ``UpdateItem`` fenced on ``state_revision`` and ``in_flight``.
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


def _next_released(stored: dict[str, Any]) -> dict[str, Any]:
    """Return the next snapshot with the in-flight slot released.

    Budget (``window_micro_usd``) and frequency (``writes_in_window``) are
    deliberately RETAINED: a failed or terminal write still consumes its
    rolling-window footprint. Only the concurrency slot returns to 0 so a later
    operation may proceed. The revision advances by exactly one.
    """
    advanced = deepcopy(stored)
    advanced["state_revision"] = stored["state_revision"] + 1
    advanced["in_flight"] = 0
    advanced["state_hash"] = autonomy_window_state_hash(advanced)
    return advanced


@dataclass(slots=True)
class _Reservation:
    """A live (or settled) in-flight reservation record keyed by operation id."""

    operation_id: str
    logical_action_id: str
    state_id: str
    settled: bool
    generation: int = 1
    lease_not_after: int | None = None
    terminal: str | None = None


class InMemoryReservationStore:
    """Deterministic reference store modelling DynamoDB conditional-write fencing.

    Holds exactly one durable window-state snapshot keyed by ``state_id`` and a
    set of reservation records keyed by ``operation_id``. A reservation is
    accepted only when the stored revision and policy reference match the request
    AND no write is in flight; the commit advances the revision by exactly one,
    adds the action cost to the window spend, increments the write count, takes
    the single in-flight concurrency slot, updates the last-write time and change
    direction, and re-binds the ``state_hash``. A replay of the same operation is
    idempotent. Everything else fails closed.
    """

    __slots__ = ("_by_state_id", "_reservations")

    def __init__(self, *, initial_state: dict[str, Any]) -> None:
        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, initial_state)
        self._by_state_id: dict[str, dict[str, Any]] = {initial_state["state_id"]: deepcopy(initial_state)}
        self._reservations: dict[str, _Reservation] = {}

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

        # Idempotent operation ownership: a replay of the same operation returns
        # the existing reservation and NEVER double counts.
        existing = self._reservations.get(request.operation_id)
        if existing is not None:
            if existing.logical_action_id != request.action_id or existing.state_id != request.state_id:
                # The same operation id bound to a different action/state is a
                # forgery/collision — fail closed rather than silently accept.
                return ReservationResult(ReservationOutcome.CONFLICT)
            return ReservationResult(ReservationOutcome.RESERVED, window_state=deepcopy(stored))

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

        # Concurrency guard: only one write may be in flight at a time.
        if stored["in_flight"] != 0:
            return ReservationResult(ReservationOutcome.CONFLICT)

        try:
            advanced = self._commit(stored, request)
        except ReservationStoreError:
            # Any store failure fails closed as retryable-unavailable, never as a
            # false precondition failure and never as a false success.
            return ReservationResult(ReservationOutcome.UNAVAILABLE)

        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, advanced)
        self._by_state_id[request.state_id] = deepcopy(advanced)
        self._reservations[request.operation_id] = _Reservation(
            operation_id=request.operation_id,
            logical_action_id=request.action_id,
            state_id=request.state_id,
            settled=False,
            generation=request.generation,
            lease_not_after=request.lease_not_after,
            terminal=None,
        )
        return ReservationResult(ReservationOutcome.RESERVED, window_state=advanced)

    def require(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
        reservation = self._reservations.get(operation_id)
        if reservation is None:
            raise ReservationStoreError("no reservation is owned for this operation")
        if reservation.logical_action_id != logical_action_id:
            raise ReservationStoreError("reservation action does not match")
        if generation is not None and reservation.generation != generation:
            # A stale holder whose expired lease was reclaimed (generation bumped)
            # is fenced from confirming ownership.
            raise ReservationStoreError("reservation generation does not match")
        if reservation.settled:
            raise ReservationStoreError("reservation has already been settled")
        stored = self._by_state_id.get(reservation.state_id)
        if stored is None or stored["in_flight"] != 1:
            raise ReservationStoreError("reservation is no longer in flight")

    def settle(
        self, *, operation_id: str, logical_action_id: str, terminal: str, generation: int | None = None
    ) -> ReservationResult:
        _validate_terminal(terminal)
        reservation = self._reservations.get(operation_id)
        if reservation is None:
            raise ReservationStoreError("no reservation is owned for this operation")
        if reservation.logical_action_id != logical_action_id:
            raise ReservationStoreError("reservation action does not match")
        if generation is not None and reservation.generation != generation:
            # A stale holder whose expired lease was reclaimed is fenced from
            # settling: it no longer owns the (already reclaimed) slot.
            raise ReservationStoreError("reservation generation does not match")
        stored = self._by_state_id.get(reservation.state_id)
        if stored is None:
            raise ReservationStoreError("reservation window state is unknown")

        # Idempotent: a second settle does not release twice or advance again.
        if reservation.settled:
            return ReservationResult(ReservationOutcome.RESERVED, window_state=deepcopy(stored))

        try:
            advanced = self._release(stored)
        except ReservationStoreError:
            return ReservationResult(ReservationOutcome.UNAVAILABLE)

        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, advanced)
        self._by_state_id[reservation.state_id] = deepcopy(advanced)
        reservation.settled = True
        reservation.terminal = terminal
        return ReservationResult(ReservationOutcome.RESERVED, window_state=advanced)

    def reservation_record(self, operation_id: str) -> dict[str, Any] | None:
        """Return a read-only view of a reservation record, or ``None`` if absent.

        Exposes the lease deadline, generation, settled flag, and terminal reason
        for recovery/audit tooling. It carries no policy body, no credential, and
        no provider surface.
        """
        reservation = self._reservations.get(operation_id)
        if reservation is None:
            return None
        return {
            "operation_id": reservation.operation_id,
            "logical_action_id": reservation.logical_action_id,
            "state_id": reservation.state_id,
            "generation": reservation.generation,
            "lease_not_after": reservation.lease_not_after,
            "settled": reservation.settled,
            "terminal": reservation.terminal,
        }

    def sweep_expired(self, *, state_id: str, now_epoch_seconds: int) -> bool:
        """Reclaim the in-flight slot of an EXPIRED lease; fence the stale holder.

        Recovery path for a crash between reserve and settle: a live lease is
        never reclaimed, but once the bounded ``lease_not_after`` deadline has
        passed the slot is released (budget/frequency retained, exactly as a
        settle) and the reclaimed record's generation is bumped so the original
        holder that later returns is fenced from require/settle. Returns whether a
        slot was reclaimed.
        """
        stored = self._by_state_id.get(state_id)
        if stored is None or stored["in_flight"] == 0:
            return False
        holder = next(
            (
                r
                for r in self._reservations.values()
                if r.state_id == state_id and not r.settled and r.lease_not_after is not None
            ),
            None,
        )
        if holder is None or holder.lease_not_after is None or holder.lease_not_after > now_epoch_seconds:
            # No holder, an unbounded lease, or a still-live lease: never reclaim.
            return False
        advanced = self._release(stored)
        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, advanced)
        self._by_state_id[state_id] = deepcopy(advanced)
        holder.settled = True
        holder.terminal = "reclaimed"
        holder.generation = holder.generation + 1
        return True

    def _commit(self, stored: dict[str, Any], request: ReservationRequest) -> dict[str, Any]:
        """Return the next reserved snapshot. Overridable seam to model failure."""
        return _next_reserved(stored, request)

    def _release(self, stored: dict[str, Any]) -> dict[str, Any]:
        """Return the next released snapshot. Overridable seam to model failure."""
        return _next_released(stored)


# -- Real durable store -------------------------------------------------------

_LOGGER = logging.getLogger(__name__)
_STATE_SK = "AUTZWINDOW"
_RESERVATION_SK = "AUTZRSV"
_AUDIT_PK_PREFIX = "AUTZAUDIT#"
_CONDITIONAL_REASON = "ConditionalCheckFailed"
_TRANSACTION_CANCELED_CODE = "TransactionCanceledException"
_BARE_CONDITIONAL_CODE = "ConditionalCheckFailedException"
_TRANSIENT_REASONS = frozenset(
    {"TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded", "ValidationError"}
)


def _cancellation_reason_codes(response: dict[str, Any]) -> set[str] | None:
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return None
    return {r.get("Code") for r in reasons if isinstance(r, dict)}


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = response.get("Error", {}).get("Code", "")
    if code == _BARE_CONDITIONAL_CODE:
        return True
    if code != _TRANSACTION_CANCELED_CODE:
        return False
    reason_codes = _cancellation_reason_codes(response)
    if reason_codes is None:
        return False
    if reason_codes & _TRANSIENT_REASONS:
        return False
    return _CONDITIONAL_REASON in reason_codes


def _marshal(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: _marshal_value(value) for key, value in item.items()}


def _marshal_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if value is None:
        return {"NULL": True}
    raise TypeError(f"unsupported attribute type: {type(value).__name__}")


def _unmarshal(item: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, cell in item.items():
        if "S" in cell:
            out[key] = cell["S"]
        elif "N" in cell:
            out[key] = int(cell["N"])
        elif "BOOL" in cell:
            out[key] = bool(cell["BOOL"])
        elif "NULL" in cell:
            out[key] = None
    return out


def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DynamoDbReservationStore:
    """Conditional, revision-fenced, idempotent durable reservation store.

    Item layout (single-table):

    * window snapshot at ``PK=AUTZ#<state_id>, SK=AUTZWINDOW`` carrying the
      canonical window-state ``document`` and denormalized ``state_revision`` /
      ``in_flight`` for the conditional fence;
    * one reservation record per operation at
      ``PK=AUTZRSV#<operation_id>, SK=AUTZRSV`` carrying ``logical_action_id``,
      ``state_id``, and ``settled`` — read directly by operation id, so
      ``require`` / ``settle`` never need a ``Scan``.

    ``reserve`` performs one ``TransactWriteItems`` that conditionally puts the
    reservation record (idempotency: ``attribute_not_exists`` — a replay is a
    conditional failure that resolves to the recorded reservation) and
    conditionally updates the window snapshot fenced on the exact
    ``state_revision`` and ``in_flight == 0``. ``settle`` conditionally releases
    ``in_flight`` back to 0 (retaining budget/frequency) and flips the
    reservation record's ``settled`` flag. No ``Scan`` and no unconditional
    ``PutItem`` is ever issued; every failure fails closed.
    """

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: Any, table_name: str) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._client = client
        self._table_name = table_name

    def current(self, state_id: str) -> dict[str, Any]:
        item = self._get(self._state_pk(state_id), _STATE_SK)
        if item is None:
            raise ReservationStoreError(f"unknown state_id: {state_id}")
        document = item.get("document")
        if not isinstance(document, str):
            raise ReservationStoreError("stored window state is malformed")
        try:
            parsed = json.loads(document)
        except (ValueError, TypeError) as exc:
            raise ReservationStoreError("stored window state is malformed") from exc
        if not isinstance(parsed, dict):
            raise ReservationStoreError("stored window state is malformed")
        return parsed

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        try:
            stored = self.current(request.state_id)
        except ReservationStoreError:
            return ReservationResult(ReservationOutcome.CONFLICT)

        # Idempotent operation ownership.
        record = self._get(self._reservation_pk(request.operation_id), _RESERVATION_SK)
        if record is not None:
            if record.get("logical_action_id") != request.action_id:
                return ReservationResult(ReservationOutcome.CONFLICT)
            return ReservationResult(ReservationOutcome.RESERVED, window_state=stored)

        if stored["state_revision"] != request.expected_revision:
            return ReservationResult(ReservationOutcome.CONFLICT)
        expected_ref = {
            "policy_id": stored["policy"]["policy_id"],
            "policy_version": stored["policy"]["policy_version"],
            "policy_hash": stored["policy"]["policy_hash"],
        }
        if request.policy_ref != expected_ref:
            return ReservationResult(ReservationOutcome.CONFLICT)
        if stored["in_flight"] != 0:
            return ReservationResult(ReservationOutcome.CONFLICT)

        advanced = _next_reserved(stored, request)
        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, advanced)

        reservation_item = _marshal(
            {
                "PK": self._reservation_pk(request.operation_id),
                "SK": _RESERVATION_SK,
                "operation_id": request.operation_id,
                "logical_action_id": request.action_id,
                "state_id": request.state_id,
                "settled": False,
                "generation": request.generation,
                "lease_not_after": request.lease_not_after if request.lease_not_after is not None else 0,
                "terminal": None,
            }
        )
        window_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": self._state_pk(request.state_id), "SK": _STATE_SK}),
                "UpdateExpression": "SET document = :doc, state_revision = :next, in_flight = :one",
                "ConditionExpression": "state_revision = :expected AND in_flight = :zero",
                "ExpressionAttributeValues": _marshal(
                    {
                        ":doc": _canonical(advanced),
                        ":next": advanced["state_revision"],
                        ":one": 1,
                        ":expected": request.expected_revision,
                        ":zero": 0,
                    }
                ),
            }
        }
        audit_put = self._audit_put(
            operation_id=request.operation_id,
            event="autonomy_reserved",
            generation=request.generation,
            logical_action_id=request.action_id,
            state_id=request.state_id,
            terminal=None,
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": reservation_item,
                            "ConditionExpression": "attribute_not_exists(SK)",
                        }
                    },
                    window_update,
                    audit_put,
                ]
            )
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if _is_conditional_failure(exc):
                # The transaction lost a race. Re-read the reservation record and
                # CONVERGE to it only when it is the SAME operation bound to the
                # same action/state/generation (a concurrent same-operation replay
                # that we lost). Anything else — a different action/generation, a
                # missing record, or the revision/in_flight fence losing to a
                # distinct operation — fails closed as a conflict, never a false
                # success.
                return self._converge_reserve(request)
            _LOGGER.warning("dynamodb_reservation_store reserve unavailable exception_type=%s", type(exc).__name__)
            return ReservationResult(ReservationOutcome.UNAVAILABLE)
        return ReservationResult(ReservationOutcome.RESERVED, window_state=advanced)

    def _converge_reserve(self, request: ReservationRequest) -> ReservationResult:
        """Re-read after a lost reserve transaction; converge only on an exact replay."""
        try:
            record = self._get(self._reservation_pk(request.operation_id), _RESERVATION_SK)
        except ReservationStoreError:
            return ReservationResult(ReservationOutcome.UNAVAILABLE)
        if (
            record is not None
            and record.get("logical_action_id") == request.action_id
            and record.get("state_id") == request.state_id
            and record.get("generation") == request.generation
        ):
            try:
                return ReservationResult(ReservationOutcome.RESERVED, window_state=self.current(request.state_id))
            except ReservationStoreError:
                return ReservationResult(ReservationOutcome.UNAVAILABLE)
        return ReservationResult(ReservationOutcome.CONFLICT)

    def require(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
        record = self._find_reservation(operation_id, logical_action_id, generation=generation)
        stored = self.current(record["state_id"])
        if stored["in_flight"] != 1:
            raise ReservationStoreError("reservation is no longer in flight")

    def settle(
        self, *, operation_id: str, logical_action_id: str, terminal: str, generation: int | None = None
    ) -> ReservationResult:
        _validate_terminal(terminal)
        record = self._find_reservation(operation_id, logical_action_id, allow_settled=True, generation=generation)
        state_id = record["state_id"]
        record_generation = int(record.get("generation", 1))
        stored = self.current(state_id)
        if record.get("settled") is True:
            return ReservationResult(ReservationOutcome.RESERVED, window_state=stored)

        advanced = _next_released(stored)
        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, advanced)

        window_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": self._state_pk(state_id), "SK": _STATE_SK}),
                "UpdateExpression": "SET document = :doc, state_revision = :next, in_flight = :zero",
                "ConditionExpression": "state_revision = :expected AND in_flight = :one",
                "ExpressionAttributeValues": _marshal(
                    {
                        ":doc": _canonical(advanced),
                        ":next": advanced["state_revision"],
                        ":zero": 0,
                        ":expected": stored["state_revision"],
                        ":one": 1,
                    }
                ),
            }
        }
        reservation_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": self._reservation_pk(operation_id), "SK": _RESERVATION_SK}),
                "UpdateExpression": "SET settled = :true, terminal = :terminal",
                "ConditionExpression": "attribute_exists(SK) AND settled = :false AND generation = :gen",
                "ExpressionAttributeValues": _marshal(
                    {":true": True, ":false": False, ":terminal": terminal, ":gen": record_generation}
                ),
            }
        }
        audit_put = self._audit_put(
            operation_id=operation_id,
            event="autonomy_settled",
            generation=record_generation,
            logical_action_id=logical_action_id,
            state_id=state_id,
            terminal=terminal,
        )
        try:
            self._client.transact_write_items(TransactItems=[window_update, reservation_update, audit_put])
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if _is_conditional_failure(exc):
                # Never fabricate success on a conditional failure. Re-read BOTH the
                # reservation record and the window state, and return success only
                # when the slot is TRULY released (in_flight == 0) and the record is
                # settled — i.e. a genuine concurrent settle. Otherwise fail closed.
                return self._converge_settle(operation_id, state_id)
            _LOGGER.warning("dynamodb_reservation_store settle unavailable exception_type=%s", type(exc).__name__)
            return ReservationResult(ReservationOutcome.UNAVAILABLE)
        return ReservationResult(ReservationOutcome.RESERVED, window_state=advanced)

    def _converge_settle(self, operation_id: str, state_id: str) -> ReservationResult:
        """Re-read after a lost settle transaction; success only if truly settled."""
        try:
            record = self._get(self._reservation_pk(operation_id), _RESERVATION_SK)
            stored = self.current(state_id)
        except ReservationStoreError:
            return ReservationResult(ReservationOutcome.UNAVAILABLE)
        if record is not None and record.get("settled") is True and stored.get("in_flight") == 0:
            return ReservationResult(ReservationOutcome.RESERVED, window_state=stored)
        # The slot is still held / the record is not settled: this settle lost a
        # race it must not paper over. Fail closed as a conflict.
        return ReservationResult(ReservationOutcome.CONFLICT)

    def sweep_expired(self, *, operation_id: str, now_epoch_seconds: int) -> bool:
        """Reclaim the in-flight slot of an EXPIRED lease; fence the stale holder.

        Recovery path for a crash between reserve and settle. Reads the reservation
        record by id; if it is unsettled, holds a bounded ``lease_not_after`` that
        has passed, and still owns the in-flight slot, one conditional transaction
        releases the slot (budget/frequency retained) and marks the record settled
        with the ``reclaimed`` terminal fenced on the exact generation. A live lease
        is never reclaimed. Returns whether a slot was reclaimed. Fails closed
        (never a false reclaim) on any conditional failure or unavailability.
        """
        if not isinstance(now_epoch_seconds, int) or isinstance(now_epoch_seconds, bool) or now_epoch_seconds < 0:
            raise ReservationStoreError("now_epoch_seconds must be a non-negative integer")
        record = self._get(self._reservation_pk(operation_id), _RESERVATION_SK)
        if record is None or record.get("settled") is True:
            return False
        lease = record.get("lease_not_after")
        if not isinstance(lease, int) or lease <= 0 or lease > now_epoch_seconds:
            # No bounded lease, or the lease is still live: never reclaim.
            return False
        state_id = record.get("state_id")
        if not isinstance(state_id, str):
            raise ReservationStoreError("reservation record is malformed")
        record_generation = int(record.get("generation", 1))
        stored = self.current(state_id)
        if stored.get("in_flight") != 1:
            return False
        advanced = _next_released(stored)
        validate_autonomy_contract(_WINDOW_STATE_SCHEMA, advanced)
        window_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": self._state_pk(state_id), "SK": _STATE_SK}),
                "UpdateExpression": "SET document = :doc, state_revision = :next, in_flight = :zero",
                "ConditionExpression": "state_revision = :expected AND in_flight = :one",
                "ExpressionAttributeValues": _marshal(
                    {
                        ":doc": _canonical(advanced),
                        ":next": advanced["state_revision"],
                        ":zero": 0,
                        ":expected": stored["state_revision"],
                        ":one": 1,
                    }
                ),
            }
        }
        # Bump the generation so the original holder that later returns is fenced
        # from require/settle, and mark it reclaimed.
        reservation_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": self._reservation_pk(operation_id), "SK": _RESERVATION_SK}),
                "UpdateExpression": "SET settled = :true, terminal = :terminal, generation = :nextgen",
                "ConditionExpression": "attribute_exists(SK) AND settled = :false AND generation = :gen",
                "ExpressionAttributeValues": _marshal(
                    {
                        ":true": True,
                        ":false": False,
                        ":terminal": "reclaimed",
                        ":gen": record_generation,
                        ":nextgen": record_generation + 1,
                    }
                ),
            }
        }
        audit_put = self._audit_put(
            operation_id=operation_id,
            event="autonomy_reclaimed",
            generation=record_generation,
            logical_action_id=str(record.get("logical_action_id", "")),
            state_id=state_id,
            terminal="reclaimed",
        )
        try:
            self._client.transact_write_items(TransactItems=[window_update, reservation_update, audit_put])
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if _is_conditional_failure(exc):
                # Someone else reclaimed/settled first: not our reclaim, fail closed.
                return False
            _LOGGER.warning("dynamodb_reservation_store sweep unavailable exception_type=%s", type(exc).__name__)
            return False
        return True

    def _audit_put(
        self,
        *,
        operation_id: str,
        event: str,
        generation: int,
        logical_action_id: str,
        state_id: str,
        terminal: str | None,
    ) -> dict[str, Any]:
        """Build a bounded, immutable audit-record Put for one lifecycle event.

        The record is keyed by ``(AUTZAUDIT#<operation_id>, <event>#<generation>)``
        and written with ``attribute_not_exists(SK)`` so it is append-only and
        idempotent on replay. It carries only identifiers and the bounded terminal
        reason — no policy body, no observation, no credential.
        """
        item = {
            "PK": f"{_AUDIT_PK_PREFIX}{operation_id}",
            "SK": f"{event}#{generation}",
            "operation_id": operation_id,
            "event": event,
            "generation": generation,
            "logical_action_id": logical_action_id,
            "state_id": state_id,
            "terminal": terminal,
        }
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": _marshal(item),
                "ConditionExpression": "attribute_not_exists(SK)",
            }
        }

    # -- Low-level helpers -----------------------------------------------

    def _find_reservation(
        self,
        operation_id: str,
        logical_action_id: str,
        *,
        allow_settled: bool = False,
        generation: int | None = None,
    ) -> dict[str, Any]:
        record = self._get(self._reservation_pk(operation_id), _RESERVATION_SK)
        if record is None:
            raise ReservationStoreError("no reservation is owned for this operation")
        if record.get("logical_action_id") != logical_action_id:
            raise ReservationStoreError("reservation action does not match")
        if generation is not None and record.get("generation") != generation:
            # A stale holder whose expired lease was reclaimed (generation bumped)
            # is fenced from confirming or settling ownership.
            raise ReservationStoreError("reservation generation does not match")
        if not allow_settled and record.get("settled") is True:
            raise ReservationStoreError("reservation has already been settled")
        if not isinstance(record.get("state_id"), str):
            raise ReservationStoreError("reservation record is malformed")
        return record

    def reservation_record(self, operation_id: str) -> dict[str, Any] | None:
        """Return the durable reservation record by id, or ``None`` if absent."""
        return self._get(self._reservation_pk(operation_id), _RESERVATION_SK)

    def _state_pk(self, state_id: str) -> str:
        return f"AUTZ#{state_id}"

    def _reservation_pk(self, operation_id: str) -> str:
        return f"AUTZRSV#{operation_id}"

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_marshal({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, dict) else None
        return _unmarshal(item) if isinstance(item, dict) else None


_BUNDLE_SK = "AUTZBUNDLE"


class AutonomyBundleStoreError(RuntimeError):
    """Persisting or reloading the immutable v2 bundle failed (fail closed).

    Raised when a conditional immutable put is refused because a *different*
    bundle already exists for the operation id (a crash/replay must never mutate
    persisted evidence), when the store is unavailable, or when a reloaded record
    is structurally malformed. It is never raised for an identical idempotent
    re-persist, which resolves cleanly.
    """


class DynamoDbAutonomyBundleStore:
    """Durable, immutable, conditional persistence of the reloadable v2 bundle.

    The bundle is the exact evidence set the executor reloads by ``operation_id``
    to run the v2 autonomous path: the resolved policy, the canonical E1
    observation, the deterministic decision, the immutable prepared operation, the
    bound pre-reservation window state, and the reservation reference. It is
    written to the existing single (issue #06) table as ONE canonical-JSON record
    at ``PK=AUTZBUNDLE#<operation_id>, SK=AUTZBUNDLE``.

    Immutability by conditional put
    -------------------------------
    ``persist_bundle`` issues a single ``PutItem`` conditioned on
    ``attribute_not_exists(SK)``. The first persist wins; a later persist of a
    *different* bundle is refused by the condition and raises
    :class:`AutonomyBundleStoreError` **without overwriting** the original — so a
    crash between persist and reserve leaves inert, immutable evidence that a
    replay can neither mutate nor escalate. An identical re-persist (byte-identical
    canonical form) is idempotent: the conditional refusal is reconciled against
    the stored bytes and resolves cleanly.

    Constructing the store captures no credential and performs no I/O until a
    method is called. It performs no provider write and holds no GameLift or
    executor credential; the executor is the sole provider writer.
    """

    __slots__ = ("_client", "_table_name")

    _SECTIONS = ("policy", "observation", "decision", "operation", "window_state", "reservation")

    def __init__(self, *, client: Any, table_name: str) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._client = client
        self._table_name = table_name

    def persist_bundle(
        self,
        *,
        operation_id: str,
        policy: dict[str, Any],
        observation: dict[str, Any],
        decision: dict[str, Any],
        operation: dict[str, Any],
        window_state: dict[str, Any],
        reservation: dict[str, Any],
    ) -> None:
        """Immutably persist the reloadable bundle; fail closed on any mutation attempt."""
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        document = {
            "policy": policy,
            "observation": observation,
            "decision": decision,
            "operation": operation,
            "window_state": window_state,
            "reservation": reservation,
        }
        canonical = _canonical(document)
        item = {
            "PK": {"S": self._bundle_pk(operation_id)},
            "SK": {"S": _BUNDLE_SK},
            "operation_id": {"S": operation_id},
            "bundle": {"S": canonical},
        }
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
        except Exception as exc:  # noqa: BLE001 - classify by conditional vs unavailable
            if _is_conditional_failure(exc):
                # A record already exists. An identical re-persist is idempotent;
                # a differing one must never overwrite the immutable original.
                existing = self._raw_bundle(operation_id)
                if existing is not None and existing == canonical:
                    return
                raise AutonomyBundleStoreError("bundle already exists and differs; refusing to overwrite") from exc
            _LOGGER.warning("dynamodb_autonomy_bundle_store persist unavailable exception_type=%s", type(exc).__name__)
            raise AutonomyBundleStoreError("bundle store is unavailable") from exc

    def load_bundle(self, operation_id: str) -> dict[str, Any] | None:
        """Reload the immutable bundle by operation id, or ``None`` if absent."""
        canonical = self._raw_bundle(operation_id)
        if canonical is None:
            return None
        try:
            parsed = json.loads(canonical)
        except (ValueError, TypeError) as exc:
            raise AutonomyBundleStoreError("stored bundle is malformed") from exc
        if not isinstance(parsed, dict) or any(section not in parsed for section in self._SECTIONS):
            raise AutonomyBundleStoreError("stored bundle is malformed")
        return parsed

    def dispatch_view(self, operation_id: str) -> dict[str, str]:
        """Return the identifier-only dispatch view for a persisted bundle.

        The view carries the ``operation_id`` and nothing else — no policy, no
        limits, no observation, no window state, and no credential — so nothing
        beyond the identifier ever crosses the dispatch boundary. Fails closed if
        no bundle is persisted for the id.
        """
        if self._raw_bundle(operation_id) is None:
            raise AutonomyBundleStoreError("no bundle is persisted for this operation")
        return {"operation_id": operation_id}

    # -- Low-level helpers -----------------------------------------------

    def _raw_bundle(self, operation_id: str) -> str | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": {"S": self._bundle_pk(operation_id)}, "SK": {"S": _BUNDLE_SK}},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, dict) else None
        if not isinstance(item, dict):
            return None
        cell = item.get("bundle")
        if not isinstance(cell, dict) or "S" not in cell:
            raise AutonomyBundleStoreError("stored bundle is malformed")
        return str(cell["S"])

    def _bundle_pk(self, operation_id: str) -> str:
        return f"AUTZBUNDLE#{operation_id}"
