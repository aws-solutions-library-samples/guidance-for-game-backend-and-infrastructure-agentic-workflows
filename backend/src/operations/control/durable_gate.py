"""Durable intent fence for immediate E4 hard-down propagation (#416).

The AppConfig Lambda extension intentionally caches configuration between
provider polls. A control CAS therefore records the exact validated target
posture in DynamoDB before AppConfig propagation. Every lifecycle gate intersects
its extension decision with that latest durable intent. A newer durable disable
blocks immediately, while a newer enable cannot bypass the still-restrictive
extension decision.

Legacy state items without ``document_json`` preserve the extension-only
behavior. Once exact state is present, malformed/inconsistent data or a DynamoDB
read failure fails closed.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any, Protocol

# Local modules
from operations.contracts.control_plane import (
    CAPABILITY_ID,
    CONTROL_PHASES,
    KILL_SWITCH_SCHEMA_NAME,
    ControlContractError,
    validate_control_contract,
)

_CONTROL_PK = "OPCONTROL#kill-switch"
_STATE_SK = "STATE#current"


class DurableControlUnavailable(RuntimeError):
    """The latest durable control intent could not be verified."""


class DurablePhaseDenied(RuntimeError):
    """A newer/equal durable control intent disables the requested phase."""


class DynamoDbClientPort(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...


class DynamoDbDurableControlGate:
    """Intersect a deployed AppConfig decision with the latest durable intent."""

    def __init__(self, *, client: DynamoDbClientPort, table_name: str) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._client = client
        self._table_name = table_name

    def require_phase(self, phase: str, *, deployed_decision: Any) -> None:
        if phase not in CONTROL_PHASES:
            raise ValueError("phase must be a supported control phase")
        deployed_version = getattr(deployed_decision, "config_version", None)
        if not isinstance(deployed_version, int) or isinstance(deployed_version, bool):
            raise DurableControlUnavailable("deployed decision has no valid version")
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={"PK": {"S": _CONTROL_PK}, "SK": {"S": _STATE_SK}},
                ConsistentRead=True,
                ProjectionExpression="config_version, document_json",
            )
        except Exception as exc:  # noqa: BLE001 - every provider failure fails closed
            raise DurableControlUnavailable("durable control state is unavailable") from exc
        item = response.get("Item") if isinstance(response, dict) else None
        if not isinstance(item, dict):
            return
        raw_version = item.get("config_version")
        version_text = raw_version.get("N") if isinstance(raw_version, dict) else None
        try:
            durable_version = int(version_text)
        except (TypeError, ValueError) as exc:
            raise DurableControlUnavailable("durable control version is invalid") from exc
        raw_document = item.get("document_json")
        document_text = raw_document.get("S") if isinstance(raw_document, dict) else None
        if document_text is None:
            # Backward compatibility for state written before exact intent was
            # stored. The fresh extension decision remains authoritative.
            return
        if not isinstance(document_text, str):
            raise DurableControlUnavailable("durable control document is invalid")
        try:
            document = json.loads(document_text)
            validate_control_contract(KILL_SWITCH_SCHEMA_NAME, document)
        except (ValueError, TypeError, ControlContractError) as exc:
            raise DurableControlUnavailable("durable control document is invalid") from exc
        if not isinstance(document, dict) or document.get("config_version") != durable_version:
            raise DurableControlUnavailable("durable control document version does not match state")
        if durable_version < deployed_version:
            return
        switches = document["capabilities"][CAPABILITY_ID]
        if not document["operations_enabled"] or not switches[phase]:
            raise DurablePhaseDenied("phase disabled by latest durable control intent")
