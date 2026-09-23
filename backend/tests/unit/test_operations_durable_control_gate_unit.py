"""DynamoDB intent fence for immediate E4 hard-down propagation (#416)."""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID
from operations.control.durable_gate import (
    DurableControlUnavailable,
    DurablePhaseDenied,
    DynamoDbDurableControlGate,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _document(version: int, *, enabled: bool, prepare: bool, dispatch: bool, execute: bool) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "config_version": version,
        "issued_at": (_NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "not_after": (_NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": prepare, "dispatch": dispatch, "execute": execute}},
    }


class _Decision:
    def __init__(self, version: int) -> None:
        self.config_version = version


class _Dynamo:
    def __init__(self, item: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.item = item
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"Item": self.item} if self.item is not None else {}


def _item(document: dict[str, Any], *, state_version: int | None = None) -> dict[str, Any]:
    return {
        "PK": {"S": "OPCONTROL#kill-switch"},
        "SK": {"S": "STATE#current"},
        "config_version": {"N": str(state_version if state_version is not None else document["config_version"])},
        "document_json": {"S": json.dumps(document, sort_keys=True, separators=(",", ":"))},
    }


def test_newer_durable_hard_down_blocks_cached_enabled_decision() -> None:
    durable = _document(8, enabled=False, prepare=False, dispatch=False, execute=False)
    gate = DynamoDbDurableControlGate(client=_Dynamo(_item(durable)), table_name="operations")
    with pytest.raises(DurablePhaseDenied):
        gate.require_phase("execute", deployed_decision=_Decision(7))


def test_newer_durable_per_phase_disable_blocks_that_phase() -> None:
    durable = _document(8, enabled=True, prepare=True, dispatch=False, execute=False)
    gate = DynamoDbDurableControlGate(client=_Dynamo(_item(durable)), table_name="operations")
    gate.require_phase("prepare", deployed_decision=_Decision(7))
    with pytest.raises(DurablePhaseDenied):
        gate.require_phase("dispatch", deployed_decision=_Decision(7))


def test_equal_version_durable_disable_is_still_enforced() -> None:
    durable = _document(7, enabled=False, prepare=False, dispatch=False, execute=False)
    gate = DynamoDbDurableControlGate(client=_Dynamo(_item(durable)), table_name="operations")
    with pytest.raises(DurablePhaseDenied):
        gate.require_phase("execute", deployed_decision=_Decision(7))


def test_older_durable_intent_does_not_override_newer_deployed_document() -> None:
    durable = _document(6, enabled=False, prepare=False, dispatch=False, execute=False)
    gate = DynamoDbDurableControlGate(client=_Dynamo(_item(durable)), table_name="operations")
    gate.require_phase("execute", deployed_decision=_Decision(7))


def test_legacy_state_without_document_preserves_extension_decision() -> None:
    item = {
        "PK": {"S": "OPCONTROL#kill-switch"},
        "SK": {"S": "STATE#current"},
        "config_version": {"N": "7"},
    }
    gate = DynamoDbDurableControlGate(client=_Dynamo(item), table_name="operations")
    gate.require_phase("execute", deployed_decision=_Decision(7))


def test_malformed_durable_document_fails_closed() -> None:
    item = {
        "PK": {"S": "OPCONTROL#kill-switch"},
        "SK": {"S": "STATE#current"},
        "config_version": {"N": "8"},
        "document_json": {"S": "{not-json"},
    }
    gate = DynamoDbDurableControlGate(client=_Dynamo(item), table_name="operations")
    with pytest.raises(DurableControlUnavailable):
        gate.require_phase("execute", deployed_decision=_Decision(7))


def test_state_document_version_mismatch_fails_closed() -> None:
    durable = _document(8, enabled=True, prepare=True, dispatch=True, execute=True)
    gate = DynamoDbDurableControlGate(
        client=_Dynamo(_item(durable, state_version=9)),
        table_name="operations",
    )
    with pytest.raises(DurableControlUnavailable):
        gate.require_phase("execute", deployed_decision=_Decision(8))


def test_dynamodb_failure_fails_closed() -> None:
    gate = DynamoDbDurableControlGate(client=_Dynamo(error=RuntimeError("down")), table_name="operations")
    with pytest.raises(DurableControlUnavailable):
        gate.require_phase("execute", deployed_decision=_Decision(7))


def test_read_is_consistent_and_key_bounded() -> None:
    durable = _document(8, enabled=True, prepare=True, dispatch=True, execute=True)
    client = _Dynamo(_item(durable))
    gate = DynamoDbDurableControlGate(client=client, table_name="operations")
    gate.require_phase("execute", deployed_decision=_Decision(7))
    assert client.calls == [
        {
            "TableName": "operations",
            "Key": {
                "PK": {"S": "OPCONTROL#kill-switch"},
                "SK": {"S": "STATE#current"},
            },
            "ConsistentRead": True,
            "ProjectionExpression": "config_version, document_json",
        }
    ]
