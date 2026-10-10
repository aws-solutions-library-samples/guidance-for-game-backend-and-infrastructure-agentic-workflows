"""Exhaustive unit tests for the read-only observation service (issue #413).

These cover the ADR 0005 "create before reads" lifecycle: the intent
fingerprint computed before any provider read, a no-second-read completed
replay, an idempotency conflict on changed intent, an in-progress retry, the
two-phase persistence outcomes, hash re-verification, typed status retrieval
with cross-workspace denial, the effective-authority cap fix, and the
timeout-vs-failure metric split.
"""

from __future__ import annotations

# Standard library
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import canonical_sha256
from operations.contracts.observation import validate_observation
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal
from operations.observation import (
    AuthorityInputs,
    GameLiftObservationReader,
    ObservationBegin,
    ObservationBeginOutcome,
    ObservationBoundaryError,
    ObservationComplete,
    ObservationCompleteOutcome,
    ObservationErrorCode,
    ObservationRequest,
    ObservationRequestContext,
    ObservationService,
    ObservationStatus,
    ObservationStatusView,
    StatusRequest,
    StatusRequestContext,
)
from operations.settings import resolve_operations_settings

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET_ID = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
IDEMPOTENCY_TOKEN = "idem_abcdefghijklmnopqrstuvwx"
REQUEST_ID = "request.observe-1"
OPERATION_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"


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
    """A two-phase store fake driving explicit begin/complete outcomes."""

    def __init__(
        self,
        *,
        begin: ObservationBeginOutcome = ObservationBeginOutcome.CREATED,
        complete: ObservationCompleteOutcome = ObservationCompleteOutcome.RECORDED,
    ) -> None:
        self.begin_outcome = begin
        self.complete_outcome = complete
        self.begin_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []
        self.fail_calls: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.replay_observation: dict[str, Any] | None = None
        self.replay_hash: str | None = None
        self.status_result: ObservationStatus | None = None
        self.commit_time: datetime | None = None
        self.failure_reason: str | None = None
        self.reclaim_generation: int = 2

    def begin_observation(self, **kwargs: Any) -> ObservationBegin:
        self.begin_calls.append(deepcopy(kwargs))
        if self.commit_time is not None and self.commit_time >= kwargs["commit_not_after"]:
            return ObservationBegin(ObservationBeginOutcome.DEADLINE_EXPIRED)
        if self.begin_outcome is ObservationBeginOutcome.REPLAY_COMPLETED:
            return ObservationBegin(
                ObservationBeginOutcome.REPLAY_COMPLETED,
                operation_id=kwargs["operation_id"],
                observation=deepcopy(self.replay_observation),
                observation_hash=self.replay_hash,
            )
        if self.begin_outcome is ObservationBeginOutcome.IN_PROGRESS:
            return ObservationBegin(
                ObservationBeginOutcome.IN_PROGRESS, operation_id=kwargs["operation_id"], current_state="observing"
            )
        if self.begin_outcome is ObservationBeginOutcome.REPLAY_FAILED:
            return ObservationBegin(
                ObservationBeginOutcome.REPLAY_FAILED,
                operation_id=kwargs["operation_id"],
                current_state="failed",
                failure_reason=self.failure_reason,
            )
        if self.begin_outcome is ObservationBeginOutcome.RECLAIMED:
            return ObservationBegin(
                ObservationBeginOutcome.RECLAIMED,
                operation_id=kwargs["operation_id"],
                current_state="observing",
                generation=self.reclaim_generation,
            )
        if self.begin_outcome is ObservationBeginOutcome.CREATED:
            return ObservationBegin(ObservationBeginOutcome.CREATED, operation_id=kwargs["operation_id"])
        return ObservationBegin(self.begin_outcome)

    def complete_observation(self, **kwargs: Any) -> ObservationComplete:
        self.complete_calls.append(deepcopy(kwargs))
        if self.complete_outcome is ObservationCompleteOutcome.ALREADY_TERMINAL:
            return ObservationComplete(
                ObservationCompleteOutcome.ALREADY_TERMINAL,
                observation=deepcopy(self.replay_observation),
                observation_hash=self.replay_hash,
            )
        if self.complete_outcome is ObservationCompleteOutcome.RECORDED:
            return ObservationComplete(
                ObservationCompleteOutcome.RECORDED, observation_hash=canonical_sha256(kwargs["observation"])
            )
        return ObservationComplete(self.complete_outcome)

    def fail_observation(self, **kwargs: Any) -> None:
        self.fail_calls.append(deepcopy(kwargs))

    def load_status(self, **kwargs: Any) -> ObservationStatus | None:
        self.status_calls.append(deepcopy(kwargs))
        return self.status_result


class RecordingMetrics:
    def __init__(self) -> None:
        self.events: list[tuple[str, float, dict[str, str] | None]] = []

    def record(self, name: str, value: float, *, dimensions: dict[str, str] | None = None) -> None:
        self.events.append((name, value, dimensions))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.events]


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
        operation_id_factory=lambda: OPERATION_ID,
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
    assert result["observation_id"] == OPERATION_ID
    assert result["target"]["fleet_id"] == FLEET_ID
    assert result["requester"]["tenant_id"] == "tenant.default"
    assert reader.calls == ["utilization", "capacity", "scaling"]
    assert len(store.begin_calls) == 1
    assert len(store.complete_calls) == 1
    assert "observation.recorded" in metrics.names()


def test_idempotency_is_resolved_before_any_read() -> None:
    # The begin call must precede any provider read: a completed replay performs
    # no reads at all.
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_COMPLETED)
    recorded_service, _, _, _ = _service()
    stored = recorded_service.observe(_request(), _context())
    store.replay_observation = stored
    store.replay_hash = canonical_sha256(stored)

    service, reader, _, _ = _service(store=store)
    result = service.observe(_request(), _context())
    assert result == stored
    assert reader.calls == []  # no second read
    assert store.complete_calls == []


def test_fingerprint_is_over_workspace_and_intent_not_result() -> None:
    service, _, store, _ = _service()
    service.observe(_request(), _context())
    call = store.begin_calls[0]
    assert call["workspace_id"] == "workspace.default"
    assert call["idempotency_token"] == IDEMPOTENCY_TOKEN
    assert call["idempotency_fingerprint"].startswith("sha256:")
    # The intent carries only the request-side identity of the operation.
    assert call["intent"]["target"] == {"provider": "gamelift", "fleet_id": FLEET_ID}
    assert call["intent"]["capability_id"] == "gamelift.observe-fleet"
    assert "observation_id" not in call["intent"]
    assert "observed_at" not in call["intent"]


def test_same_intent_different_fleet_produces_different_fingerprint() -> None:
    service, _, store, _ = _service()
    other_fleet = "fleet-9999abcd-5678-90ef-a1b2-c3d4e5f60789"
    service.observe(_request(), _context())
    service.observe(ObservationRequest(fleet_id=other_fleet, idempotency_token=IDEMPOTENCY_TOKEN), _context())
    assert store.begin_calls[0]["idempotency_fingerprint"] != store.begin_calls[1]["idempotency_fingerprint"]


# --- Effective authority (higher ceilings still yield observe) ------------


def test_effective_authority_is_minimum_capped_at_observe() -> None:
    service, _, _, _ = _service()
    result = service.observe(_request(), _context(authority_inputs=_authority_inputs(principal_authority="operate")))
    assert result["effective_authority"] == "observe"


def test_higher_deployment_ceiling_still_yields_observe_without_contract_disagreement() -> None:
    # Every input is >= observe and the deployment mode is 'operate'. The five
    # caller/deployment inputs are recorded EXACTLY as supplied (truthful raw
    # ceilings), only this capability's own maximum is normalized to the observe
    # phase ceiling, and the effective authority is the real min(inputs) = observe.
    # validate_observation still agrees because capability_maximum == observe
    # makes observe the true minimum.
    service, _, _, _ = _service(settings_mode="operate")
    result = service.observe(
        _request(),
        _context(
            authority_inputs=_authority_inputs(
                tenant_policy="operate",
                workspace_policy="operate",
                principal_authority="operate",
                capability_maximum="operate",
                risk_policy="operate",
            )
        ),
    )
    validate_observation(result)
    assert result["effective_authority"] == "observe"
    # Raw ceilings are preserved unchanged; only capability_maximum is the phase
    # ceiling; deployment_mode reflects the real 'operate' deployment.
    inputs = result["authority_inputs"]
    assert inputs["deployment_mode"] == "operate"
    assert inputs["tenant_policy"] == "operate"
    assert inputs["workspace_policy"] == "operate"
    assert inputs["principal_authority"] == "operate"
    assert inputs["risk_policy"] == "operate"
    assert inputs["capability_maximum"] == "observe"


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
    assert store.begin_calls == []


# --- Identity boundary ----------------------------------------------------


def test_tenant_mismatch_is_denied() -> None:
    service, reader, store, _ = _service()
    context = _context(requester=_principal(tenant_id="tenant.other"))
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), context)
    assert exc.value.error_code is ObservationErrorCode.IDENTITY_CONTEXT_INVALID
    assert reader.calls == []
    assert store.begin_calls == []


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
    service, reader, store, _ = _service()
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context(authority_inputs=_authority_inputs(capability_maximum="disabled")))
    assert exc.value.error_code is ObservationErrorCode.AUTHORIZATION_DENIED
    assert reader.calls == []
    assert store.begin_calls == []


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


def test_provider_read_exception_fails_closed_and_records_failed() -> None:
    class BoomReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("provider boom")

    service, _, store, metrics = _service(reader=BoomReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    # The provider failure is recorded terminally for this idempotency token, so
    # the FIRST response is non-retryable and tells the client to use a new
    # token — a same-token retry can never make progress.
    assert exc.value.retryable is False
    assert "new idempotency token" in exc.value.safe_message
    # The operation was created before the read, so a bounded failed transition
    # is recorded and no result is completed.
    assert len(store.begin_calls) == 1
    assert store.complete_calls == []
    assert len(store.fail_calls) == 1
    assert "observation.failed" in metrics.names()


def test_provider_none_result_fails_closed() -> None:
    class NoneReader(FakeReader):
        def read_scaling_policies(self, fleet_id: str) -> Any:
            return None

    service, _, store, _ = _service(reader=NoneReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.complete_calls == []


def test_slow_read_exceeding_budget_records_timeout_metric() -> None:
    # Local modules
    from operations.validation.e0_latency import LatencyBudget

    class SlowReader(FakeReader):
        def read_utilization(self, fleet_id: str) -> dict[str, int]:
            time.sleep(0.05)
            return super().read_utilization(fleet_id)

    service, _, store, metrics = _service(
        reader=SlowReader(),
        monotonic_times=[0.0, 0.0, 100.0, 200.0, 300.0, 400.0],
    )
    service._budget = LatencyBudget(per_read_s=1.0, persistence_s=1.0, cancellation_margin_s=1.0)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.complete_calls == []
    assert "observation.timeout" in metrics.names()


def test_malformed_provider_shape_fails_closed() -> None:
    class BadReader(FakeReader):
        def read_utilization(self, fleet_id: str) -> Any:
            return {"active_server_processes": -1}

    service, _, store, _ = _service(reader=BadReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.complete_calls == []


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
    assert store.complete_calls == []


# --- Begin outcomes -------------------------------------------------------


def test_completed_replay_returns_stored_without_rerunning() -> None:
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_COMPLETED)
    recorded_service, _, _, _ = _service()
    stored = recorded_service.observe(_request(), _context())
    store.replay_observation = stored
    store.replay_hash = canonical_sha256(stored)

    service, reader, _, metrics = _service(store=store)
    result = service.observe(_request(), _context())
    assert result == stored
    assert reader.calls == []
    assert "observation.replay" in metrics.names()


def test_replay_with_tampered_hash_fails_closed() -> None:
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_COMPLETED)
    recorded_service, _, _, _ = _service()
    stored = recorded_service.observe(_request(), _context())
    store.replay_observation = stored
    store.replay_hash = "sha256:" + "0" * 64  # does not match the canonical hash

    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


def test_idempotency_conflict_is_typed_and_not_retryable() -> None:
    store = FakeStore(begin=ObservationBeginOutcome.IDEMPOTENCY_CONFLICT)
    service, reader, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.IDEMPOTENCY_CONFLICT
    assert exc.value.retryable is False
    assert reader.calls == []


def test_in_progress_retry_does_not_create_second_operation() -> None:
    store = FakeStore(begin=ObservationBeginOutcome.IN_PROGRESS)
    service, reader, _, metrics = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT
    assert exc.value.retryable is True
    assert reader.calls == []
    assert store.complete_calls == []
    assert "observation.in_progress" in metrics.names()


def test_begin_deadline_expired_is_retryable() -> None:
    store = FakeStore()
    store.commit_time = NOW + timedelta(hours=2)
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True


def test_begin_state_conflict_is_typed() -> None:
    store = FakeStore(begin=ObservationBeginOutcome.STATE_CONFLICT)
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


def test_begin_string_lookalike_fails_closed() -> None:
    class StringStore(FakeStore):
        def begin_observation(self, **kwargs: Any) -> Any:
            return "created"

    service, _, _, _ = _service(store=StringStore())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


# --- Complete outcomes ----------------------------------------------------


def test_complete_already_terminal_replays_racing_result() -> None:
    store = FakeStore(complete=ObservationCompleteOutcome.ALREADY_TERMINAL)
    recorded_service, _, _, _ = _service()
    stored = recorded_service.observe(_request(), _context())
    store.replay_observation = stored
    store.replay_hash = canonical_sha256(stored)

    service, _, _, metrics = _service(store=store)
    result = service.observe(_request(), _context())
    assert result == stored
    assert "observation.replay" in metrics.names()


def test_complete_state_conflict_is_typed() -> None:
    store = FakeStore(complete=ObservationCompleteOutcome.STATE_CONFLICT)
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


def test_complete_deadline_expired_is_retryable() -> None:
    store = FakeStore(complete=ObservationCompleteOutcome.DEADLINE_EXPIRED)
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True


def test_complete_string_lookalike_fails_closed() -> None:
    class StringStore(FakeStore):
        def complete_observation(self, **kwargs: Any) -> Any:
            return "recorded"

    service, _, _, _ = _service(store=StringStore())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


# --- Status retrieval -----------------------------------------------------


def _status_context(**overrides: Any) -> StatusRequestContext:
    values: dict[str, Any] = {"requester": _principal(), "request_id": REQUEST_ID}
    values.update(overrides)
    return StatusRequestContext(**values)


def test_status_returns_typed_succeeded_view() -> None:
    recorded_service, _, _, _ = _service()
    stored = recorded_service.observe(_request(), _context())
    store = FakeStore()
    store.status_result = ObservationStatus(
        operation_id=OPERATION_ID,
        workspace_id="workspace.default",
        state=ObservationStatusView.SUCCEEDED,
        observation=stored,
        observation_hash=canonical_sha256(stored),
    )
    service, _, _, _ = _service(store=store)
    status = service.get_status(StatusRequest(operation_id=OPERATION_ID), _status_context())
    assert status.state is ObservationStatusView.SUCCEEDED
    assert status.observation == stored
    assert store.status_calls[0]["workspace_id"] == "workspace.default"


def test_status_missing_is_not_found() -> None:
    store = FakeStore()
    store.status_result = None
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.get_status(StatusRequest(operation_id=OPERATION_ID), _status_context())
    assert exc.value.error_code is ObservationErrorCode.NOT_FOUND


def test_status_cross_workspace_is_denied_as_not_found() -> None:
    store = FakeStore()
    store.status_result = ObservationStatus(
        operation_id=OPERATION_ID,
        workspace_id="workspace.other",
        state=ObservationStatusView.OBSERVING,
    )
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.get_status(StatusRequest(operation_id=OPERATION_ID), _status_context())
    assert exc.value.error_code is ObservationErrorCode.NOT_FOUND


def test_status_denied_when_observe_disabled() -> None:
    service, _, _, _ = _service(settings_mode="disabled")
    with pytest.raises(ObservationBoundaryError) as exc:
        service.get_status(StatusRequest(operation_id=OPERATION_ID), _status_context())
    assert exc.value.error_code is ObservationErrorCode.AUTHORIZATION_DENIED


def test_status_verifies_stored_hash() -> None:
    recorded_service, _, _, _ = _service()
    stored = recorded_service.observe(_request(), _context())
    store = FakeStore()
    store.status_result = ObservationStatus(
        operation_id=OPERATION_ID,
        workspace_id="workspace.default",
        state=ObservationStatusView.SUCCEEDED,
        observation=stored,
        observation_hash="sha256:" + "0" * 64,
    )
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.get_status(StatusRequest(operation_id=OPERATION_ID), _status_context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


# --- No provider-write surface -------------------------------------------


def test_no_write_method_on_service_or_ports() -> None:
    public = {name for name in dir(ObservationService) if not name.startswith("_")}
    assert public == {"observe", "get_status"}
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


# --- Adversarial: honest stale-lease recovery ------------------------------


def test_reclaimed_operation_reruns_reads_and_finalizes_under_generation() -> None:
    # A stale-lease reclaim reruns the read-only observation and finalizes under
    # the reclaimed fencing generation reported by the store.
    store = FakeStore(begin=ObservationBeginOutcome.RECLAIMED)
    store.reclaim_generation = 2
    service, reader, _, metrics = _service(store=store)
    result = service.observe(_request(), _context())
    validate_observation(result)
    # The read-only observation was actually rerun under the reclaimed lease.
    assert reader.calls == ["utilization", "capacity", "scaling"]
    # The finalize used the reclaimed generation, not the initial generation 1.
    assert store.complete_calls[0]["generation"] == 2
    assert "observation.reclaimed" in metrics.names()


def test_reclaimed_read_failure_records_failed_under_reclaimed_generation() -> None:
    class BoomReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("provider boom")

    store = FakeStore(begin=ObservationBeginOutcome.RECLAIMED)
    store.reclaim_generation = 3
    service, _, _, _ = _service(store=store, reader=BoomReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.fail_calls[0]["generation"] == 3


def test_reclaimed_without_operation_id_is_state_conflict() -> None:
    class NoIdStore(FakeStore):
        def begin_observation(self, **kwargs: Any) -> ObservationBegin:
            return ObservationBegin(ObservationBeginOutcome.RECLAIMED, operation_id=None, generation=2)

    service, _, _, _ = _service(store=NoIdStore())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.STATE_CONFLICT


# --- Adversarial: terminal-failed replay (new token required) --------------


def test_terminal_failed_replay_is_not_retryable_and_reruns_nothing() -> None:
    # A terminally failed operation replays its bounded failure deterministically
    # and is NOT retryable: the same token can never make progress.
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_FAILED)
    store.failure_reason = "provider_unavailable"
    service, reader, _, metrics = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is False
    assert "new idempotency token" in exc.value.safe_message
    assert reader.calls == []  # no rerun under the same token
    assert store.complete_calls == []


@pytest.mark.parametrize(
    ("stored_reason", "expected_code", "expected_retryable"),
    [
        ("not_found", ObservationErrorCode.NOT_FOUND, False),
        ("contract_invalid", ObservationErrorCode.CONTRACT_INVALID, False),
        ("provider_contract_invalid", ObservationErrorCode.INTERNAL_ERROR, False),
        ("provider_unavailable", ObservationErrorCode.PROVIDER_UNAVAILABLE, False),
    ],
)
def test_terminal_failed_replay_maps_the_stored_reason_to_the_first_responses_code(
    stored_reason: str, expected_code: ObservationErrorCode, expected_retryable: bool
) -> None:
    # A same-token retry after a terminal failure replays the SAME typed code the
    # first response returned, not a blanket PROVIDER_UNAVAILABLE. The stored
    # bounded failure reason drives the code so a lost 404/400/500 does not
    # become a 503 on retry.
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_FAILED)
    store.failure_reason = stored_reason
    service, reader, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is expected_code
    assert exc.value.retryable is expected_retryable
    assert reader.calls == []
    assert store.complete_calls == []


def test_terminal_failed_replay_without_a_stored_reason_is_provider_unavailable() -> None:
    # An older record with no stored reason falls back to the non-retryable
    # provider-unavailable terminal response.
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_FAILED)
    store.failure_reason = None
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is False


def test_replay_of_a_client_caused_failure_does_not_publish_observation_failures() -> None:
    # A replay of a not_found terminal failure is a client error, not a service
    # fault: through the real CloudWatch sink it must publish NO
    # ObservationFailures metric, exactly as the first not_found response does
    # not. A client hammering a mistyped fleet id cannot feed the failures alarm
    # through replays.
    # Local modules
    from operations.observe.metrics import CloudWatchObservationMetrics

    class FakeCloudWatch:
        def __init__(self) -> None:
            self.puts: list[dict[str, Any]] = []

        def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            self.puts.append(kwargs)
            return {}

    cloudwatch = FakeCloudWatch()
    sink = CloudWatchObservationMetrics(client=cloudwatch, namespace="GBAW/Operations", emf_emit=lambda _line: None)
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_FAILED)
    store.failure_reason = "not_found"
    service, _, _, _ = _service(store=store, metrics=sink)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.NOT_FOUND
    # No ObservationFailures (nor any other metric) was published for the replay.
    names = [put["MetricData"][0]["MetricName"] for put in cloudwatch.puts]
    assert "ObservationFailures" not in names


def test_replay_of_a_provider_unavailable_failure_does_publish_observation_failures() -> None:
    # The other side of the boundary: a provider_unavailable replay IS a service
    # fault and still publishes ObservationFailures through the sink.
    # Local modules
    from operations.observe.metrics import CloudWatchObservationMetrics

    class FakeCloudWatch:
        def __init__(self) -> None:
            self.puts: list[dict[str, Any]] = []

        def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            self.puts.append(kwargs)
            return {}

    cloudwatch = FakeCloudWatch()
    sink = CloudWatchObservationMetrics(client=cloudwatch, namespace="GBAW/Operations", emf_emit=lambda _line: None)
    store = FakeStore(begin=ObservationBeginOutcome.REPLAY_FAILED)
    store.failure_reason = "provider_unavailable"
    service, _, _, _ = _service(store=store, metrics=sink)
    with pytest.raises(ObservationBoundaryError):
        service.observe(_request(), _context())
    names = [put["MetricData"][0]["MetricName"] for put in cloudwatch.puts]
    assert "ObservationFailures" in names


# --- Budget spent during begin, before the first read (minor) --------------


def test_budget_spent_during_begin_is_retryable_without_a_terminal_record() -> None:
    # If begin itself consumes the request budget (a slow conflict/reclaim
    # chain), the service returns the retryable budget error BEFORE the first
    # read, without burning the token on a terminal provider failure. No read
    # runs and no fail transition is recorded.
    mono = {"t": 0.0}

    def monotonic() -> float:
        return mono["t"]

    class SlowBeginStore(FakeStore):
        def begin_observation(self, **kwargs: Any) -> ObservationBegin:
            # begin advances the monotonic clock past the whole-request deadline.
            mono["t"] += 20.0
            return super().begin_observation(**kwargs)

    store = SlowBeginStore(begin=ObservationBeginOutcome.CREATED)
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    service = ObservationService(
        settings=settings,
        identity_boundary=_boundary(),
        reader=FakeReader(),
        store=store,
        clock=lambda: NOW,
        monotonic=monotonic,
        operation_id_factory=lambda: OPERATION_ID,
        metrics=RecordingMetrics(),
    )
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True
    assert store.fail_calls == []  # the token is not burned terminally
    assert store.complete_calls == []
    assert service._reader.calls == []  # no provider read ran


def test_new_idempotency_token_after_terminal_failure_starts_new_operation() -> None:
    # Documented behavior: a NEW idempotency token is a new fingerprint, so the
    # store creates a brand-new operation rather than replaying the failure.
    store = FakeStore(begin=ObservationBeginOutcome.CREATED)
    service, reader, _, _ = _service(store=store)
    new_token = "idem_zzzzzzzzzzzzzzzzzzzzzzzz"
    result = service.observe(ObservationRequest(fleet_id=FLEET_ID, idempotency_token=new_token), _context())
    validate_observation(result)
    assert reader.calls == ["utilization", "capacity", "scaling"]
    assert store.begin_calls[0]["idempotency_token"] == new_token


# --- Adversarial: non-conditional store fault is retryable, not 409 --------


def test_begin_provider_unavailable_is_retryable_not_conflict() -> None:
    store = FakeStore(begin=ObservationBeginOutcome.PROVIDER_UNAVAILABLE)
    service, reader, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True
    assert reader.calls == []


def test_complete_provider_unavailable_is_retryable_not_conflict() -> None:
    store = FakeStore(complete=ObservationCompleteOutcome.PROVIDER_UNAVAILABLE)
    service, _, _, _ = _service(store=store)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True


# --- Adversarial: truthful raw authority ceilings --------------------------


def test_high_raw_authority_inputs_are_recorded_unchanged() -> None:
    # A caller presenting high raw ceilings has them recorded verbatim; only this
    # capability's own maximum is normalized to the observe phase ceiling, and
    # effective authority is the real min(inputs) = observe.
    service, _, _, _ = _service(settings_mode="operate")
    result = service.observe(
        _request(),
        _context(
            authority_inputs=_authority_inputs(
                tenant_policy="operate",
                workspace_policy="remediate",
                principal_authority="operate",
                capability_maximum="operate",
                risk_policy="advise",
            )
        ),
    )
    validate_observation(result)
    inputs = result["authority_inputs"]
    assert inputs["deployment_mode"] == "operate"
    assert inputs["tenant_policy"] == "operate"
    assert inputs["workspace_policy"] == "remediate"
    assert inputs["principal_authority"] == "operate"
    assert inputs["risk_policy"] == "advise"
    # Only capability_maximum is the phase ceiling.
    assert inputs["capability_maximum"] == "observe"
    # Effective is the real deterministic minimum of the six recorded inputs.
    assert result["effective_authority"] == "observe"


def test_effective_authority_is_real_minimum_when_an_input_is_below_capability() -> None:
    # If a raw input is itself observe (the true min alongside capability_maximum),
    # effective stays observe. The min is NOT computed by capping every input.
    service, _, _, _ = _service()
    result = service.observe(_request(), _context(authority_inputs=_authority_inputs(risk_policy="observe")))
    assert result["effective_authority"] == "observe"
    # tenant_policy 'remediate' from the default fixture is preserved unchanged.
    assert result["authority_inputs"]["tenant_policy"] == "remediate"


# --- Read abandonment: a hung read returns within its per-read budget -------


def test_hung_read_is_abandoned_within_the_per_read_budget() -> None:
    # Standard library
    import threading

    # Local modules
    from operations.validation.e0_latency import LatencyBudget

    release = threading.Event()

    class HangingReader(FakeReader):
        def read_utilization(self, fleet_id: str) -> dict[str, int]:
            # Block until released; the service must abandon the read at its
            # per-read deadline rather than wait for this to return.
            release.wait(timeout=5.0)
            return super().read_utilization(fleet_id)

    service, _, store, metrics = _service(reader=HangingReader())
    # A short real per-read budget so the wall-clock abandonment is observable
    # without a slow test. The service uses real time for this measurement.
    service._budget = LatencyBudget(per_read_s=0.3, persistence_s=0.3, cancellation_margin_s=0.3)
    try:
        started = time.monotonic()
        with pytest.raises(ObservationBoundaryError) as exc:
            service.observe(_request(), _context())
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    # The request returned near the per-read budget, not after the full 5s block.
    assert elapsed < 2.0
    assert store.complete_calls == []
    assert "observation.timeout" in metrics.names()


# --- Provider error classification before any terminal record (major) -------


def _client_error(code: str) -> Exception:
    # Third-party packages
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "x"}}, "DescribeFleetCapacity")


def test_not_found_fleet_maps_to_not_found_non_retryable() -> None:
    class NotFoundReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            raise _client_error("NotFoundException")

    service, _, store, _ = _service(reader=NotFoundReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.NOT_FOUND
    assert exc.value.retryable is False
    # A terminal reason is recorded, and no result is completed.
    assert store.fail_calls[0]["reason_code"] == "not_found"
    assert store.complete_calls == []


def test_invalid_request_maps_to_contract_invalid() -> None:
    class InvalidReader(FakeReader):
        def read_scaling_policies(self, fleet_id: str) -> list[dict[str, str]]:
            raise _client_error("InvalidRequestException")

    service, _, store, _ = _service(reader=InvalidReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.CONTRACT_INVALID
    assert exc.value.retryable is False
    assert store.fail_calls[0]["reason_code"] == "contract_invalid"


def test_provider_data_failing_contract_is_a_5xx_not_client_400() -> None:
    # A provider value that passes shape normalization but fails the observation
    # contract is a server/provider-side fault, reported as a 5xx.
    class ContractBreakingReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            # desired above maximum violates the observation contract's ranges.
            return [{"location": "us-west-2", "desired": 99, "minimum": 0, "maximum": 1, "active": 0, "idle": 0}]

    service, _, store, _ = _service(reader=ContractBreakingReader())
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.INTERNAL_ERROR
    assert store.fail_calls[0]["reason_code"] == "provider_contract_invalid"


def test_metrics_failure_does_not_mask_the_typed_outcome() -> None:
    # With the production-shaped sink that swallows CloudWatch errors, a metrics
    # fault during a terminal failure never surfaces: the request raises ONLY the
    # typed ObservationBoundaryError. The service also records the failed
    # transition BEFORE emitting metrics, so the snapshot is never stranded.
    # Local modules
    from operations.observe.metrics import CloudWatchObservationMetrics

    class RaisingCloudWatch:
        def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("cloudwatch down")

    sink = CloudWatchObservationMetrics(client=RaisingCloudWatch(), namespace="GBAW/Operations")

    class BoomReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("provider boom")

    service, _, store, _ = _service(reader=BoomReader(), metrics=sink)
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    # A failed transition was recorded (count only — ordering is pinned by the
    # dedicated ordering tests below, which capture the fail-call count at the
    # moment each metric is recorded).
    assert len(store.fail_calls) == 1
    # The sink swallowed the CloudWatch error and counted the drop.
    assert sink.dropped == 1


# --- Fail-before-metrics ordering, pinned per branch (minor) ----------------


class _OrderingMetrics:
    """A metrics fake that records the store's fail-call count at each record.

    It is wired to the store under test so each ``record`` call can capture
    ``len(store.fail_calls)`` at that instant. The first failure metric must see
    a count of 1, proving ``fail_observation`` ran before the metric was emitted
    and so a metrics fault can never strand the snapshot in ``observing``.
    """

    def __init__(self, store: "FakeStore") -> None:
        self._store = store
        self.events: list[tuple[str, int, dict[str, str] | None]] = []

    def record(self, name: str, value: float, *, dimensions: dict[str, str] | None = None) -> None:
        self.events.append((name, len(self._store.fail_calls), dimensions))

    def first_failure_metric(self) -> tuple[str, int, dict[str, str] | None]:
        return next(e for e in self.events if e[0] in {"observation.failed", "observation.timeout"})


def _ordering_service(reader: Any) -> tuple[ObservationService, "FakeStore", _OrderingMetrics]:
    store = FakeStore(begin=ObservationBeginOutcome.CREATED)
    metrics = _OrderingMetrics(store)
    settings = resolve_operations_settings(env={"GBAW_OPERATIONS_MODE": "observe"})
    service = ObservationService(
        settings=settings,
        identity_boundary=_boundary(),
        reader=reader,
        store=store,
        clock=lambda: NOW,
        operation_id_factory=lambda: OPERATION_ID,
        metrics=metrics,
    )
    return service, store, metrics


def test_classified_not_found_records_fail_before_metrics() -> None:
    class NotFoundReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            raise _client_error("NotFoundException")

    service, store, metrics = _ordering_service(NotFoundReader())
    with pytest.raises(ObservationBoundaryError):
        service.observe(_request(), _context())
    name, fail_count_at_record, _ = metrics.first_failure_metric()
    # The terminal record (fail_observation) ran BEFORE the failure metric.
    assert fail_count_at_record == 1
    assert store.fail_calls[0]["reason_code"] == "not_found"


def test_provider_contract_invalid_records_fail_before_metrics() -> None:
    class ContractBreakingReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            return [{"location": "us-west-2", "desired": 99, "minimum": 0, "maximum": 1, "active": 0, "idle": 0}]

    service, store, metrics = _ordering_service(ContractBreakingReader())
    with pytest.raises(ObservationBoundaryError):
        service.observe(_request(), _context())
    _, fail_count_at_record, _ = metrics.first_failure_metric()
    assert fail_count_at_record == 1
    assert store.fail_calls[0]["reason_code"] == "provider_contract_invalid"


def test_provider_failure_records_fail_before_metrics() -> None:
    class BoomReader(FakeReader):
        def read_capacity(self, fleet_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("provider boom")

    service, store, metrics = _ordering_service(BoomReader())
    with pytest.raises(ObservationBoundaryError):
        service.observe(_request(), _context())
    _, fail_count_at_record, _ = metrics.first_failure_metric()
    assert fail_count_at_record == 1
    assert store.fail_calls[0]["reason_code"] == "provider_unavailable"


def test_commit_and_lease_deadlines_are_the_single_request_lease() -> None:
    # The begin call carries commit_not_after == lease_not_after == NOW + the
    # whole-request deadline (15 s with defaults), so the reclaim lease and the
    # commit fence share one request-scoped horizon rather than a far-off window.
    service, _, store, _ = _service()
    service.observe(_request(), _context())
    begin = store.begin_calls[0]
    expected = NOW + timedelta(seconds=15)
    assert begin["commit_not_after"] == expected
    assert begin["lease_not_after"] == expected


def test_cumulative_read_time_from_request_start_abandons_a_later_read() -> None:
    # The per-read remaining budget is measured from the single request-entry
    # clock, so time already spent earlier in the request counts against a later
    # read. A read whose start is past the whole-request deadline is abandoned
    # (never submitted to the provider) rather than run with a fresh full budget.
    # monotonic calls: request_start(0), begin-check(0), read-check(0),
    # read0 start(1)+elapsed(1), read1 start(2)+elapsed(2), read2 start(16) ->
    # remaining_total = 15 - 16 < 0 so read2 is abandoned before it runs.
    reader = FakeReader()
    service, _, store, metrics = _service(
        reader=reader,
        monotonic_times=[0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 16.0, 16.0],
    )
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert store.complete_calls == []
    assert "observation.timeout" in metrics.names()
    # The third read was abandoned before it was ever submitted to the provider:
    # the earlier read time is charged against it via the request-entry clock.
    assert reader.calls == ["utilization", "capacity"]


def test_budget_spent_after_the_reads_is_retryable_with_no_complete() -> None:
    # Time advances past the request deadline between the reads and the finalize
    # transaction. The service returns a retryable 503 and never issues the
    # complete, so the Lambda deadline cannot race the commit.
    # monotonic calls: request_start, begin-check, read-check, then per read a
    # call_start + call_elapsed (6), then the complete-check which is spent.
    service, _, store, metrics = _service(
        monotonic_times=[0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 13.0, 13.0],
    )
    with pytest.raises(ObservationBoundaryError) as exc:
        service.observe(_request(), _context())
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True
    assert store.complete_calls == []
    assert "observation.timeout" in metrics.names()


# --- Read cut short by the request budget vs its own per-read budget --------


def test_read_cut_short_by_the_request_budget_is_retryable_without_burning_the_token() -> None:
    # Standard library
    import threading

    # Local modules
    from operations.validation.e0_latency import LatencyBudget

    release = threading.Event()

    class HangingReader(FakeReader):
        def read_utilization(self, fleet_id: str) -> dict[str, int]:
            release.wait(timeout=5.0)
            return super().read_utilization(fleet_id)

    # The first read reaches the provider when only a sliver of the request
    # budget remains (less than its own per_read_s), so the request budget — not
    # the provider — cuts it short. That must be a retryable 503 with NO terminal
    # failure record, so the idempotency token is not burned.
    # Budget total = 1.0*3 + 1.0 + 0.3 = 4.3; margin = 0.3.
    # monotonic: request_start(0), begin-check(0), read-check(0),
    # read0 call_start(3.6) -> request_remaining = 4.3 - 0.3 - 3.6 = 0.4 < per_read(1.0).
    service_budget_reader = HangingReader()
    service, _, store, metrics = _service(
        reader=service_budget_reader,
        monotonic_times=[0.0, 0.0, 0.0, 3.6, 3.6, 3.6],
    )
    service._budget = LatencyBudget(per_read_s=1.0, persistence_s=1.0, cancellation_margin_s=0.3)
    try:
        with pytest.raises(ObservationBoundaryError) as exc:
            service.observe(_request(), _context())
    finally:
        release.set()
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is True  # the budget cut is retryable
    assert store.fail_calls == []  # the token is NOT burned terminally
    assert store.complete_calls == []
    assert "observation.timeout" in metrics.names()


def test_read_timeout_with_its_full_budget_is_a_terminal_provider_failure() -> None:
    # Standard library
    import threading

    # Local modules
    from operations.validation.e0_latency import LatencyBudget

    release = threading.Event()

    class HangingReader(FakeReader):
        def read_utilization(self, fleet_id: str) -> dict[str, int]:
            release.wait(timeout=5.0)
            return super().read_utilization(fleet_id)

    # The read is given its FULL per-read budget and still times out: this is a
    # provider timeout, recorded as a terminal, non-retryable failure (the token
    # cannot make progress), distinct from a request-budget cut.
    service, _, store, _ = _service(reader=HangingReader())
    service._budget = LatencyBudget(per_read_s=0.2, persistence_s=0.2, cancellation_margin_s=0.2)
    try:
        with pytest.raises(ObservationBoundaryError) as exc:
            service.observe(_request(), _context())
    finally:
        release.set()
    assert exc.value.error_code is ObservationErrorCode.PROVIDER_UNAVAILABLE
    assert exc.value.retryable is False  # a provider timeout burns the token
    assert len(store.fail_calls) == 1
    assert store.fail_calls[0]["reason_code"] == "provider_unavailable"
