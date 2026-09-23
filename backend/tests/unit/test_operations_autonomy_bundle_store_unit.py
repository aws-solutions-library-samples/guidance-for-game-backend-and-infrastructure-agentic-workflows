"""DynamoDbAutonomyBundleStore round-trip + conditional-immutability tests (#439).

The bundle store is the durable, immutable evidence record the executor reloads
by ``operation_id`` to run the v2 autonomous path. It is written with a
conditional ``attribute_not_exists`` put so a crash/replay can never mutate an
existing bundle, and it exposes a bounded dispatch view (identifier + envelope
fields only). These tests prove the persist/reload round-trip, the immutable
conditional semantics (a second persist of a differing bundle fails closed and
never overwrites), an idempotent identical re-persist, and that a missing bundle
reloads as ``None``.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime.store import (
    AutonomyBundleStoreError,
    DynamoDbAutonomyBundleStore,
)

_OP = "op_" + "c" * 26
_ACTION = "act_" + "d" * 64
_STATE_ID = "state-1"


class _ConditionalFailure(Exception):
    """Mimics botocore ClientError for a ConditionalCheckFailed put."""

    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeDynamo:
    """A tiny single-table fake honouring attribute_not_exists conditional puts."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_calls = 0

    def put_item(self, *, TableName: str, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        self.put_calls += 1
        pk = Item["PK"]["S"]
        sk = Item["SK"]["S"]
        key = (pk, sk)
        if ConditionExpression and "attribute_not_exists" in ConditionExpression and key in self.items:
            raise _ConditionalFailure()
        self.items[key] = Item

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        pk = Key["PK"]["S"]
        sk = Key["SK"]["S"]
        item = self.items.get((pk, sk))
        return {"Item": item} if item is not None else {}


def _is_conditional(exc: Exception) -> bool:
    return isinstance(exc, _ConditionalFailure)


def _bundle(desired: int = 5) -> dict[str, Any]:
    return {
        "policy": {"policy_id": "pol-1", "policy_version": "1", "policy_hash": "h" * 64},
        "observation": {"observation_hash": "o" * 64},
        "decision": {"decision_id": "autz." + _OP, "decision": "authorized"},
        "operation": {
            "operation_id": _OP,
            "prepared_hash": "p" * 64,
            "parameters": {"requested": {"desired": desired}},
        },
        "window_state": {"state_id": _STATE_ID, "state_revision": 7},
        "reservation": {"operation_id": _OP, "logical_action_id": _ACTION, "state_id": _STATE_ID},
    }


def _store() -> tuple[DynamoDbAutonomyBundleStore, _FakeDynamo]:
    client = _FakeDynamo()
    return DynamoDbAutonomyBundleStore(client=client, table_name="ops-06"), client


@pytest.mark.unit
def test_persist_then_reload_round_trips_every_section() -> None:
    store, _ = _store()
    bundle = _bundle()
    store.persist_bundle(
        operation_id=_OP,
        policy=bundle["policy"],
        observation=bundle["observation"],
        decision=bundle["decision"],
        operation=bundle["operation"],
        window_state=bundle["window_state"],
        reservation=bundle["reservation"],
    )
    reloaded = store.load_bundle(_OP)
    assert reloaded is not None
    for section in ("policy", "observation", "decision", "operation", "window_state", "reservation"):
        assert reloaded[section] == bundle[section]


@pytest.mark.unit
def test_missing_bundle_reloads_as_none() -> None:
    store, _ = _store()
    assert store.load_bundle("op_" + "z" * 26) is None


@pytest.mark.unit
def test_identical_repersist_is_idempotent_and_never_overwrites() -> None:
    store, client = _store()
    bundle = _bundle()
    kwargs = {
        "operation_id": _OP,
        "policy": bundle["policy"],
        "observation": bundle["observation"],
        "decision": bundle["decision"],
        "operation": bundle["operation"],
        "window_state": bundle["window_state"],
        "reservation": bundle["reservation"],
    }
    store.persist_bundle(**kwargs)
    stored_after_first = dict(client.items)
    # An identical replay resolves cleanly (idempotent) and leaves the record byte-identical.
    store.persist_bundle(**kwargs)
    assert client.items == stored_after_first


@pytest.mark.unit
def test_differing_repersist_fails_closed_and_leaves_original_intact() -> None:
    store, client = _store()
    first = _bundle(desired=5)
    store.persist_bundle(
        operation_id=_OP,
        policy=first["policy"],
        observation=first["observation"],
        decision=first["decision"],
        operation=first["operation"],
        window_state=first["window_state"],
        reservation=first["reservation"],
    )
    original = json.loads(client.items[(f"AUTZBUNDLE#{_OP}", "AUTZBUNDLE")]["bundle"]["S"])
    conflicting = _bundle(desired=999)
    with pytest.raises(AutonomyBundleStoreError):
        store.persist_bundle(
            operation_id=_OP,
            policy=conflicting["policy"],
            observation=conflicting["observation"],
            decision=conflicting["decision"],
            operation=conflicting["operation"],
            window_state=conflicting["window_state"],
            reservation=conflicting["reservation"],
        )
    # The immutable original is untouched: no overwrite of an existing bundle.
    after = json.loads(client.items[(f"AUTZBUNDLE#{_OP}", "AUTZBUNDLE")]["bundle"]["S"])
    assert after == original
    assert after["operation"]["parameters"]["requested"]["desired"] == 5


@pytest.mark.unit
def test_dispatch_view_is_identifier_only() -> None:
    store, _ = _store()
    bundle = _bundle()
    store.persist_bundle(
        operation_id=_OP,
        policy=bundle["policy"],
        observation=bundle["observation"],
        decision=bundle["decision"],
        operation=bundle["operation"],
        window_state=bundle["window_state"],
        reservation=bundle["reservation"],
    )
    view = store.dispatch_view(_OP)
    assert view == {"operation_id": _OP}
