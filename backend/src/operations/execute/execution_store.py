"""DynamoDB E3 execution store: fenced, atomic, one-logical-update store (#415).

This is the durable backing store for one logical capacity update. It provides
three operations, all keyed by the operation and its stable, attempt-independent
``logical_action_id``:

* :meth:`acquire_execution_lease` — conditionally take a fenced single-flight
  execution lease and return the fencing ``generation``. If a terminal result
  was already recorded for this ``logical_action_id`` it is replayed instead
  (idempotent: one operation produces at most one logical update, so a re-invoke
  never writes twice).
* :meth:`record_execution_result` — atomically persist, under lease+generation
  fencing, the provider intent, the normalized result, the post-action
  verification, the operation state change, and a ledger event, all in one
  transaction. Returns the authoritative :class:`ExecutionCommitOutcome`.
* :meth:`load_recorded_result` — read a prior terminal result for idempotent
  replay.

TTL policy
----------

**Audit records carry no TTL.** The provider intent, the result, the
verification, the state snapshot, the state change, and the ledger events are
permanent audit records and never carry a ``ttl`` attribute. **Only the
transient single-flight lease item may carry a ``ttl``** so an abandoned lease
expires and a later attempt can fence past it. The one-logical-update and
single-flight guarantees come from the conditional/transactional writes, not
from IAM alone.

Error classification (shared shape with the E1/E2 stores)
---------------------------------------------------------

Only a *pure* ``ConditionalCheckFailed`` (a bare
``ConditionalCheckFailedException`` or a ``TransactionCanceledException`` whose
cancellation reasons are conditional with no transient/validation reason) is a
precondition/idempotency race, surfaced as
:attr:`ExecutionCommitOutcome.PRECONDITION_FAILED`. Every throttle, transaction
conflict, capacity limit, validation error, unknown reason, or provider fault is
raised as a retryable :class:`ExecutionStoreError` — never silently swallowed
and never a false precondition failure.
"""

from __future__ import annotations

# Standard library
import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from typing import Any

_INITIAL_GENERATION = 1

_LEASE_SK = "EXECLEASE"
_STATE_SNAPSHOT_SK = "EXECSTATE"
_INTENT_SK = "EXECINTENT"
_RESULT_SK = "EXECRESULT"
_VERIFICATION_SK = "EXECVERIFY"
_PROVIDER_RECEIPT_SK = "EXECPROVIDER"
_MAX_PROVIDER_REQUEST_ID_LENGTH = 256
_PROVIDER_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+=@-]{0,255}$")


class ExecutionStoreError(RuntimeError):
    """A retryable, non-precondition failure of the execution store transaction."""


class ExecutionCommitOutcome(str, Enum):
    """Authoritative result of the fenced execution-result transaction."""

    RECORDED = "recorded"
    PRECONDITION_FAILED = "precondition_failed"


class LeaseAcquisition:
    """The result of acquiring (or replaying past) one execution lease."""

    __slots__ = ("generation", "recorded_result")

    def __init__(self, *, generation: int, recorded_result: dict[str, Any] | None) -> None:
        self.generation = generation
        self.recorded_result = recorded_result


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


class DynamoDbExecutionStore:
    """Conditional, idempotent, append-only, lease-fenced E3 execution store."""

    def __init__(self, *, client: Any, table_name: str, clock: Any = _system_clock) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._client = client
        self._table_name = table_name
        self._clock = clock

    # -- Lease acquisition / idempotent replay ---------------------------

    def acquire_execution_lease(
        self,
        *,
        operation_id: str,
        logical_action_id: str,
        lease_holder: str,
        lease_not_after: datetime,
    ) -> LeaseAcquisition:
        """Take a fenced execution lease, or replay a prior terminal result.

        The lease is keyed by ``logical_action_id`` so every re-invocation of the
        same operation contends for the same lease and can never spawn a second
        logical update. If a terminal result already exists it is returned and no
        new lease is written.
        """
        recorded = self.load_recorded_result(logical_action_id)
        if recorded is not None:
            return LeaseAcquisition(generation=_INITIAL_GENERATION, recorded_result=recorded)

        deadline = _utc(lease_not_after, "lease_not_after")
        lease_epoch_s = int(deadline.timestamp())
        action_pk = f"EXEC#{logical_action_id}"

        lease_item = _marshal(
            {
                "PK": action_pk,
                "SK": _LEASE_SK,
                "operation_id": operation_id,
                "logical_action_id": logical_action_id,
                "lease_holder": lease_holder,
                "lease_not_after": lease_epoch_s,
                "generation": _INITIAL_GENERATION,
                # TTL ONLY on the transient lease item.
                "ttl": lease_epoch_s,
            }
        )
        try:
            self._client.transact_write_items(
                TransactItems=[self._conditional_put(lease_item, "attribute_not_exists(SK)")]
            )
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if _is_conditional_failure(exc):
                # A live lease already exists for this logical action. Re-read a
                # possible terminal result; otherwise report the existing lease's
                # generation so the caller fences correctly.
                recorded = self.load_recorded_result(logical_action_id)
                existing = self._get(action_pk, _LEASE_SK)
                generation = existing.get("generation") if isinstance(existing, dict) else None
                return LeaseAcquisition(
                    generation=generation if isinstance(generation, int) else _INITIAL_GENERATION,
                    recorded_result=recorded,
                )
            _log_store_exception("acquire_execution_lease", exc, classification="unavailable")
            raise ExecutionStoreError("execution store is unavailable") from exc

        return LeaseAcquisition(generation=_INITIAL_GENERATION, recorded_result=None)

    # -- Fenced, atomic result commit ------------------------------------

    def record_execution_result(
        self,
        *,
        operation_id: str,
        logical_action_id: str,
        generation: int,
        expected_state: str,
        new_state: str,
        result: Mapping[str, Any],
        provider_request_id: str | None = None,
    ) -> ExecutionCommitOutcome:
        """Atomically record provider intent/result/verification/state/ledger.

        All records are written in ONE transaction fenced on the held generation
        and the expected prior operation state, so a superseded writer or a
        state race fails closed as :attr:`ExecutionCommitOutcome.PRECONDITION_FAILED`.
        The result, verification, intent, state change, and ledger records carry
        NO ``ttl``.
        """
        op_pk = f"OP#{operation_id}"
        action_pk = f"EXEC#{logical_action_id}"
        # Use the injected clock (not the module-level system clock) so every
        # persisted audit timestamp is deterministic and testable.
        recorded_at = _utc(self._clock(), "clock").isoformat().replace("+00:00", "Z")
        result_doc = deepcopy(dict(result))
        provider_receipt = _provider_receipt_item(
            operation_id=operation_id,
            logical_action_id=logical_action_id,
            result=result_doc,
            provider_request_id=provider_request_id,
        )

        result_item = _marshal(
            {
                "PK": action_pk,
                "SK": _RESULT_SK,
                "operation_id": operation_id,
                "logical_action_id": logical_action_id,
                "outcome": result_doc["outcome"],
                "document": _canonical_json(result_doc),
                "generation": generation,
            }
        )
        verification_item = _marshal(
            {
                "PK": action_pk,
                "SK": _VERIFICATION_SK,
                "operation_id": operation_id,
                "document": _canonical_json(result_doc["verification"]),
            }
        )
        intent_item = _marshal(
            {
                "PK": action_pk,
                "SK": _INTENT_SK,
                "operation_id": operation_id,
                "intent_hash": result_doc.get("intent_hash", ""),
                "provider_write_issued": bool(result_doc.get("provider_write_issued", False)),
            }
        )
        state_change_item = _marshal(
            {
                "PK": op_pk,
                "SK": f"STATE#{new_state}#{logical_action_id}",
                "operation_id": operation_id,
                "previous_state": expected_state,
                "state": new_state,
                "logical_action_id": logical_action_id,
                "recorded_at": recorded_at,
            }
        )
        ledger_item = _marshal(
            {
                "PK": op_pk,
                "SK": f"LEDGER#EXEC#{logical_action_id}",
                "operation_id": operation_id,
                "event_type": "operation.execution_recorded",
                "outcome": result_doc["outcome"],
                "generation": generation,
                "recorded_at": recorded_at,
            }
        )
        # Fence the lease snapshot on the held generation so a superseded writer
        # (whose generation was advanced by a reclaim) fails closed.
        lease_fence = {
            "Update": {
                "TableName": self._table_name,
                "Key": _marshal({"PK": action_pk, "SK": _LEASE_SK}),
                "UpdateExpression": "SET committed = :true",
                "ConditionExpression": "attribute_exists(SK) AND #gen = :gen",
                "ExpressionAttributeNames": {"#gen": "generation"},
                "ExpressionAttributeValues": _marshal_values({":true": True, ":gen": generation}),
            }
        }

        transact_items: list[dict[str, Any]] = [
            lease_fence,
            self._conditional_put(result_item, "attribute_not_exists(SK)"),
            self._conditional_put(verification_item, "attribute_not_exists(SK)"),
            self._conditional_put(intent_item, "attribute_not_exists(SK)"),
            *(
                [self._conditional_put(provider_receipt, "attribute_not_exists(SK)")]
                if provider_receipt is not None
                else []
            ),
            self._conditional_put(state_change_item, "attribute_not_exists(SK)"),
            self._conditional_put(ledger_item, "attribute_not_exists(SK)"),
        ]
        try:
            self._client.transact_write_items(TransactItems=transact_items)
        except Exception as exc:  # noqa: BLE001 - classify by cancellation reason
            if _is_conditional_failure(exc):
                return ExecutionCommitOutcome.PRECONDITION_FAILED
            _log_store_exception("record_execution_result", exc, classification="unavailable")
            raise ExecutionStoreError("execution store is unavailable") from exc
        return ExecutionCommitOutcome.RECORDED

    # -- Idempotent result read ------------------------------------------

    def load_recorded_result(self, logical_action_id: str) -> dict[str, Any] | None:
        """Return the prior terminal result document for one logical action, if any."""
        action_pk = f"EXEC#{logical_action_id}"
        item = self._get(action_pk, _RESULT_SK)
        if not item:
            return None
        document = item.get("document")
        if not isinstance(document, str):
            return None
        try:
            # Standard library
            import json

            parsed = json.loads(document)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def load_provider_write_receipt(self, operation_id: str) -> dict[str, Any] | None:
        """Return bounded, operation-scoped provider correlation evidence.

        The receipt is written atomically with the terminal E3 result. It is
        intentionally separate from the frozen execution-result contract because
        it contains an opaque provider request id used only by the E5 validation
        path. Missing or malformed evidence is unavailable, never reconstructed.
        """
        item = self._get(f"OP#{operation_id}", _PROVIDER_RECEIPT_SK)
        if not isinstance(item, dict) or item.get("operation_id") != operation_id:
            return None
        logical_action_id = item.get("logical_action_id")
        provider_request_id = item.get("provider_request_id")
        expected_capacity = item.get("expected_capacity")
        if not isinstance(logical_action_id, str) or not isinstance(provider_request_id, str):
            return None
        if not _valid_provider_request_id(provider_request_id):
            return None
        if not isinstance(expected_capacity, str):
            return None
        try:
            # Standard library
            import json

            parsed_capacity = json.loads(expected_capacity)
        except (TypeError, ValueError):
            return None
        if not _valid_capacity(parsed_capacity):
            return None
        return {
            "operation_id": operation_id,
            "logical_action_id": logical_action_id,
            "provider_request_id": provider_request_id,
            "expected_capacity": parsed_capacity,
        }

    # -- Low-level helpers -----------------------------------------------

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_marshal({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, dict) else None
        return _unmarshal(item) if isinstance(item, dict) else None

    def _conditional_put(self, item: dict[str, dict[str, Any]], condition: str) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": item,
                "ConditionExpression": condition,
            }
        }


def _canonical_json(document: Mapping[str, Any]) -> str:
    # Standard library
    import json

    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _valid_provider_request_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _MAX_PROVIDER_REQUEST_ID_LENGTH
        and _PROVIDER_REQUEST_ID_PATTERN.fullmatch(value) is not None
    )


def _valid_capacity(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"desired", "minimum", "maximum"}:
        return False
    return all(
        isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000 for item in value.values()
    )


def _provider_receipt_item(
    *,
    operation_id: str,
    logical_action_id: str,
    result: Mapping[str, Any],
    provider_request_id: str | None,
) -> dict[str, dict[str, Any]] | None:
    """Build the bounded operation-scoped receipt written with a provider result.

    A write with no usable response request id remains a valid executor outcome,
    but it yields unavailable attribution evidence. The E5 harness will fail
    closed rather than credit a nearby CloudTrail event to that operation.
    """
    write_issued = result.get("provider_write_issued")
    if write_issued is not True or not _valid_provider_request_id(provider_request_id):
        # Receipt evidence is optional audit enrichment. A malformed or missing
        # provider response must not turn a write that already landed into a lost
        # terminal result; the E5 shakedown treats its absence as unavailable.
        return None
    if result.get("operation_id") != operation_id or result.get("logical_action_id") != logical_action_id:
        return None
    verification = result.get("verification")
    expected_capacity = verification.get("expected_capacity") if isinstance(verification, Mapping) else None
    if not _valid_capacity(expected_capacity):
        return None
    return _marshal(
        {
            "PK": f"OP#{operation_id}",
            "SK": _PROVIDER_RECEIPT_SK,
            "operation_id": operation_id,
            "logical_action_id": logical_action_id,
            "provider_request_id": provider_request_id,
            "expected_capacity": _canonical_json(expected_capacity),
        }
    )


# -- ClientError classification (shared shape with E1/E2 stores) --------------

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


# -- Safe diagnostic logging (bounded metadata only) --------------------------

_MAX_CODE_LEN = 128
_MAX_REASON_CODES = 16
_SAFE_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_LOGGER = logging.getLogger(__name__)


def _safe_token(value: str) -> str:
    return "".join(ch if ch in _SAFE_TOKEN_CHARS else "." for ch in value[:_MAX_CODE_LEN]) or "."


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


def _log_store_exception(operation: str, exc: Exception, *, classification: str) -> None:
    """Emit ONLY bounded, sanitized metadata — never the message or a traceback."""
    error_code = _aws_error_code(exc)
    reason_codes = _bounded_reason_codes(exc)
    _LOGGER.warning(
        "dynamodb_execution_store_exception %s %s %s %s",
        "operation=" + _safe_token(operation),
        "classification=" + _safe_token(classification),
        "exception_type=" + _safe_token(type(exc).__name__),
        "error_code="
        + (_safe_token(error_code) if error_code else "none")
        + (" reason_codes=" + ",".join(_safe_token(code) for code in reason_codes) if reason_codes else ""),
    )


# -- Marshalling --------------------------------------------------------------


def _marshal(item: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: _marshal_value(value, key) for key, value in item.items()}


def _marshal_values(values: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: _marshal_value(value, key) for key, value in values.items()}


def _marshal_value(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if value is None:
        return {"NULL": True}
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
