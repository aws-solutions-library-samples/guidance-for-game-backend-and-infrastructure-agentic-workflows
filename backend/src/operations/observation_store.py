"""DynamoDB observation store: conditional/idempotent state + append-only ledger.

Implements :class:`~operations.observation.ObservationStore` for the E1 observe
phase (issue #413) following ADR 0005 ("Durable observation state without
queues"). One :meth:`record_observation` performs a single DynamoDB
``TransactWriteItems`` that writes, all or nothing:

1. the **idempotency mapping** (``PK=WS#<workspace>#IDEM#<token>``), conditional
   on absence and carrying the canonical idempotency fingerprint;
2. the **current-state snapshot** (``PK=OBS#<observation_id>``, ``SK=STATE#current``)
   at the ``observed`` state, sequence 0;
3. the immutable **initial state transition** (``SK=STATE#0``), conditional on
   ``attribute_not_exists``; and
4. the initial append-only **ledger event** (``SK=LEDGER#1``), conditional on
   ``attribute_not_exists`` so a ledger event is never overwritten.

Because the four items commit together, a partially recorded observation cannot
exist. If the mapping condition fails, a mapping already exists: the store reads
it and returns :class:`~operations.observation.ObservationCommitOutcome.REPLAY`
when the stored fingerprint matches (same token + content) or
``IDEMPOTENCY_CONFLICT`` when it differs (token reused with different content).

The store never issues a ``Scan`` or an unconditional ``PutItem``, holds no
write path to any provider, and enforces the exclusive ``commit_not_after``
deadline before issuing the transaction. Observe-phase records are transient and
carry a numeric ``ttl`` for safe expiry; the append-only guarantee comes from the
mandatory conditional puts, not from IAM alone.
"""

from __future__ import annotations

# Standard library
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

# Local modules
from operations.contracts.canonical import canonical_sha256
from operations.observation import ObservationCommit, ObservationCommitOutcome, ObservationStore

_STATE_SNAPSHOT_SK = "STATE#current"
_STATE_TRANSITION_SK = "STATE#0"
_LEDGER_SK = "LEDGER#1"
_IDEM_SK = "MAP#current"
_OBSERVED_STATE = "observed"
_CONTRACT_VERSION = "1.0"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("commit_not_after must be timezone-aware")
    return value.astimezone(timezone.utc)


class DynamoDbObservationStore(ObservationStore):
    """Conditional, idempotent, append-only observation store on one table."""

    def __init__(
        self,
        *,
        client: Any,
        table_name: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._client = client
        self._table_name = table_name
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
        deadline = _utc(commit_not_after)
        # Fail closed before issuing the write if the deadline has already passed.
        if self._clock() >= deadline:
            return ObservationCommit(ObservationCommitOutcome.DEADLINE_EXPIRED)

        observation_hash = canonical_sha256(dict(observation))
        obs_pk = f"OBS#{observation_id}"
        idem_pk = f"WS#{workspace_id}#IDEM#{idempotency_token}"

        idem_item = {
            "PK": idem_pk,
            "SK": _IDEM_SK,
            "record_type": "idempotency_mapping",
            "contract_version": _CONTRACT_VERSION,
            "observation_id": observation_id,
            "idempotency_fingerprint": idempotency_fingerprint,
            "ttl": int(ttl_epoch_s),
        }
        snapshot_item = {
            "PK": obs_pk,
            "SK": _STATE_SNAPSHOT_SK,
            "record_type": "observation_state",
            "contract_version": _CONTRACT_VERSION,
            "observation_id": observation_id,
            "state": _OBSERVED_STATE,
            "sequence": 0,
            "observation_hash": observation_hash,
            "ttl": int(ttl_epoch_s),
        }
        transition_item = {
            "PK": obs_pk,
            "SK": _STATE_TRANSITION_SK,
            "record_type": "observation_state_transition",
            "contract_version": _CONTRACT_VERSION,
            "observation_id": observation_id,
            "previous_state": None,
            "new_state": _OBSERVED_STATE,
            "sequence": 0,
            "observation_hash": observation_hash,
            "ttl": int(ttl_epoch_s),
        }
        ledger_item = {
            "PK": obs_pk,
            "SK": _LEDGER_SK,
            "record_type": "observation_ledger_event",
            "contract_version": _CONTRACT_VERSION,
            "observation_id": observation_id,
            "event_type": "observation.recorded",
            "sequence": 1,
            "observation_hash": observation_hash,
            "ttl": int(ttl_epoch_s),
        }

        transact_items = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _marshal(idem_item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _marshal(snapshot_item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _marshal(transition_item),
                    "ConditionExpression": "attribute_not_exists(SK)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _marshal(ledger_item),
                    # Append-only: never overwrite an existing ledger event.
                    "ConditionExpression": "attribute_not_exists(SK)",
                }
            },
        ]

        if self._clock() >= deadline:
            return ObservationCommit(ObservationCommitOutcome.DEADLINE_EXPIRED)

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except Exception as exc:  # noqa: BLE001 - classify by conditional-failure shape
            if not _is_conditional_failure(exc):
                # A non-conditional error is a fail-closed state conflict; the
                # caller maps it to a typed error and never treats it as success.
                return ObservationCommit(ObservationCommitOutcome.STATE_CONFLICT)
            return self._resolve_existing(idem_pk, idempotency_fingerprint)

        return ObservationCommit(ObservationCommitOutcome.RECORDED)

    def _resolve_existing(self, idem_pk: str, expected_fingerprint: str) -> ObservationCommit:
        """A mapping already exists: replay iff the fingerprint matches."""
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_marshal({"PK": idem_pk, "SK": _IDEM_SK}),
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, dict) else None
        if not item:
            # The condition failed but the mapping is gone; treat conservatively
            # as a state conflict rather than fabricating a replay.
            return ObservationCommit(ObservationCommitOutcome.STATE_CONFLICT)
        stored = _unmarshal(item)
        if stored.get("idempotency_fingerprint") == expected_fingerprint:
            observation_id = stored.get("observation_id")
            replay = self._load_observation(observation_id) if isinstance(observation_id, str) else None
            return ObservationCommit(ObservationCommitOutcome.REPLAY, replay)
        return ObservationCommit(ObservationCommitOutcome.IDEMPOTENCY_CONFLICT)

    def _load_observation(self, observation_id: str) -> dict[str, Any] | None:
        """Best-effort load of a stored snapshot for replay.

        The observe-phase snapshot stores bounded metadata only; there is no full
        observation document to rehydrate here, so replay returns ``None`` and the
        service surfaces a typed state conflict rather than a fabricated body. A
        richer replay body is future work owned by the read-model issue.
        """
        return None


def _is_conditional_failure(exc: Exception) -> bool:
    """Return whether a boto3/botocore error is a conditional-check failure."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
        if code in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
            return True
    reasons = getattr(exc, "cancellation_reasons", None)
    if isinstance(reasons, list):
        return any(isinstance(r, dict) and r.get("Code") == "ConditionalCheckFailed" for r in reasons)
    return "ConditionalCheckFailed" in str(exc)


def _marshal(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Marshal a flat item to the DynamoDB low-level attribute map."""
    marshalled: dict[str, dict[str, Any]] = {}
    for key, value in item.items():
        if value is None:
            marshalled[key] = {"NULL": True}
        elif isinstance(value, bool):  # before int: bool is a subclass of int
            marshalled[key] = {"BOOL": value}
        elif isinstance(value, str):
            marshalled[key] = {"S": value}
        elif isinstance(value, int):
            marshalled[key] = {"N": str(value)}
        else:
            raise TypeError(f"unsupported attribute type for {key!r}: {type(value).__name__}")
    return marshalled


def _unmarshal(item: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Unmarshal the small scalar subset this store writes."""
    result: dict[str, Any] = {}
    for key, attribute in item.items():
        if "S" in attribute:
            result[key] = attribute["S"]
        elif "N" in attribute:
            result[key] = int(attribute["N"])
        elif "BOOL" in attribute:
            result[key] = attribute["BOOL"]
        elif "NULL" in attribute:
            result[key] = None
    return result
