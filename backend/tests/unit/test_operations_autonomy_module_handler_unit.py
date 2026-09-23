"""Module-level Lambda handler tests for the E5 evaluator entry (issue #439).

These lock the *final* source-level wiring the earlier commits deferred: a real,
deployable module-level ``handler(event, context)`` in
``operations.autonomy_runtime.evaluator_entry`` that turns a **closed** event —
carrying only ``observation_operation_id`` and the requested desired/min/max —
into one durable, identifier-only dispatch, using only trusted, server-owned
loaders.

The tests prove:

* the module handler reaches only trusted loaders (DynamoDB observation store,
  the code-owned policy loader, the durable window-state loader) and never a
  GameLift client or a Lambda-invoke client;
* the event contract is closed — identity, policy, limits, authority, principal,
  and correlation are NEVER read from the event, and unknown event fields are
  refused;
* identity/policy/authority/principal/correlation are owned server-side (derived
  from settings and the loaded policy, not the event);
* a duplicate Step Functions start (``ExecutionAlreadyExists``) is a successful
  idempotent replay, while an ambiguous start failure refuses without claiming a
  provider write;
* the dispatch audit is truthful — a ``dispatch_requested`` record is written
  before StartExecution and a ``dispatched`` record only after a confirmed (or
  idempotent) start;
* a partial/misconfigured deployment refuses to construct a handler at all.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts import load_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"

_EVALUATED_AT = datetime(2026, 9, 21, 19, 12, 52, tzinfo=timezone.utc)


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


def _observation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-autonomy-observation.valid.json")


def _window_state() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")


def _base_env() -> dict[str, str]:
    policy = _policy()
    return {
        "GBAW_OPERATIONS_MODE": "operate",
        "GBAW_OPERATIONS_AUTONOMY_ENABLED": "true",
        "GBAW_OPERATIONS_TABLE_NAME": "ops-06",
        "GBAW_OPERATIONS_METRIC_NAMESPACE": "GBAW/Operations",
        "GBAW_OPERATIONS_TENANT_ID": "tenant.default",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace.default",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "aud.default",
        "GBAW_OPERATIONS_CAPABILITY_MAXIMUM": "operate",
        "GBAW_OPERATIONS_STATE_MACHINE_ARN": "arn:aws:states:us-west-2:123456789012:stateMachine:execute",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": policy["target"]["fleet_id"],
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": "arn:aws:gamelift:us-west-2:123456789012:fleet/"
        + policy["target"]["fleet_id"],
        "GBAW_OPERATIONS_ENROLLED_LOCATION": policy["target"]["location"],
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "gbaw-ops",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_APPLICATION": "autonomy-app",
        "GBAW_OPERATIONS_AUTONOMY_APPCONFIG_ENVIRONMENT": "prod",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE": "kill-switch",
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN": (
            "arn:aws:states:us-west-2:123456789012:stateMachine:autonomy-execute"
        ),
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE": "autonomy-switch",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID": policy["policy_id"],
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION": policy["policy_version"],
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH": policy["policy_hash"],
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID": _window_state()["state_id"],
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT": "subject.autonomy-agent",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT": "client.autonomy-runtime",
    }


# -- Fakes for the trusted loaders and the durable ports ---------------------


class _FakeObservationStore:
    """Returns a SUCCEEDED status carrying the canonical E1 observation."""

    def __init__(self, observation: dict[str, Any] | None, *, state: str = "succeeded") -> None:
        self._observation = observation
        self._state = state
        self.calls: list[dict[str, str]] = []

    def load_status(self, *, operation_id: str, workspace_id: str) -> Any:
        # Local modules
        from operations.observation import ObservationStatus, ObservationStatusView

        self.calls.append({"operation_id": operation_id, "workspace_id": workspace_id})
        if self._observation is None:
            return None
        return ObservationStatus(
            operation_id=operation_id,
            workspace_id=workspace_id,
            state=ObservationStatusView(self._state),
            observation=self._observation if self._state == "succeeded" else None,
            observation_hash="sha256:" + "a" * 64,
        )


class _FakePolicyLoader:
    def __init__(self, policy: dict[str, Any]) -> None:
        self._policy = policy
        self.calls: list[dict[str, str]] = []

    def load(self, *, policy_id: str, policy_version: str, policy_hash: str) -> dict[str, Any]:
        self.calls.append({"policy_id": policy_id, "policy_version": policy_version, "policy_hash": policy_hash})
        if (
            policy_id != self._policy["policy_id"]
            or policy_version != self._policy["policy_version"]
            or policy_hash != self._policy["policy_hash"]
        ):
            raise KeyError("policy not found for the configured id/version/hash")
        return dict(self._policy)


class _FakeWindowLoader:
    def __init__(self, window_state: dict[str, Any]) -> None:
        self._window_state = window_state
        self.calls: list[str] = []

    def load(self, state_id: str) -> dict[str, Any]:
        self.calls.append(state_id)
        return dict(self._window_state)


class _RecordingBundleStore:
    def __init__(self) -> None:
        self.bundles: list[dict[str, Any]] = []
        self.audits: list[dict[str, Any]] = []

    def persist_bundle(self, **kwargs: Any) -> None:
        self.bundles.append(kwargs)

    def record_dispatch_requested(self, *, operation_id: str, execution_name: str) -> None:
        self.audits.append({"phase": "dispatch_requested", "operation_id": operation_id, "name": execution_name})

    def record_dispatched(self, *, operation_id: str, execution_name: str) -> None:
        self.audits.append({"phase": "dispatched", "operation_id": operation_id, "name": execution_name})


class _RecordingReservationStore:
    def __init__(self, window_state: dict[str, Any]) -> None:
        self._window_state = window_state
        self.reserved: list[Any] = []
        self.settled: list[dict[str, Any]] = []

    def current(self, state_id: str) -> dict[str, Any]:
        return dict(self._window_state)

    def reserve(self, request: Any) -> Any:
        # Local modules
        from operations.autonomy_runtime.store import ReservationOutcome, ReservationResult

        self.reserved.append(request)
        return ReservationResult(ReservationOutcome.RESERVED, window_state=dict(self._window_state))

    def settle(self, *, operation_id: str, logical_action_id: str, terminal: str, generation: int = 1) -> None:
        self.settled.append({"operation_id": operation_id, "terminal": terminal})


class _AllowGate:
    def require_pre_dispatch(self) -> None:
        return None


def _build_handler_with_fakes(
    *,
    observation_store: _FakeObservationStore,
    policy_loader: _FakePolicyLoader,
    window_loader: _FakeWindowLoader,
    bundle_store: _RecordingBundleStore,
    reservation_store: _RecordingReservationStore,
    start_execution: Any,
    gate: Any | None = None,
) -> Any:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import AutonomyModuleRuntime, resolve_autonomy_evaluator_settings
    from operations.autonomy_runtime.handler import AutonomyRuntimeHandler
    from operations.autonomy_runtime.service import AutonomyRuntimeService

    settings = resolve_autonomy_evaluator_settings(_base_env())
    handler = AutonomyRuntimeHandler(
        service=AutonomyRuntimeService(),
        reservation_store=reservation_store,
        bundle_store=bundle_store,
        gate=gate or _AllowGate(),
        start_execution=start_execution,
    )
    return AutonomyModuleRuntime(
        settings=settings,
        handler=handler,
        observation_store=observation_store,
        policy_loader=policy_loader,
        window_loader=window_loader,
        clock=lambda: _EVALUATED_AT,
    )


# -- Tests -------------------------------------------------------------------


@pytest.mark.unit
def test_module_exposes_module_level_lambda_handler() -> None:
    """A real, callable module-level ``handler`` exists (deployable entrypoint)."""
    # Local modules
    from operations.autonomy_runtime import evaluator_entry

    assert callable(getattr(evaluator_entry, "handler", None))


@pytest.mark.unit
def test_closed_event_reaches_only_trusted_loaders_and_dispatches() -> None:
    obs_store = _FakeObservationStore(_observation())
    policy_loader = _FakePolicyLoader(_policy())
    window_loader = _FakeWindowLoader(_window_state())
    bundle_store = _RecordingBundleStore()
    reservation_store = _RecordingReservationStore(_window_state())
    starts: list[dict[str, Any]] = []

    def _start(payload: dict[str, str], *, name: str) -> None:
        starts.append({"payload": dict(payload), "name": name})

    runtime = _build_handler_with_fakes(
        observation_store=obs_store,
        policy_loader=policy_loader,
        window_loader=window_loader,
        bundle_store=bundle_store,
        reservation_store=reservation_store,
        start_execution=_start,
    )

    result = runtime.evaluate({"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1})

    assert result["outcome"] == "dispatched"
    # The trusted observation loader was consulted with the server-owned workspace.
    assert obs_store.calls == [{"operation_id": "op_obs1", "workspace_id": "workspace.default"}]
    # The policy loader was consulted with the server-configured id/version/hash.
    assert policy_loader.calls == [
        {
            "policy_id": _policy()["policy_id"],
            "policy_version": _policy()["policy_version"],
            "policy_hash": _policy()["policy_hash"],
        }
    ]
    # The window state was loaded by the server-configured state id.
    assert window_loader.calls == [_window_state()["state_id"]]
    # Exactly one identifier-only dispatch happened.
    assert len(starts) == 1
    assert set(starts[0]["payload"]) == {"operation_id"}


@pytest.mark.unit
def test_event_contract_is_closed_and_rejects_unknown_fields() -> None:
    runtime = _build_handler_with_fakes(
        observation_store=_FakeObservationStore(_observation()),
        policy_loader=_FakePolicyLoader(_policy()),
        window_loader=_FakeWindowLoader(_window_state()),
        bundle_store=_RecordingBundleStore(),
        reservation_store=_RecordingReservationStore(_window_state()),
        start_execution=lambda payload, *, name: None,
    )

    # Identity/policy/limits smuggled through the event are refused outright.
    for smuggled in (
        {"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1, "workspace_id": "evil"},
        {"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1, "policy_hash": "sha256:x"},
        {"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1, "authority_inputs": {}},
    ):
        with pytest.raises(ValueError):
            runtime.evaluate(smuggled)


@pytest.mark.unit
def test_missing_requested_field_refuses() -> None:
    runtime = _build_handler_with_fakes(
        observation_store=_FakeObservationStore(_observation()),
        policy_loader=_FakePolicyLoader(_policy()),
        window_loader=_FakeWindowLoader(_window_state()),
        bundle_store=_RecordingBundleStore(),
        reservation_store=_RecordingReservationStore(_window_state()),
        start_execution=lambda payload, *, name: None,
    )
    with pytest.raises(ValueError):
        runtime.evaluate({"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0})


@pytest.mark.unit
def test_non_succeeded_observation_refuses_without_dispatch() -> None:
    obs_store = _FakeObservationStore(_observation(), state="observing")
    starts: list[Any] = []
    runtime = _build_handler_with_fakes(
        observation_store=obs_store,
        policy_loader=_FakePolicyLoader(_policy()),
        window_loader=_FakeWindowLoader(_window_state()),
        bundle_store=_RecordingBundleStore(),
        reservation_store=_RecordingReservationStore(_window_state()),
        start_execution=lambda payload, *, name: starts.append(payload),
    )
    result = runtime.evaluate({"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1})
    assert result["outcome"] == "refused"
    assert starts == []


@pytest.mark.unit
def test_server_owns_identity_policy_authority_and_principal() -> None:
    """The event never contributes identity/policy/authority/principal/correlation."""
    obs_store = _FakeObservationStore(_observation())
    bundle_store = _RecordingBundleStore()
    runtime = _build_handler_with_fakes(
        observation_store=obs_store,
        policy_loader=_FakePolicyLoader(_policy()),
        window_loader=_FakeWindowLoader(_window_state()),
        bundle_store=bundle_store,
        reservation_store=_RecordingReservationStore(_window_state()),
        start_execution=lambda payload, *, name: None,
    )
    runtime.evaluate({"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1})

    # The persisted bundle's decision carries the server-owned principal + authority.
    assert bundle_store.bundles, "a bundle must be persisted"
    decision = bundle_store.bundles[0]["decision"]
    assert decision["automation_principal"] == _policy()["automation_principal"]
    assert set(decision["authority_inputs"]) == {
        "deployment_mode",
        "tenant_policy",
        "workspace_policy",
        "principal_authority",
        "capability_maximum",
        "operation_risk_policy",
    }
    # Correlation is server-derived and deterministic from the operation id.
    assert decision["correlation"]["correlation_id"]


@pytest.mark.unit
def test_duplicate_start_is_idempotent_success() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import StepFunctionsStartExecution

    class _AlreadyExists(Exception):
        pass

    class _Sfn:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def start_execution(self, **kwargs: Any) -> dict[str, str]:
            self.calls.append(kwargs)
            exc = _AlreadyExists("ExecutionAlreadyExists")
            exc.response = {"Error": {"Code": "ExecutionAlreadyExists"}}  # type: ignore[attr-defined]
            raise exc

    sfn = _Sfn()
    start = StepFunctionsStartExecution(
        client=sfn, state_machine_arn="arn:aws:states:us-west-2:123456789012:stateMachine:autonomy-execute"
    )
    # A duplicate start with a deterministic name is a successful idempotent replay.
    start({"operation_id": "op_" + "a" * 26}, name="op_" + "a" * 26)
    assert len(sfn.calls) == 1
    assert sfn.calls[0]["name"] == "op_" + "a" * 26


@pytest.mark.unit
def test_ambiguous_start_failure_raises_and_does_not_confirm_write() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import StepFunctionsStartExecution

    class _Timeout(Exception):
        pass

    class _Sfn:
        def start_execution(self, **kwargs: Any) -> dict[str, str]:
            raise _Timeout("connection reset")

    start = StepFunctionsStartExecution(
        client=_Sfn(), state_machine_arn="arn:aws:states:us-west-2:123456789012:stateMachine:autonomy-execute"
    )
    with pytest.raises(_Timeout):
        start({"operation_id": "op_" + "a" * 26}, name="op_" + "a" * 26)


@pytest.mark.unit
def test_dispatch_audit_is_truthful_requested_before_dispatched() -> None:
    bundle_store = _RecordingBundleStore()
    runtime = _build_handler_with_fakes(
        observation_store=_FakeObservationStore(_observation()),
        policy_loader=_FakePolicyLoader(_policy()),
        window_loader=_FakeWindowLoader(_window_state()),
        bundle_store=bundle_store,
        reservation_store=_RecordingReservationStore(_window_state()),
        start_execution=lambda payload, *, name: None,
    )
    runtime.evaluate({"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1})
    phases = [a["phase"] for a in bundle_store.audits]
    assert phases == ["dispatch_requested", "dispatched"]


@pytest.mark.unit
def test_ambiguous_start_records_requested_but_not_dispatched() -> None:
    bundle_store = _RecordingBundleStore()

    def _start(payload: dict[str, str], *, name: str) -> None:
        raise RuntimeError("start ambiguous")

    runtime = _build_handler_with_fakes(
        observation_store=_FakeObservationStore(_observation()),
        policy_loader=_FakePolicyLoader(_policy()),
        window_loader=_FakeWindowLoader(_window_state()),
        bundle_store=bundle_store,
        reservation_store=_RecordingReservationStore(_window_state()),
        start_execution=_start,
    )
    result = runtime.evaluate({"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1})
    assert result["outcome"] == "refused"
    phases = [a["phase"] for a in bundle_store.audits]
    # A fail-closed record of the request is retained; no fabricated "dispatched".
    assert "dispatch_requested" in phases
    assert "dispatched" not in phases


@pytest.mark.unit
def test_partial_configuration_refuses_to_build_runtime() -> None:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings

    env = _base_env()
    del env["GBAW_OPERATIONS_AUTONOMY_STATE_ID"]
    with pytest.raises(ValueError):
        resolve_autonomy_evaluator_settings(env)
