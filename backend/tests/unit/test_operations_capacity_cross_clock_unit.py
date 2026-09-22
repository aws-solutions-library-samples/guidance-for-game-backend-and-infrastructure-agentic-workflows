"""Cross-clock idempotency tests for the E2 advise -> prepare layer (#414).

The E2 prepared operation must be a pure function of (token, intent, current
state revision), *not* of the wall clock at the moment of the call. A retry of
the same intent against the same trusted E1 current-state revision — issued
later, on a different clock — must reproduce byte-for-byte identical advice and
prepared-operation documents, identical timestamps, identical operation_id, and
an identical prepared_hash. The live clock is only allowed to decide whether the
trusted anchor/current state is still fresh, never to alter the emitted bytes.

These tests drive real ``AdviceService``/``PrepareService`` instances whose
clocks are advanced between calls. They fail on the pre-fix implementation,
which stamps ``created_at``/``expires_at``/``advised_at`` from the live clock.
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

# The trusted E1 observation revision anchor. Advice/prepared timestamps must be
# derived from this, never from the live clock at call time.
OBSERVED_AT = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
OBS_TTL = timedelta(seconds=1800)
OBS_EXPIRES_AT = OBSERVED_AT + OBS_TTL

# Live clocks used across retries; all sit inside the observation validity window
# so the only difference between calls is the wall clock, not the anchor.
CLOCK_EARLY = OBSERVED_AT
CLOCK_LATER = OBSERVED_AT + timedelta(minutes=5)
CLOCK_LATEST = OBSERVED_AT + timedelta(minutes=20)

FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OBS_HASH = "sha256:" + "0" * 64
PREPARE_TTL_SECONDS = 900


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
    # The principal token stays valid across every clock exercised below.
    return VerifiedPrincipal(
        subject_id="subject.operator-1",
        client_id="client.web-console",
        audience="gbaw-operations",
        tenant_id="tenant.default",
        workspace_id="workspace.default",
        expires_at=OBS_EXPIRES_AT + timedelta(hours=1),
    )


class FakeStatePort:
    def __init__(self, current: CurrentCapacity) -> None:
        self._current = current

    def load_current_capacity(self, *, requester, fleet_id, location):
        return self._current


class FakeBoundsPort:
    def __init__(self, bounds: CapacityBounds) -> None:
        self._bounds = bounds

    def resolve_bounds(self, *, requester, fleet_id, location):
        return self._bounds


def _current(
    desired=10, minimum=2, maximum=20, *, observed_at=OBSERVED_AT, expires_at=OBS_EXPIRES_AT
) -> CurrentCapacity:
    return CurrentCapacity(
        observation_id="obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        observation_hash=OBS_HASH,
        capacity=CapacityValues(desired=desired, minimum=minimum, maximum=maximum),
        observed_at=observed_at,
        expires_at=expires_at,
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


def _advice_service(current: CurrentCapacity, bounds: CapacityBounds, *, clock, mode="advise"):
    return AdviceService(
        settings=_settings(mode),
        identity_boundary=_boundary(),
        state_port=FakeStatePort(current),
        bounds_port=FakeBoundsPort(bounds),
        clock=clock,
        advice_id_factory=lambda: "adv_aaaaaaaaaaaaaaaaaaaaaaaaaa",
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


def _prepare_context() -> PrepareRequestContext:
    return PrepareRequestContext(
        requester=_principal(),
        request_id="request.advise-1",
        correlation_id="corr.advise-1",
        deployment_mode="operate",
        tenant_policy="operate",
        workspace_policy="operate",
        principal_authority="remediate",
        capability_maximum="remediate",
        operation_risk_policy="operate",
    )


def _prepare_service(*, clock) -> PrepareService:
    return PrepareService(
        settings=_settings("advise"),
        identity_boundary=_boundary(),
        playbook=_playbook(),
        clock=clock,
        operation_ttl_seconds=PREPARE_TTL_SECONDS,
    )


def _advise_then_prepare(*, current, bounds, advise_clock, prepare_clock):
    advice = _advice_service(current, bounds, clock=advise_clock).advise(_proposal(), _advice_context())
    prepared = _prepare_service(clock=prepare_clock).prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    return advice, prepared


# -- Cross-clock idempotency (the blocker) -----------------------------------


def test_prepared_operation_is_identical_when_retried_on_a_later_clock() -> None:
    """Same token+intent+current-state revision, retried later -> identical bytes."""
    _, first = _advise_then_prepare(
        current=_current(), bounds=_bounds(), advise_clock=lambda: CLOCK_EARLY, prepare_clock=lambda: CLOCK_EARLY
    )
    _, second = _advise_then_prepare(
        current=_current(), bounds=_bounds(), advise_clock=lambda: CLOCK_LATER, prepare_clock=lambda: CLOCK_LATER
    )
    assert first.operation == second.operation
    assert first.operation["operation_id"] == second.operation["operation_id"]
    assert first.operation["created_at"] == second.operation["created_at"]
    assert first.operation["expires_at"] == second.operation["expires_at"]
    assert first.prepared_hash == second.prepared_hash
    assert first.authorization == second.authorization


def test_advice_is_identical_when_recomputed_on_a_later_clock() -> None:
    """Advice for one observation revision is byte-identical across clocks."""
    early = _advice_service(_current(), _bounds(), clock=lambda: CLOCK_EARLY).advise(_proposal(), _advice_context())
    later = _advice_service(_current(), _bounds(), clock=lambda: CLOCK_LATEST).advise(_proposal(), _advice_context())
    assert early == later


def test_prepared_timestamps_are_anchored_to_the_observation_revision() -> None:
    """created_at is the observation anchor; expires_at is bounded and derived."""
    _, prepared = _advise_then_prepare(
        current=_current(), bounds=_bounds(), advise_clock=lambda: CLOCK_LATER, prepare_clock=lambda: CLOCK_LATER
    )
    op = prepared.operation
    # created_at is anchored to the trusted observation revision, not the clock.
    assert op["created_at"] == "2026-09-21T19:11:52Z"
    # expires_at is deterministic: min(observation expiry, created_at + prep TTL).
    # created_at + 900s (19:26:52) is earlier than the observation expiry
    # (19:41:52), so the preparation TTL governs.
    assert op["expires_at"] == "2026-09-21T19:26:52Z"


def test_expires_at_never_exceeds_the_trusted_observation_expiry() -> None:
    """A prep TTL longer than the remaining observation life is clamped to expiry."""
    # Observation with only 300s of life left; prep TTL is 900s.
    short_current = _current(observed_at=OBSERVED_AT, expires_at=OBSERVED_AT + timedelta(seconds=300))
    _, prepared = _advise_then_prepare(
        current=short_current, bounds=_bounds(), advise_clock=lambda: CLOCK_EARLY, prepare_clock=lambda: CLOCK_EARLY
    )
    op = prepared.operation
    assert op["created_at"] == "2026-09-21T19:11:52Z"
    # Clamped to the observation expiry (19:16:52), not created_at + 900s.
    assert op["expires_at"] == "2026-09-21T19:16:52Z"
    validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, op)


# -- Freshness is evaluated on the live clock (bytes unchanged) --------------


def test_prepare_within_validity_window_succeeds_and_validates() -> None:
    _, prepared = _advise_then_prepare(
        current=_current(), bounds=_bounds(), advise_clock=lambda: CLOCK_EARLY, prepare_clock=lambda: CLOCK_LATER
    )
    validate_capacity_contract(PREPARED_OPERATION_SCHEMA_NAME, prepared.operation)
    validate_capacity_contract(AUTHORIZATION_SCHEMA_NAME, prepared.authorization)
    validate_prepared_operation_binding(prepared.operation, prepared.authorization)
    assert prepared.decision is PreparedDecision.APPROVAL_REQUIRED


def test_prepare_at_expiry_boundary_fails_closed_as_stale() -> None:
    """At/after the trusted observation expiry, prepare fails closed on the clock."""
    advice = _advice_service(_current(), _bounds(), clock=lambda: CLOCK_EARLY).advise(_proposal(), _advice_context())
    at_expiry = OBS_EXPIRES_AT  # exactly at expiry -> no longer fresh
    with pytest.raises(PrepareBoundaryError) as exc:
        _prepare_service(clock=lambda: at_expiry).prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert exc.value.error_code is PrepareErrorCode.ADVICE_STALE


def test_prepare_after_expiry_fails_closed_as_stale() -> None:
    advice = _advice_service(_current(), _bounds(), clock=lambda: CLOCK_EARLY).advise(_proposal(), _advice_context())
    past_expiry = OBS_EXPIRES_AT + timedelta(minutes=1)
    with pytest.raises(PrepareBoundaryError) as exc:
        _prepare_service(clock=lambda: past_expiry).prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert exc.value.error_code is PrepareErrorCode.ADVICE_STALE


def test_prepare_before_anchor_fails_closed() -> None:
    """A clock before the trusted anchor cannot validly prepare."""
    advice = _advice_service(_current(), _bounds(), clock=lambda: CLOCK_EARLY).advise(_proposal(), _advice_context())
    before = OBSERVED_AT - timedelta(minutes=1)
    with pytest.raises(PrepareBoundaryError) as exc:
        _prepare_service(clock=lambda: before).prepare(advice, _prepare_context(), idempotency_token=TOKEN)
    assert exc.value.error_code is PrepareErrorCode.ADVICE_STALE


def test_advice_fails_closed_when_observation_already_expired() -> None:
    """A stale current-state revision cannot even produce advice."""
    service = _advice_service(_current(), _bounds(), clock=lambda: OBS_EXPIRES_AT + timedelta(minutes=1))
    with pytest.raises(AdviceBoundaryError) as exc:
        service.advise(_proposal(), _advice_context())
    assert exc.value.error_code is AdviceErrorCode.CURRENT_STATE_STALE


# -- Changed current-state revision -> distinct hash -------------------------


def test_changed_observation_revision_yields_distinct_hash_even_on_same_clock() -> None:
    _, low = _advise_then_prepare(
        current=_current(desired=10),
        bounds=_bounds(),
        advise_clock=lambda: CLOCK_EARLY,
        prepare_clock=lambda: CLOCK_EARLY,
    )
    high_obs = CurrentCapacity(
        observation_id="obs_bbbbbbbbbbbbbbbbbbbbbbbbbb",
        observation_hash="sha256:" + "1" * 64,
        capacity=CapacityValues(desired=12, minimum=2, maximum=20),
        observed_at=OBSERVED_AT,
        expires_at=OBS_EXPIRES_AT,
    )
    _, high = _advise_then_prepare(
        current=high_obs, bounds=_bounds(), advise_clock=lambda: CLOCK_EARLY, prepare_clock=lambda: CLOCK_EARLY
    )
    assert low.prepared_hash != high.prepared_hash
    assert low.operation["operation_id"] != high.operation["operation_id"]


def test_changed_observation_anchor_changes_prepared_bytes() -> None:
    """Two revisions with different observed_at anchors produce different bytes."""
    _, first = _advise_then_prepare(
        current=_current(observed_at=OBSERVED_AT, expires_at=OBS_EXPIRES_AT),
        bounds=_bounds(),
        advise_clock=lambda: CLOCK_EARLY,
        prepare_clock=lambda: CLOCK_EARLY,
    )
    later_anchor = OBSERVED_AT + timedelta(minutes=1)
    _, second = _advise_then_prepare(
        current=_current(observed_at=later_anchor, expires_at=later_anchor + OBS_TTL),
        bounds=_bounds(),
        advise_clock=lambda: CLOCK_LATER,
        prepare_clock=lambda: CLOCK_LATER,
    )
    assert first.operation["created_at"] != second.operation["created_at"]
    assert first.prepared_hash != second.prepared_hash
