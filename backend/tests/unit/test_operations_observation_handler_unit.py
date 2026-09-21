"""Unit tests for the API Gateway observation handler (issue #413).

Covers POST observe and GET status route/method dispatch, JWT-only identity,
body-identity rejection, typed error mapping, and catch-all sanitization of any
unexpected internal exception.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Local modules
from operations.contracts import canonical_sha256
from operations.identity import ApprovalIdentityBoundary
from operations.observation import (
    AuthorityInputs,
    ObservationBegin,
    ObservationBeginOutcome,
    ObservationComplete,
    ObservationCompleteOutcome,
    ObservationService,
    ObservationStatus,
    ObservationStatusView,
)
from operations.observation_handler import ObservationRequestHandler
from operations.settings import resolve_operations_settings

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET_ID = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
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
    def __init__(
        self,
        *,
        begin: ObservationBeginOutcome = ObservationBeginOutcome.CREATED,
        status: ObservationStatus | None = None,
    ) -> None:
        self.begin_outcome = begin
        self.status_result = status
        self.begin_calls = 0
        self.complete_calls = 0

    def begin_observation(self, **kwargs: Any) -> ObservationBegin:
        self.begin_calls += 1
        if self.begin_outcome is ObservationBeginOutcome.CREATED:
            return ObservationBegin(ObservationBeginOutcome.CREATED, operation_id=kwargs["operation_id"])
        return ObservationBegin(self.begin_outcome)

    def complete_observation(self, **kwargs: Any) -> ObservationComplete:
        self.complete_calls += 1
        return ObservationComplete(
            ObservationCompleteOutcome.RECORDED, observation_hash=canonical_sha256(kwargs["observation"])
        )

    def fail_observation(self, **kwargs: Any) -> None:
        return None

    def load_status(self, **kwargs: Any) -> ObservationStatus | None:
        return self.status_result


class ExplodingService(ObservationService):
    """A service whose observe raises an unexpected, non-boundary exception."""

    def observe(self, request: Any, context: Any) -> dict[str, Any]:
        raise KeyError("unexpected internal detail: arn:aws:secret")


def _service(
    store: FakeStore, *, mode: str = "observe", cls: type[ObservationService] = ObservationService
) -> ObservationService:
    return cls(
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
        operation_id_factory=lambda: OPERATION_ID,
    )


def _handler(
    store: FakeStore | None = None,
    *,
    mode: str = "observe",
    cls: type[ObservationService] = ObservationService,
) -> tuple[ObservationRequestHandler, FakeStore]:
    store = store or FakeStore()
    handler = ObservationRequestHandler(
        service=_service(store, mode=mode, cls=cls),
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


def _claims() -> dict[str, Any]:
    return {"sub": "subject.operator-1", "client_id": "client.web-console", "token_use": "access", "exp": EXP}


def _event(
    *,
    method: str = "POST",
    claims: dict[str, Any] | None = None,
    body: Any = None,
    path_parameters: dict[str, Any] | None = None,
    with_authorizer: bool = True,
) -> dict[str, Any]:
    request_context: dict[str, Any] = {"requestId": "abc123def456", "http": {"method": method}}
    if with_authorizer:
        request_context["authorizer"] = {"jwt": {"claims": claims if claims is not None else _claims()}}
    event: dict[str, Any] = {"requestContext": request_context}
    if method == "POST":
        event["body"] = json.dumps({"fleet_id": FLEET_ID, "idempotency_token": TOKEN}) if body is None else body
    if path_parameters is not None:
        event["pathParameters"] = path_parameters
    return event


# --- POST observe ---------------------------------------------------------


def test_post_success_returns_200_and_valid_observation() -> None:
    handler, store = _handler()
    response = handler.handle(_event())
    assert response["statusCode"] == 200
    observation = json.loads(response["body"])
    assert observation["target"]["fleet_id"] == FLEET_ID
    assert observation["requester"]["subject_id"] == "subject.operator-1"
    assert store.begin_calls == 1
    assert store.complete_calls == 1


def test_direct_call_without_authorizer_is_rejected() -> None:
    handler, store = _handler()
    response = handler.handle(_event(with_authorizer=False))
    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error_code"] == "IDENTITY_CONTEXT_INVALID"
    assert store.begin_calls == 0


def test_identity_from_body_is_ignored() -> None:
    handler, _ = _handler()
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
    assert store.begin_calls == 0


def test_disabled_deployment_denies() -> None:
    handler, store = _handler(mode="disabled")
    response = handler.handle(_event())
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error_code"] == "AUTHORIZATION_DENIED"
    assert store.begin_calls == 0


def test_idempotency_conflict_maps_to_409() -> None:
    handler, _ = _handler(FakeStore(begin=ObservationBeginOutcome.IDEMPOTENCY_CONFLICT))
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


# --- GET status -----------------------------------------------------------


def test_get_status_returns_200_with_state() -> None:
    status = ObservationStatus(
        operation_id=OPERATION_ID,
        workspace_id="workspace.default",
        state=ObservationStatusView.OBSERVING,
    )
    handler, _ = _handler(FakeStore(status=status))
    response = handler.handle(_event(method="GET", path_parameters={"operationId": OPERATION_ID}))
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["operation_id"] == OPERATION_ID
    assert body["state"] == "observing"


def test_get_status_missing_is_404() -> None:
    handler, _ = _handler(FakeStore(status=None))
    response = handler.handle(_event(method="GET", path_parameters={"operationId": OPERATION_ID}))
    assert response["statusCode"] == 404
    assert json.loads(response["body"])["error_code"] == "NOT_FOUND"


def test_get_status_cross_workspace_is_404() -> None:
    status = ObservationStatus(
        operation_id=OPERATION_ID,
        workspace_id="workspace.other",
        state=ObservationStatusView.OBSERVING,
    )
    handler, _ = _handler(FakeStore(status=status))
    response = handler.handle(_event(method="GET", path_parameters={"operationId": OPERATION_ID}))
    assert response["statusCode"] == 404


def test_get_status_invalid_operation_id_is_400() -> None:
    handler, _ = _handler()
    response = handler.handle(_event(method="GET", path_parameters={"operationId": "not-an-op"}))
    assert response["statusCode"] == 400


def test_get_status_without_authorizer_is_401() -> None:
    handler, _ = _handler()
    response = handler.handle(
        _event(method="GET", path_parameters={"operationId": OPERATION_ID}, with_authorizer=False)
    )
    assert response["statusCode"] == 401


# --- Method routing -------------------------------------------------------


def test_unsupported_method_is_400() -> None:
    handler, _ = _handler()
    response = handler.handle(_event(method="DELETE", path_parameters={"operationId": OPERATION_ID}))
    assert response["statusCode"] == 400


# --- Catch-all sanitization -----------------------------------------------


def test_unexpected_exception_is_sanitized_to_500() -> None:
    handler, _ = _handler(cls=ExplodingService)
    response = handler.handle(_event())
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    # No internal detail leaks: only a bounded, generic safe message.
    assert "arn:aws:secret" not in json.dumps(body)
    assert body["error_code"] == "INTERNAL_ERROR"
    assert body["safe_message"] == "observation request failed"


# --- No write surface -----------------------------------------------------


def test_handler_exposes_no_write_surface() -> None:
    public = {name for name in dir(ObservationRequestHandler) if not name.startswith("_")}
    assert public == {"handle"}
