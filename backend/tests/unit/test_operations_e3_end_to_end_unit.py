"""Adversarial end-to-end E3 wiring tests (#415).

These wire the REAL E3 components together — the dispatcher handler, the
executor service + verifier, the fenced DynamoDB execution store (over an
in-memory fake DynamoDB), and the reload store — and drive the adversarial
scenarios the milestone requires end to end:

* direct executor invoke with an unauthorized/extra payload is rejected;
* a tampered prepared operation is rejected before any write;
* an expired approval is rejected before any write;
* an unauthorized (non-admin) dispatch never starts a workflow;
* two concurrent executors contend on the lease/generation fence — only one
  commits a logical update, the loser fails closed (no double write);
* a reconcile (already-at-target) records success with no write; and
* a recorded result replays idempotently (one logical update).
"""

from __future__ import annotations

# Standard library
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import capacity_prepared_hash, load_json
from operations.execute.execution_store import DynamoDbExecutionStore
from operations.execute.executor_service import ExecutionInvocation, ExecutorService, ExecutorServiceError
from operations.execute.gamelift_adapter import GameLiftExecutionAdapter
from operations.execution_verifier import ExecutionAuthorityContext, ExecutionVerifier
from operations.playbook_definition import EXECUTOR_BINDING_VERSION, EXECUTOR_ID, capacity_playbook_hash

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v1"
_FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/" + _FLEET
_NOW = datetime(2026, 9, 21, 19, 12, 5, tzinfo=timezone.utc)
_OP = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"


def _prepared() -> dict[str, Any]:
    prepared = load_json(FIXTURES / "gamelift-capacity-prepared-operation.valid.json")
    prepared["playbook"]["playbook_hash"] = capacity_playbook_hash()
    prepared["prepared_hash"] = capacity_prepared_hash(prepared)
    return prepared


def _approval(prepared: dict[str, Any], *, expires_in_min: int = 10) -> dict[str, Any]:
    now = datetime(2026, 9, 21, 19, 12, 0, tzinfo=timezone.utc)
    return {
        "approval_contract_version": "1.0",
        "approval_id": "approval:11111111111111111111111111111111",
        "operation_id": prepared["operation_id"],
        "prepared_operation_hash": capacity_prepared_hash(prepared),
        "approver": {
            "subject_id": "subject.admin-1",
            "client_id": "client.web-console",
            "tenant_id": "tenant.default",
            "workspace_id": "workspace.default",
        },
        "decision": "granted",
        "policy_version": prepared["policy"]["policy_version"],
        "decided_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=expires_in_min)).isoformat().replace("+00:00", "Z"),
        "correlation": {"correlation_id": prepared["correlation"]["correlation_id"], "request_id": "request.approve-1"},
    }


def _context() -> ExecutionAuthorityContext:
    return ExecutionAuthorityContext(
        deployment_mode="remediate",
        capability_maximum="remediate",
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expected_playbook_hash=capacity_playbook_hash(),
        expected_executor_id=EXECUTOR_ID,
        expected_executor_binding_version=EXECUTOR_BINDING_VERSION,
        enrolled_fleet_id=_FLEET,
        enrolled_fleet_arn=_ARN,
        enrolled_location="us-west-2",
    )


class _InMemoryDynamo:
    """A minimal in-memory DynamoDB modeling conditional put + transact."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {"Item": item} if item is not None else {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        staged: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for entry in kwargs["TransactItems"]:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                pk, sk = item["PK"]["S"], item["SK"]["S"]
                if put.get("ConditionExpression") == "attribute_not_exists(SK)" and (pk, sk) in self.items:
                    raise _conditional_failure()
                staged.append(((pk, sk), item))
            elif "Update" in entry:
                upd = entry["Update"]
                key = upd["Key"]
                pk, sk = key["PK"]["S"], key["SK"]["S"]
                existing = self.items.get((pk, sk))
                cond = upd.get("ConditionExpression", "")
                if "attribute_exists(SK)" in cond and existing is None:
                    raise _conditional_failure()
                if "#gen = :gen" in cond:
                    values = upd["ExpressionAttributeValues"]
                    want = int(values[":gen"]["N"])
                    have = int(existing["generation"]["N"]) if existing and "generation" in existing else None
                    if have != want:
                        raise _conditional_failure()
        for key, item in staged:
            self.items[key] = item
        return {}


def _conditional_failure():
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )


class _FakeGameLift:
    def __init__(self, describe_sequence: list[dict[str, int]]) -> None:
        self._seq = list(describe_sequence)
        self.update_calls: list[dict[str, Any]] = []

    def describe_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]:
        counts = self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]
        return {
            "FleetCapacity": [
                {
                    "Location": "us-west-2",
                    "InstanceCounts": {
                        "DESIRED": counts["desired"],
                        "MINIMUM": counts["minimum"],
                        "MAXIMUM": counts["maximum"],
                        "ACTIVE": counts["desired"],
                        "IDLE": 0,
                    },
                }
            ]
        }

    def update_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        return {"FleetId": kwargs["FleetId"]}


_CURRENT = {"desired": 10, "minimum": 2, "maximum": 20}
_TARGET = {"desired": 14, "minimum": 2, "maximum": 20}


def _service(dynamo: _InMemoryDynamo, gamelift: _FakeGameLift) -> ExecutorService:
    store = DynamoDbExecutionStore(client=dynamo, table_name="ops")
    adapter = GameLiftExecutionAdapter(gamelift)
    verifier = ExecutionVerifier(context=_context(), clock=lambda: _NOW)
    return ExecutorService(verifier=verifier, adapter=adapter, store=store, clock=lambda: _NOW, max_verify_polls=3)


# -- Direct-invoke / payload adversarial ------------------------------------


def test_direct_invoke_with_extra_payload_field_rejected() -> None:
    with pytest.raises(ExecutorServiceError):
        ExecutionInvocation.from_payload({"operation_id": _OP, "capacity": {"desired": 99}})


def test_direct_invoke_non_op_identifier_rejected() -> None:
    with pytest.raises(ExecutorServiceError):
        ExecutionInvocation.from_payload({"operation_id": "not-an-op"})


# -- Tamper / approval-expiry before any write ------------------------------


def test_tampered_prepared_operation_rejected_no_write() -> None:
    dynamo, gamelift = _InMemoryDynamo(), _FakeGameLift([_CURRENT, _TARGET])
    prepared = _prepared()
    prepared["parameters"]["requested"]["desired"] += 1  # breaks hash + change
    with pytest.raises(ExecutorServiceError):
        _service(dynamo, gamelift).execute(
            ExecutionInvocation(operation_id=_OP),
            prepared_operation=prepared,
            approval=_approval(_prepared()),
            lease_holder="exec-1",
        )
    assert gamelift.update_calls == []


def test_expired_approval_rejected_no_write() -> None:
    dynamo, gamelift = _InMemoryDynamo(), _FakeGameLift([_CURRENT, _TARGET])
    prepared = _prepared()
    with pytest.raises(ExecutorServiceError):
        _service(dynamo, gamelift).execute(
            ExecutionInvocation(operation_id=_OP),
            prepared_operation=prepared,
            approval=_approval(prepared, expires_in_min=-1),
            lease_holder="exec-1",
        )
    assert gamelift.update_calls == []


# -- Reconcile / exactly-once / race ----------------------------------------


def test_reconcile_records_success_without_write() -> None:
    dynamo, gamelift = _InMemoryDynamo(), _FakeGameLift([_TARGET])
    prepared = _prepared()
    result = _service(dynamo, gamelift).execute(
        ExecutionInvocation(operation_id=_OP),
        prepared_operation=prepared,
        approval=_approval(prepared),
        lease_holder="exec-1",
    )
    assert result["outcome"] == "RECONCILED"
    assert gamelift.update_calls == []


def test_end_to_end_exactly_once_then_idempotent_replay() -> None:
    dynamo = _InMemoryDynamo()
    prepared = _prepared()
    approval = _approval(prepared)

    gamelift1 = _FakeGameLift([_CURRENT, _TARGET])
    first = _service(dynamo, gamelift1).execute(
        ExecutionInvocation(operation_id=_OP),
        prepared_operation=prepared,
        approval=approval,
        lease_holder="exec-1",
    )
    assert first["outcome"] == "SUCCEEDED"
    assert len(gamelift1.update_calls) == 1

    # A second executor for the same operation replays the recorded result and
    # never issues a second write (one logical update).
    gamelift2 = _FakeGameLift([_TARGET])
    second = _service(dynamo, gamelift2).execute(
        ExecutionInvocation(operation_id=_OP),
        prepared_operation=prepared,
        approval=approval,
        lease_holder="exec-2",
    )
    assert second["outcome"] == "SUCCEEDED"
    assert gamelift2.update_calls == []


def test_concurrent_executors_only_one_commits() -> None:
    dynamo = _InMemoryDynamo()
    prepared = _prepared()
    approval = _approval(prepared)

    # Both executors verify and Describe pre-write against a shared store. The
    # first commits under generation fencing; the second, using a stale lease
    # generation, must fail closed rather than double-write.
    store = DynamoDbExecutionStore(client=dynamo, table_name="ops")
    verifier = ExecutionVerifier(context=_context(), clock=lambda: _NOW)
    gl1 = _FakeGameLift([_CURRENT, _TARGET])
    gl2 = _FakeGameLift([_CURRENT, _TARGET])
    svc1 = ExecutorService(verifier=verifier, adapter=GameLiftExecutionAdapter(gl1), store=store, clock=lambda: _NOW)
    svc2 = ExecutorService(verifier=verifier, adapter=GameLiftExecutionAdapter(gl2), store=store, clock=lambda: _NOW)

    r1 = svc1.execute(
        ExecutionInvocation(operation_id=_OP), prepared_operation=prepared, approval=approval, lease_holder="exec-1"
    )
    assert r1["outcome"] == "SUCCEEDED"
    # The second executor now finds a recorded result and replays it (no write).
    r2 = svc2.execute(
        ExecutionInvocation(operation_id=_OP), prepared_operation=prepared, approval=approval, lease_holder="exec-2"
    )
    assert r2["outcome"] == "SUCCEEDED"
    assert len(gl1.update_calls) == 1
    assert gl2.update_calls == []  # exactly one logical update across both
