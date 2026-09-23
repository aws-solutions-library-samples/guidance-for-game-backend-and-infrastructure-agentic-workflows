"""Identifier-only dispatch + emergency-disablement tests for E5 autonomy (#439).

The dispatch layer is the *only* boundary between the deterministic autonomy
runtime and the existing narrow executor. It carries the ``operation_id`` and
nothing else — no policy, no limits, no observation, no credentials, and no
model or request-body input. A durable, authenticated Step Functions state
machine is the sole caller permitted to invoke the executor; the runtime itself
holds no provider-write permission and no executor credential.

Emergency disablement is a hard, fail-closed gate that is checked *before*
dispatch and again *immediately before* the provider write. When the emergency
switch is engaged, dispatch is refused and no execution envelope is produced.

These tests assert:

* the dispatch envelope carries the operation id only (identifier-only);
* an authorized decision is required before an envelope is built;
* emergency disablement blocks dispatch (checked before dispatch);
* the pre-write gate contract exists and fails closed when engaged;
* the layer carries no credential/policy/limits/observation fields.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_runtime import (
    AutonomyRuntimeInputs,
    AutonomyRuntimeService,
    DispatchEnvelope,
    DispatchOutcome,
    DispatchRefused,
    EmergencyDisablement,
    build_dispatch_envelope,
)
from operations.contracts import load_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"

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
_EVALUATED_AT = datetime(2026, 9, 21, 19, 12, 52, tzinfo=timezone.utc)


def _prepare(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "policy": load_json(FIXTURES / "gamelift-capacity-autonomy-policy.valid.json"),
        "observation": load_json(FIXTURES / "gamelift-autonomy-observation.valid.json"),
        "authority_inputs": dict(_AUTHORITY),
        "automation_principal": dict(_PRINCIPAL),
        "window_state": load_json(FIXTURES / "gamelift-capacity-autonomy-window-state.valid.json"),
        "requested": {"desired": 1, "minimum": 0, "maximum": 1},
        "correlation": dict(_CORRELATION),
        "evaluated_at": _EVALUATED_AT,
    }
    kwargs.update(overrides)
    return AutonomyRuntimeService().prepare(AutonomyRuntimeInputs(**kwargs))


class _Switch(EmergencyDisablement):
    def __init__(self, engaged: bool) -> None:
        self._engaged = engaged
        self.checks = 0

    def is_engaged(self) -> bool:
        self.checks += 1
        return self._engaged


# -- Identifier-only dispatch envelope --------------------------------------


@pytest.mark.unit
def test_dispatch_envelope_carries_operation_id_only() -> None:
    prepared = _prepare()
    switch = _Switch(engaged=False)
    result = build_dispatch_envelope(prepared, emergency=switch)
    assert result.outcome is DispatchOutcome.DISPATCHED
    envelope = result.envelope
    assert isinstance(envelope, DispatchEnvelope)
    assert envelope.operation_id == prepared.operation["operation_id"]
    # Identifier-only: the payload dict is exactly {"operation_id": ...}.
    assert envelope.payload() == {"operation_id": prepared.operation["operation_id"]}


@pytest.mark.unit
def test_dispatch_payload_leaks_no_policy_limits_or_credentials() -> None:
    prepared = _prepare()
    result = build_dispatch_envelope(prepared, emergency=_Switch(engaged=False))
    payload_keys = set(result.envelope.payload())
    assert payload_keys == {"operation_id"}
    for forbidden in ("policy", "policy_hash", "budget", "executor_credential", "observation", "window_state"):
        assert forbidden not in payload_keys


# -- Authorization required --------------------------------------------------


@pytest.mark.unit
def test_denied_decision_cannot_be_dispatched() -> None:
    authority = dict(_AUTHORITY)
    authority["deployment_mode"] = "disabled"
    prepared = _prepare(authority_inputs=authority)
    result = build_dispatch_envelope(prepared, emergency=_Switch(engaged=False))
    assert result.outcome is DispatchOutcome.REFUSED
    assert result.envelope is None
    assert result.reason == DispatchRefused.NOT_AUTHORIZED


# -- Emergency disablement checked before dispatch --------------------------


@pytest.mark.unit
def test_emergency_disablement_blocks_dispatch() -> None:
    prepared = _prepare()
    switch = _Switch(engaged=True)
    result = build_dispatch_envelope(prepared, emergency=switch)
    assert result.outcome is DispatchOutcome.REFUSED
    assert result.reason == DispatchRefused.EMERGENCY_DISABLED
    assert result.envelope is None
    # The gate was actually consulted before dispatch.
    assert switch.checks >= 1


@pytest.mark.unit
def test_emergency_gate_is_checked_before_authorization_shortcut() -> None:
    # Even an authorized decision must not dispatch when the switch is engaged.
    prepared = _prepare()
    switch = _Switch(engaged=True)
    result = build_dispatch_envelope(prepared, emergency=switch)
    assert result.outcome is DispatchOutcome.REFUSED
    assert switch.checks >= 1


# -- Pre-write gate contract -------------------------------------------------


@pytest.mark.unit
def test_pre_write_gate_refuses_when_engaged() -> None:
    prepared = _prepare()
    dispatched = build_dispatch_envelope(prepared, emergency=_Switch(engaged=False))
    assert dispatched.outcome is DispatchOutcome.DISPATCHED
    envelope = dispatched.envelope
    # Immediately before the provider write the gate is checked again.
    assert envelope.authorize_provider_write(emergency=_Switch(engaged=False)) is True
    assert envelope.authorize_provider_write(emergency=_Switch(engaged=True)) is False


@pytest.mark.unit
def test_envelope_holds_no_credential_or_policy_surface() -> None:
    prepared = _prepare()
    envelope = build_dispatch_envelope(prepared, emergency=_Switch(engaged=False)).envelope
    for forbidden in ("executor_credential", "credential", "policy", "client", "provider_client"):
        assert not hasattr(envelope, forbidden)
