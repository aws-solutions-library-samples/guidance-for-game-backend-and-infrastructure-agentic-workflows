"""Service tests for the E2 AdviceService and PrepareService ports (#414).

These drive the two protocol-neutral services through their real deterministic
paths using in-memory fakes for the injected E1 observation/status port, the
server-owned bounds port, and the identity boundary. They assert: identity is
verifier-derived only; the untrusted proposal cannot inject trusted fields;
current state is loaded from the trusted port and fails closed when
missing/mismatched; advice change/risk are deterministic; preparation is
immutable and idempotent; a GameLift capacity change always yields
approval_required (never authorized); and disabled/insufficient authority or
out-of-bounds proposals deterministically deny.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.advice import (
    AdviceBoundaryError,
    AdviceErrorCode,
    AdviceRequestContext,
    AdviceService,
    AuthorityCeilings,
    CapacityBounds,
    CapacityProposalRequest,
    CapacityValues,
    CurrentCapacity,
)
from operations.contracts.capacity import (
    ADVICE_SCHEMA_NAME,
    AUTHORIZATION_SCHEMA_NAME,
    PREPARED_OPERATION_SCHEMA_NAME,
    validate_capacity_contract,
    validate_prepared_operation_binding,
)
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal
from operations.prepare import (
    CapacityPlaybook,
    PrepareBoundaryError,
    PreparedDecision,
    PrepareErrorCode,
    PrepareRequestContext,
    PrepareService,
)
from operations.settings import resolve_operations_settings

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OBS_HASH = "sha256:" + "0" * 64


def _settings(mode: str = "advise"):
    return resolve_operations_settings({"GBAW_OPERATIONS_MODE": mode})


def _boundary() -> ApprovalIdentityBoundary:
    return ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        requester_client_ids=frozenset({"client.web-console"}),
        approver_client_ids=frozenset({"client.approver"}),
        trusted_audiences=frozenset({"gbaw-operations"}),
    )


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject_id="subject.operator-1",
        client_id="client.web-console",
        audience="gbaw-operations",
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expires_at=NOW + timedelta(hours=1),
    )


class FakeStatePort:
    def __init__(self, current: CurrentCapacity | None) -> None:
        self._current = current
        self.calls: list[tuple[str, str, str]] = []

    def load_current_capacity(self, *, requester, fleet_id, location):
        self.calls.append((requester.workspace_id, fleet_id, location))
        return self._current


class FakeBoundsPort:
    def __init__(self, bounds: CapacityBounds | None) -> None:
        self._bounds = bounds

    def resolve_bounds(self, *, requester, fleet_id, location):
        return self._bounds


# The trusted observation revision anchor. NOW sits inside [OBSERVED_AT,
# OBSERVED_AT + 30m), so the fixed-clock service paths stay fresh.
OBSERVED_AT = NOW
OBS_EXPIRES_AT = NOW + timedelta(minutes=30)


def _current(desired=10, minimum=2, maximum=20) -> CurrentCapacity:
    return CurrentCapacity(
        observation_id="obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        observation_hash=OBS_HASH,
        capacity=CapacityValues(desired=desired, minimum=minimum, maximum=maximum),
        observed_at=OBSERVED_AT,
        expires_at=OBS_EXPIRES_AT,
    )


def _bounds(*, floor=1, ceiling=30, max_step=10, enrolled=True) -> CapacityBounds:
    return CapacityBounds(
        floor=floor,
        ceiling=ceiling,
        max_step=max_step,
        enrollment_id="enroll.fleet-default",
        enrollment_version="2026-09-01",
        policy_id="policy.capacity-default",
        policy_version="2026-09-01",
        target_enrolled=enrolled,
    )


def _proposal(desired=14, minimum=2, maximum=20) -> CapacityProposalRequest:
    return CapacityProposalRequest(
        fleet_id=FLEET,
        location=LOCATION,
        requested=CapacityValues(desired=desired, minimum=minimum, maximum=maximum),
        idempotency_token=TOKEN,
    )


def _advice_context() -> AdviceRequestContext:
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


def _advice_service(
    state: FakeStatePort, bounds: FakeBoundsPort, mode="advise", advice_id="adv_aaaaaaaaaaaaaaaaaaaaaaaaaa"
):
    return AdviceService(
        settings=_settings(mode),
        identity_boundary=_boundary(),
        state_port=state,
        bounds_port=bounds,
        clock=lambda: NOW,
        advice_id_factory=lambda: advice_id,
    )


def _playbook() -> CapacityPlaybook:
    return CapacityPlaybook(
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
    )


def _prepare_context(mode="operate", principal_authority="remediate") -> PrepareRequestContext:
    return PrepareRequestContext(
        requester=_principal(),
        request_id="request.advise-1",
        correlation_id="corr.advise-1",
        deployment_mode=mode,
        tenant_policy="operate",
        workspace_policy="operate",
        principal_authority=principal_authority,
        capability_maximum="remediate",
        operation_risk_policy="operate",
    )


def _prepare_service(mode="operate", clock=None) -> PrepareService:
    return PrepareService(
        settings=_settings("advise" if mode != "disabled" else "disabled"),
        identity_boundary=_boundary(),
        playbook=_playbook(),
        clock=clock or (lambda: NOW),
    )


# -- AdviceService -----------------------------------------------------------


def test_advice_is_deterministic_and_valid() -> None:
    service = _advice_service(FakeStatePort(_current()), FakeBoundsPort(_bounds()))
    a = service.advise(_proposal(), _advice_context())
    b = service.advise(_proposal(), _advice_context())
    validate_capacity_contract(ADVICE_SCHEMA_NAME, a)
    assert a == b
    assert a["change"] == {"desired": 4, "minimum": 0, "maximum": 0}
    assert a["current_state"]["observation_hash"] == OBS_HASH
    assert a["bounds"]["within_bounds"] is True


def test_advice_identity_comes_only_from_principal() -> None:
    state = FakeStatePort(_current())
    service = _advice_service(state, FakeBoundsPort(_bounds()))
    service.advise(_proposal(), _advice_context())
    # The state port is queried with the verifier-derived workspace, never
    # anything from the untrusted proposal.
    assert state.calls == [("workspace.default", FLEET, LOCATION)]


def test_advice_fails_closed_when_current_state_missing() -> None:
    service = _advice_service(FakeStatePort(None), FakeBoundsPort(_bounds()))
    with pytest.raises(AdviceBoundaryError) as exc:
        service.advise(_proposal(), _advice_context())
    assert exc.value.error_code is AdviceErrorCode.CURRENT_STATE_UNAVAILABLE
    assert exc.value.retryable is True


def test_advice_denies_when_advise_disabled() -> None:
    service = _advice_service(FakeStatePort(_current()), FakeBoundsPort(_bounds()), mode="observe")
    with pytest.raises(AdviceBoundaryError) as exc:
        service.advise(_proposal(), _advice_context())
    assert exc.value.error_code is AdviceErrorCode.AUTHORIZATION_DENIED


def test_advice_flags_out_of_bounds_proposal() -> None:
    service = _advice_service(FakeStatePort(_current()), FakeBoundsPort(_bounds(ceiling=15)))
    advice = service.advise(_proposal(desired=25, maximum=25), _advice_context())
    assert advice["bounds"]["within_bounds"] is False
    assert "DESIRED_ABOVE_CEILING" in advice["bounds"]["violations"]


def test_advice_flags_unenrolled_target() -> None:
    service = _advice_service(FakeStatePort(_current()), FakeBoundsPort(_bounds(enrolled=False)))
    advice = service.advise(_proposal(), _advice_context())
    assert "TARGET_NOT_ENROLLED" in advice["bounds"]["violations"]


@pytest.mark.parametrize(
    "payload",
    [
        {"fleet_id": FLEET, "idempotency_token": TOKEN},  # missing request envelope
        {
            "request_contract_version": "1.0",
            "capability_id": "gamelift.capacity-adjustment",
            "idempotency_token": TOKEN,
            "proposal": {
                "fleet_id": FLEET,
                "location": LOCATION,
                "requested": {"desired": 1, "minimum": 1, "maximum": 1},
                "requester": "x",
            },
        },
        {
            "request_contract_version": "1.0",
            "capability_id": "gamelift.observe-fleet",  # wrong capability
            "idempotency_token": TOKEN,
            "proposal": {
                "fleet_id": FLEET,
                "location": LOCATION,
                "requested": {"desired": 1, "minimum": 1, "maximum": 1},
            },
        },
        {
            "request_contract_version": "1.0",
            "capability_id": "gamelift.capacity-adjustment",
            "idempotency_token": TOKEN,
            "proposal": {
                "fleet_id": FLEET,
                "location": LOCATION,
                "requested": {"desired": 1, "minimum": 1, "maximum": 1},
            },
            "current_state": {"capacity": {"desired": 0, "minimum": 0, "maximum": 0}},  # injected trusted field
        },
    ],
)
def test_proposal_from_payload_rejects_injection(payload) -> None:
    with pytest.raises(AdviceBoundaryError) as exc:
        CapacityProposalRequest.from_payload(payload)
    assert exc.value.error_code is AdviceErrorCode.CONTRACT_INVALID


# -- PrepareService ----------------------------------------------------------


def _prepared_advice() -> dict:
    service = _advice_service(FakeStatePort(_current()), FakeBoundsPort(_bounds()))
    return service.advise(_proposal(), _advice_context())


def test_prepare_produces_approval_required_and_binds() -> None:
    advice = _prepared_advice()
    prepared = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert prepared.decision is PreparedDecision.APPROVAL_REQUIRED
    validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, prepared.operation)
    validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, prepared.authorization)
    validate_prepared_operation_binding(prepared.operation, prepared.authorization)
    assert prepared.operation["prepared_hash"] == prepared.prepared_hash
    assert prepared.operation["authority"]["decision"] == "approval_required"


def test_prepare_is_idempotent_byte_for_byte() -> None:
    advice = _prepared_advice()
    a = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    b = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert a.operation == b.operation
    assert a.prepared_hash == b.prepared_hash
    assert a.authorization == b.authorization


def test_prepare_never_authorizes_outright() -> None:
    advice = _prepared_advice()
    prepared = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    # The only non-denied decision an E2 GameLift change can reach is
    # approval_required; "authorized" is not even a permissible value.
    assert prepared.operation["authority"]["decision"] != "authorized"


def test_prepare_denies_disabled_deployment() -> None:
    advice = _prepared_advice()
    prepared = _prepare_service().prepare(advice, _prepare_context(mode="disabled"), idempotency_token=TOKEN)
    assert prepared.decision is PreparedDecision.DENIED
    assert prepared.operation["authority"]["reason_codes"] == ["DEPLOYMENT_DISABLED"]


def test_prepare_denies_insufficient_authority() -> None:
    advice = _prepared_advice()
    prepared = _prepare_service().prepare(
        advice, _prepare_context(principal_authority="observe"), idempotency_token=TOKEN
    )
    assert prepared.decision is PreparedDecision.DENIED
    assert "INSUFFICIENT_AUTHORITY" in prepared.operation["authority"]["reason_codes"]


def test_prepare_denies_out_of_bounds_advice() -> None:
    service = _advice_service(FakeStatePort(_current()), FakeBoundsPort(_bounds(ceiling=15)))
    advice = service.advise(_proposal(desired=25, maximum=25), _advice_context())
    prepared = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert prepared.decision is PreparedDecision.DENIED
    assert "BOUNDS_EXCEEDED" in prepared.operation["authority"]["reason_codes"]


def test_prepare_fails_closed_on_future_dated_advice() -> None:
    advice = _prepared_advice()
    earlier = NOW - timedelta(hours=1)
    with pytest.raises(PrepareBoundaryError) as exc:
        _prepare_service(clock=lambda: earlier).prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert exc.value.error_code is PrepareErrorCode.ADVICE_STALE


def test_prepare_rejects_invalid_advice() -> None:
    with pytest.raises(PrepareBoundaryError) as exc:
        _prepare_service().prepare({"not": "advice"}, _prepare_context(), idempotency_token=TOKEN)
    assert exc.value.error_code is PrepareErrorCode.CONTRACT_INVALID


def test_prepare_operation_id_is_stable_for_same_intent() -> None:
    advice = _prepared_advice()
    a = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    b = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert a.operation["operation_id"] == b.operation["operation_id"]


def test_prepare_holds_no_executor_credential() -> None:
    advice = _prepared_advice()
    prepared = _prepare_service().prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    binding = prepared.operation["future_executor_binding"]
    # Only an identifier binding forward — never a credential/secret/token.
    assert set(binding) == {"executor_id", "executor_binding_version"}
