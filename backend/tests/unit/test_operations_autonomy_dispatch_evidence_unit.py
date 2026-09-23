"""Mandatory dispatch-audit + final pre-write re-verify reproductions (#439).

Finding 2 (dispatch evidence):
* ``dispatch_requested`` MUST be durably recorded before StartExecution; if that
  audit write fails, the runtime refuses and releases the reservation rather than
  starting an execution with no request evidence.
* ``dispatched`` MUST be durably recorded after a confirmed/idempotent start; if
  that audit write fails, the runtime refuses (the execution is not claimed as a
  proven dispatch).
* The executor v2 reload requires the exact ``dispatched`` audit record to exist
  before it verifies or writes; without it the reload fails closed.

Finding 3 (final expiry re-check):
* The immediate pre-write hook reloads the bundle and re-runs the
  AutonomyExecutionVerifier at the CURRENT clock (after E4's second check, before
  the write) and compares the action id, so a clock that crosses the
  decision/window deadline DURING Describe fails closed with no write.

No AWS calls, no infrastructure, no provider writes.
"""

from __future__ import annotations

# Standard library
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


class _FakeObservationStore:
    def __init__(self, observation: dict[str, Any] | None, *, state: str = "succeeded") -> None:
        self._observation = observation
        self._state = state

    def load_status(self, *, operation_id: str, workspace_id: str) -> Any:
        # Local modules
        from operations.observation import ObservationStatus, ObservationStatusView

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

    def load(self, *, policy_id: str, policy_version: str, policy_hash: str) -> dict[str, Any]:
        return dict(self._policy)


class _FakeWindowLoader:
    def __init__(self, window_state: dict[str, Any]) -> None:
        self._window_state = window_state

    def load(self, state_id: str) -> dict[str, Any]:
        return dict(self._window_state)


class _RecordingReservationStore:
    def __init__(self, window_state: dict[str, Any]) -> None:
        self._window_state = window_state
        self.settled: list[dict[str, Any]] = []

    def current(self, state_id: str) -> dict[str, Any]:
        return dict(self._window_state)

    def reserve(self, request: Any) -> Any:
        # Local modules
        from operations.autonomy_runtime.store import ReservationOutcome, ReservationResult

        return ReservationResult(ReservationOutcome.RESERVED, window_state=dict(self._window_state))

    def settle(self, *, operation_id: str, logical_action_id: str, terminal: str, generation: int = 1) -> Any:
        # Local modules
        from operations.autonomy_runtime.store import ReservationOutcome, ReservationResult

        self.settled.append({"operation_id": operation_id, "terminal": terminal})
        return ReservationResult(ReservationOutcome.RESERVED, window_state=dict(self._window_state))


class _AllowGate:
    def require_pre_dispatch(self) -> None:
        return None


class _BundleStore:
    """Records bundles/audits; a phase in ``fail_phases`` raises on that audit."""

    def __init__(self, *, fail_phases: frozenset[str] = frozenset()) -> None:
        self.bundles: list[dict[str, Any]] = []
        self.audits: list[str] = []
        self._fail_phases = fail_phases

    def persist_bundle(self, **kwargs: Any) -> None:
        self.bundles.append(kwargs)

    def record_dispatch_requested(self, *, operation_id: str, execution_name: str) -> None:
        if "dispatch_requested" in self._fail_phases:
            raise RuntimeError("audit store unavailable")
        self.audits.append("dispatch_requested")

    def record_dispatched(self, *, operation_id: str, execution_name: str) -> None:
        if "dispatched" in self._fail_phases:
            raise RuntimeError("audit store unavailable")
        self.audits.append("dispatched")


def _runtime(*, bundle_store: _BundleStore, reservation_store: _RecordingReservationStore, start: Any) -> Any:
    # Local modules
    from operations.autonomy_runtime.evaluator_entry import AutonomyModuleRuntime, resolve_autonomy_evaluator_settings
    from operations.autonomy_runtime.handler import AutonomyRuntimeHandler
    from operations.autonomy_runtime.service import AutonomyRuntimeService

    settings = resolve_autonomy_evaluator_settings(_base_env())
    handler = AutonomyRuntimeHandler(
        service=AutonomyRuntimeService(),
        reservation_store=reservation_store,
        bundle_store=bundle_store,
        gate=_AllowGate(),
        start_execution=start,
    )
    return AutonomyModuleRuntime(
        settings=settings,
        handler=handler,
        observation_store=_FakeObservationStore(_observation()),
        policy_loader=_FakePolicyLoader(_policy()),
        window_loader=_FakeWindowLoader(_window_state()),
        clock=lambda: _EVALUATED_AT,
    )


_EVENT = {"observation_operation_id": "op_obs1", "desired": 1, "minimum": 0, "maximum": 1}


# ===========================================================================
# Finding 2 — mandatory dispatch audit
# ===========================================================================


@pytest.mark.unit
def test_requested_audit_failure_refuses_and_releases_without_start() -> None:
    bundle_store = _BundleStore(fail_phases=frozenset({"dispatch_requested"}))
    reservation_store = _RecordingReservationStore(_window_state())
    starts: list[Any] = []
    runtime = _runtime(
        bundle_store=bundle_store,
        reservation_store=reservation_store,
        start=lambda payload, *, name: starts.append(payload),
    )
    result = runtime.evaluate(_EVENT)
    assert result["outcome"] == "refused"
    assert result["reason"] == "dispatch_requested_audit_failed"
    # No StartExecution was attempted without recorded request evidence. The
    # request audit is recorded BEFORE the handler reserves, so a request-audit
    # failure refuses before anything is reserved — no dangling in-flight slot.
    assert starts == []


@pytest.mark.unit
def test_dispatched_audit_failure_refuses_the_dispatch() -> None:
    bundle_store = _BundleStore(fail_phases=frozenset({"dispatched"}))
    reservation_store = _RecordingReservationStore(_window_state())
    starts: list[Any] = []
    runtime = _runtime(
        bundle_store=bundle_store,
        reservation_store=reservation_store,
        start=lambda payload, *, name: starts.append(payload),
    )
    result = runtime.evaluate(_EVENT)
    # A start happened but the confirming audit failed: the runtime must NOT
    # report a proven dispatch, and it releases the in-flight reservation so the
    # slot is not wedged.
    assert result["outcome"] == "refused"
    assert result["reason"] == "dispatched_audit_failed"
    assert reservation_store.settled and reservation_store.settled[-1]["terminal"] == "failed"


@pytest.mark.unit
def test_successful_dispatch_records_both_audits_in_order() -> None:
    bundle_store = _BundleStore()
    reservation_store = _RecordingReservationStore(_window_state())
    runtime = _runtime(
        bundle_store=bundle_store,
        reservation_store=reservation_store,
        start=lambda payload, *, name: None,
    )
    result = runtime.evaluate(_EVENT)
    assert result["outcome"] == "dispatched"
    assert bundle_store.audits == ["dispatch_requested", "dispatched"]


@pytest.mark.unit
def test_executor_reload_requires_dispatched_audit_before_verify_or_write() -> None:
    """The v2 executor reload fails closed unless the ``dispatched`` audit exists."""
    # Local modules
    from operations.execute.executor_entry import AutonomyExecutorRuntime, execute_reloaded
    from operations.execute.executor_service import ExecutionInvocation, ExecutorServiceError

    op_id = "op_cccccccccccccccccccccccccc"

    class _BundleNoDispatched:
        def load_bundle(self, operation_id: str) -> dict[str, Any]:
            return {"operation": {"operation_id": op_id, "prepared_hash": "sha256:" + "a" * 64}}

        def load_dispatch_audit(self, *, operation_id: str, phase: str) -> dict[str, Any] | None:
            # No dispatched evidence recorded.
            return None

    class _Verifier:
        def verify(self, **kwargs: Any) -> Any:
            raise AssertionError("verify must not run without dispatched evidence")

    class _NoopReservation:
        def require(self, **kwargs: Any) -> None:
            return None

        def settle(self, **kwargs: Any) -> Any:
            return None

    runtime = AutonomyExecutorRuntime(
        service=object(),
        reload_store=_NeverV1ReloadStore(),
        metrics=None,
        bundle_store=_BundleNoDispatched(),
        autonomy_verifier=_Verifier(),
        reservation=_NoopReservation(),
    )
    invocation = ExecutionInvocation(operation_id=op_id)
    with pytest.raises(ExecutorServiceError):
        execute_reloaded(runtime, invocation, lease_holder="executor.test")


class _NeverV1ReloadStore:
    def load_for_execution(self, operation_id: str) -> None:
        return None


# ===========================================================================
# Finding 3 — final pre-write re-verify at the current clock
# ===========================================================================


@pytest.mark.unit
def test_pre_write_reverify_fails_closed_when_clock_crosses_deadline_during_describe() -> None:
    """The pre-write hook re-runs the verifier at the CURRENT clock and fails
    closed when the decision/window deadline was crossed during Describe."""
    # Local modules
    from operations.autonomy_execution_verifier import (
        AutonomyExecutionVerificationError,
        AutonomyExecutionVerificationReason,
    )
    from operations.execute.executor_entry import build_pre_write_reverify_hook

    op_id = "op_dddddddddddddddddddddddddd"
    action_id = "act_" + "d" * 64

    class _Plan:
        intent = {"operation_id": op_id}
        logical_action_id = action_id

    class _BundleStore:
        def load_bundle(self, operation_id: str) -> dict[str, Any]:
            return {
                "policy": {},
                "observation": {},
                "decision": {},
                "operation": {"operation_id": op_id},
                "window_state": {},
            }

    class _ExpiringVerifier:
        """Passes at decision time but, on the pre-write re-run (current clock),
        raises because the window/decision has now expired."""

        def __init__(self) -> None:
            self.calls = 0

        def verify(self, *, operation_id: str, evidence: Any) -> Any:
            self.calls += 1
            raise AutonomyExecutionVerificationError(
                AutonomyExecutionVerificationReason.WINDOW_STATE_EXPIRED,
                "rolling-window state is no longer fresh for execution",
            )

    verifier = _ExpiringVerifier()
    hook = build_pre_write_reverify_hook(
        bundle_store=_BundleStore(),
        verifier=verifier,
        switch_port=_AllowSwitch(),
        reservation_port=_AllowReservation(),
    )
    with pytest.raises(Exception):
        hook(_Plan())
    assert verifier.calls == 1


@pytest.mark.unit
def test_pre_write_reverify_requires_action_id_match() -> None:
    """If the re-verified plan's action id differs from the in-flight plan's,
    the hook fails closed (no write)."""
    # Local modules
    from operations.execute.executor_entry import build_pre_write_reverify_hook
    from operations.execution_verifier import VerifiedExecutionPlan

    op_id = "op_eeeeeeeeeeeeeeeeeeeeeeeeee"
    plan_action = "act_" + "e" * 64
    other_action = "act_" + "f" * 64

    class _Plan:
        intent = {"operation_id": op_id}
        logical_action_id = plan_action

    class _BundleStore:
        def load_bundle(self, operation_id: str) -> dict[str, Any]:
            return {
                "policy": {},
                "observation": {},
                "decision": {},
                "operation": {"operation_id": op_id},
                "window_state": {},
            }

    class _MismatchVerifier:
        def verify(self, *, operation_id: str, evidence: Any) -> Any:
            return VerifiedExecutionPlan(
                intent={"operation_id": op_id, "parameters": {}, "expected_current_capacity": {}},
                logical_action_id=other_action,
                fleet_arn="arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-x",
                fleet_id="fleet-x",
                location="us-west-2",
                max_writes=1,
            )

    hook = build_pre_write_reverify_hook(
        bundle_store=_BundleStore(),
        verifier=_MismatchVerifier(),
        switch_port=_AllowSwitch(),
        reservation_port=_AllowReservation(),
    )
    with pytest.raises(Exception):
        hook(_Plan())


@pytest.mark.unit
def test_pre_write_reverify_passes_when_still_fresh_and_action_matches() -> None:
    # Local modules
    from operations.execute.executor_entry import build_pre_write_reverify_hook
    from operations.execution_verifier import VerifiedExecutionPlan

    op_id = "op_ffffffffffffffffffffffffff"
    action_id = "act_" + "1" * 64

    class _Plan:
        intent = {"operation_id": op_id}
        logical_action_id = action_id

    class _BundleStore:
        def load_bundle(self, operation_id: str) -> dict[str, Any]:
            return {
                "policy": {},
                "observation": {},
                "decision": {},
                "operation": {"operation_id": op_id},
                "window_state": {},
            }

    class _FreshVerifier:
        def verify(self, *, operation_id: str, evidence: Any) -> Any:
            return VerifiedExecutionPlan(
                intent={"operation_id": op_id, "parameters": {}, "expected_current_capacity": {}},
                logical_action_id=action_id,
                fleet_arn="arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-x",
                fleet_id="fleet-x",
                location="us-west-2",
                max_writes=1,
            )

    hook = build_pre_write_reverify_hook(
        bundle_store=_BundleStore(),
        verifier=_FreshVerifier(),
        switch_port=_AllowSwitch(),
        reservation_port=_AllowReservation(),
    )
    # No exception: still fresh and the action id matches.
    hook(_Plan())


class _AllowSwitch:
    def require_autonomy(self) -> None:
        return None


class _AllowReservation:
    def require_reservation(self, *, operation_id: str, logical_action_id: str, generation: int | None = None) -> None:
        return None


# ===========================================================================
# Finding 3 — end-to-end: clock advances during Describe (real verifier + core)
# ===========================================================================

_E2E_FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
_E2E_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/" + _E2E_FLEET


def _e2e_bundle() -> dict[str, Any]:
    return {
        "policy": load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json"),
        "observation": load_json(FIXTURES / "gamelift-autonomy-observation.valid.json"),
        "decision": load_json(FIXTURES / "gamelift-capacity-autonomous-decision.valid.json"),
        "operation": load_json(FIXTURES / "gamelift-capacity-autonomous-operation.valid.json"),
        "window_state": load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json"),
    }


class _AdvancingClock:
    """A mutable clock a Describe can advance past the window deadline."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class _CrossingAdapter:
    """Describe advances the shared clock PAST the window deadline before write."""

    def __init__(self, clock: _AdvancingClock, *, jump_seconds: int) -> None:
        self._clock = clock
        self._jump = jump_seconds
        self.update_calls: list[dict[str, Any]] = []
        self._described = False

    def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]:
        # The pre-write Describe takes long enough that the decision/window
        # deadline is crossed by the time the write would be issued.
        if not self._described:
            self._described = True
            self._clock.advance(self._jump)
        op = load_json(FIXTURES / "gamelift-capacity-autonomous-operation.valid.json")
        return dict(op["current_state"]["capacity"])

    def update_capacity(self, **kwargs: Any) -> None:
        self.update_calls.append(kwargs)


@pytest.mark.unit
def test_end_to_end_clock_crossing_during_describe_refuses_write() -> None:
    """A window/decision fresh at plan time but crossed DURING the pre-write
    Describe is caught by the re-verifying hook: no UpdateFleetCapacity."""
    # Local modules
    from operations.autonomy_execution_verifier import (
        AutonomyExecutionAuthorityContext,
        AutonomyExecutionVerifier,
    )
    from operations.autonomy_playbook_definition import autonomy_playbook_hash
    from operations.execute.executor_entry import build_pre_write_reverify_hook
    from operations.execute.executor_service import ExecutionInvocation, ExecutorService
    from operations.execution_verifier import VerifiedExecutionPlan
    from operations.playbook_definition import EXECUTOR_BINDING_VERSION, EXECUTOR_ID

    bundle = _e2e_bundle()
    policy = bundle["policy"]
    operation = bundle["operation"]
    window = bundle["window_state"]

    # Fresh instant: equal to the decision's evaluated_at, strictly before both
    # the decision expiry and the window ``expires_at_epoch_seconds``.
    fresh = datetime.fromtimestamp(window["as_of_epoch_seconds"], tz=timezone.utc)
    clock = _AdvancingClock(fresh)

    context = AutonomyExecutionAuthorityContext(
        deployment_mode="operate",
        capability_maximum="operate",
        tenant_id=policy["tenant_id"],
        workspace_id=policy["workspace_id"],
        expected_policy_id=policy["policy_id"],
        expected_policy_version=policy["policy_version"],
        expected_policy_hash=policy["policy_hash"],
        expected_playbook_hash=autonomy_playbook_hash(),
        expected_executor_id=EXECUTOR_ID,
        expected_executor_binding_version=EXECUTOR_BINDING_VERSION,
        enrolled_fleet_id=policy["target"]["fleet_id"],
        enrolled_fleet_arn=_E2E_ARN,
        enrolled_location=policy["target"]["location"],
    )
    verifier = AutonomyExecutionVerifier(
        context=context,
        autonomy_switch=_AllowSwitch(),
        reservation=_AllowReservation(),
        clock=clock.now,
    )

    # The plan produced by the FIRST (fresh) verify — proving the write was
    # legitimate at plan time.
    plan_at_fresh = verifier.verify(operation_id=operation["operation_id"], evidence=_bundle_evidence(bundle))
    assert isinstance(plan_at_fresh, VerifiedExecutionPlan)

    class _BundleStore:
        def load_bundle(self, operation_id: str) -> dict[str, Any]:
            return _e2e_bundle()

    hook = build_pre_write_reverify_hook(
        bundle_store=_BundleStore(),
        verifier=verifier,
        switch_port=_AllowSwitch(),
        reservation_port=_AllowReservation(),
    )

    # Jump past the window deadline (60s horizon) during the pre-write Describe.
    adapter = _CrossingAdapter(clock, jump_seconds=120)
    service = ExecutorService(
        verifier=object(),
        adapter=adapter,
        store=_E2EStore(),
        clock=clock.now,
        pre_write_hook=hook,
    )
    result = service.execute_verified(
        ExecutionInvocation(operation_id=operation["operation_id"]),
        plan=plan_at_fresh,
        lease_holder="executor.e2e",
    )
    # No provider write was issued; the re-verify caught the crossed deadline.
    assert adapter.update_calls == []
    assert result["outcome"] == "FAILED"
    assert result["provider_write_issued"] is False


def _bundle_evidence(bundle: dict[str, Any]) -> Any:
    # Local modules
    from operations.autonomy_execution_verifier import AutonomyExecutionEvidence

    return AutonomyExecutionEvidence(
        policy=dict(bundle["policy"]),
        observation=dict(bundle["observation"]),
        decision=dict(bundle["decision"]),
        operation=dict(bundle["operation"]),
        window_state=dict(bundle["window_state"]),
    )


class _E2EStore:
    def acquire_execution_lease(self, **kwargs: Any) -> Any:
        # Local modules
        from operations.execute.execution_store import LeaseAcquisition

        return LeaseAcquisition(generation=1, recorded_result=None)

    def record_execution_result(self, **kwargs: Any) -> Any:
        # Local modules
        from operations.execute.execution_store import ExecutionCommitOutcome

        return ExecutionCommitOutcome.RECORDED
