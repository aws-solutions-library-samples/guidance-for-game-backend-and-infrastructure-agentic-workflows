"""Execution store injected-clock tests (#415, E3 execute).

The independent review flagged that :meth:`DynamoDbExecutionStore.record_execution_result`
stamped the persisted ``recorded_at`` on the state-change and ledger records from
the module-level ``_system_clock`` instead of the store's injected ``clock``. That
makes persisted timestamps non-deterministic and untestable, and inconsistent with
the verifier/executor that already thread an injected clock.

These red-green tests pin that every persisted timestamp comes from the injected
clock, so a frozen clock produces exactly reproducible audit records.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Local modules
from operations.execute.execution_store import DynamoDbExecutionStore, ExecutionCommitOutcome

_FIXED = datetime(2031, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
_EXPECTED_ISO = "2031-03-04T05:06:07Z"
_LEASE_NOT_AFTER = _FIXED + timedelta(seconds=30)
_OP = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"
_ACTION = "act_" + "b" * 64


def _result() -> dict[str, Any]:
    return {
        "execution_contract_version": "1.0",
        "operation_id": _OP,
        "logical_action_id": _ACTION,
        "intent_hash": "sha256:" + "1" * 64,
        "outcome": "SUCCEEDED",
        "provider_write_issued": True,
        "verification": {
            "execution_contract_version": "1.0",
            "operation_id": _OP,
            "logical_action_id": _ACTION,
            "verified_at": "2031-03-04T05:06:10Z",
            "observed_capacity": {"desired": 14, "minimum": 2, "maximum": 20},
            "expected_capacity": {"desired": 14, "minimum": 2, "maximum": 20},
            "matches_target": True,
            "attempts_observed": 1,
        },
        "recorded_at": "2031-03-04T05:06:11Z",
    }


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {"Item": item} if item is not None else {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        for entry in kwargs["TransactItems"]:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
        return {}


def test_persisted_recorded_at_uses_injected_clock() -> None:
    fake = _FakeDynamo()
    store = DynamoDbExecutionStore(client=fake, table_name="ops-table", clock=lambda: _FIXED)
    acquisition = store.acquire_execution_lease(
        operation_id=_OP, logical_action_id=_ACTION, lease_holder="exec-1", lease_not_after=_LEASE_NOT_AFTER
    )
    outcome = store.record_execution_result(
        operation_id=_OP,
        logical_action_id=_ACTION,
        generation=acquisition.generation,
        expected_state="dispatched",
        new_state="succeeded",
        result=_result(),
    )
    assert outcome is ExecutionCommitOutcome.RECORDED

    stamped = [
        item.get("recorded_at", {}).get("S")
        for (_, sk), item in fake.items.items()
        if sk.startswith("STATE#") or sk.startswith("LEDGER#")
    ]
    assert stamped, "state-change and ledger records must carry a recorded_at"
    assert all(
        value == _EXPECTED_ISO for value in stamped
    ), f"persisted recorded_at must come from the injected clock, got {stamped}"
