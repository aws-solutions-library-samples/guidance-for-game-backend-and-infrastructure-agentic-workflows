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

``fail_observation`` records a bounded, terminal ``failed`` transition where
possible; a later retry under the same idempotency token replays that stored
failure deterministically rather than being marked retryable. A retry that finds
an ``observing`` operation whose single-flight lease has expired triggers a fenced
reclaim: the fencing generation is advanced under a conditional write, a recovery
ledger event (``LEDGER#RECLAIM#<generation>``) is appended atomically, and the
operation is reported reclaimed so the caller reruns the read-only work under the
new generation. Both ``complete_observation`` and ``fail_observation`` accept the
holder's fencing ``generation`` so a superseded writer fails closed.

A ``TransactionCanceledException`` is classified from its actual cancellation
reasons, read from the real botocore ``exc.response["CancellationReasons"]`` wire
shape (never a fabricated ``cancellation_reasons`` attribute): only a pure
``ConditionalCheckFailed`` (with no transient/validation reason) resolves to
idempotency/state handling; a throttle, transaction conflict, capacity limit,
validation error, unknown reason, absent reasons, or a mix of conditional with a
transient reason all fail closed as a retryable unavailable outcome and never as
a false 409. ``load_status`` resolves an operation by id under trusted workspace
ownership.

The store never issues a ``Scan`` or an unconditional ``PutItem``, holds no
write path to any provider, and enforces the exclusive ``commit_not_after``
deadline before issuing each transaction. The idempotency mapping, state
snapshot, transitions, ledger events, and result are retained for the full audit
and replay window: per ADR 0005 none of them carries a DynamoDB ``ttl`` and the
table has no ``TimeToLiveSpecification``, so a late replay can never race an
asynchronous deletion. Observation document freshness is expressed by the
``expires_at`` field inside the result document, not by item expiry. The
append-only and single-flight guarantees come from the conditional/transactional
writes, not from IAM alone. Every DynamoDB item serializes below the 400 KB
item-size limit; the canonical observation JSON is validated against that
ceiling before it is written.
"""

from __future__ import annotations

# Standard library
import json
import logging
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
    ObservationStoreReadError,
)

_STATE_SNAPSHOT_SK = "STATE#current"
_STATE_TRANSITION_0_SK = "STATE#0"
_STATE_TRANSITION_1_SK = "STATE#1"
_LEDGER_0_SK = "LEDGER#0"
_LEDGER_1_SK = "LEDGER#1"
_RESULT_SK = "RESULT#current"
_IDEM_SK = "MAP#current"
_CONTRACT_VERSION = "1.0"

# The initial fencing generation a fresh operation holds. A stale-lease reclaim
# advances it so a superseded writer's generation-1 commit fails closed.
_INITIAL_GENERATION = 1

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
        }
        ledger_item = {
            "PK": op_pk,
            "SK": _LEDGER_0_SK,
            "record_type": "observation_ledger_event",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "event_type": "observation.created",
            "sequence": 0,
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
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=self._request_token(operation_id, "begin", 1),
            )
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            # Only a genuine ConditionalCheckFailed means the idempotency mapping
            # already exists: route it to idempotency resolution. A throttle,
            # transaction conflict, validation, or provider fault is a retryable
            # unavailable state and must never masquerade as a 409 conflict.
            if not _is_conditional_failure(exc):
                _log_store_exception("begin_observation", exc, classification="unavailable")
                return ObservationBegin(ObservationBeginOutcome.PROVIDER_UNAVAILABLE)
            try:
                return self._resolve_existing(
                    idem_pk,
                    idempotency_fingerprint,
                    deadline,
                    reclaim_lease_holder=lease_holder,
                    reclaim_lease_not_after=lease_deadline,
                )
            except ObservationStoreReadError:
                # A read during idempotency resolution failed: retryable, never a
                # false conflict or not-found.
                return ObservationBegin(ObservationBeginOutcome.PROVIDER_UNAVAILABLE)

        return ObservationBegin(ObservationBeginOutcome.CREATED, operation_id=operation_id)

    def _resolve_existing(
        self,
        idem_pk: str,
        expected_fingerprint: str,
        deadline: datetime,
        *,
        reclaim_lease_holder: str,
        reclaim_lease_not_after: datetime,
    ) -> ObservationBegin:
        """A mapping already exists: resolve it by fingerprint and current state.

        * A matching completed operation replays its stored bounded observation.
        * A matching terminally-failed operation replays its stored bounded
          failure (distinct from in-progress — it can never make progress under
          the same token).
        * A matching in-progress operation with a still-live single-flight lease
          reports in-progress (no second operation, no recovery claim).
        * A matching in-progress operation whose lease has EXPIRED is reclaimed:
          the fencing generation is advanced under a conditional write and a
          recovery ledger transition is appended, so the caller can safely rerun.
        """
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
        if state == STATE_FAILED:
            # A terminal failure replays deterministically and is never retried
            # under the same token.
            reason = snapshot.get("reason_code")
            return ObservationBegin(
                ObservationBeginOutcome.REPLAY_FAILED,
                operation_id=operation_id,
                current_state=STATE_FAILED,
                failure_reason=reason if isinstance(reason, str) else None,
            )
        if state != STATE_OBSERVING:
            return ObservationBegin(ObservationBeginOutcome.STATE_CONFLICT)

        # In progress. If the single-flight lease is still live, report
        # in-progress. If it has expired, attempt a fenced reclaim.
        now_epoch_s = int(self._clock().timestamp())
        lease_not_after = snapshot.get("lease_not_after")
        current_generation = snapshot.get("generation")
        lease_expired = isinstance(lease_not_after, int) and lease_not_after <= now_epoch_s
        if lease_expired and isinstance(current_generation, int):
            return self._reclaim_observation(
                op_pk=op_pk,
                operation_id=operation_id,
                current_generation=current_generation,
                new_lease_holder=reclaim_lease_holder,
                new_lease_not_after=reclaim_lease_not_after,
                deadline=deadline,
            )
        return ObservationBegin(
            ObservationBeginOutcome.IN_PROGRESS,
            operation_id=operation_id,
            lease_holder=snapshot.get("lease_holder") if isinstance(snapshot.get("lease_holder"), str) else None,
            current_state=state if isinstance(state, str) else None,
        )

    def _reclaim_observation(
        self,
        *,
        op_pk: str,
        operation_id: str,
        current_generation: int,
        new_lease_holder: str,
        new_lease_not_after: datetime,
        deadline: datetime,
    ) -> ObservationBegin:
        """Conditionally reclaim a stale-lease observing operation.

        The snapshot's fencing generation is advanced (and the lease taken) under
        a condition that still requires the observing state, sequence 0, the
        observed generation, and an EXPIRED lease. The new lease is the
        reclaiming caller's own request lease (``new_lease_not_after``), not the
        far-off commit deadline, so a reclaiming invocation that dies frees the
        operation again within one request lease rather than holding it for the
        whole commit window. A recovery ledger event is appended in the same
        transaction, keyed so it sorts strictly between the create event and the
        terminal event and carries a strictly increasing ``sequence``. If the
        condition fails (another writer reclaimed or the op advanced), we fall
        back to in-progress rather than creating a second operation.
        """
        now = self._clock()
        if now >= _utc(deadline, "commit_not_after"):
            return ObservationBegin(ObservationBeginOutcome.DEADLINE_EXPIRED)
        now_epoch_s = int(now.timestamp())
        new_generation = current_generation + 1
        # The new lease is the caller's own request lease, capped at the commit
        # deadline, so a reclaimer that dies does not hold the operation open for
        # the whole commit window.
        new_lease = min(_utc(new_lease_not_after, "lease_not_after"), _utc(deadline, "commit_not_after"))
        new_lease_epoch_s = int(new_lease.timestamp())

        # The recovery event sorts between ``LEDGER#0`` (create) and ``LEDGER#1``
        # (terminal) and before any later reclaim, using a zero-padded ordinal,
        # and carries a monotonic sub-sequence so the ledger order is strictly
        # increasing per operation.
        reclaim_ledger_sk = f"LEDGER#0#RECLAIM#{new_generation:04d}"
        ledger_item = {
            "PK": op_pk,
            "SK": reclaim_ledger_sk,
            "record_type": "observation_ledger_event",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "event_type": "observation.lease_reclaimed",
            "generation": new_generation,
            "sequence": 0,
            "sub_sequence": new_generation,
        }
        snapshot_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": op_pk, "SK": _STATE_SNAPSHOT_SK}),
                "UpdateExpression": ("SET #gen = :new_gen, lease_holder = :holder, lease_not_after = :new_lease"),
                "ConditionExpression": (
                    "attribute_exists(PK) AND #state = :observing AND #seq = :zero "
                    "AND #gen = :cur_gen AND lease_not_after <= :now"
                ),
                "ExpressionAttributeNames": {
                    "#state": "state",
                    "#seq": "sequence",
                    "#gen": "generation",
                },
                "ExpressionAttributeValues": _marshal_values(
                    {
                        ":observing": STATE_OBSERVING,
                        ":zero": 0,
                        ":cur_gen": current_generation,
                        ":new_gen": new_generation,
                        ":holder": new_lease_holder,
                        ":new_lease": new_lease_epoch_s,
                        ":now": now_epoch_s,
                    }
                ),
            }
        }
        transact_items = [snapshot_update, self._conditional_put(ledger_item, "attribute_not_exists(SK)")]
        try:
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=self._request_token(operation_id, "reclaim", new_generation),
            )
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if not _is_conditional_failure(exc):
                _log_store_exception("reclaim_observation", exc, classification="unavailable")
                return ObservationBegin(ObservationBeginOutcome.PROVIDER_UNAVAILABLE)
            # Lost the reclaim race (another writer advanced the generation) or
            # the operation already left observing: report in-progress so the
            # caller retries and eventually replays the winner's result.
            return ObservationBegin(
                ObservationBeginOutcome.IN_PROGRESS, operation_id=operation_id, current_state=STATE_OBSERVING
            )
        return ObservationBegin(
            ObservationBeginOutcome.RECLAIMED,
            operation_id=operation_id,
            current_state=STATE_OBSERVING,
            generation=new_generation,
        )

    # -- Phase 2: finalize after reads -----------------------------------

    def complete_observation(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        lease_holder: str,
        commit_not_after: datetime,
        observation: Mapping[str, Any],
        generation: int = _INITIAL_GENERATION,
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
        }

        # The snapshot advance is fenced on the writer's lease: expected prior
        # state/sequence/generation, holder identity, and an unexpired deadline.
        snapshot_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": op_pk, "SK": _STATE_SNAPSHOT_SK}),
                "UpdateExpression": (
                    "SET #state = :succeeded, #seq = :one, last_transition = :state1, observation_hash = :hash"
                ),
                "ConditionExpression": (
                    "attribute_exists(PK) AND #state = :observing AND #seq = :zero "
                    "AND #gen = :gen AND lease_holder = :holder AND lease_not_after > :now"
                ),
                "ExpressionAttributeNames": {
                    "#state": "state",
                    "#seq": "sequence",
                    "#gen": "generation",
                },
                "ExpressionAttributeValues": _marshal_values(
                    {
                        ":succeeded": STATE_SUCCEEDED,
                        ":observing": STATE_OBSERVING,
                        ":zero": 0,
                        ":one": 1,
                        ":gen": generation,
                        ":holder": lease_holder,
                        ":now": now_epoch_s,
                        ":state1": _STATE_TRANSITION_1_SK,
                        ":hash": observation_hash,
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
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=self._request_token(operation_id, "complete", generation),
            )
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if not _is_conditional_failure(exc):
                # Throttle/conflict/validation/provider fault: retryable, not 409.
                _log_store_exception("complete_observation", exc, classification="unavailable")
                return ObservationComplete(ObservationCompleteOutcome.PROVIDER_UNAVAILABLE)
            # A racing writer may have already reached succeeded: replay it.
            try:
                snapshot = self._get(op_pk, _STATE_SNAPSHOT_SK)
            except ObservationStoreReadError:
                return ObservationComplete(ObservationCompleteOutcome.PROVIDER_UNAVAILABLE)
            if snapshot and snapshot.get("state") == STATE_SUCCEEDED:
                try:
                    stored, stored_hash = self._load_result(op_pk)
                except ObservationStoreReadError:
                    return ObservationComplete(ObservationCompleteOutcome.PROVIDER_UNAVAILABLE)
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
        generation: int = _INITIAL_GENERATION,
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
        }
        snapshot_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": op_pk, "SK": _STATE_SNAPSHOT_SK}),
                "UpdateExpression": "SET #state = :failed, #seq = :one, reason_code = :reason",
                "ConditionExpression": (
                    "attribute_exists(PK) AND #state = :observing AND #seq = :zero "
                    "AND #gen = :gen AND lease_holder = :holder"
                ),
                "ExpressionAttributeNames": {
                    "#state": "state",
                    "#seq": "sequence",
                    "#gen": "generation",
                },
                "ExpressionAttributeValues": _marshal_values(
                    {
                        ":failed": STATE_FAILED,
                        ":observing": STATE_OBSERVING,
                        ":zero": 0,
                        ":one": 1,
                        ":gen": generation,
                        ":holder": lease_holder,
                        ":reason": reason_code,
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
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=self._request_token(operation_id, "fail", generation),
            )
        except Exception as exc:  # noqa: BLE001 - best effort; a fail record is not mandatory
            # Best-effort: a fail record is not mandatory, but leave a bounded
            # breadcrumb rather than vanishing silently.
            _log_store_exception("fail_observation", exc, classification="best_effort")
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
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=_marshal({"PK": pk, "SK": sk}),
                ConsistentRead=True,
            )
        except Exception as exc:  # noqa: BLE001 - log a bounded breadcrumb, then raise typed
            # A read failure must not vanish into a false "not found" or a
            # non-retryable 500; surface a bounded diagnostic and raise a typed,
            # retryable read error the caller maps to PROVIDER_UNAVAILABLE.
            _log_store_exception("read_item", exc, classification="read_error")
            raise ObservationStoreReadError("observation state read failed") from exc
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

    @staticmethod
    def _request_token(operation_id: str, phase: str, generation: int) -> str:
        """A deterministic ``ClientRequestToken`` for a transaction attempt.

        botocore may transparently retry a ``TransactWriteItems`` call once. A
        deterministic idempotency token (bound to the operation, phase, and
        generation) makes a transparent retry of a transaction whose first
        attempt already committed a no-op on the server, so a lost response can
        never resurface as a spurious ``STATE_CONFLICT`` for the caller's own
        write. It is a bounded hex digest and carries no identity.
        """
        digest = canonical_sha256({"operation_id": operation_id, "phase": phase, "generation": generation})
        # ClientRequestToken accepts 1..36 chars; take a bounded hex slice.
        return digest.split(":", 1)[-1][:36]


# Cancellation-reason codes DynamoDB reports on a TransactionCanceledException.
# Only ConditionalCheckFailed means a real precondition failed (the idempotency
# mapping or a fenced state already exists) and routes to idempotency/state
# resolution. Every other reason — a transient conflict, throttle, capacity
# limit, or validation error — is NOT a 409 conflict and must fail closed as a
# retryable unavailable condition.
#
# The reasons live on the wire inside ``exc.response["CancellationReasons"]`` —
# a positional list aligned to the transaction's legs, where a non-failing leg
# reports ``{"Code": "None"}``. A real ``botocore.exceptions.ClientError`` does
# NOT expose a ``cancellation_reasons`` attribute; reading one is a test-fake
# artifact that never matches production, so classification reads ``response``.
_CONDITIONAL_REASON = "ConditionalCheckFailed"
_TRANSACTION_CANCELED_CODE = "TransactionCanceledException"
_BARE_CONDITIONAL_CODE = "ConditionalCheckFailedException"
# "None" is DynamoDB's marker for a transaction leg that did not fail and is
# inert here (a pure-conditional cancel is [CCF, None, ...]).
_INERT_REASONS = frozenset({None, "None"})


def _cancellation_reason_codes(response: dict[str, Any]) -> set[str] | None:
    """Extract the set of cancellation-reason codes from a ClientError response.

    Returns ``None`` when the response carries no structured reasons list, which
    the caller treats as unconfirmable (and therefore fail-closed).
    """
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return None
    return {r.get("Code") for r in reasons if isinstance(r, dict)}


def _is_conditional_failure(exc: Exception) -> bool:
    """Return whether an error is a genuine conditional-check failure.

    Classification reads the real botocore wire shape from ``exc.response``:

    * A bare ``ConditionalCheckFailedException`` (from a non-transactional
      conditional write) is conditional — an idempotency/state precondition.
    * A ``TransactionCanceledException`` is conditional only when every
      non-inert ``CancellationReasons`` code is ``ConditionalCheckFailed``. A
      ``TransactionConflict``, ``ThrottlingError``,
      ``ProvisionedThroughputExceeded``, ``ValidationError``, any other
      unrecognized reason, absent/empty reasons, or a mix of conditional with
      any such reason fails closed as retryable/unavailable — never a false
      idempotency/state 409.

    Every other error is non-conditional and maps to a retryable unavailable
    state by the caller.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error_code = response.get("Error", {}).get("Code", "")
    if error_code == _BARE_CONDITIONAL_CODE:
        return True
    if error_code != _TRANSACTION_CANCELED_CODE:
        return False
    reason_codes = _cancellation_reason_codes(response)
    if reason_codes is None:
        # A TransactionCanceledException with no structured reasons cannot be
        # confirmed as conditional; fail closed as transient/retryable.
        return False
    # Conditional only when every non-inert reason is ConditionalCheckFailed: a
    # transient/validation reason OR any unrecognized reason anywhere makes the
    # whole transaction retryable, never a false idempotency/state 409.
    meaningful = {reason for reason in reason_codes if reason not in _INERT_REASONS}
    if not meaningful:
        return False
    return meaningful == {_CONDITIONAL_REASON}


# -- Safe diagnostic logging at store exception boundaries (#413) -------------
#
# The store maps a non-conditional DynamoDB failure to PROVIDER_UNAVAILABLE. To
# keep that path observable, these helpers emit a BOUNDED structured record at
# each boundary: the store operation name, the exception TYPE, the AWS error
# code, and the transaction cancellation-reason codes — and nothing else.
#
# This boundary is deliberately strict. The store handles idempotency tokens,
# operation ids, canonical intent, and request items, any of which can appear in
# a botocore exception MESSAGE or in stack locals. So this boundary emits
# sanitized metadata ONLY: never the exception message, ``str(exc)``, a
# traceback, or ``exc_info``. Bounded lengths cap any adversarial code/reason a
# provider could return.
_MAX_CODE_LEN = 128
_MAX_REASON_CODES = 16


def _aws_error_code(exc: Exception) -> str | None:
    """The bounded AWS error code from a botocore-shaped exception, or None.

    Reads only ``exc.response["Error"]["Code"]`` (a short, provider-defined
    token). Never reads the error MESSAGE.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    code = response.get("Error", {}).get("Code")
    if not isinstance(code, str) or not code:
        return None
    return code[:_MAX_CODE_LEN]


def _bounded_reason_codes(exc: Exception) -> list[str]:
    """The bounded, sorted set of transaction cancellation-reason codes.

    Reads only the ``Code`` of each entry in ``CancellationReasons`` (dropping
    the inert ``"None"`` marker). Never reads any reason ``Message``.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return []
    codes = _cancellation_reason_codes(response)
    if not codes:
        return []
    bounded = sorted(code[:_MAX_CODE_LEN] for code in codes if isinstance(code, str) and code and code != "None")
    return bounded[:_MAX_REASON_CODES]


# The store diagnostic uses the Python standard-library ``logging`` module, NOT
# loguru. This boundary ships in the minimal E1 observe Lambda, whose dependency
# closure deliberately excludes loguru; importing it here would break the real
# package import at deployment. ``logging`` is always present in the Lambda
# runtime, so the diagnostic stays Lambda-safe without expanding that closure.
#
# The whole record lives in the ``LogRecord`` message string: a CloudWatch/Lambda
# handler renders ``%(message)s`` and carries no structured ``extra`` mapping, so
# a datum bound as ``extra`` would be dropped before it reached CloudWatch.
# Provider-defined codes are echoed into that string, so each token is reduced to
# ``[A-Za-z0-9._-]`` (anything else becomes ``.``). That keeps the record a
# single, unambiguous line and forecloses log-forging via an embedded newline,
# separator, or brace in an adversarial code.
_DIAG_FIELD_SEP = " "
_SAFE_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

# The store-boundary diagnostic channel (stdlib logging, Lambda-safe).
_LOGGER = logging.getLogger(__name__)


def _safe_token(value: str) -> str:
    """Reduce a bounded provider token to a single-line, separator-safe form."""
    return "".join(ch if ch in _SAFE_TOKEN_CHARS else "." for ch in value)


def _log_store_exception(operation: str, exc: Exception, *, classification: str) -> None:
    """Emit a bounded, sanitized diagnostic record for a store-boundary error.

    Logs ONLY the operation name, the exception type, the AWS error code, and
    the bounded cancellation-reason codes. It never logs the exception message,
    request items, ids, tokens, table/account/ARN, or provider data, and it
    never attaches a traceback or ``exc_info`` (see the boundary note above).

    The fields are embedded directly into a fixed ``key=value`` message string
    rather than passed as ``logging`` args or ``extra``: a Lambda/CloudWatch
    handler renders ``%(message)s`` only, so anything carried in ``extra`` is
    dropped before it reaches CloudWatch. ``exception_type`` is a Python class name and ``operation`` /
    ``classification`` are static literals we pass, so only the two
    provider-controlled tokens (the AWS error code and each cancellation-reason
    code) are passed through :func:`_safe_token`.
    """
    error_code = _aws_error_code(exc)
    reason_codes = _bounded_reason_codes(exc)
    fields = [
        "dynamodb_store_exception",
        "operation=" + _safe_token(operation),
        "classification=" + _safe_token(classification),
        "exception_type=" + _safe_token(type(exc).__name__),
        "aws_error_code=" + (_safe_token(error_code) if error_code else "none"),
        "cancellation_reason_codes="
        + (",".join(_safe_token(code) for code in reason_codes) if reason_codes else "none"),
    ]
    message = _DIAG_FIELD_SEP.join(fields)
    # Pass the fully-formed message with no ``args`` so ``logging`` never runs
    # ``%``-formatting over it; a literal ``%`` in a (sanitized) token is inert.
    # No ``exc_info`` -> no traceback and no stack locals reach the handler.
    _LOGGER.warning(message)


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
