"""DynamoDB E2 approval store: durable prepared operations + fenced decisions.

Implements the E2 half of ADR 0005 for issue #414. It is the durable backing
store for the prepared-operation approval lifecycle and satisfies both the
:class:`~operations.approval.ApprovalStore` (grant) and
:class:`~operations.decisions.DecisionStore` (reject/cancel/expire) ports, plus
a :meth:`persist_prepared_operation` that atomically materializes one prepared
operation awaiting approval.

Item layout (single-table, ``PK``/``SK``):

* idempotency mapping — ``PK=WS#<workspace>#IDEM#<token>``, ``SK=MAP#current`` —
  written conditional on absence, carrying the bound ``operation_id`` and the
  canonical *intent* fingerprint. A retry under the same token whose intent
  fingerprint matches replays the bound operation; a different intent conflicts.
* prepared operation — ``PK=OP#<operation_id>``, ``SK=PREPARED#current`` —
  immutable, carrying the canonical prepared-operation JSON and its
  ``prepared_hash``.
* state snapshot — ``SK=STATE#current`` — the mutable current state, sequence,
  and fencing generation.
* immutable state transitions — ``SK=STATE#<sequence>``.
* append-only ledger — ``SK=LEDGER#<sequence>``.
* approval record — ``SK=APPROVAL#current`` — the granted/denied decision.

``persist_prepared_operation`` performs one ``TransactWriteItems`` writing, all
or nothing: the idempotency mapping (conditional on absence), the immutable
prepared operation, the ``pending_approval`` snapshot (sequence 0, generation
1), the initial ``STATE#0`` transition, and the initial ``LEDGER#0`` event.

``record_granted_approval`` and ``record_terminal_decision`` each perform one
``TransactWriteItems`` fenced on the expected ``prepared_hash`` and expected
current state, advancing the snapshot to the terminal state, appending the
immutable transition and ledger event, and (for grant/reject) writing the
approval record. A lost conditional check is reported as a precondition failure
(never a silent success); the caller surfaces it as a state conflict.

**E2 records carry no TTL.** The prepared operation, its state snapshot and
transitions, the append-only ledger, the idempotency mapping, and the approval
record are the durable audit trail and MUST NOT expire. Only the transient E1
observation records carry a numeric ``ttl`` (see
:mod:`operations.observation_store`); this store never writes a ``ttl``
attribute. Expiry of an operation is a *state transition* to ``expired`` in the
ledger, not the physical deletion of a record.

Error classification reads the real botocore wire shape
(``exc.response["CancellationReasons"]``): only a pure ``ConditionalCheckFailed``
is a precondition/idempotency race; a throttle, transaction conflict, capacity
limit, validation error, unknown reason, or absent reasons all fail closed as a
retryable unavailable outcome and never as a false precondition failure. The
store issues no ``Scan`` and no unconditional ``PutItem``, holds no provider
write path, and enforces the exclusive ``commit_not_after`` deadline before each
transaction.
"""

from __future__ import annotations

# Standard library
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Local modules
from operations.approval import ApprovalCommitOutcome, StoredPreparedOperation
from operations.decisions import DecisionCommitOutcome
from operations.evidence import OperationEvidence

_PREPARED_SK = "PREPARED#current"
_STATE_SNAPSHOT_SK = "STATE#current"
_STATE_TRANSITION_0_SK = "STATE#0"
_LEDGER_0_SK = "LEDGER#0"
_APPROVAL_SK = "APPROVAL#current"
_IDEM_SK = "MAP#current"
_CONTRACT_VERSION = "1.0"
_INITIAL_GENERATION = 1
_PENDING_APPROVAL = "pending_approval"

# The prepared-operation lifecycle appends at most a small, bounded number of
# ledger entries (materialization + one terminal transition), so an evidence
# read walks a short, contiguous LEDGER#<seq> chain rather than issuing a Scan.
_MAX_LEDGER_SEQUENCE = 32

# The DynamoDB item-size limit. The canonical prepared-operation JSON must
# serialize below this ceiling; a larger operation fails closed.
_DYNAMODB_ITEM_SIZE_LIMIT_BYTES = 400 * 1024


class ApprovalStoreError(RuntimeError):
    """A structural precondition of a store write was violated (fail closed)."""


class PersistOutcome(str, Enum):
    """Authoritative result of the prepared-operation persistence transaction."""

    PERSISTED = "persisted"
    REPLAYED = "replayed"
    INTENT_CONFLICT = "intent_conflict"
    DEADLINE_EXPIRED = "deadline_expired"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class PersistResult:
    """The outcome of persisting a prepared operation, with the bound id."""

    __slots__ = ("outcome", "operation_id")

    def __init__(self, outcome: PersistOutcome, *, operation_id: str | None = None) -> None:
        self.outcome = outcome
        self.operation_id = operation_id


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class DynamoDbApprovalStore:
    """Conditional, idempotent, append-only, TTL-free E2 approval store."""

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

    # -- Persist a prepared operation awaiting approval -------------------

    def persist_prepared_operation(
        self,
        *,
        prepared_operation: Mapping[str, Any],
        prepared_hash: str,
        workspace_id: str,
        idempotency_token: str,
        idempotency_fingerprint: str,
        state_change: Mapping[str, Any],
        ledger_event: Mapping[str, Any],
        commit_not_after: datetime,
    ) -> PersistResult:
        """Atomically materialize one prepared operation awaiting approval."""
        deadline = _utc(commit_not_after, "commit_not_after")
        if self._clock() >= deadline:
            return PersistResult(PersistOutcome.DEADLINE_EXPIRED)

        operation_id = prepared_operation["operation_id"]
        # The capacity operation carries its own deterministic ``prepared_hash``
        # field (binding every other field). The caller must pass exactly that
        # bound hash; anything else is a caller bug and fails closed.
        if prepared_operation.get("prepared_hash") != prepared_hash:
            raise ApprovalStoreError("prepared_hash does not bind the prepared operation")

        operation_json = _canonical_json(prepared_operation)
        if len(operation_json.encode("utf-8")) >= _DYNAMODB_ITEM_SIZE_LIMIT_BYTES:
            raise ApprovalStoreError("prepared operation exceeds the durable item-size limit")

        op_pk = f"OP#{operation_id}"
        idem_pk = f"WS#{workspace_id}#IDEM#{idempotency_token}"

        idem_item = {
            "PK": idem_pk,
            "SK": _IDEM_SK,
            "record_type": "idempotency_mapping",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "workspace_id": workspace_id,
            "idempotency_fingerprint": idempotency_fingerprint,
            "prepared_hash": prepared_hash,
        }
        prepared_item = {
            "PK": op_pk,
            "SK": _PREPARED_SK,
            "record_type": "prepared_operation",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "workspace_id": workspace_id,
            "prepared_hash": prepared_hash,
            "operation_json": operation_json,
        }
        snapshot_item = {
            "PK": op_pk,
            "SK": _STATE_SNAPSHOT_SK,
            "record_type": "operation_state",
            "contract_version": _CONTRACT_VERSION,
            "operation_id": operation_id,
            "workspace_id": workspace_id,
            "prepared_hash": prepared_hash,
            "state": _PENDING_APPROVAL,
            "sequence": 0,
            "generation": _INITIAL_GENERATION,
        }
        transition_item = _marshal(
            {
                "PK": op_pk,
                "SK": _STATE_TRANSITION_0_SK,
                "record_type": "operation_state_transition",
                "contract_version": _CONTRACT_VERSION,
                "operation_id": operation_id,
                "sequence": 0,
                "state_change_json": _canonical_json(state_change),
            }
        )
        ledger_item = _marshal(
            {
                "PK": op_pk,
                "SK": _LEDGER_0_SK,
                "record_type": "operation_ledger_event",
                "contract_version": _CONTRACT_VERSION,
                "operation_id": operation_id,
                "sequence": 0,
                "ledger_json": _canonical_json(ledger_event),
            }
        )

        transact_items = [
            self._conditional_put(_marshal(idem_item), "attribute_not_exists(PK)"),
            self._conditional_put(_marshal(prepared_item), "attribute_not_exists(PK)"),
            self._conditional_put(_marshal(snapshot_item), "attribute_not_exists(PK)"),
            self._conditional_put(transition_item, "attribute_not_exists(SK)"),
            self._conditional_put(ledger_item, "attribute_not_exists(SK)"),
        ]

        if self._clock() >= deadline:
            return PersistResult(PersistOutcome.DEADLINE_EXPIRED)

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if not _is_conditional_failure(exc):
                _log_store_exception("persist_prepared_operation", exc, classification="unavailable")
                return PersistResult(PersistOutcome.PROVIDER_UNAVAILABLE)
            return self._resolve_existing_mapping(idem_pk, idempotency_fingerprint)

        return PersistResult(PersistOutcome.PERSISTED, operation_id=operation_id)

    def _resolve_existing_mapping(self, idem_pk: str, expected_fingerprint: str) -> PersistResult:
        """A mapping already exists: replay on a matching intent, else conflict."""
        existing = self._get(idem_pk, _IDEM_SK)
        if existing is None:
            # The conditional check failed but the item is gone: fail closed.
            return PersistResult(PersistOutcome.PROVIDER_UNAVAILABLE)
        if existing.get("idempotency_fingerprint") != expected_fingerprint:
            return PersistResult(PersistOutcome.INTENT_CONFLICT)
        operation_id = existing.get("operation_id")
        if not isinstance(operation_id, str):
            return PersistResult(PersistOutcome.PROVIDER_UNAVAILABLE)
        return PersistResult(PersistOutcome.REPLAYED, operation_id=operation_id)

    # -- ApprovalStore (grant) -------------------------------------------

    def load_for_approval(self, operation_id: str) -> StoredPreparedOperation | None:
        return self._load_stored(operation_id)

    def record_granted_approval(
        self,
        *,
        operation_id: str,
        expected_prepared_operation_hash: str,
        expected_state: str,
        commit_not_after: datetime,
        approval: Mapping[str, Any],
    ) -> ApprovalCommitOutcome:
        deadline = _utc(commit_not_after, "commit_not_after")
        if self._clock() >= deadline:
            return ApprovalCommitOutcome.DEADLINE_EXPIRED
        state_change = _approval_state_change(operation_id, expected_prepared_operation_hash, approval)
        ledger_event = _approval_ledger_event(operation_id, expected_prepared_operation_hash, approval)
        outcome = self._commit_terminal(
            operation_id=operation_id,
            expected_prepared_operation_hash=expected_prepared_operation_hash,
            expected_state=expected_state,
            new_state="approved",
            sequence=1,
            state_change=state_change,
            ledger_event=ledger_event,
            approval=approval,
            deadline=deadline,
        )
        return _to_approval_outcome(outcome)

    # -- DecisionStore (reject / cancel / expire) ------------------------

    def load_for_decision(self, operation_id: str) -> StoredPreparedOperation | None:
        return self._load_stored(operation_id)

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
    ) -> DecisionCommitOutcome:
        deadline = _utc(commit_not_after, "commit_not_after")
        if self._clock() >= deadline:
            return DecisionCommitOutcome.DEADLINE_EXPIRED
        return self._commit_terminal(
            operation_id=operation_id,
            expected_prepared_operation_hash=expected_prepared_operation_hash,
            expected_state=expected_state,
            new_state=new_state,
            sequence=1,
            state_change=state_change,
            ledger_event=ledger_event,
            approval=approval,
            deadline=deadline,
        )

    # -- Shared terminal-commit transaction ------------------------------

    def _commit_terminal(
        self,
        *,
        operation_id: str,
        expected_prepared_operation_hash: str,
        expected_state: str,
        new_state: str,
        sequence: int,
        state_change: Mapping[str, Any],
        ledger_event: Mapping[str, Any],
        approval: Mapping[str, Any] | None,
        deadline: datetime,
    ) -> DecisionCommitOutcome:
        op_pk = f"OP#{operation_id}"

        snapshot_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": op_pk, "SK": _STATE_SNAPSHOT_SK}),
                "UpdateExpression": "SET #s = :new, #seq = :seq",
                "ConditionExpression": (
                    "attribute_exists(PK) AND #s = :expected AND prepared_hash = :hash AND #seq = :prev_seq"
                ),
                "ExpressionAttributeNames": {"#s": "state", "#seq": "sequence"},
                "ExpressionAttributeValues": _marshal(
                    {
                        ":new": new_state,
                        ":seq": sequence,
                        ":expected": expected_state,
                        ":hash": expected_prepared_operation_hash,
                        ":prev_seq": sequence - 1,
                    }
                ),
            }
        }
        transition_item = _marshal(
            {
                "PK": op_pk,
                "SK": f"STATE#{sequence}",
                "record_type": "operation_state_transition",
                "contract_version": _CONTRACT_VERSION,
                "operation_id": operation_id,
                "sequence": sequence,
                "state_change_json": _canonical_json(state_change),
            }
        )
        ledger_item = _marshal(
            {
                "PK": op_pk,
                "SK": f"LEDGER#{sequence}",
                "record_type": "operation_ledger_event",
                "contract_version": _CONTRACT_VERSION,
                "operation_id": operation_id,
                "sequence": sequence,
                "ledger_json": _canonical_json(ledger_event),
            }
        )
        transact_items: list[dict[str, Any]] = [
            snapshot_update,
            self._conditional_put(transition_item, "attribute_not_exists(SK)"),
            self._conditional_put(ledger_item, "attribute_not_exists(SK)"),
        ]
        if approval is not None:
            approval_item = _marshal(
                {
                    "PK": op_pk,
                    "SK": _APPROVAL_SK,
                    "record_type": "approval_record",
                    "contract_version": _CONTRACT_VERSION,
                    "operation_id": operation_id,
                    "prepared_hash": expected_prepared_operation_hash,
                    "approval_json": _canonical_json(approval),
                }
            )
            transact_items.append(self._conditional_put(approval_item, "attribute_not_exists(SK)"))

        if self._clock() >= deadline:
            return DecisionCommitOutcome.DEADLINE_EXPIRED

        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            # Only a genuine, pure ConditionalCheckFailed is a precondition
            # failure (the state/hash moved, or a transition/ledger/approval SK
            # already exists) — a real, safe conflict the caller reports as a
            # STATE_CONFLICT. A throttle, transaction conflict, capacity limit,
            # validation error, unknown reason, or absent reasons is transient
            # and MUST NOT masquerade as a conflict; it fails closed loudly so
            # the service boundary maps it to a retryable unavailable outcome.
            if not _is_conditional_failure(exc):
                _log_store_exception("commit_terminal_decision", exc, classification="unavailable")
                raise ApprovalStoreError("terminal decision commit failed and is retryable") from exc
            return DecisionCommitOutcome.PRECONDITION_FAILED

        return DecisionCommitOutcome.RECORDED

    # -- Loads ------------------------------------------------------------

    def _load_stored(self, operation_id: str) -> StoredPreparedOperation | None:
        prepared = self._get(f"OP#{operation_id}", _PREPARED_SK)
        snapshot = self._get(f"OP#{operation_id}", _STATE_SNAPSHOT_SK)
        if prepared is None or snapshot is None:
            return None
        operation_json = prepared.get("operation_json")
        if not isinstance(operation_json, str):
            return None
        try:
            # Standard library
            import json

            document = json.loads(operation_json)
        except (ValueError, TypeError):
            return None
        prepared_hash = prepared.get("prepared_hash")
        state = snapshot.get("state")
        if not isinstance(prepared_hash, str) or not isinstance(state, str) or not isinstance(document, dict):
            return None
        return StoredPreparedOperation(document, prepared_hash, state)

    # -- Evidence load ----------------------------------------------------

    def load_operation_evidence(self, operation_id: str) -> OperationEvidence | None:
        """Read the bounded durable evidence for one operation (read-only).

        Returns the immutable prepared operation, its bound ``prepared_hash``,
        the current state, the approval record when present, and the append-only
        ledger (each entry reduced to its sequence, event type, and timestamp).
        Returns ``None`` when the operation does not exist. The read is a bounded
        set of key lookups plus a bounded ledger walk; it issues no Scan.
        """
        # Standard library
        import json

        op_pk = f"OP#{operation_id}"
        prepared = self._get(op_pk, _PREPARED_SK)
        snapshot = self._get(op_pk, _STATE_SNAPSHOT_SK)
        if prepared is None or snapshot is None:
            return None
        operation_json = prepared.get("operation_json")
        prepared_hash = prepared.get("prepared_hash")
        state = snapshot.get("state")
        if not isinstance(operation_json, str) or not isinstance(prepared_hash, str) or not isinstance(state, str):
            return None
        try:
            document = json.loads(operation_json)
        except (ValueError, TypeError):
            return None
        if not isinstance(document, dict):
            return None

        approval: dict[str, Any] | None = None
        approval_item = self._get(op_pk, _APPROVAL_SK)
        if approval_item is not None:
            approval_json = approval_item.get("approval_json")
            if isinstance(approval_json, str):
                try:
                    parsed = json.loads(approval_json)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    approval = parsed

        ledger = self._load_ledger(op_pk)
        return OperationEvidence(
            operation=document,
            prepared_hash=prepared_hash,
            state=state,
            approval=approval,
            ledger=ledger,
        )

    def _load_ledger(self, op_pk: str) -> list[dict[str, Any]]:
        """Walk the bounded, contiguous LEDGER#<seq> chain from sequence 0."""
        # Standard library
        import json

        entries: list[dict[str, Any]] = []
        # A prepared operation transitions at most once beyond its initial
        # pending_approval materialization, so the ledger is short and bounded.
        for sequence in range(_MAX_LEDGER_SEQUENCE + 1):
            item = self._get(op_pk, f"LEDGER#{sequence}")
            if item is None:
                break
            ledger_json = item.get("ledger_json")
            event_type: Any = None
            occurred_at: Any = None
            if isinstance(ledger_json, str):
                try:
                    parsed = json.loads(ledger_json)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    event_type = parsed.get("event_type")
                    occurred_at = parsed.get("occurred_at")
            # ``sequence`` reported here is the physical ``LEDGER#<n>`` sort-key
            # index (0 for the initial materialization, 1 for the terminal
            # decision), NOT the ledger event payload's own contract ``sequence``
            # field (which the ledger-event schema floors at 1). The two are
            # intentionally distinct; evidence ordering follows the physical key.
            entries.append({"sequence": sequence, "event_type": event_type, "occurred_at": occurred_at})
        return entries

    # -- Low-level helpers ------------------------------------------------

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_marshal({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return _unmarshal(item)

    def _conditional_put(self, item: dict[str, dict[str, Any]], condition: str) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": item,
                "ConditionExpression": condition,
            }
        }


# -- Canonical JSON + record builders ----------------------------------------


def _canonical_json(document: Mapping[str, Any]) -> str:
    # Third-party packages
    import rfc8785

    canonical: str = rfc8785.dumps(dict(document)).decode("utf-8")
    return canonical


def _approval_state_change(operation_id: str, prepared_hash: str, approval: Mapping[str, Any]) -> dict[str, Any]:
    """A server-owned state change binding a granted approval to ``approved``."""
    return {
        "state_contract_version": _CONTRACT_VERSION,
        "state_change_id": f"state.{approval['approval_id']}",
        "operation_id": operation_id,
        "prepared_operation_hash": prepared_hash,
        "previous_state": _PENDING_APPROVAL,
        "new_state": "approved",
        "attempt": 0,
        "reason_code": "APPROVAL_GRANTED",
        "changed_at": approval["decided_at"],
        "actor": {
            "actor_type": "principal",
            "actor_id": approval["approver"]["subject_id"],
            "client_id": approval["approver"]["client_id"],
        },
        "correlation": dict(approval["correlation"]),
    }


def _approval_ledger_event(operation_id: str, prepared_hash: str, approval: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ledger_contract_version": _CONTRACT_VERSION,
        "event_id": f"event.{approval['approval_id']}",
        "event_type": "approval.recorded",
        "operation_id": operation_id,
        "prepared_operation_hash": prepared_hash,
        "sequence": 1,
        "occurred_at": approval["decided_at"],
        "actor": {
            "actor_type": "principal",
            "actor_id": approval["approver"]["subject_id"],
            "client_id": approval["approver"]["client_id"],
        },
        "correlation": dict(approval["correlation"]),
        "payload": {
            "payload_type": "approval_recorded",
            "approval_id": approval["approval_id"],
            "decision": "granted",
        },
    }


def _to_approval_outcome(outcome: DecisionCommitOutcome) -> ApprovalCommitOutcome:
    if outcome is DecisionCommitOutcome.RECORDED:
        return ApprovalCommitOutcome.RECORDED
    if outcome is DecisionCommitOutcome.DEADLINE_EXPIRED:
        return ApprovalCommitOutcome.DEADLINE_EXPIRED
    return ApprovalCommitOutcome.PRECONDITION_FAILED


# -- Real-botocore ClientError classification (shared shape with E1 store) ----

_CONDITIONAL_REASON = "ConditionalCheckFailed"
_TRANSACTION_CANCELED_CODE = "TransactionCanceledException"
_BARE_CONDITIONAL_CODE = "ConditionalCheckFailedException"
_TRANSIENT_REASONS = frozenset(
    {
        "TransactionConflict",
        "ThrottlingError",
        "ProvisionedThroughputExceeded",
        "ValidationError",
    }
)


def _cancellation_reason_codes(response: dict[str, Any]) -> set[str] | None:
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return None
    return {r.get("Code") for r in reasons if isinstance(r, dict)}


def _is_conditional_failure(exc: Exception) -> bool:
    """Return whether an error is a genuine conditional-check failure.

    Reads the real botocore wire shape from ``exc.response``. Only a bare
    ``ConditionalCheckFailedException`` or a ``TransactionCanceledException``
    whose ``CancellationReasons`` contain ``ConditionalCheckFailed`` AND no
    transient/validation reason is conditional. Every other error — a throttle,
    transaction conflict, capacity limit, validation error, unknown reason,
    absent/empty reasons, or a mix — fails closed as retryable/unavailable.
    """
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


# -- Bounded, sanitized store-boundary diagnostics (stdlib logging) -----------

_MAX_CODE_LEN = 128
_MAX_REASON_CODES = 16
_DIAG_FIELD_SEP = " "
_SAFE_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_LOGGER = logging.getLogger(__name__)


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    code = response.get("Error", {}).get("Code")
    if not isinstance(code, str) or not code:
        return None
    return code[:_MAX_CODE_LEN]


def _bounded_reason_codes(exc: Exception) -> list[str]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return []
    codes = _cancellation_reason_codes(response)
    if not codes:
        return []
    bounded = sorted(code[:_MAX_CODE_LEN] for code in codes if isinstance(code, str) and code and code != "None")
    return bounded[:_MAX_REASON_CODES]


def _safe_token(value: str) -> str:
    return "".join(ch if ch in _SAFE_TOKEN_CHARS else "." for ch in value)


def _log_store_exception(operation: str, exc: Exception, *, classification: str) -> None:
    """Emit a bounded, sanitized diagnostic for a store-boundary error.

    Logs ONLY the operation name, the exception type, the AWS error code, and
    the bounded cancellation-reason codes — never the exception message,
    request items, ids, tokens, table/account/ARN, provider data, a traceback,
    or ``exc_info``. The record is a fully-formed ``key=value`` message string
    (Lambda/CloudWatch renders ``%(message)s`` only), with the two
    provider-controlled tokens passed through :func:`_safe_token`.
    """
    error_code = _aws_error_code(exc)
    reason_codes = _bounded_reason_codes(exc)
    fields = [
        "dynamodb_approval_store_exception",
        "operation=" + _safe_token(operation),
        "classification=" + _safe_token(classification),
        "exception_type=" + _safe_token(type(exc).__name__),
        "aws_error_code=" + (_safe_token(error_code) if error_code else "none"),
        "cancellation_reason_codes="
        + (",".join(_safe_token(code) for code in reason_codes) if reason_codes else "none"),
    ]
    _LOGGER.warning(_DIAG_FIELD_SEP.join(fields))


# -- DynamoDB marshalling (scalar subset this store writes) -------------------


def _marshal(item: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: _marshal_value(value, key) for key, value in item.items()}


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
