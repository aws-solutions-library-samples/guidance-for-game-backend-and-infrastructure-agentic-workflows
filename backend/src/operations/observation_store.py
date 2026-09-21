"""DynamoDB observation store: two-phase conditional state + append-only ledger.

Implements :class:`~operations.observation.ObservationStore` for the E1 observe
phase (issue #413) following ADR 0005 ("Persist operations state and recover
workflows"). The lifecycle is *create before reads*:

``begin_observation`` performs a single DynamoDB ``TransactWriteItems`` that
writes, all or nothing, **before** any provider read:

1. the **idempotency mapping** (``PK=WS#<workspace>#IDEM#<token>``), conditional
   on absence and carrying the canonical *intent* fingerprint and the bound
   operation id;
2. the **current-state snapshot** (``PK=OP#<operation_id>``, ``SK=STATE#current``)
   at the ``observing`` state, sequence 0, fencing generation 1, holding the
   caller's single-flight lease and absolute lease deadline;
3. the immutable **initial state transition** (``SK=STATE#0``); and
4. the initial append-only **ledger event** (``SK=LEDGER#0``).

``complete_observation`` performs one ``TransactWriteItems`` that, all or
nothing, and fenced on the caller's lease holder/generation and unexpired lease
deadline:

1. writes the **result** item (``SK=RESULT#current``) carrying the canonical
   observation JSON (verified below the DynamoDB item-size limit) and its hash;
2. advances the snapshot ``observing`` -> ``succeeded`` (sequence 1), conditional
   on the expected prior state, prior sequence, held generation, holder, and an
   unexpired lease deadline;
3. writes the immutable ``STATE#1`` transition; and
4. appends the ``LEDGER#1`` event.

``fail_observation`` records a bounded ``failed`` transition where possible.
``load_status`` resolves an operation by id under trusted workspace ownership.

The store never issues a ``Scan`` or an unconditional ``PutItem``, holds no
write path to any provider, and enforces the exclusive ``commit_not_after``
deadline before issuing each transaction. Observe-phase records are transient
and carry a numeric ``ttl`` for safe expiry; the append-only and single-flight
guarantees come from the conditional/transactional writes, not from IAM alone.
Every DynamoDB item serializes below the 400 KB item-size limit; the canonical
observation JSON is validated against that ceiling before it is written.
"""

from __future__ import annotations

# Standard library
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

# Local modules
from operations.contracts.canonical import canonical_sha256
from operations.observation import (
    STATE_FAILED,
    STATE_OBSERVING,
    STATE_SUCCEEDED,
    ObservationBegin,
    ObservationBeginOutcome,
    ObservationComplete,
    ObservationCompleteOutcome,
    ObservationStatus,
    ObservationStatusView,
    ObservationStore,
)

_STATE_SNAPSHOT_SK = "STATE#current"
_STATE_TRANSITION_0_SK = "STATE#0"
_STATE_TRANSITION_1_SK = "STATE#1"
_LEDGER_0_SK = "LEDGER#0"
_LEDGER_1_SK = "LEDGER#1"
_RESULT_SK = "RESULT#current"
_IDEM_SK = "MAP#current"
_CONTRACT_VERSION = "1.0"

# The DynamoDB item-size limit. The canonical observation JSON must serialize
# below this ceiling; a larger result fails closed rather than being written.
_DYNAMODB_ITEM_SIZE_LIMIT_BYTES = 400 * 1024


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class ObservationStoreError(RuntimeError):
    """A structural precondition of a store write was violated (fail closed)."""


class DynamoDbObservationStore(ObservationStore):
    """Conditional, idempotent, append-only, two-phase observation store."""

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

    # -- Phase 1: create before reads ------------------------------------

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
        deadline = _utc(commit_not_after, "commit_not_after")
        lease_deadline = _utc(lease_not_after, "lease_not_after")
        if self._clock() >= deadline:
            return ObservationBegin(ObservationBeginOutcome.DEADLINE_EXPIRED)

        intent_hash = canonical_sha256(dict(intent))
        op_pk = f"OP#{operation_id}"
        idem_pk = f"WS#{workspace_id}#IDEM#{idempotency_token}"
        lease_epoch_s = int(lease_deadline.timestamp())

        idem_item = {
            "PK": idem_pk,
            "SK": _IDEM_SK,
            "record_type": "idempotency_mapping",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "workspace_id": workspace_id,
            "idempotency_fingerprint": idempotency_fingerprint,
            "intent_hash": intent_hash,
            "ttl": int(ttl_epoch_s),
        }
        snapshot_item = {
            "PK": op_pk,
            "SK": _STATE_SNAPSHOT_SK,
            "record_type": "observation_state",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "workspace_id": workspace_id,
            "state": STATE_OBSERVING,
            "sequence": 0,
            "generation": 1,
            "lease_holder": lease_holder,
            "lease_not_after": lease_epoch_s,
            "intent_hash": intent_hash,
            "ttl": int(ttl_epoch_s),
        }
        transition_item = {
            "PK": op_pk,
            "SK": _STATE_TRANSITION_0_SK,
            "record_type": "observation_state_transition",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "previous_state": None,
            "new_state": STATE_OBSERVING,
            "sequence": 0,
            "ttl": int(ttl_epoch_s),
        }
        ledger_item = {
            "PK": op_pk,
            "SK": _LEDGER_0_SK,
            "record_type": "observation_ledger_event",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "event_type": "observation.created",
            "sequence": 0,
            "ttl": int(ttl_epoch_s),
        }

        transact_items = [
            self._conditional_put(idem_item, "attribute_not_exists(PK)"),
            self._conditional_put(snapshot_item, "attribute_not_exists(PK)"),
            self._conditional_put(transition_item, "attribute_not_exists(SK)"),
            self._conditional_put(ledger_item, "attribute_not_exists(SK)"),
        ]

        if self._clock() >= deadline:
            return ObservationBegin(ObservationBeginOutcome.DEADLINE_EXPIRED)

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except Exception as exc:  # noqa: BLE001 - classify by conditional-failure shape
            if not _is_conditional_failure(exc):
                return ObservationBegin(ObservationBeginOutcome.STATE_CONFLICT)
            return self._resolve_existing(idem_pk, idempotency_fingerprint)

        return ObservationBegin(ObservationBeginOutcome.CREATED, operation_id=operation_id)

    def _resolve_existing(self, idem_pk: str, expected_fingerprint: str) -> ObservationBegin:
        """A mapping already exists: replay/in-progress iff the fingerprint matches."""
        mapping = self._get(idem_pk, _IDEM_SK)
        if not mapping:
            return ObservationBegin(ObservationBeginOutcome.STATE_CONFLICT)
        if mapping.get("idempotency_fingerprint") != expected_fingerprint:
            # Same token, different intent: never mutate the existing operation.
            return ObservationBegin(ObservationBeginOutcome.IDEMPOTENCY_CONFLICT)

        operation_id = mapping.get("operation_id")
        if not isinstance(operation_id, str):
            return ObservationBegin(ObservationBeginOutcome.STATE_CONFLICT)
        op_pk = f"OP#{operation_id}"
        snapshot = self._get(op_pk, _STATE_SNAPSHOT_SK)
        if not snapshot:
            return ObservationBegin(ObservationBeginOutcome.STATE_CONFLICT)

        state = snapshot.get("state")
        if state == STATE_SUCCEEDED:
            observation, observation_hash = self._load_result(op_pk)
            if observation is None:
                return ObservationBegin(ObservationBeginOutcome.STATE_CONFLICT)
            return ObservationBegin(
                ObservationBeginOutcome.REPLAY_COMPLETED,
                operation_id=operation_id,
                observation=observation,
                observation_hash=observation_hash,
            )
        # Non-terminal (observing) or terminal failure: report as in progress so
        # the caller retries rather than creating a second operation.
        return ObservationBegin(
            ObservationBeginOutcome.IN_PROGRESS,
            operation_id=operation_id,
            lease_holder=snapshot.get("lease_holder") if isinstance(snapshot.get("lease_holder"), str) else None,
            current_state=state if isinstance(state, str) else None,
        )

    # -- Phase 2: finalize after reads -----------------------------------

    def complete_observation(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        lease_holder: str,
        commit_not_after: datetime,
        ttl_epoch_s: int,
        observation: Mapping[str, Any],
    ) -> ObservationComplete:
        deadline = _utc(commit_not_after, "commit_not_after")
        now = self._clock()
        if now >= deadline:
            return ObservationComplete(ObservationCompleteOutcome.DEADLINE_EXPIRED)

        canonical_json = json.dumps(dict(observation), separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        if len(canonical_json.encode("utf-8")) > _DYNAMODB_ITEM_SIZE_LIMIT_BYTES:
            raise ObservationStoreError("canonical observation exceeds the DynamoDB item-size limit")
        observation_hash = canonical_sha256(dict(observation))

        op_pk = f"OP#{operation_id}"
        now_epoch_s = int(now.timestamp())

        result_item = {
            "PK": op_pk,
            "SK": _RESULT_SK,
            "record_type": "observation_result",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "observation_hash": observation_hash,
            "observation_json": canonical_json,
            "ttl": int(ttl_epoch_s),
        }
        transition_item = {
            "PK": op_pk,
            "SK": _STATE_TRANSITION_1_SK,
            "record_type": "observation_state_transition",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "previous_state": STATE_OBSERVING,
            "new_state": STATE_SUCCEEDED,
            "sequence": 1,
            "observation_hash": observation_hash,
            "ttl": int(ttl_epoch_s),
        }
        ledger_item = {
            "PK": op_pk,
            "SK": _LEDGER_1_SK,
            "record_type": "observation_ledger_event",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "event_type": "observation.succeeded",
            "sequence": 1,
            "observation_hash": observation_hash,
            "ttl": int(ttl_epoch_s),
        }

        # The snapshot advance is fenced on the writer's lease: expected prior
        # state/sequence/generation, holder identity, and an unexpired deadline.
        snapshot_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": op_pk, "SK": _STATE_SNAPSHOT_SK}),
                "UpdateExpression": (
                    "SET #state = :succeeded, #seq = :one, last_transition = :state1, "
                    "observation_hash = :hash, #ttl = :ttl"
                ),
                "ConditionExpression": (
                    "attribute_exists(PK) AND #state = :observing AND #seq = :zero "
                    "AND #gen = :gen AND lease_holder = :holder AND lease_not_after > :now"
                ),
                "ExpressionAttributeNames": {
                    "#state": "state",
                    "#seq": "sequence",
                    "#gen": "generation",
                    "#ttl": "ttl",
                },
                "ExpressionAttributeValues": _marshal_values(
                    {
                        ":succeeded": STATE_SUCCEEDED,
                        ":observing": STATE_OBSERVING,
                        ":zero": 0,
                        ":one": 1,
                        ":gen": 1,
                        ":holder": lease_holder,
                        ":now": now_epoch_s,
                        ":state1": _STATE_TRANSITION_1_SK,
                        ":hash": observation_hash,
                        ":ttl": int(ttl_epoch_s),
                    }
                ),
            }
        }

        transact_items = [
            self._conditional_put(result_item, "attribute_not_exists(SK)"),
            snapshot_update,
            self._conditional_put(transition_item, "attribute_not_exists(SK)"),
            self._conditional_put(ledger_item, "attribute_not_exists(SK)"),
        ]

        if self._clock() >= deadline:
            return ObservationComplete(ObservationCompleteOutcome.DEADLINE_EXPIRED)

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except Exception as exc:  # noqa: BLE001
            if not _is_conditional_failure(exc):
                return ObservationComplete(ObservationCompleteOutcome.STATE_CONFLICT)
            # A racing writer may have already reached succeeded: replay it.
            snapshot = self._get(op_pk, _STATE_SNAPSHOT_SK)
            if snapshot and snapshot.get("state") == STATE_SUCCEEDED:
                stored, stored_hash = self._load_result(op_pk)
                if stored is not None:
                    return ObservationComplete(
                        ObservationCompleteOutcome.ALREADY_TERMINAL,
                        observation=stored,
                        observation_hash=stored_hash,
                    )
            return ObservationComplete(ObservationCompleteOutcome.STATE_CONFLICT)

        return ObservationComplete(ObservationCompleteOutcome.RECORDED, observation_hash=observation_hash)

    def fail_observation(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        lease_holder: str,
        reason_code: str,
        ttl_epoch_s: int,
    ) -> None:
        """Record a bounded failed transition where possible (best effort)."""
        op_pk = f"OP#{operation_id}"
        transition_item = {
            "PK": op_pk,
            "SK": _STATE_TRANSITION_1_SK,
            "record_type": "observation_state_transition",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "previous_state": STATE_OBSERVING,
            "new_state": STATE_FAILED,
            "sequence": 1,
            "reason_code": reason_code,
            "ttl": int(ttl_epoch_s),
        }
        ledger_item = {
            "PK": op_pk,
            "SK": _LEDGER_1_SK,
            "record_type": "observation_ledger_event",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "event_type": "observation.failed",
            "sequence": 1,
            "reason_code": reason_code,
            "ttl": int(ttl_epoch_s),
        }
        snapshot_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": op_pk, "SK": _STATE_SNAPSHOT_SK}),
                "UpdateExpression": "SET #state = :failed, #seq = :one, reason_code = :reason, #ttl = :ttl",
                "ConditionExpression": (
                    "attribute_exists(PK) AND #state = :observing AND #seq = :zero "
                    "AND #gen = :gen AND lease_holder = :holder"
                ),
                "ExpressionAttributeNames": {
                    "#state": "state",
                    "#seq": "sequence",
                    "#gen": "generation",
                    "#ttl": "ttl",
                },
                "ExpressionAttributeValues": _marshal_values(
                    {
                        ":failed": STATE_FAILED,
                        ":observing": STATE_OBSERVING,
                        ":zero": 0,
                        ":one": 1,
                        ":gen": 1,
                        ":holder": lease_holder,
                        ":reason": reason_code,
                        ":ttl": int(ttl_epoch_s),
                    }
                ),
            }
        }
        transact_items = [
            snapshot_update,
            self._conditional_put(transition_item, "attribute_not_exists(SK)"),
            self._conditional_put(ledger_item, "attribute_not_exists(SK)"),
        ]
        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except Exception:  # noqa: BLE001 - best effort; a fail record is not mandatory
            return None
        return None

    # -- Status ----------------------------------------------------------

    def load_status(self, *, operation_id: str, workspace_id: str) -> ObservationStatus | None:
        """Return the typed status if it exists and is owned by the workspace."""
        op_pk = f"OP#{operation_id}"
        snapshot = self._get(op_pk, _STATE_SNAPSHOT_SK)
        if not snapshot:
            return None
        # Enforce trusted workspace ownership: a snapshot owned by another
        # workspace is invisible to this caller.
        if snapshot.get("workspace_id") != workspace_id:
            return None
        state = snapshot.get("state")
        try:
            view = ObservationStatusView(state)
        except ValueError:
            return None
        observation: dict[str, Any] | None = None
        observation_hash: str | None = None
        if view is ObservationStatusView.SUCCEEDED:
            observation, observation_hash = self._load_result(op_pk)
            if observation is None:
                return None
        return ObservationStatus(
            operation_id=operation_id,
            workspace_id=workspace_id,
            state=view,
            observation=observation,
            observation_hash=observation_hash,
        )

    # -- Helpers ---------------------------------------------------------

    def _load_result(self, op_pk: str) -> tuple[dict[str, Any] | None, str | None]:
        item = self._get(op_pk, _RESULT_SK)
        if not item:
            return None, None
        raw = item.get("observation_json")
        recorded_hash = item.get("observation_hash")
        if not isinstance(raw, str):
            return None, None
        try:
            observation = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
        if not isinstance(observation, dict):
            return None, None
        return observation, recorded_hash if isinstance(recorded_hash, str) else None

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_marshal({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, dict) else None
        return _unmarshal(item) if item else None

    def _conditional_put(self, item: dict[str, Any], condition: str) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": _marshal(item),
                "ConditionExpression": condition,
            }
        }


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
    return {key: _marshal_value(value, key) for key, value in item.items()}


def _marshal_values(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Marshal an expression-attribute-values map."""
    return {key: _marshal_value(value, key) for key, value in values.items()}


def _marshal_value(value: Any, key: str) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return {"BOOL": value}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, int):
        return {"N": str(value)}
    raise TypeError(f"unsupported attribute type for {key!r}: {type(value).__name__}")


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
