"""Execute-time window-freshness gate for the E5 autonomy verifier (#439).

The #438 binding validator re-runs the deterministic evaluator at the decision's
OWN ``evaluated_at`` — not at the current execute time. So a rolling-window state
that was fresh when the decision was made but has since passed its
``expires_at_epoch_seconds`` is NOT caught by the binding check. The runtime
blockers review flagged this as a missing execute-time window freshness check.

This suite locks an explicit, discriminating gate in
:class:`AutonomyExecutionVerifier`: immediately after the decision-freshness
check and BEFORE the separate autonomy switch and the atomic reservation, the
verifier compares the current clock to the window state's
``expires_at_epoch_seconds`` and fails closed with ``WINDOW_STATE_EXPIRED`` when
the window has expired — even when the decision itself is still within its TTL.

To keep the #438 hash binding intact, the bundle is produced by the real
:class:`AutonomyRuntimeService` over a window whose ``expires_at_epoch_seconds``
is set earlier than the decision's TTL deadline (but still fresh at the decision
instant). Execution then happens between the two: the decision is still valid,
but the window has expired.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_execution_verifier import (
    AutonomyExecutionAuthorityContext,
    AutonomyExecutionEvidence,
    AutonomyExecutionVerificationError,
    AutonomyExecutionVerificationReason,
    AutonomyExecutionVerifier,
)
from operations.autonomy_playbook_definition import autonomy_playbook_hash
from operations.autonomy_runtime import AutonomyRuntimeInputs, AutonomyRuntimeService
from operations.contracts import load_json
from operations.contracts.autonomy import autonomy_window_state_hash
from operations.playbook_definition import EXECUTOR_BINDING_VERSION, EXECUTOR_ID

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"
_FLEET_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"

_EVALUATED_AT = datetime(2026, 9, 21, 19, 12, 52, tzinfo=timezone.utc)
_EVALUATED_EPOCH = int(_EVALUATED_AT.timestamp())
# Window expires 40s after the decision instant — still fresh when the decision
# is made, but earlier than the decision's own 60s TTL deadline.
_WINDOW_EXPIRES_EPOCH = _EVALUATED_EPOCH + 40
# Execute 50s after the decision instant: past the window, before the decision.
_EXECUTE_CLOCK = datetime.fromtimestamp(_EVALUATED_EPOCH + 50, tz=timezone.utc)

_AUTHORITY = {
    "deployment_mode": "operate",
    "tenant_policy": "operate",
    "workspace_policy": "operate",
    "principal_authority": "operate",
    "capability_maximum": "operate",
    "operation_risk_policy": "operate",
}
_PRINCIPAL = {
    "source_type": "automation",
    "subject_id": "subject.autonomy-agent",
    "client_id": "client.autonomy-runtime",
}
_CORRELATION = {"correlation_id": "corr.autonomy-1", "request_id": "request.autonomy-1"}


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


def _observation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-autonomy-observation.valid.json")


def _window_expiring_early() -> dict[str, Any]:
    window = load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")
    window["expires_at_epoch_seconds"] = _WINDOW_EXPIRES_EPOCH
    window["state_hash"] = autonomy_window_state_hash(window)
    return window


def _requested() -> dict[str, int]:
    return {"desired": 1, "minimum": 0, "maximum": 1}


def _prepared_bundle() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    policy = _policy()
    observation = _observation()
    window = _window_expiring_early()
    prepared = AutonomyRuntimeService().prepare(
        AutonomyRuntimeInputs(
            policy=policy,
            observation=observation,
            authority_inputs=_AUTHORITY,
            automation_principal=_PRINCIPAL,
            window_state=window,
            requested=_requested(),
            correlation=_CORRELATION,
            evaluated_at=_EVALUATED_AT,
        )
    )
    assert prepared.authorized, "fixture must produce an authorized decision"
    return policy, observation, prepared.decision, prepared.operation, window


def _context() -> AutonomyExecutionAuthorityContext:
    policy = _policy()
    return AutonomyExecutionAuthorityContext(
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
        enrolled_fleet_arn=_FLEET_ARN,
        enrolled_location=policy["target"]["location"],
    )


class _FreshSwitch:
    def __init__(self) -> None:
        self.calls = 0

    def require_autonomy(self) -> None:
        self.calls += 1


class _OwnedReservation:
    def __init__(self) -> None:
        self.calls = 0

    def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None:
        self.calls += 1


@pytest.mark.unit
def test_window_expired_at_execute_time_fails_closed_before_switch_and_reservation() -> None:
    policy, observation, decision, operation, window = _prepared_bundle()
    switch = _FreshSwitch()
    reservation = _OwnedReservation()
    verifier = AutonomyExecutionVerifier(
        context=_context(),
        autonomy_switch=switch,
        reservation=reservation,
        clock=lambda: _EXECUTE_CLOCK,
    )
    evidence = AutonomyExecutionEvidence(
        policy=policy,
        observation=observation,
        decision=decision,
        operation=operation,
        window_state=window,
    )
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=operation["operation_id"], evidence=evidence)
    assert exc.value.reason == AutonomyExecutionVerificationReason.WINDOW_STATE_EXPIRED
    # The window gate fires BEFORE the switch and reservation are consulted.
    assert switch.calls == 0
    assert reservation.calls == 0


@pytest.mark.unit
def test_fresh_window_still_verifies() -> None:
    policy, observation, decision, operation, window = _prepared_bundle()
    switch = _FreshSwitch()
    reservation = _OwnedReservation()
    # Execute BEFORE the window expiry (25s after the decision instant): fresh.
    fresh_clock = datetime.fromtimestamp(_EVALUATED_EPOCH + 25, tz=timezone.utc)
    verifier = AutonomyExecutionVerifier(
        context=_context(),
        autonomy_switch=switch,
        reservation=reservation,
        clock=lambda: fresh_clock,
    )
    evidence = AutonomyExecutionEvidence(
        policy=policy,
        observation=observation,
        decision=decision,
        operation=operation,
        window_state=window,
    )
    plan = verifier.verify(operation_id=operation["operation_id"], evidence=evidence)
    assert plan.max_writes == 1
    assert switch.calls == 1
    assert reservation.calls == 1
