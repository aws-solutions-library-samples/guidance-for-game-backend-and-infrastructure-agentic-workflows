"""Persistence sinks for the E0 latency harness (issue #412).

A live provider-read measurement exposed that the harness's *default* persistence
callable is a no-op: it measures only canonical serialization, not a real durable
write. That does not meet the durable acceptance boundary in ADR 0005, which
requires representative persistence of the observation state transition and its
ledger events **alongside** the three real GameLift reads before a measured p99
can be accepted.

This module supplies two explicit, mutually exclusive persistence modes for the
runner:

* :class:`InMemoryPersistenceSink` -- a deterministic, in-process sink used by
  **unit tests only**. It performs canonical serialization and records the bytes
  it would have written, but it issues no I/O. It is *explicitly not acceptable*
  as live ADR evidence (``acceptable_for_live_evidence`` is ``False``): a run
  that used it must never be reported as a synchronous acceptance.

* :class:`DynamoDbTransactionalSink` -- an **opt-in, real** DynamoDB write. Each
  sample writes a synthetic, per-sample **unique** operation-state item and an
  append-only ledger item to a *caller-specified, task-owned, disposable* table
  in **one** :meth:`TransactWriteItems` call, using conditional
  no-replacement (``attribute_not_exists``) semantics so a write never overwrites
  an existing item. It canonicalizes each item (RFC 8785) inside the measured
  persistence path, enforces the persistence deadline, and writes **only**
  synthetic data: never a fleet id, an account id, an ARN, or any provider
  payload. This is the only mode acceptable for live ADR evidence.

Table contract (minimal; see ``docs/evidence/README.md``): a single table with a
partition key ``PK`` (string) and a sort key ``SK`` (string), billing mode
``PAY_PER_REQUEST`` (on-demand), and an optional numeric ``ttl`` attribute
configured as the table's Time to Live attribute. The harness performs no
``Scan`` and no read; it issues only conditional puts. The final production
operations table (ADR 0005) additionally requires PITR, encryption, and access
logs/metrics -- those are provisioned and reviewed separately and are out of
scope for this disposable spike.
"""

from __future__ import annotations

# Standard library
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# Local modules
from operations.contracts.canonical import canonical_sha256, canonicalize
from operations.validation.e0_latency import DeadlineExceededError

# Public, stable labels recorded verbatim in the evidence document's
# ``assumptions.persistence_mode`` field. The evidence evaluator keys synchronous
# acceptance off ``DYNAMODB_TRANSACT``.
MODE_IN_MEMORY = "in-memory-deterministic"
MODE_DYNAMODB_TRANSACT = "dynamodb-transactional"

# The synthetic operation state and ledger event this spike persists. These are
# fixed, non-secret, non-provider constants: the write path exercises the
# transactional cost of one state transition plus one append-only ledger event,
# exactly as ADR 0005 describes, without ever touching real operation data.
_SYNTHETIC_STATE = "observed"
_SYNTHETIC_LEDGER_EVENT = "provider_read_completed"
_CONTRACT_VERSION = "1.0"

# Item-shape constants for the minimal PK/SK table contract.
_PK_PREFIX = "OP#"
_STATE_SK = "STATE#0"
_LEDGER_SK_PREFIX = "LEDGER#"


class PersistenceSink(Protocol):
    """A persistence sink invoked once per observation sample.

    The runner calls :meth:`persist` inside the measured persistence phase with
    the observation ``record`` it built and that record's RFC 8785 canonical
    bytes. Implementations must raise :class:`DeadlineExceededError` (or let the
    runner's own post-phase check fire) when they exceed their deadline, and must
    never persist a fleet id, account id, ARN, or provider payload.
    """

    #: Public, stable mode label recorded in the evidence document.
    mode: str
    #: Whether a run using this sink may be reported as live ADR evidence.
    acceptable_for_live_evidence: bool

    def persist(self, record: dict[str, Any], canonical_bytes: bytes) -> None:
        """Persist one observation record. Raises on overrun; returns None."""
        ...


@dataclass
class InMemoryPersistenceSink:
    """Deterministic, in-process persistence sink for **unit tests only**.

    Canonicalizes the record (so the measured path still includes canonical
    serialization) and records the bytes it would have written. It performs no
    I/O and is **not** acceptable as live ADR evidence.
    """

    mode: str = MODE_IN_MEMORY
    acceptable_for_live_evidence: bool = False
    written: list[bytes] = field(default_factory=list)

    def persist(self, record: dict[str, Any], canonical_bytes: bytes) -> None:
        # Re-canonicalize to keep the deterministic mode's measured work
        # comparable to the real mode's serialization step; store, do not emit.
        self.written.append(canonicalize(record))


@dataclass
class DynamoDbTransactionalSink:
    """Real, opt-in DynamoDB transactional persistence for one disposable table.

    Each :meth:`persist` writes, in **one** ``TransactWriteItems`` call:

    1. a synthetic per-sample **unique** operation-state item
       (``PK = OP#<uuid4>``, ``SK = STATE#0``), conditional on
       ``attribute_not_exists(PK)`` so it never replaces an existing item; and
    2. the matching append-only ledger item (same ``PK``, ``SK = LEDGER#0``),
       conditional on ``attribute_not_exists(SK)`` so a ledger event is never
       overwritten.

    Both items carry only synthetic, bounded values plus an optional numeric
    ``ttl`` for safe expiry of leftover disposable rows. No fleet id, account id,
    ARN, or provider payload is ever written. The two items canonicalized
    payloads are serialized (RFC 8785) inside the measured persistence path.

    The sink enforces its own persistence deadline: it computes a wall-clock
    budget from the injected clock and raises a typed retryable
    :class:`DeadlineExceededError` if the transactional write exceeds it, so a
    slow table surfaces as a fail-closed timeout rather than a silent long wait.
    """

    #: A boto3/botocore DynamoDB client (or a compatible fake in tests). Only
    #: ``transact_write_items`` is ever called; the sink issues no read and no
    #: ``Scan``.
    client: Any
    #: The caller-specified, task-owned, disposable table name.
    table_name: str
    #: The persistence budget in seconds; the transactional write must complete
    #: within it or the sink fails closed.
    persistence_budget_s: float
    #: Optional TTL horizon (seconds from now). When set, each item carries a
    #: numeric ``ttl`` epoch-second attribute so leftover disposable rows expire.
    ttl_seconds: int | None = None
    #: Clock used to measure and enforce the persistence deadline. Injected so
    #: tests can drive it deterministically; defaults to the monotonic clock.
    clock: Callable[[], float] = time.monotonic
    #: Wall-clock source for the TTL epoch value. Injected for determinism.
    epoch_clock: Callable[[], float] = time.time

    mode: str = MODE_DYNAMODB_TRANSACT
    acceptable_for_live_evidence: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.table_name, str) or not self.table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if not (self.persistence_budget_s > 0):
            raise ValueError("persistence_budget_s must be positive")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds, when set, must be positive")

    def persist(self, record: dict[str, Any], canonical_bytes: bytes) -> None:
        start = self.clock()

        operation_id = self._operation_id(record)
        pk = f"{_PK_PREFIX}{operation_id}"

        state_item = self._state_item(pk, operation_id, canonical_bytes)
        ledger_item = self._ledger_item(pk, operation_id, canonical_bytes)

        # Canonical serialization of each persisted item, inside the measured
        # path. The digests are stored on the items so a reader can confirm the
        # persisted payload without exposing any provider data.
        state_item["canonical_sha256"] = canonical_sha256(state_item)
        ledger_item["canonical_sha256"] = canonical_sha256(ledger_item)

        transact_items = [
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": _to_dynamodb_item(state_item),
                    # No-replacement: never overwrite an existing operation item.
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": _to_dynamodb_item(ledger_item),
                    # Append-only: never overwrite an existing ledger event.
                    "ConditionExpression": "attribute_not_exists(SK)",
                }
            },
        ]

        # Enforce the persistence deadline defensively before issuing the write:
        # if the clock has already consumed the budget (e.g. a slow serialize),
        # fail closed rather than start a write that cannot finish in time.
        elapsed_before = self.clock() - start
        if elapsed_before > self.persistence_budget_s:
            raise DeadlineExceededError("persistence", elapsed_before, self.persistence_budget_s)

        self.client.transact_write_items(TransactItems=transact_items)

        elapsed = self.clock() - start
        if elapsed > self.persistence_budget_s:
            raise DeadlineExceededError("persistence", elapsed, self.persistence_budget_s)

    def _operation_id(self, record: dict[str, Any]) -> str:
        """Per-sample-unique operation id.

        Prefer an id the runner already stamped on the record so the persisted
        keys line up with the canonicalized observation; fall back to a fresh
        uuid4 so uniqueness never depends on the caller.
        """
        candidate = record.get("operation_id")
        if isinstance(candidate, str) and candidate:
            return candidate
        return str(uuid.uuid4())

    def _ttl_value(self) -> int | None:
        if self.ttl_seconds is None:
            return None
        return int(self.epoch_clock()) + self.ttl_seconds

    def _state_item(self, pk: str, operation_id: str, canonical_bytes: bytes) -> dict[str, Any]:
        item: dict[str, Any] = {
            "PK": pk,
            "SK": _STATE_SK,
            "record_type": "operation_state",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "state": _SYNTHETIC_STATE,
            "sequence": 0,
            "synthetic": True,
            "canonical_len": len(canonical_bytes),
        }
        ttl = self._ttl_value()
        if ttl is not None:
            item["ttl"] = ttl
        return item

    def _ledger_item(self, pk: str, operation_id: str, canonical_bytes: bytes) -> dict[str, Any]:
        item: dict[str, Any] = {
            "PK": pk,
            "SK": f"{_LEDGER_SK_PREFIX}0",
            "record_type": "ledger_event",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "event_type": _SYNTHETIC_LEDGER_EVENT,
            "sequence": 0,
            "synthetic": True,
            "canonical_len": len(canonical_bytes),
        }
        ttl = self._ttl_value()
        if ttl is not None:
            item["ttl"] = ttl
        return item


def _to_dynamodb_item(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Marshal a flat synthetic item to the DynamoDB low-level attribute map.

    Only the small set of scalar types this spike writes is supported: ``str``,
    ``bool``, and ``int``. Any other type is a programming error here (the items
    are built entirely in this module from synthetic constants), so it raises
    rather than silently coercing.
    """
    marshalled: dict[str, dict[str, Any]] = {}
    for key, value in item.items():
        # bool must be checked before int: bool is a subclass of int.
        if isinstance(value, bool):
            marshalled[key] = {"BOOL": value}
        elif isinstance(value, str):
            marshalled[key] = {"S": value}
        elif isinstance(value, int):
            marshalled[key] = {"N": str(value)}
        else:  # pragma: no cover - defensive; synthetic items never hit this
            raise TypeError(f"unsupported synthetic attribute type for {key!r}: {type(value).__name__}")
    return marshalled
