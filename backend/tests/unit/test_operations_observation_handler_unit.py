"""Unit tests for the API Gateway observation handler (issue #413)."""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.identity import ApprovalIdentityBoundary
from operations.observation import (
    AuthorityInputs,
    ObservationCommit,
    ObservationCommitOutcome,
    ObservationService,
)
from operations.observation_handler import ObservationRequestHandler
from operations.settings import resolve_operations_settings

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET_ID = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
EXP = int((NOW + timedelta(minutes=30)).timestamp())


class FakeReader:
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


class FakeStore:
    def __init__(self, outcome: ObservationCommitOutcome = ObservationCommitOutcome.RECORDED) -> None:
        self.outcome = outcome
        self.calls = 0

    def record_observation(self, **kwargs: Any) -> ObservationCommit:
        self.calls += 1
        return ObservationCommit(self.outcome)


def _handler(store: FakeStore | None = None, mode: str = "observe") -> tuple[ObservationRequestHandler, FakeStore]:
    store = store or FakeStore()
    service = ObservationService(
        settings=resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": mode}),
        identity_boundary=ApprovalIdentityBoundary(
            tenant_id="tenant.default",
            workspace_id="workspace.default",
            requester_client_ids=frozenset({"client.web-console"}),
            approver_client_ids=frozenset({"client.approver"}),
            trusted_audiences=frozenset({"operations-api"}),
        ),
        reader=FakeReader(),
        store=store,
        clock=lambda: NOW,
        observation_id_factory=lambda: "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    handler = ObservationRequestHandler(
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
    return handler, store


def _event(*, claims: dict[str, Any] | None = None, body: Any = None, with_authorizer: bool = True) -> dict[str, Any]:
    request_context: dict[str, Any] = {"requestId": "abc123def456"}
    if with_authorizer:
        request_context["authorizer"] = {
            "jwt": {
                "claims": (
                    claims
                    if claims is not None
                    else {
                        "sub": "subject.operator-1",
                        "client_id": "client.web-console",
                        "token_use": "access",
                        "exp": EXP,
                    }
                )
            }
        }
    return {
        "requestContext": request_context,
        "body": json.dumps({"fleet_id": FLEET_ID, "idempotency_token": TOKEN}) if body is None else body,
    }


def test_success_returns_200_and_valid_observation() -> None:
    handler, store = _handler()
    response = handler.handle(_event())
    assert response["statusCode"] == 200
    observation = json.loads(response["body"])
    assert observation["target"]["fleet_id"] == FLEET_ID
    assert observation["requester"]["subject_id"] == "subject.operator-1"
    assert store.calls == 1


def test_direct_call_without_authorizer_is_rejected() -> None:
    handler, store = _handler()
    response = handler.handle(_event(with_authorizer=False))
    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error_code"] == "IDENTITY_CONTEXT_INVALID"
    assert store.calls == 0


def test_identity_from_body_is_ignored() -> None:
    handler, _ = _handler()
    # An attacker cannot smuggle identity via the body; the schema rejects extra keys.
    body = json.dumps({"fleet_id": FLEET_ID, "idempotency_token": TOKEN, "sub": "subject.admin"})
    response = handler.handle(_event(body=body))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error_code"] == "CONTRACT_INVALID"


def test_non_access_token_is_rejected() -> None:
    handler, _ = _handler()
    claims = {"sub": "subject.operator-1", "client_id": "client.web-console", "token_use": "id", "exp": EXP}
    response = handler.handle(_event(claims=claims))
    assert response["statusCode"] == 401


def test_untrusted_client_is_denied() -> None:
    handler, store = _handler()
    claims = {"sub": "subject.operator-1", "client_id": "client.attacker", "token_use": "access", "exp": EXP}
    response = handler.handle(_event(claims=claims))
    assert response["statusCode"] == 401
    assert store.calls == 0


def test_disabled_deployment_denies() -> None:
    handler, store = _handler(mode="disabled")
    response = handler.handle(_event())
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error_code"] == "AUTHORIZATION_DENIED"
    assert store.calls == 0


def test_idempotency_conflict_maps_to_409() -> None:
    handler, _ = _handler(store=FakeStore(ObservationCommitOutcome.IDEMPOTENCY_CONFLICT))
    response = handler.handle(_event())
    assert response["statusCode"] == 409
    assert json.loads(response["body"])["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_missing_body_is_400() -> None:
    handler, _ = _handler()
    response = handler.handle(_event(body=""))
    assert response["statusCode"] == 400


def test_base64_body_is_rejected() -> None:
    handler, _ = _handler()
    event = _event()
    event["isBase64Encoded"] = True
    response = handler.handle(event)
    assert response["statusCode"] == 400


def test_handler_exposes_no_write_surface() -> None:
    public = {name for name in dir(ObservationRequestHandler) if not name.startswith("_")}
    assert public == {"handle"}
