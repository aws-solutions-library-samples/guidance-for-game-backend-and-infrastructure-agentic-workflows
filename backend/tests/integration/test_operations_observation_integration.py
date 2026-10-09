"""Integration tests for the E1 observation stack (issue #413).

Exercises the full observe path — API Gateway handler, application service, the
two-phase conditional/idempotent DynamoDB store (against an in-process fake
DynamoDB client that enforces conditional-put and conditional-update semantics),
the additive observation contract, canonical hashing, and the deployable
``lambda_entry`` bootstrap — without any AWS calls. This wires the real
components together rather than mocking the service.

Covered lifecycle behaviors: create-before-reads, no-second-read completed
replay (a lost response), an idempotency conflict on changed intent, a typed
status GET, cross-workspace status denial, and the module-level Lambda handler
end-to-end over a fake boto3 session.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
from operations.contracts.canonical import canonical_sha256
from operations.contracts.observation import validate_observation
from operations.identity import ApprovalIdentityBoundary
from operations.observation import AuthorityInputs, ObservationService
from operations.observation_handler import ObservationRequestHandler
from operations.observation_store import DynamoDbObservationStore
from operations.settings import resolve_operations_settings

pytestmark = [pytest.mark.integration, pytest.mark.localhost]

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET_ID = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
EXP = int((NOW + timedelta(minutes=30)).timestamp())


class FakeDynamoClient:
    """In-process DynamoDB stand-in enforcing conditional put/update semantics."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        # All-or-nothing: evaluate every condition, then apply.
        for entry in TransactItems:
            if "Put" in entry:
                put = entry["Put"]
                item = put["Item"]
                key = (item["PK"]["S"], item["SK"]["S"])
                condition = put.get("ConditionExpression", "")
                if condition == "attribute_not_exists(PK)" and key in self.items:
                    raise self._cancelled()
                if condition == "attribute_not_exists(SK)" and key in self.items:
                    raise self._cancelled()
            elif "Update" in entry:
                if not self._update_holds(entry["Update"]):
                    raise self._cancelled()
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[(item["PK"]["S"], item["SK"]["S"])] = item
            elif "Update" in entry:
                self._apply_update(entry["Update"])
        return {}

    def _update_holds(self, upd: dict[str, Any]) -> bool:
        key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
        current = self.items.get(key)
        if current is None:
            return False
        values = upd.get("ExpressionAttributeValues", {})
        if current.get("state", {}).get("S") != values.get(":observing", {}).get("S"):
            return False
        if int(current.get("sequence", {}).get("N", "-1")) != int(values.get(":zero", {}).get("N", "0")):
            return False
        if ":cur_gen" in values:  # reclaim: observed generation + expired lease
            if int(current.get("generation", {}).get("N", "-1")) != int(values[":cur_gen"]["N"]):
                return False
            if int(current.get("lease_not_after", {}).get("N", "0")) > int(values[":now"]["N"]):
                return False
            return True
        if int(current.get("generation", {}).get("N", "-1")) != int(values.get(":gen", {}).get("N", "0")):
            return False
        if current.get("lease_holder", {}).get("S") != values.get(":holder", {}).get("S"):
            return False
        if ":now" in values and int(current.get("lease_not_after", {}).get("N", "0")) <= int(values[":now"]["N"]):
            return False
        return True

    def _apply_update(self, upd: dict[str, Any]) -> None:
        key = (upd["Key"]["PK"]["S"], upd["Key"]["SK"]["S"])
        current = dict(self.items[key])
        values = upd.get("ExpressionAttributeValues", {})
        if ":succeeded" in values:
            current["state"] = values[":succeeded"]
            current["sequence"] = values[":one"]
        elif ":failed" in values:
            current["state"] = values[":failed"]
            current["sequence"] = values[":one"]
            current["reason_code"] = values[":reason"]
        elif ":cur_gen" in values:
            current["generation"] = values[":new_gen"]
            current["lease_holder"] = values[":holder"]
            current["lease_not_after"] = values[":new_lease"]
        self.items[key] = current

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        key = (Key["PK"]["S"], Key["SK"]["S"])
        item = self.items.get(key)
        return {"Item": item} if item else {}

    @staticmethod
    def _cancelled() -> ClientError:
        # A real botocore ClientError: reasons live in response["CancellationReasons"],
        # not a fabricated cancellation_reasons attribute. Pure ConditionalCheckFailed.
        return ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
                "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
            },
            "TransactWriteItems",
        )


class _Reader:
    def __init__(self) -> None:
        self.calls = 0

    def read_utilization(self, fleet_id: str) -> dict[str, int]:
        self.calls += 1
        return {
            "active_server_processes": 12,
            "active_game_sessions": 8,
            "current_player_sessions": 30,
            "maximum_player_sessions": 100,
        }

    def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
        return [{"location": "us-west-2", "desired": 10, "minimum": 2, "maximum": 20, "active": 10, "idle": 2}]

    def read_scaling_policies(self, fleet_id: str) -> list[dict[str, str]]:
        return [{"name": "target", "status": "ACTIVE", "metric_name": "PercentAvailableGameSessions"}]


def _handler(client: FakeDynamoClient, reader: _Reader | None = None) -> ObservationRequestHandler:
    reader = reader or _Reader()
    store = DynamoDbObservationStore(client=client, table_name="obs-table", clock=lambda: NOW)
    service = ObservationService(
        settings=resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"}),
        identity_boundary=ApprovalIdentityBoundary(
            tenant_id="tenant.default",
            workspace_id="workspace.default",
            requester_client_ids=frozenset({"client.web-console"}),
            approver_client_ids=frozenset({"client.approver"}),
            trusted_audiences=frozenset({"operations-api"}),
        ),
        reader=reader,
        store=store,
        clock=lambda: NOW,
        operation_id_factory=lambda: OPERATION_ID,
    )
    return ObservationRequestHandler(
        service=service,
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        trusted_audience="operations-api",
        capability_id="gamelift.observe-fleet",
        capability_version="1.0",
        authority_inputs=AuthorityInputs(
            tenant_policy="observe",
            workspace_policy="observe",
            principal_authority="observe",
            capability_maximum="observe",
            risk_policy="observe",
        ),
    )


def _post_event(body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "requestContext": {
            "requestId": "req-abc123",
            "http": {"method": "POST"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "subject.operator-1",
                        "client_id": "client.web-console",
                        "token_use": "access",
                        "exp": EXP,
                    }
                }
            },
        },
        "body": json.dumps(body or {"fleet_id": FLEET_ID, "idempotency_token": TOKEN}),
    }


def _get_event(operation_id: str) -> dict[str, Any]:
    event = _post_event()
    event["requestContext"]["http"]["method"] = "GET"
    event.pop("body")
    event["pathParameters"] = {"operationId": operation_id}
    return event


def test_full_observe_path_persists_and_validates() -> None:
    client = FakeDynamoClient()
    handler = _handler(client)
    response = handler.handle(_post_event())
    assert response["statusCode"] == 200
    observation = json.loads(response["body"])
    validate_observation(observation)
    # Create wrote four items (mapping, snapshot, STATE#0, LEDGER#0); complete
    # added the result, STATE#1, and LEDGER#1.
    assert ("OP#" + OPERATION_ID, "STATE#current") in client.items
    assert ("OP#" + OPERATION_ID, "RESULT#current") in client.items
    assert client.items[("OP#" + OPERATION_ID, "STATE#current")]["state"]["S"] == "succeeded"


def test_lost_response_replays_stored_result_without_second_read() -> None:
    client = FakeDynamoClient()
    reader = _Reader()
    handler = _handler(client, reader)
    first = handler.handle(_post_event())
    assert first["statusCode"] == 200
    reads_after_first = reader.calls

    # A retry with the same token+intent (the client never saw the first
    # response) replays the stored observation and performs no new reads.
    second = handler.handle(_post_event())
    assert second["statusCode"] == 200
    assert json.loads(second["body"]) == json.loads(first["body"])
    assert reader.calls == reads_after_first


def test_conflict_when_token_reused_with_different_intent() -> None:
    client = FakeDynamoClient()
    handler = _handler(client)
    first = handler.handle(_post_event())
    assert first["statusCode"] == 200

    other_fleet = "fleet-9999abcd-5678-90ef-a1b2-c3d4e5f60789"
    conflict = handler.handle(_post_event({"fleet_id": other_fleet, "idempotency_token": TOKEN}))
    assert conflict["statusCode"] == 409
    assert json.loads(conflict["body"])["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_status_get_returns_succeeded_after_observe() -> None:
    client = FakeDynamoClient()
    handler = _handler(client)
    handler.handle(_post_event())
    status = handler.handle(_get_event(OPERATION_ID))
    assert status["statusCode"] == 200
    body = json.loads(status["body"])
    assert body["state"] == "succeeded"
    assert body["operation_id"] == OPERATION_ID
    validate_observation(body["observation"])


def test_status_get_cross_workspace_is_404() -> None:
    client = FakeDynamoClient()
    handler = _handler(client)
    handler.handle(_post_event())
    # A genuinely different workspace binding cannot see the operation.
    store = DynamoDbObservationStore(client=client, table_name="obs-table", clock=lambda: NOW)
    service = ObservationService(
        settings=resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"}),
        identity_boundary=ApprovalIdentityBoundary(
            tenant_id="tenant.default",
            workspace_id="workspace.other",
            requester_client_ids=frozenset({"client.web-console"}),
            approver_client_ids=frozenset({"client.approver"}),
            trusted_audiences=frozenset({"operations-api"}),
        ),
        reader=_Reader(),
        store=store,
        clock=lambda: NOW,
        operation_id_factory=lambda: OPERATION_ID,
    )
    other_ws_handler = ObservationRequestHandler(
        service=service,
        tenant_id="tenant.default",
        workspace_id="workspace.other",
        trusted_audience="operations-api",
        capability_id="gamelift.observe-fleet",
        capability_version="1.0",
        authority_inputs=AuthorityInputs(
            tenant_policy="observe",
            workspace_policy="observe",
            principal_authority="observe",
            capability_maximum="observe",
            risk_policy="observe",
        ),
    )
    status = other_ws_handler.handle(_get_event(OPERATION_ID))
    assert status["statusCode"] == 404


def test_deployable_lambda_handler_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wire the module-level Lambda handler over a fake boto3 session so the real
    # bootstrap path (settings -> clients -> adapter/store/metrics -> handler)
    # is exercised without AWS.
    # Local modules
    import operations.observe.lambda_entry as entry
    from operations.observe.gamelift_adapter import GameLiftObservationAdapter

    client = FakeDynamoClient()
    metric_puts: list[dict[str, Any]] = []

    class FakeGameLift:
        def describe_fleet_utilization(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "FleetUtilization": [
                    {
                        "ActiveServerProcessCount": 12,
                        "ActiveGameSessionCount": 8,
                        "CurrentPlayerSessionCount": 30,
                        "MaximumPlayerSessionCount": 100,
                    }
                ]
            }

        def describe_fleet_capacity(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "FleetCapacity": [
                    {
                        "Location": "us-west-2",
                        "InstanceCounts": {"DESIRED": 10, "MINIMUM": 2, "MAXIMUM": 20, "ACTIVE": 10, "IDLE": 2},
                    }
                ]
            }

        def describe_scaling_policies(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "ScalingPolicies": [
                    {"Name": "target", "Status": "ACTIVE", "MetricName": "PercentAvailableGameSessions"}
                ]
            }

    class FakeCloudWatch:
        def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            metric_puts.append(kwargs)
            return {}

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def client(self, name: str, **kwargs: Any) -> Any:
            return {"gamelift": FakeGameLift(), "dynamodb": client, "cloudwatch": FakeCloudWatch()}[name]

    env = {
        "GBAW_OPERATIONS_MODE": "observe",
        "GBAW_OPERATIONS_TABLE_NAME": "obs-table",
        "GBAW_OPERATIONS_METRIC_NAMESPACE": "GBAW/Operations",
        "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "operations-api",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(entry, "_region", lambda: "us-west-2")
    monkeypatch.setattr("boto3.Session", FakeSession, raising=False)
    entry._handler.cache_clear()

    # The bootstrapped service uses the real system clock, so the token must
    # expire in the future relative to now (not the frozen NOW used elsewhere).
    # Standard library
    import time as _time

    future_exp = int(_time.time()) + 3600
    # The bootstrapped boundary trusts the audience as the requester client id.
    claims = {
        "sub": "subject.operator-1",
        "client_id": "operations-api",
        "token_use": "access",
        "exp": future_exp,
    }
    event = _post_event()
    event["requestContext"]["authorizer"]["jwt"]["claims"] = claims
    response = entry.handler(event, None)
    assert response["statusCode"] == 200, response
    observation = json.loads(response["body"])
    validate_observation(observation)
    # Latency was published under the frozen namespace with the exact name.
    latency = [p for p in metric_puts if p["MetricData"][0]["MetricName"] == "ObservationRequestLatency"]
    assert latency and latency[0]["Namespace"] == "GBAW/Operations"
    entry._handler.cache_clear()

    # And the adapter class exposes exactly the three read-only methods.
    adapter_methods = {m for m in dir(GameLiftObservationAdapter) if not m.startswith("_")}
    assert adapter_methods == {"read_utilization", "read_capacity", "read_scaling_policies"}


def test_record_then_replay_returns_byte_identical_body_and_same_operation() -> None:
    # Live E1 diagnosis (#413): a first success serializes the freshly built
    # observation in Python insertion order, while the replay loads the stored
    # ``observation_json`` (written sort-key canonical) and serializes THAT
    # order. Both bodies are semantically equal and equal in size, yet differ
    # byte-for-byte, so a client asserting raw-body idempotency fails on retry.
    # The handler MUST serialize every response deterministically so a real
    # record and its replay are byte-identical, carry the same operation id, and
    # trigger zero additional provider reads.
    client = FakeDynamoClient()
    reader = _Reader()
    handler = _handler(client, reader)

    first = handler.handle(_post_event())
    assert first["statusCode"] == 200
    reads_after_first = reader.calls

    second = handler.handle(_post_event())
    assert second["statusCode"] == 200

    # Raw bytes, not just parsed structure, must match on replay.
    assert second["body"] == first["body"]
    # The replay is the same logical operation.
    assert json.loads(first["body"])["observation_id"] == OPERATION_ID
    assert json.loads(second["body"])["observation_id"] == OPERATION_ID
    # A replay performs no new provider read.
    assert reader.calls == reads_after_first
