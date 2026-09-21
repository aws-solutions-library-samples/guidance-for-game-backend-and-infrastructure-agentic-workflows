"""Integration tests for the E1 observation stack (issue #413).

Exercises the full observe path — API Gateway handler, application service, the
conditional/idempotent DynamoDB store (against an in-process fake DynamoDB client
that enforces conditional-put semantics), the additive observation contract, and
canonical hashing — without any AWS calls. This is the contract/integration
coverage the frozen plan requires for Agent A, wiring the real components
together rather than mocking the service.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
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
EXP = int((NOW + timedelta(minutes=30)).timestamp())


class FakeDynamoClient:
    """In-process DynamoDB stand-in enforcing conditional-put and TransactWrite."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:
        # Validate all conditions first; a transaction is all-or-nothing.
        staged: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for entry in TransactItems:
            put = entry["Put"]
            item = put["Item"]
            key = (item["PK"]["S"], item["SK"]["S"])
            condition = put.get("ConditionExpression", "")
            if condition == "attribute_not_exists(PK)" and any(k[0] == key[0] for k in self.items):
                raise self._cancelled()
            if condition == "attribute_not_exists(SK)" and key in self.items:
                raise self._cancelled()
            staged.append((key, item))
        for key, item in staged:
            self.items[key] = item
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:
        key = (Key["PK"]["S"], Key["SK"]["S"])
        item = self.items.get(key)
        return {"Item": item} if item else {}

    @staticmethod
    def _cancelled() -> Exception:
        exc = Exception("TransactionCanceledException")
        exc.response = {"Error": {"Code": "TransactionCanceledException"}}  # type: ignore[attr-defined]
        exc.cancellation_reasons = [{"Code": "ConditionalCheckFailed"}]  # type: ignore[attr-defined]
        return exc


def _handler(client: FakeDynamoClient) -> ObservationRequestHandler:
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
        reader=_Reader(),
        store=store,
        clock=lambda: NOW,
        observation_id_factory=lambda: "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
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


class _Reader:
    def read_utilization(self, fleet_id: str) -> dict[str, int]:
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


def _event(body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "requestContext": {
            "requestId": "req-abc123",
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


def test_full_observe_path_persists_and_validates() -> None:
    client = FakeDynamoClient()
    handler = _handler(client)
    response = handler.handle(_event())
    assert response["statusCode"] == 200
    observation = json.loads(response["body"])
    validate_observation(observation)
    # Four items committed atomically: mapping, snapshot, transition, ledger.
    assert len(client.items) == 4


def test_replay_conflict_when_token_reused_with_different_content() -> None:
    client = FakeDynamoClient()
    handler = _handler(client)
    first = handler.handle(_event())
    assert first["statusCode"] == 200

    # Same token, different fleet -> different fingerprint -> idempotency conflict.
    other_fleet = "fleet-9999abcd-5678-90ef-a1b2-c3d4e5f60789"
    conflict = handler.handle(_event({"fleet_id": other_fleet, "idempotency_token": TOKEN}))
    assert conflict["statusCode"] == 409
    assert json.loads(conflict["body"])["error_code"] == "IDEMPOTENCY_CONFLICT"
    # No partial second write: only the original four items remain.
    assert len(client.items) == 4


def test_append_only_ledger_is_never_overwritten() -> None:
    client = FakeDynamoClient()
    handler = _handler(client)
    handler.handle(_event())
    ledger_key = ("OBS#obs_aaaaaaaaaaaaaaaaaaaaaaaaaa", "LEDGER#1")
    assert ledger_key in client.items
    original = dict(client.items[ledger_key])
    # A replay of identical content must not rewrite the ledger event.
    handler.handle(_event())
    assert client.items[ledger_key] == original
