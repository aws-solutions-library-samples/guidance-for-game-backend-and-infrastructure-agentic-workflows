"""DynamoDB control audit store: CAS state + immutable audit records (#416).

This is the durable backing store for the admin kill-switch control plane. It
holds one authoritative control-state item carrying the monotonic
``config_version`` and it records two kinds of immutable, hash-bound,
never-expiring audit records:

* a *control intent* record, written before a decision is attempted, capturing
  the acting admin (subject/client identifiers only) and the desired boolean
  state; and
* a *control outcome* record, written with a separate idempotent ``PutItem``
  immediately after the conditional ``UpdateItem`` that advances the state item,
  capturing before/after ``config_version`` and the decision outcome. The state
  advance is the atomic compare-and-set; no ``TransactWriteItems`` is used.

The decision commit is a compare-and-set on ``expected_config_version``: the
state item advances only if its stored ``config_version`` still equals the
version the admin based the change on. A stale write (the stored version already
moved on, e.g. a concurrent admin) fails the conditional check and is reported as
:attr:`ControlCommitOutcome.VERSION_CONFLICT` — never a silent clobber and never
a fabricated success. Only a *pure* conditional-check failure is a conflict; a
throttle, transaction conflict, validation error, or any other fault is raised
as a retryable :class:`ControlStoreError`.

TTL policy: control audit records and the control-state item are permanent and
never carry a ``ttl``. The store issues no ``Scan``.
"""

from __future__ import annotations

# Standard library
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Local modules
from operations.contracts.control_plane import (
    CONTROL_AUDIT_RECORD_SCHEMA_NAME,
    CONTROL_CONTRACT_VERSION,
    ControlContractError,
    control_audit_record_hash,
    validate_control_contract,
)

_LOGGER = logging.getLogger(__name__)

# The single control partition. The state item holds the authoritative
# config_version; audit records live under the same partition with distinct SKs.
_CONTROL_PK = "OPCONTROL#kill-switch"
_STATE_SK = "STATE#current"
_INTENT_SK_PREFIX = "CTLINTENT#"
_OUTCOME_SK_PREFIX = "CTLAUDIT#"

_INTENT_RECORD_TYPE = "control_intent_record"
_AUDIT_RECORD_TYPE = "control_audit_record"
_STATE_RECORD_TYPE = "control_state"


class ControlStoreError(RuntimeError):
    """A control store write failed transiently and is retryable (fail closed)."""


class ControlCommitOutcome(str, Enum):
    """Authoritative result of a control compare-and-set commit."""

    COMMITTED = "committed"
    VERSION_CONFLICT = "version_conflict"


class DynamoDbControlAuditStore:
    """CAS control-state store with immutable, hash-bound audit records."""

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

    # -- state -----------------------------------------------------------

    def initialize_state_if_absent(self, *, config_version: int) -> None:
        """Create the control-state item once; a no-op if it already exists."""
        item = _marshal(
            {
                "PK": _CONTROL_PK,
                "SK": _STATE_SK,
                "record_type": _STATE_RECORD_TYPE,
                "contract_version": CONTROL_CONTRACT_VERSION,
                "config_version": int(config_version),
            }
        )
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:  # noqa: BLE001 - a pre-existing item is expected, not an error
            if _is_conditional_failure(exc):
                return
            raise ControlStoreError("could not initialize control state") from exc

    def current_config_version(self) -> int | None:
        """Return the authoritative stored ``config_version``, or ``None``."""
        state = self._get(_CONTROL_PK, _STATE_SK)
        if state is None:
            return None
        version = state.get("config_version")
        return version if isinstance(version, int) else None

    # -- intent ----------------------------------------------------------

    def record_control_intent(self, *, record_id: str, actor: Mapping[str, str], desired: Mapping[str, Any]) -> None:
        """Write the immutable pre-decision intent record (never expires)."""
        item = _marshal(
            {
                "PK": _CONTROL_PK,
                "SK": f"{_INTENT_SK_PREFIX}{record_id}",
                "record_type": _INTENT_RECORD_TYPE,
                "contract_version": CONTROL_CONTRACT_VERSION,
                "record_id": record_id,
                "recorded_at": self._now_iso(),
                "actor_subject_id": actor["subject_id"],
                "actor_client_id": actor["client_id"],
                "desired_json": _canonical_json(desired),
            }
        )
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                # A same record_id intent already exists: idempotent, not an error.
                return
            raise ControlStoreError("could not record control intent") from exc

    # -- commit (CAS) ----------------------------------------------------

    def commit_control_decision(
        self,
        *,
        record_id: str,
        actor: Mapping[str, str],
        expected_config_version: int,
        desired: Mapping[str, Any],
        outcome: str,
        resulting_config_version: int,
    ) -> ControlCommitOutcome:
        """CAS-advance the state and write the immutable outcome audit record.

        The state item advances only if its stored ``config_version`` equals
        ``expected_config_version`` (a conditional ``UpdateItem``). On a stale
        version the conditional check fails and
        :attr:`ControlCommitOutcome.VERSION_CONFLICT` is returned; neither the
        state nor the outcome audit record is written.
        """
        # Always record the immutable pre-decision intent first (idempotent).
        self.record_control_intent(record_id=record_id, actor=actor, desired=desired)

        audit_record = self._build_audit_record(
            record_id=record_id,
            actor=actor,
            expected_config_version=expected_config_version,
            resulting_config_version=resulting_config_version,
            desired=desired,
            outcome=outcome,
        )

        # CAS-advance the state item with a conditional UpdateItem. Only IAM
        # actions PutItem/UpdateItem/GetItem are granted to the control store, so
        # this is deliberately NOT a TransactWriteItems call (which would require
        # an ungranted action and fail with AccessDenied at runtime). The state
        # advance is the atomic compare-and-set that decides the outcome: if it
        # succeeds, the immutable outcome audit record is written with a separate
        # idempotent PutItem; if the version moved on, the conditional check
        # fails and no audit outcome record is written.
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=_marshal({"PK": _CONTROL_PK, "SK": _STATE_SK}),
                UpdateExpression="SET config_version = :new_version",
                ConditionExpression="attribute_exists(PK) AND config_version = :expected",
                ExpressionAttributeValues=_marshal(
                    {":new_version": int(resulting_config_version), ":expected": int(expected_config_version)}
                ),
            )
        except Exception as exc:  # noqa: BLE001 - classify a pure conditional race
            if _is_conditional_failure(exc):
                # The stored version moved on: a genuine CAS conflict, never a
                # silent success and never a fabricated audit outcome record.
                return ControlCommitOutcome.VERSION_CONFLICT
            _LOGGER.error("control state advance failed and is retryable")
            raise ControlStoreError("control decision commit failed") from exc

        # The state advanced. Persist the immutable, hash-bound outcome record
        # with an idempotent conditional PutItem. A duplicate record_id (a retry
        # after the state already advanced) is a no-op, not an error.
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_marshal(
                    {
                        "PK": _CONTROL_PK,
                        "SK": f"{_OUTCOME_SK_PREFIX}{record_id}",
                        "record_type": _AUDIT_RECORD_TYPE,
                        "contract_version": CONTROL_CONTRACT_VERSION,
                        "record_id": record_id,
                        "audit_json": _canonical_json(audit_record),
                    }
                ),
                ConditionExpression="attribute_not_exists(SK)",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                # The outcome record already exists for this record_id: the
                # decision is already recorded and committed. Idempotent.
                return ControlCommitOutcome.COMMITTED
            _LOGGER.error("control outcome audit write failed and is retryable")
            raise ControlStoreError("control audit outcome write failed") from exc

        return ControlCommitOutcome.COMMITTED

    # -- internals -------------------------------------------------------

    def _build_audit_record(
        self,
        *,
        record_id: str,
        actor: Mapping[str, str],
        expected_config_version: int,
        resulting_config_version: int,
        desired: Mapping[str, Any],
        outcome: str,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "contract_version": CONTROL_CONTRACT_VERSION,
            "record_id": record_id,
            "recorded_at": self._now_iso(),
            "actor": {"subject_id": actor["subject_id"], "client_id": actor["client_id"]},
            "outcome": outcome,
            "previous_config_version": int(expected_config_version),
            "resulting_config_version": int(resulting_config_version),
            "desired": {
                "operations_enabled": bool(desired["operations_enabled"]),
                "capabilities": {
                    key: {
                        "prepare": bool(value["prepare"]),
                        "dispatch": bool(value["dispatch"]),
                        "execute": bool(value["execute"]),
                    }
                    for key, value in desired["capabilities"].items()
                },
            },
        }
        record["record_hash"] = control_audit_record_hash(record)
        # Fail closed if our own audit record is malformed rather than persist it.
        try:
            validate_control_contract(CONTROL_AUDIT_RECORD_SCHEMA_NAME, record)
        except ControlContractError as exc:  # pragma: no cover - server-owned output
            raise ControlStoreError("control audit record failed its contract") from exc
        return record

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=_marshal({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return _unmarshal(item) if item else None


# -- marshalling + error classification --------------------------------------


def _canonical_json(document: Mapping[str, Any]) -> str:
    # Standard library
    import json

    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _marshal(item: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: _marshal_value(value, key) for key, value in item.items()}


def _marshal_value(value: Any, key: str) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
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


def _is_conditional_failure(exc: Exception) -> bool:
    """Return whether ``exc`` is a *pure* conditional-check failure.

    A bare ``ConditionalCheckFailedException`` or a ``TransactionCanceledException``
    whose cancellation reasons are all conditional (no throttle/validation/other)
    is a genuine precondition race. Anything else — a throttle, transaction
    conflict, capacity limit, validation error, unknown or absent reason — is
    transient and MUST NOT masquerade as a conflict.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, Mapping) else None
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list) or not reasons:
        return False
    saw_conditional = False
    for reason in reasons:
        reason_code = reason.get("Code") if isinstance(reason, Mapping) else None
        if reason_code == "ConditionalCheckFailed":
            saw_conditional = True
        elif reason_code not in (None, "None"):
            # A transient/validation/other reason: not a pure conditional failure.
            return False
    return saw_conditional
