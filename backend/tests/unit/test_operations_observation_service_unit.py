"""Exhaustive unit tests for the read-only observation service (issue #413)."""

from __future__ import annotations

# Standard library
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.observation import validate_observation
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal
from operations.observation import (
    AuthorityInputs,
    GameLiftObservationReader,
    ObservationBoundaryError,
    ObservationCommit,
    ObservationCommitOutcome,
    ObservationErrorCode,
    ObservationRequest,
    ObservationRequestContext,
    ObservationService,
)
from operations.settings import resolve_operations_settings
from operations.validation.e0_latency import LatencyBudget

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET_ID = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
IDEMPOTENCY_TOKEN = "idem_abcdefghijklmnopqrstuvwx"
REQUEST_ID = "request.observe-1"


def _principal(**overrides: Any) -> VerifiedPrincipal:
    values: dict[str, Any] = {
        "subject_id": "subject.operator-1",
        "client_id": "client.web-console",
        "audience": "operations-api",
        "tenant_id": "tenant.default",
        "workspace_id": "workspace.default",
        "expires_at": NOW + timedelta(minutes=30),
        "groups": frozenset({"operators"}),
        "scopes": frozenset({"operations:observe"}),
    }
    values.update(overrides)
    return VerifiedPrincipal(**values)


def _boundary() -> ApprovalIdentityBoundary:
    return ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        requester_client_ids=frozenset({"client.web-console"}),
        approver_client_ids=frozenset({"client.approver"}),
        trusted_audiences=frozenset({"operations-api"}),
    )


def _authority_inputs(**overrides: str) -> AuthorityInputs:
    values = {
        "tenant_policy": "remediate",
        "workspace_policy": "advise",
        "principal_authority": "observe",
        "capability_maximum": "observe",
        "risk_policy": "remediate",
    }
    values.update(overrides)
    return AuthorityInputs(**values)


def _context(**overrides: Any) -> ObservationRequestContext:
    values: dict[str, Any] = {
        "requester": _principal(),
        "request_id": REQUEST_ID,
        "authority_inputs": _authority_inputs(),
        "capability_id": "gamelift.observe-fleet",
        "capability_version": "1.0",
    }
    values.update(overrides)
    return ObservationRequestContext(**values)


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_utilization(self, fleet_id: str) -> dict[str, int]:
        self.calls.append("utilization")
        return {
            "active_server_processes": 12,
            "active_game_sessions": 8,
            "current_player_sessions": 30,
            "maximum_player_sessions": 100,
        }

    def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
        self.calls.append("capacity")
        return [
            {"location": "us-west-2", "desired": 10, "minimum": 2, "maximum": 20, "active": 10, "idle": 2},
        ]

    def read_scaling_policies(self, fleet_id: str) -> list[dict[str, str]]:
        self.calls.append("scaling")
        return [{"name": "target", "status": "ACTIVE", "metric_name": "PercentAvailableGameSessions"}]


class FakeStore:
    def __init__(self, outcome: ObservationCommitOutcome = ObservationCommitOutcome.RECORDED) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []
        self.replay_observation: dict[str, Any] | None = None
        self.commit_time: datetime | None = None

    def record_observation(self, **kwargs: Any) -> ObservationCommit:
        self.calls.append(deepcopy(kwargs))
        if self.commit_time is not None and self.commit_time >= kwargs["commit_not_after"]:
            return ObservationCommit(ObservationCommitOutcome.DEADLINE_EXPIRED)
        if self.outcome is ObservationCommitOutcome.REPLAY:
            return ObservationCommit(ObservationCommitOutcome.REPLAY, deepcopy(self.replay_observation))
        return ObservationCommit(self.outcome)


class RecordingMetrics:
    def __init__(self) -> None:
        self.events: list[tuple[str, float, dict[str, str] | None]] = []

    def record(self, name: str, value: float, *, dimensions: dict[str, str] | None = None) -> None:
        self.events.append((name, value, dimensions))


def _service(
    *,
    settings_mode: str = "observe",
    reader: Any = None,
    store: Any = None,
    clock_times: list[datetime] | None = None,
    monotonic_times: list[float] | None = None,
    metrics: Any = None,
) -> tuple[ObservationService, FakeReader, FakeStore, RecordingMetrics]:
    reader = reader or FakeReader()
    store = store or FakeStore()
    metrics = metrics or RecordingMetrics()
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": settings_mode})

    times = list(clock_times) if clock_times is not None else [NOW, NOW]

    def clock() -> datetime:
        return times.pop(0) if len(times) > 1 else times[0]

    mono = list(monotonic_times) if monotonic_times is not None else None

    def monotonic() -> float:
        if mono is None:
            return time.monotonic()
        return mono.pop(0) if len(mono) > 1 else mono[0]

    service = ObservationService(
        settings=settings,
        identity_boundary=_boundary(),
        reader=reader,
        store=store,
        clock=clock,
        monotonic=monotonic,
        observation_id_factory=lambda: "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        metrics=metrics,
    )
    return service, reader, store, metrics


def _request() -> ObservationRequest:
    return ObservationRequest(fleet_id=FLEET_ID, idempotency_token=IDEMPOTENCY_TOKEN)


# --- Happy path -----------------------------------------------------------


def test_successful_observation_is_valid_and_persisted() -> None:
    service, reader, store, metrics = _service()
    result = service.observe(_request(), _context())

    validate_observation(result)
    assert result["effective_authority"] == "observe"
    assert result["target"]["fleet_id"] == FLEET_ID
    assert result["requester"]["tenant_id"] == "tenant.default"
    assert reader.calls == ["utilization", "capacity", "scaling"]
    assert len(store.calls) == 1
    assert any(name == "observation.recorded" for name, _, _ in metrics.events)


def test_effective_authority_is_minimum_capped_at_observe() -> None:
    service, _, _, _ = _service()
    result = service.observe(_request(), _context(authority_inputs=_authority_inputs(principal_authority="operate")))
    # Every input is >= observe and the deployment mode is observe, so the
    # deterministic minimum (and the phase cap) is observe.
    assert result["effective_authority"] == "observe"


def test_requester_identity_comes_from_verified_principal_not_request() -> None:
    service, _, _, _ = _service()
    result = service.observe(_request(), _context())
    assert set(result["requester"]) == {"subject_id", "client_id", "tenant_id", "workspace_id"}
    assert result["requester"]["subject_id"] == "subject.operator-1"


# --- Deployment ceiling ---------------------------------------------------


def test_disabled_deployment_denies_before_any_read() -> None:
    service, reader, store, _ = _service(settings_mode="disabled")
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.AUTHORIZATION_DENIED
    assert reader.calls == []
    assert store.calls == []


# --- Identity boundary ----------------------------------------------------


def test_tenant_mismatch_is_denied() -> None:
    service, reader, _, _ = _service()
    context = _context(requester=_principal(tenant_id="tenant.other"))
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), context)
    assert exc.value.error_code is ObservationErrorCode.IDENTITY_CONTEXT_INVALID
    assert reader.calls == []


def test_expired_credential_is_denied() -> None:
    service, _, _, _ = _service()
    context = _context(requester=_principal(expires_at=NOW - timedelta(seconds=1)))
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), context)
    assert exc.value.error_code is ObservationErrorCode.IDENTITY_CONTEXT_INVALID


def test_untrusted_client_is_denied() -> None:
    service, _, _, _ = _service()
    context = _context(requester=_principal(client_id="client.attacker"))
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), context)
    assert exc.value.error_code is ObservationErrorCode.IDENTITY_CONTEXT_INVALID


# --- Authority ceiling ----------------------------------------------------


def test_principal_below_observe_is_denied() -> None:
    service, reader, _, _ = _service()
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context(authority_inputs=_authority_inputs(capability_maximum="disabled")))
    assert exc.value.error_code is ObservationErrorCode.AUTHORIZATION_DENIED
    assert reader.calls == []


# --- Request payload boundary --------------------------------------------


def test_request_payload_rejects_identity_injection() -> None:
    with pytest.raises(ObservationBoundaryError) as exc:
        ObservationRequest.from_payload(
            {"fleet_id": FLEET_ID, "idempotency_token": IDEMPOTENCY_TOKEN, "tenant_id": "x"}
        )
    assert exc.value.error_code is ObservationErrorCode.CONTRACT_INVALID


def test_request_payload_rejects_invalid_fleet() -> None:
    with pytest.raises(ObservationBoundaryError):
        ObservationRequest.from_payload({"fleet_id": "not-a-fleet", "idempotency_token": IDEMPOTENCY_TOKEN})


def test_request_payload_accepts_exact_shape() -> None:
    request = ObservationRequest.from_payload({"fleet_id": FLEET_ID, "idempotency_token": IDEMPOTENCY_TOKEN})
    assert request.fleet_id == FLEET_ID


# --- Provider read failures (fail closed, no partial success) -------------


def test_provider_read_exception_fails_closed_retryable() -> None:
    class BoomReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("provider boom")

    service, _, store, _ = _service(reader=BoomReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True
    assert store.calls == []


def test_provider_none_result_fails_closed() -> None:
    class NoneReader(FakeReader):
        def read_scaling_policies(self, fleet_id: str) -> Any:
            return None

    service, _, store, _ = _service(reader=NoneReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.calls == []


def test_slow_read_exceeding_budget_fails_closed() -> None:
    class SlowReader(FakeReader):
        def read_utilization(self, fleet_id: str) -> dict[str, int]:
            time.sleep(0.05)
            return super().read_utilization(fleet_id)

    # Drive the monotonic clock so the measured elapsed exceeds the per-read budget.
    service, _, store, _ = _service(
        reader=SlowReader(),
        monotonic_times=[0.0, 0.0, 100.0, 200.0, 300.0, 400.0],
    )
    service._budget = LatencyBudget(per_read_s=1.0, persistence_s=1.0, cancellation_margin_s=1.0)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.calls == []


def test_malformed_provider_shape_fails_closed() -> None:
    class BadReader(FakeReader):
        def read_utilization(self, fleet_id: str) -> Any:
            return {"active_server_processes": -1}

    service, _, store, _ = _service(reader=BadReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.calls == []


def test_oversized_capacity_fails_closed() -> None:
    class BigReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            return [
                {"location": f"loc-{i}", "desired": 1, "minimum": 0, "maximum": 2, "active": 1, "idle": 0}
                for i in range(65)
            ]

    service, _, store, _ = _service(reader=BigReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.calls == []


# --- Persistence outcomes -------------------------------------------------


def test_replay_returns_stored_observation_without_rerunning() -> None:
    store = FakeStore(ObservationCommitOutcome.REPLAY)
    service, _, _, metrics = _service(store=store)
    recorded_service, _, _, _ = _service()
    stored = recorded_service.observe(_request(), _context())
    store.replay_observation = stored

    result = service.observe(_request(), _context())
    assert result == stored
    assert any(name == "observation.replay" for name, _, _ in metrics.events)


def test_idempotency_conflict_is_typed_and_not_retryable() -> None:
    store = FakeStore(ObservationCommitOutcome.IDEMPOTENCY_CONFLICT)
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.IDEMPOTENCY_CONFLICT
    assert exc.value.retryable is False


def test_state_conflict_is_typed() -> None:
    store = FakeStore(ObservationCommitOutcome.STATE_CONFLICT)
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


def test_deadline_expired_is_retryable() -> None:
    store = FakeStore()
    store.commit_time = NOW + timedelta(hours=2)  # past any commit_not_after
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True


def test_string_lookalike_outcome_fails_closed() -> None:
    class StringStore:
        def record_observation(self, **kwargs: Any) -> Any:
            return "recorded"

    service, _, _, _ = _service(store=StringStore())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


# --- Persistence transaction inputs --------------------------------------


def test_store_receives_fingerprint_ttl_and_deadline() -> None:
    service, _, store, _ = _service()
    service.observe(_request(), _context())
    call = store.calls[0]
    assert call["workspace_id"] == "workspace.default"
    assert call["idempotency_token"] == IDEMPOTENCY_TOKEN
    assert call["idempotency_fingerprint"].startswith("sha256:")
    assert call["ttl_epoch_s"] > int(NOW.timestamp())
    assert call["commit_not_after"] <= _principal().expires_at


def test_no_write_method_on_service_or_ports() -> None:
    public = {name for name in dir(ObservationService) if not name.startswith("_")}
    assert public == {"observe"}
    reader_methods = {m for m in dir(GameLiftObservationReader) if not m.startswith("_")}
    assert reader_methods == {"read_utilization", "read_capacity", "read_scaling_policies"}


def test_observation_carries_no_provider_payload_fields() -> None:
    service, _, _, _ = _service()
    result = service.observe(_request(), _context())
    forbidden = {"arn", "account_id", "provider_response", "raw"}

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert key not in forbidden
                _walk(child)
        elif isinstance(value, list):
            for child in value:
                _walk(child)

    _walk(result)
