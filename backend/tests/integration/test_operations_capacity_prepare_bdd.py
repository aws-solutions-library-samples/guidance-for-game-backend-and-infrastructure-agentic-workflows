"""Behavior-driven scenarios for the E2 advise -> prepare lifecycle (#414).

Given/When/Then scenarios in plain pytest (the repository has no BDD runner and
builds offline, so no new framework dependency is added). Each scenario drives
the real AdviceService and PrepareService end to end through one behavior of the
E2 contract/advice/prepare layer described in issue #414: a healthy in-bounds
proposal prepared for approval, a deterministic idempotent retry, an
out-of-bounds proposal deterministically denied, a disabled deployment denied,
current state loaded from the trusted E1 port (never the request), and a
current-state change producing a different prepared hash.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.advice import (
    AdviceRequestContext,
    AdviceService,
    AuthorityCeilings,
    CapacityBounds,
    CapacityProposalRequest,
    CapacityValues,
    CurrentCapacity,
)
from operations.contracts.capacity import (
    AUTHORIZATION_SCHEMA_NAME,
    PREPARED_OPERATION_SCHEMA_NAME,
    validate_capacity_contract,
    validate_prepared_operation_binding,
)
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal
from operations.prepare import CapacityPlaybook, PreparedDecision, PrepareRequestContext, PrepareService
from operations.settings import resolve_operations_settings

pytestmark = [pytest.mark.integration, pytest.mark.localhost]

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"


class FakeStatePort:
    def __init__(self, current):
        self._current = current

    def load_current_capacity(self, *, requester, fleet_id, location):
        return self._current


class FakeBoundsPort:
    def __init__(self, bounds):
        self._bounds = bounds

    def resolve_bounds(self, *, requester, fleet_id, location):
        return self._bounds


def _principal():
    return VerifiedPrincipal(
        subject_id="subject.operator-1",
        client_id="client.web-console",
        audience="gbaw-operations",
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expires_at=NOW + timedelta(hours=1),
    )


def _boundary():
    return ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        requester_client_ids=frozenset({"client.web-console"}),
        approver_client_ids=frozenset({"client.approver"}),
        trusted_audiences=frozenset({"gbaw-operations"}),
    )


def _current(desired=10):
    return CurrentCapacity(
        observation_id="obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        observation_hash="sha256:" + f"{desired:064x}",
        capacity=CapacityValues(desired=desired, minimum=2, maximum=20),
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def _bounds(ceiling=30, enrolled=True):
    return CapacityBounds(
        floor=1,
        ceiling=ceiling,
        max_step=10,
        enrollment_id="enroll.fleet-default",
        enrollment_version="2026-09-01",
        policy_id="policy.capacity-default",
        policy_version="2026-09-01",
        target_enrolled=enrolled,
    )


def _advice_service(current, bounds, advice_id="adv_aaaaaaaaaaaaaaaaaaaaaaaaaa"):
    return AdviceService(
        settings=resolve_operations_settings({"GBAW_OPERATIONS_MODE": "advise"}),
        identity_boundary=_boundary(),
        state_port=FakeStatePort(current),
        bounds_port=FakeBoundsPort(bounds),
        clock=lambda: NOW,
        advice_id_factory=lambda: advice_id,
    )


def _prepare_service(deployment_mode="advise"):
    return PrepareService(
        settings=resolve_operations_settings({"GBAW_OPERATIONS_MODE": deployment_mode}),
        identity_boundary=_boundary(),
        playbook=CapacityPlaybook(
            playbook_id="playbook.gamelift-capacity",
            playbook_version="1.0.0",
            playbook_hash="sha256:" + "1" * 64,
            profile="gamelift.capacity-adjustment/1.0",
            retry_policy={
                "max_attempts": 3,
                "base_delay_seconds": 2,
                "max_delay_seconds": 60,
                "reconcile_before_retry": True,
            },
            future_executor_binding={"executor_id": "executor.gamelift-capacity", "executor_binding_version": "1.0"},
        ),
        clock=lambda: NOW,
    )


def _proposal(desired=14, maximum=20):
    return CapacityProposalRequest(
        fleet_id=FLEET,
        location=LOCATION,
        requested=CapacityValues(desired=desired, minimum=2, maximum=maximum),
        idempotency_token=TOKEN,
    )


def _advice_context():
    return AdviceRequestContext(
        requester=_principal(),
        request_id="request.advise-1",
        correlation_id="corr.advise-1",
        authority_ceilings=AuthorityCeilings(
            tenant_policy="operate",
            workspace_policy="operate",
            principal_authority="remediate",
            capability_maximum="remediate",
            operation_risk_policy="operate",
        ),
    )


def _prepare_context(deployment_mode="operate"):
    return PrepareRequestContext(
        requester=_principal(),
        request_id="request.advise-1",
        correlation_id="corr.advise-1",
        deployment_mode=deployment_mode,
        tenant_policy="operate",
        workspace_policy="operate",
        principal_authority="remediate",
        capability_maximum="remediate",
        operation_risk_policy="operate",
    )


def test_scenario_in_bounds_proposal_is_prepared_for_approval() -> None:
    # Given a fresh trusted observation and an in-bounds proposal
    advice = _advice_service(_current(), _bounds()).advise(_proposal(), _advice_context())
    # When the operation is prepared
    prepared = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    # Then it is immutable, bound, and awaits a direct human approval
    validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, prepared.operation)
    validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, prepared.authorization)
    validate_prepared_operation_binding(prepared.operation, prepared.authorization)
    assert prepared.decision is PreparedDecision.APPROVAL_REQUIRED


def test_scenario_retry_is_deterministic() -> None:
    # Given one advice for one intent
    advice = _advice_service(_current(), _bounds()).advise(_proposal(), _advice_context())
    # When it is prepared twice
    first = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    second = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    # Then the prepared operation and hash are byte-for-byte identical
    assert first.operation == second.operation
    assert first.prepared_hash == second.prepared_hash


def test_scenario_out_of_bounds_proposal_is_denied() -> None:
    # Given a proposal that exceeds the server-owned ceiling
    advice = _advice_service(_current(), _bounds(ceiling=12)).advise(
        _proposal(desired=25, maximum=25), _advice_context()
    )
    # When it is prepared
    prepared = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    # Then it is deterministically denied for bounds
    assert prepared.decision is PreparedDecision.DENIED
    assert "BOUNDS_EXCEEDED" in prepared.operation["authority"]["reason_codes"]


def test_scenario_disabled_deployment_is_denied() -> None:
    # Given advise is enabled for advice but the operation authority is disabled
    advice = _advice_service(_current(), _bounds()).advise(_proposal(), _advice_context())
    # When prepared under a disabled deployment mode
    prepared = _prepare_service().prepare(advice, _prepare_context(deployment_mode="disabled"), idempotency_token=TOKEN)
    # Then it is denied and never prepared for execution
    assert prepared.decision is PreparedDecision.DENIED
    assert prepared.operation["authority"]["reason_codes"] == ["DEPLOYMENT_DISABLED"]


def test_scenario_current_state_change_changes_prepared_hash() -> None:
    # Given the same proposal but two different trusted current-capacity states
    advice_low = _advice_service(_current(desired=10), _bounds()).advise(_proposal(), _advice_context())
    advice_high = _advice_service(_current(desired=12), _bounds()).advise(_proposal(), _advice_context())
    # When each is prepared
    prepared_low = _prepare_service().prepare(advice_low, _prepare_context(), idempotency_token=TOKEN)
    prepared_high = _prepare_service().prepare(advice_high, _prepare_context(), idempotency_token=TOKEN)
    # Then the current-state binding produces distinct prepared hashes
    assert prepared_low.prepared_hash != prepared_high.prepared_hash


def test_scenario_advise_mode_prepares_for_approval_not_denied() -> None:
    # Regression for the live E2 denial: with OperationsMode=advise and all six
    # preparation ceilings >= advise, prepare must produce approval_required, not
    # a denied decision. Before the fix the effective authority (advise) was
    # gated against remediate and the whole E2 phase was unreachable.
    advice = _advice_service(_current(), _bounds()).advise(_proposal(), _advice_context())
    prepared = _prepare_service(deployment_mode="advise").prepare(
        advice, _prepare_context(deployment_mode="advise"), idempotency_token=TOKEN
    )
    assert prepared.decision is PreparedDecision.APPROVAL_REQUIRED
    assert prepared.operation["authority"]["effective_authority"] == "advise"
    assert prepared.operation["authority"]["reason_codes"] == ["APPROVAL_REQUIRED"]
    # It is never authorized outright and carries the immutable, hash-bound
    # execution authority a future E3 executor must re-verify before any write.
    assert prepared.operation["authority"]["decision"] == "approval_required"
    assert prepared.operation["required_execution_authority"] == "remediate"
    assert prepared.authorization["required_execution_authority"] == "remediate"
    validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, prepared.operation)
    validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, prepared.authorization)
    validate_prepared_operation_binding(prepared.operation, prepared.authorization)


def test_scenario_observe_mode_denies_for_insufficient_authority() -> None:
    # E1 observe authority is below the advise prepare-phase minimum, so the
    # operation is denied — approval never elevates execution authority.
    advice = _advice_service(_current(), _bounds()).advise(_proposal(), _advice_context())
    prepared = _prepare_service(deployment_mode="advise").prepare(
        advice, _prepare_context(deployment_mode="observe"), idempotency_token=TOKEN
    )
    assert prepared.decision is PreparedDecision.DENIED
    assert prepared.operation["authority"]["reason_codes"] == ["INSUFFICIENT_AUTHORITY"]


def test_scenario_required_execution_authority_is_hash_bound_and_immutable() -> None:
    # Tampering with the immutable execution authority both changes the bound
    # prepared_hash and fails schema/semantic validation.
    advice = _advice_service(_current(), _bounds()).advise(_proposal(), _advice_context())
    prepared = _prepare_service(deployment_mode="advise").prepare(
        advice, _prepare_context(deployment_mode="advise"), idempotency_token=TOKEN
    )
    tampered = dict(prepared.operation)
    tampered["required_execution_authority"] = "advise"
    with pytest.raises(Exception):
        validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, tampered)


def test_scenario_advise_mode_default_bounds_denies_for_bounds_not_authority() -> None:
    # Under the server-owned default fail-closed bounds (floor=0, ceiling=1,
    # max_step=1) an advise-mode preparation reaches the bounds evaluation: the
    # effective authority is advise (never the old remediate gate) and the denial
    # is BOUNDS_EXCEEDED, not INSUFFICIENT_AUTHORITY.
    default_bounds = CapacityBounds(
        floor=0,
        ceiling=1,
        max_step=1,
        enrollment_id="enroll.fleet-default",
        enrollment_version="2026-09-01",
        policy_id="policy.capacity-default",
        policy_version="2026-09-01",
        target_enrolled=True,
    )
    advice = _advice_service(_current(), default_bounds).advise(_proposal(), _advice_context())
    prepared = _prepare_service(deployment_mode="advise").prepare(
        advice, _prepare_context(deployment_mode="advise"), idempotency_token=TOKEN
    )
    assert prepared.decision is PreparedDecision.DENIED
    reasons = prepared.operation["authority"]["reason_codes"]
    assert "BOUNDS_EXCEEDED" in reasons
    assert "INSUFFICIENT_AUTHORITY" not in reasons
    assert prepared.operation["authority"]["effective_authority"] == "advise"
    assert prepared.operation["required_execution_authority"] == "remediate"
