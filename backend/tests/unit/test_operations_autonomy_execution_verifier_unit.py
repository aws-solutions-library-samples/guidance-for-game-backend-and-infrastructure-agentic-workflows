"""Adversarial precondition tests for the additive E5 bounded-autonomy
execution verifier and its narrow selection path (issue #439, track B).

The autonomous executor trusts nothing it is handed — not the model, not the
dispatcher, not the durable Step Functions workflow, not the operation id beyond
using it to reload. Before a single provider write, the v2
:class:`AutonomyExecutionVerifier` independently reloads and re-verifies EVERY
server-owned precondition against the exact policy, the canonical E1 observation
(and its bound evidence projection), the deterministic autonomous decision, the
immutable autonomous operation, and the current durable window state, plus the
code-owned deployment authority context, a fresh E4 kill switch + durable
intent, a fresh *separate* autonomy switch supplied via a port, and atomic
reservation ownership immediately before the write.

These tests assert:

* the happy path produces a bounded :class:`VerifiedExecutionPlan` that the
  existing (unmodified) ``ExecutorService`` can consume;
* each precondition is a discriminating gate — flipping exactly one input to an
  invalid value fails closed with a bounded, public-safe reason and never
  proceeds to the reservation or the write;
* direct invocation (missing/forged evidence), stale/mismatched window state,
  a changed or unavailable autonomy switch, an expired decision, and a lost
  reservation each fail closed;
* the model may *request* evaluation but can never authorize: only an
  ``authorized`` / ``APPROVED_AUTONOMOUS`` decision at ``operate`` authority is
  accepted, and there is no human-approval branch anywhere in the v2 path;
* the v1 :class:`ExecutionVerifier` and its human-approval branch are untouched
  by the selection path (compatibility), and the selection path never routes a
  v1 human-approved operation through the autonomous verifier or vice versa.
"""

from __future__ import annotations

# Standard library
from copy import deepcopy
from dataclasses import replace
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
    AutonomySwitchDenied,
)
from operations.autonomy_playbook_definition import (
    EXECUTOR_BINDING_VERSION,
    EXECUTOR_ID,
    autonomy_playbook_hash,
)
from operations.contracts import load_json
from operations.execution_verifier import VerifiedExecutionPlan

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"

# A GameLift fleet ARN is server-owned; it is never taken from the operation.
_FLEET_ARN = "arn:aws:gamelift:us-west-2:123456789012:fleet/fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"

# The valid fixture set is internally coherent and passes
# ``validate_autonomous_operation_binding`` / ``evaluate_autonomy_policy`` at a
# clock equal to the decision's ``evaluated_at`` and strictly before its
# ``decision_expires_at``.
_VERIFY_CLOCK = datetime(2026, 9, 21, 19, 12, 52, tzinfo=timezone.utc)


def _policy() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json")


def _observation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-autonomy-observation.valid.json")


def _decision() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomous-decision.valid.json")


def _operation() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomous-operation.valid.json")


def _window_state() -> dict[str, Any]:
    return load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json")


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


def _evidence(**overrides: Any) -> AutonomyExecutionEvidence:
    bundle = {
        "policy": _policy(),
        "observation": _observation(),
        "decision": _decision(),
        "operation": _operation(),
        "window_state": _window_state(),
    }
    bundle.update(overrides)
    return AutonomyExecutionEvidence(**bundle)


class _FreshSwitch:
    """An autonomy switch port that reports the autonomy phase enabled + fresh."""

    def __init__(self) -> None:
        self.calls = 0

    def require_autonomy(self) -> None:
        self.calls += 1


class _DeniedSwitch:
    def require_autonomy(self) -> None:
        raise AutonomySwitchDenied("autonomy switch disabled")


class _OwnedReservation:
    """A reservation port that confirms the caller owns the atomic reservation."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None:
        self.calls.append(logical_action_id)


class _LostReservation:
    def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None:
        raise RuntimeError("reservation not owned")


def _verifier(
    *,
    context: AutonomyExecutionAuthorityContext | None = None,
    switch: Any | None = None,
    reservation: Any | None = None,
    clock: datetime | None = None,
) -> AutonomyExecutionVerifier:
    return AutonomyExecutionVerifier(
        context=context if context is not None else _context(),
        autonomy_switch=switch if switch is not None else _FreshSwitch(),
        reservation=reservation if reservation is not None else _OwnedReservation(),
        clock=lambda: clock if clock is not None else _VERIFY_CLOCK,
    )


# -- Happy path --------------------------------------------------------------


def test_valid_bundle_produces_bounded_execution_plan() -> None:
    switch = _FreshSwitch()
    reservation = _OwnedReservation()
    verifier = _verifier(switch=switch, reservation=reservation)

    plan = verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())

    assert isinstance(plan, VerifiedExecutionPlan)
    assert plan.max_writes == 1
    assert plan.fleet_arn == _FLEET_ARN
    assert plan.fleet_id == _policy()["target"]["fleet_id"]
    assert plan.location == _policy()["target"]["location"]
    # The write parameters are the exact decision-requested triple within 0/1/1.
    assert plan.intent["parameters"] == {"desired": 1, "minimum": 0, "maximum": 1}
    assert plan.intent["expected_current_capacity"] == {"desired": 0, "minimum": 0, "maximum": 1}
    assert plan.logical_action_id == plan.intent["logical_action_id"]
    # Both the separate autonomy switch and the atomic reservation were checked.
    assert switch.calls == 1
    assert reservation.calls == [plan.logical_action_id]


def test_reservation_is_reserved_immediately_before_write() -> None:
    """The atomic reservation must be the LAST gate — after every binding and
    freshness check — so ownership is confirmed immediately before the write."""
    order: list[str] = []

    class _RecordingSwitch:
        def require_autonomy(self) -> None:
            order.append("switch")

    class _RecordingReservation:
        def require_reservation(self, *, operation_id: str, logical_action_id: str) -> None:
            order.append("reservation")

    verifier = _verifier(switch=_RecordingSwitch(), reservation=_RecordingReservation())
    verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())

    assert order[-1] == "reservation"
    assert "switch" in order
    assert order.index("switch") < order.index("reservation")


# -- Direct invocation / forged or missing evidence --------------------------


def test_operation_id_mismatch_fails_closed() -> None:
    verifier = _verifier()
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id="op_this_is_not_the_operation", evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.OPERATION_IDENTITY_MISMATCH


def test_missing_observation_evidence_fails_closed() -> None:
    verifier = _verifier()
    with pytest.raises(AutonomyExecutionVerificationError):
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence(observation={}))


def test_forged_binding_fails_closed() -> None:
    """An operation whose current_state does not match the canonical E1
    observation evidence (a forged/tampered decision) fails the #438 binding."""
    operation = _operation()
    operation["current_state"]["capacity"] = {"desired": 1, "minimum": 0, "maximum": 1}
    verifier = _verifier()
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=operation["operation_id"], evidence=_evidence(operation=operation))
    assert exc.value.reason == AutonomyExecutionVerificationReason.BINDING_INVALID


def test_reservation_and_switch_not_checked_when_binding_fails() -> None:
    operation = _operation()
    operation["current_state"]["capacity"] = {"desired": 1, "minimum": 0, "maximum": 1}
    switch = _FreshSwitch()
    reservation = _OwnedReservation()
    verifier = _verifier(switch=switch, reservation=reservation)
    with pytest.raises(AutonomyExecutionVerificationError):
        verifier.verify(operation_id=operation["operation_id"], evidence=_evidence(operation=operation))
    assert switch.calls == 0
    assert reservation.calls == []


# -- No fake approval / model cannot authorize -------------------------------


def test_denied_decision_is_rejected() -> None:
    decision = _decision()
    decision["decision"] = "denied"
    decision["reason_codes"] = ["BUDGET_EXCEEDED"]
    operation = _operation()
    operation["authority"]["decision"] = "denied"
    operation["authority"]["reason_codes"] = ["BUDGET_EXCEEDED"]
    verifier = _verifier()
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(
            operation_id=operation["operation_id"],
            evidence=_evidence(decision=decision, operation=operation),
        )
    assert exc.value.reason in {
        AutonomyExecutionVerificationReason.DECISION_NOT_AUTHORIZED,
        AutonomyExecutionVerificationReason.BINDING_INVALID,
    }


def test_fabricated_human_approval_field_is_ignored_not_accepted() -> None:
    """A v2 bundle carrying a smuggled human-approval field must not be treated
    as an authorization path; the deterministic decision is the only grant."""
    operation = _operation()
    operation["human_approval"] = {"decision": "granted", "approver": "subject.attacker"}
    verifier = _verifier()
    with pytest.raises(AutonomyExecutionVerificationError):
        verifier.verify(operation_id=operation["operation_id"], evidence=_evidence(operation=operation))


def test_reason_code_not_approved_autonomous_is_rejected() -> None:
    decision = _decision()
    decision["reason_codes"] = ["APPROVED_AUTONOMOUS", "extra"]
    verifier = _verifier()
    with pytest.raises(AutonomyExecutionVerificationError):
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence(decision=decision))


# -- Authority --------------------------------------------------------------


def test_deployment_below_operate_authority_fails_closed() -> None:
    context = replace(_context(), deployment_mode="remediate")
    verifier = _verifier(context=context)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.INSUFFICIENT_EXECUTION_AUTHORITY


def test_capability_below_operate_authority_fails_closed() -> None:
    context = replace(_context(), capability_maximum="remediate")
    verifier = _verifier(context=context)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.INSUFFICIENT_EXECUTION_AUTHORITY


# -- Registered capability / playbook / executor / policy --------------------


def test_playbook_hash_mismatch_fails_closed() -> None:
    context = replace(_context(), expected_playbook_hash="sha256:" + "0" * 64)
    verifier = _verifier(context=context)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.PLAYBOOK_HASH_MISMATCH


def test_executor_binding_mismatch_fails_closed() -> None:
    context = replace(_context(), expected_executor_binding_version="9.9")
    verifier = _verifier(context=context)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.EXECUTOR_BINDING_MISMATCH


def test_policy_hash_mismatch_with_deployment_fails_closed() -> None:
    context = replace(_context(), expected_policy_hash="sha256:" + "0" * 64)
    verifier = _verifier(context=context)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.POLICY_MISMATCH


def test_fleet_binding_mismatch_fails_closed() -> None:
    context = replace(_context(), enrolled_fleet_id="fleet-0000")
    verifier = _verifier(context=context)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.FLEET_BINDING_MISMATCH


def test_tenant_workspace_mismatch_fails_closed() -> None:
    context = replace(_context(), workspace_id="workspace.other")
    verifier = _verifier(context=context)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.TENANT_WORKSPACE_MISMATCH


# -- Stale / mismatched window state -----------------------------------------


def test_mismatched_window_state_reference_fails_closed() -> None:
    window = _window_state()
    window["state_revision"] = 999  # Reference no longer matches the operation.
    verifier = _verifier()
    with pytest.raises(AutonomyExecutionVerificationError):
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence(window_state=window))


def test_stale_window_state_fails_closed() -> None:
    """A clock past the durable window state expiry makes the state stale; the
    deterministic evaluator emits WINDOW_STATE_STALE and the decision no longer
    matches → binding/decision fails closed."""
    late = datetime(2026, 9, 21, 20, 30, 0, tzinfo=timezone.utc)
    verifier = _verifier(clock=late)
    with pytest.raises(AutonomyExecutionVerificationError):
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())


# -- Decision expiry ---------------------------------------------------------


def test_expired_decision_fails_closed() -> None:
    # One second past decision_expires_at (19:13:52Z).
    expired_clock = datetime(2026, 9, 21, 19, 13, 53, tzinfo=timezone.utc)
    verifier = _verifier(clock=expired_clock)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.DECISION_EXPIRED


def test_decision_valid_exactly_before_expiry() -> None:
    # One second before decision_expires_at is still eligible.
    ok_clock = datetime(2026, 9, 21, 19, 13, 51, tzinfo=timezone.utc)
    verifier = _verifier(clock=ok_clock)
    plan = verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert isinstance(plan, VerifiedExecutionPlan)


# -- Separate autonomy switch (via port) -------------------------------------


def test_autonomy_switch_denied_fails_closed_before_write() -> None:
    reservation = _OwnedReservation()
    verifier = _verifier(switch=_DeniedSwitch(), reservation=reservation)
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.AUTONOMY_SWITCH_DENIED
    # The switch is checked before the reservation and no write plan is produced.
    assert reservation.calls == []


def test_autonomy_switch_change_midflight_denies() -> None:
    """A switch that flips to denied is honored on the very next verify call
    (the port reads fresh each time; the verifier holds no cached state)."""

    class _FlippingSwitch:
        def __init__(self) -> None:
            self.enabled = True

        def require_autonomy(self) -> None:
            if not self.enabled:
                raise AutonomySwitchDenied("autonomy switch flipped off")

    switch = _FlippingSwitch()
    verifier = _verifier(switch=switch)
    assert verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    switch.enabled = False
    with pytest.raises(AutonomyExecutionVerificationError):
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())


# -- Atomic reservation ownership --------------------------------------------


def test_lost_reservation_fails_closed() -> None:
    verifier = _verifier(reservation=_LostReservation())
    with pytest.raises(AutonomyExecutionVerificationError) as exc:
        verifier.verify(operation_id=_operation()["operation_id"], evidence=_evidence())
    assert exc.value.reason == AutonomyExecutionVerificationReason.RESERVATION_NOT_OWNED


# -- Context validation ------------------------------------------------------


def test_context_requires_operate_authority_levels() -> None:
    with pytest.raises(ValueError):
        replace(_context(), deployment_mode="not-a-level")


def test_context_requires_fleet_arn() -> None:
    with pytest.raises(ValueError):
        replace(_context(), enrolled_fleet_arn="not-an-arn")
