"""Production-factory cross-clock idempotency tests for advice_id (#414).

The cross-clock idempotency suite injects a fixed ``advice_id_factory`` so it can
compare byte-for-byte output. That masks the ``advice_id`` derivation itself:
whatever the *production default* does is never exercised there. These tests
drive ``AdviceService``/``PrepareService`` with **no injected factory**, so the
production ``advice_id`` derivation is the thing under test.

The contract:

* Recomputing advice for one trusted observation revision on a later clock, with
  the production factory, yields a byte-identical ``advice_id`` (and therefore a
  byte-identical advice document, prepared operation, and ``prepared_hash``).
* ``advice_id`` is a pure function of the trusted workspace, the exact untrusted
  proposal intent (idempotency token + fleet/location + requested triple), the
  code-selected capability, and the trusted current observation revision
  (id + hash). Changing any of those changes ``advice_id``; changing only the
  wall clock does not.

They fail on any implementation whose default ``advice_id`` is random (e.g.
``uuid4``), because two runs on different clocks then differ.
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
from operations.identity import ApprovalIdentityBoundary, VerifiedPrincipal
from operations.prepare import (
    CapacityPlaybook,
    PrepareRequestContext,
    PrepareService,
)
from operations.settings import resolve_operations_settings

pytestmark = pytest.mark.unit

OBSERVED_AT = datetime(2026, 9, 21, 19, 11, 52, tzinfo=timezone.utc)
OBS_TTL = timedelta(seconds=1800)
OBS_EXPIRES_AT = OBSERVED_AT + OBS_TTL

CLOCK_EARLY = OBSERVED_AT
CLOCK_LATER = OBSERVED_AT + timedelta(minutes=5)
CLOCK_LATEST = OBSERVED_AT + timedelta(minutes=20)

FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
LOCATION = "us-west-2"
TOKEN = "idem_abcdefghijklmnopqrstuvwx"
OTHER_TOKEN = "idem_zyxwvutsrqponmlkjihgfe"
OBS_HASH = "sha256:" + "0" * 64
PREPARE_TTL_SECONDS = 900

_ADVICE_ID_PATTERN = __import__("re").compile(r"^adv_[a-z0-9]{26}$")


def _settings(mode: str = "advise"):
    return resolve_operations_settings({"GBAW_OPERATIONS_MODE": mode})


def _boundary(workspace_id: str = "workspace.default") -> ApprovalIdentityBoundary:
    return ApprovalIdentityBoundary(
        tenant_id="tenant.default",
        workspace_id=workspace_id,
        requester_client_ids=frozenset({"client.web-console"}),
        approver_client_ids=frozenset({"client.approver"}),
        trusted_audiences=frozenset({"gbaw-operations"}),
    )


def _principal(workspace_id: str = "workspace.default") -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject_id="subject.operator-1",
        client_id="client.web-console",
        audience="gbaw-operations",
        tenant_id="tenant.default",
        workspace_id=workspace_id,
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
    desired=10,
    minimum=2,
    maximum=20,
    *,
    observed_at=OBSERVED_AT,
    expires_at=OBS_EXPIRES_AT,
    observation_id="obs_aaaaaaaaaaaaaaaaaaaaaaaaaa",
    observation_hash=OBS_HASH,
) -> CurrentCapacity:
    return CurrentCapacity(
        observation_id=observation_id,
        observation_hash=observation_hash,
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


def _proposal(
    desired=14, minimum=2, maximum=20, *, token=TOKEN, fleet=FLEET, location=LOCATION
) -> CapacityProposalRequest:
    return CapacityProposalRequest(
        fleet_id=fleet,
        location=location,
        requested=CapacityValues(desired=desired, minimum=minimum, maximum=maximum),
        idempotency_token=token,
    )


def _advice_context(workspace_id: str = "workspace.default") -> AdviceRequestContext:
    return AdviceRequestContext(
        requester=_principal(workspace_id),
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


def _advice_service_default(
    current: CurrentCapacity, bounds: CapacityBounds, *, clock, workspace_id="workspace.default"
):
    """Build the service with the PRODUCTION default advice_id derivation."""
    return AdviceService(
        settings=_settings("advise"),
        identity_boundary=_boundary(workspace_id),
        state_port=FakeStatePort(current),
        bounds_port=FakeBoundsPort(bounds),
        clock=clock,
        # No advice_id_factory -> exercise the production default.
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


# -- advice_id is deterministic under the production factory ------------------


def test_production_advice_id_is_identical_across_clocks() -> None:
    """The production default advice_id is byte-identical when recomputed later."""
    early = _advice_service_default(_current(), _bounds(), clock=lambda: CLOCK_EARLY).advise(
        _proposal(), _advice_context()
    )
    later = _advice_service_default(_current(), _bounds(), clock=lambda: CLOCK_LATEST).advise(
        _proposal(), _advice_context()
    )
    assert _ADVICE_ID_PATTERN.fullmatch(early["advice_id"])
    assert early["advice_id"] == later["advice_id"]
    # The whole advice document (which embeds advice_id) is byte-identical.
    assert early == later


def test_production_advise_then_prepare_is_identical_across_clocks() -> None:
    """Full advise->prepare with the production factory is idempotent across clocks."""
    advice_a = _advice_service_default(_current(), _bounds(), clock=lambda: CLOCK_EARLY).advise(
        _proposal(), _advice_context()
    )
    prepared_a = _prepare_service(clock=lambda: CLOCK_EARLY).prepare(
        advice_a, _prepare_context(), idempotency_token=TOKEN
    )
    advice_b = _advice_service_default(_current(), _bounds(), clock=lambda: CLOCK_LATER).advise(
        _proposal(), _advice_context()
    )
    prepared_b = _prepare_service(clock=lambda: CLOCK_LATER).prepare(
        advice_b, _prepare_context(), idempotency_token=TOKEN
    )
    assert advice_a == advice_b
    assert prepared_a.operation == prepared_b.operation
    assert prepared_a.prepared_hash == prepared_b.prepared_hash
    assert prepared_a.authorization == prepared_b.authorization


# -- Each trusted/untrusted input changes advice_id ---------------------------


def _advice_id_for(*, proposal=None, current=None, workspace_id="workspace.default") -> str:
    proposal = proposal or _proposal()
    current = current or _current()
    service = _advice_service_default(current, _bounds(), clock=lambda: CLOCK_EARLY, workspace_id=workspace_id)
    return service.advise(proposal, _advice_context(workspace_id))["advice_id"]


def test_changed_idempotency_token_changes_advice_id() -> None:
    base = _advice_id_for(proposal=_proposal(token=TOKEN))
    other = _advice_id_for(proposal=_proposal(token=OTHER_TOKEN))
    assert base != other


def test_changed_requested_intent_changes_advice_id() -> None:
    base = _advice_id_for(proposal=_proposal(desired=14))
    other = _advice_id_for(proposal=_proposal(desired=15))
    assert base != other


def test_changed_workspace_changes_advice_id() -> None:
    base = _advice_id_for(workspace_id="workspace.default")
    other = _advice_id_for(workspace_id="workspace.other")
    assert base != other


def test_changed_observation_revision_changes_advice_id() -> None:
    base = _advice_id_for(current=_current(observation_hash="sha256:" + "0" * 64))
    other = _advice_id_for(current=_current(observation_hash="sha256:" + "1" * 64))
    assert base != other


def test_changed_observation_id_changes_advice_id() -> None:
    base = _advice_id_for(current=_current(observation_id="obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"))
    other = _advice_id_for(current=_current(observation_id="obs_bbbbbbbbbbbbbbbbbbbbbbbbbb"))
    assert base != other
