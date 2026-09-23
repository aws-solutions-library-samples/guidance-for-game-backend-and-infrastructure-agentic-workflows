"""Composite bounded-autonomy runtime gate + settings (E5 track C of #439).

These tests pin the two composite decision points the future durable Step
Functions workflow consults — never the model, never the chat path:

* ``require_pre_dispatch`` — checked before an autonomous dispatch. It permits
  only when ALL of: static deployment mode is exactly ``operate``; a fresh,
  separate AppConfig autonomy switch is enabled; the E4 kill-switch ``dispatch``
  phase allows; and the E4 durable control intent allows ``dispatch``. Any
  missing/malformed/stale/conflicting source denies (fail closed).

* ``require_pre_provider_write`` — checked immediately before the single provider
  write. It re-checks emergency disablement (autonomy switch + E4 ``execute``
  phase + durable ``execute``), runs the pure deterministic evaluator over the
  trusted inputs, and then ATOMICALLY reserves budget/cooldown/frequency/
  concurrency/tenant/workspace/enrollment/policy/observation/state through an
  injected durable reservation port. Any denial or reservation failure fails
  closed. The gate holds no provider-write permission and performs no write.

The gate is default-disabled: with no autonomy configuration it never permits.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.autonomy_gate import (
    AutonomyGateDenied,
    AutonomyRuntimeGate,
    AutonomyRuntimeSettings,
    resolve_autonomy_runtime_settings,
)
from operations.autonomy_switch import AutonomySwitchUnavailable

pytestmark = [pytest.mark.unit, pytest.mark.fast]


# -- Fakes ----------------------------------------------------------------


class _SwitchGate:
    def __init__(self, *, allowed: bool = True, unavailable: bool = False) -> None:
        self._allowed = allowed
        self._unavailable = unavailable
        self.calls = 0

    def require_enabled(self) -> Any:
        self.calls += 1
        if self._unavailable or not self._allowed:
            raise AutonomySwitchUnavailable("switch off")
        return _obj(config_version=1)


class _KillDecision:
    def __init__(self, version: int = 4) -> None:
        self.config_version = version


class _KillGate:
    def __init__(self, *, denied: bool = False) -> None:
        self._denied = denied
        self.phases: list[str] = []

    def require_phase(self, phase: str) -> Any:
        self.phases.append(phase)
        if self._denied:
            raise RuntimeError("phase denied")
        return _KillDecision()


class _DurableGate:
    def __init__(self, *, denied: bool = False) -> None:
        self._denied = denied
        self.phases: list[str] = []

    def require_phase(self, phase: str, *, deployed_decision: Any) -> None:
        self.phases.append(phase)
        if self._denied:
            raise RuntimeError("durable denied")


class _Evaluator:
    """Stands in for the pure ``evaluate_autonomy_policy`` reading."""

    def __init__(self, *, decision: str = "authorized", reasons: list[str] | None = None) -> None:
        self._result = {
            "decision": decision,
            "reason_codes": reasons or (["APPROVED_AUTONOMOUS"] if decision == "authorized" else ["BUDGET_EXCEEDED"]),
            "effective_authority": "operate",
        }
        self.calls = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return dict(self._result)


class _Reservation:
    def __init__(self, *, ok: bool = True, error: Exception | None = None) -> None:
        self._ok = ok
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def reserve(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._ok


def _obj(**kwargs: Any) -> Any:
    return type("_O", (), kwargs)()


def _settings(*, enabled: bool = True, mode: str = "operate") -> AutonomyRuntimeSettings:
    """Build a settings object, bypassing the invariant only to simulate a
    tampered/corrupted object reaching the gate (for defense-in-depth tests)."""
    if enabled and mode != "operate":
        settings = AutonomyRuntimeSettings(enabled=False, static_deployment_mode="operate")
        object.__setattr__(settings, "enabled", True)
        object.__setattr__(settings, "static_deployment_mode", mode)
        return settings
    return AutonomyRuntimeSettings(enabled=enabled, static_deployment_mode=mode)


def _gate(
    *,
    settings: AutonomyRuntimeSettings | None = None,
    switch: _SwitchGate | None = None,
    kill: _KillGate | None = None,
    durable: _DurableGate | None = None,
    evaluator: Any = None,
    reservation: _Reservation | None = None,
) -> AutonomyRuntimeGate:
    return AutonomyRuntimeGate(
        settings=settings or _settings(),
        switch_gate=switch or _SwitchGate(),
        kill_switch_gate=kill or _KillGate(),
        durable_control_gate=durable or _DurableGate(),
        evaluator=evaluator or _Evaluator(),
        reservation_port=reservation or _Reservation(),
    )


def _write_inputs() -> dict[str, Any]:
    # Opaque trusted inputs; the fake evaluator ignores content. The gate must
    # forward these to the evaluator and the reservation, never to the model.
    return {
        "policy": {"policy_id": "p"},
        "authority_inputs": {"deployment_mode": "operate"},
        "automation_principal": {"source_type": "automation"},
        "observation": {"observation_id": "o"},
        "requested": {"desired": 1, "minimum": 1, "maximum": 1},
        "window_state": {"state_id": "s", "state_revision": 7},
        "now_epoch_seconds": 1790017999,
    }


# -- Settings: default disabled ------------------------------------------


def test_settings_default_disabled_when_unset() -> None:
    settings = resolve_autonomy_runtime_settings({})
    assert settings.enabled is False


def test_settings_disabled_when_flag_off() -> None:
    settings = resolve_autonomy_runtime_settings(
        {"GBAW_OPERATIONS_AUTONOMY_ENABLED": "false", "GBAW_OPERATIONS_MODE": "operate"}
    )
    assert settings.enabled is False


def test_settings_enabled_requires_operate_mode() -> None:
    settings = resolve_autonomy_runtime_settings(
        {"GBAW_OPERATIONS_AUTONOMY_ENABLED": "true", "GBAW_OPERATIONS_MODE": "operate"}
    )
    assert settings.enabled is True
    assert settings.static_deployment_mode == "operate"


def test_settings_enabled_denied_when_mode_not_operate() -> None:
    # Autonomy cannot be enabled unless the static deployment mode is exactly
    # operate; anything else fails closed at resolution.
    with pytest.raises(ValueError):
        resolve_autonomy_runtime_settings(
            {"GBAW_OPERATIONS_AUTONOMY_ENABLED": "true", "GBAW_OPERATIONS_MODE": "remediate"}
        )


def test_settings_enabled_denied_when_mode_observe() -> None:
    with pytest.raises(ValueError):
        resolve_autonomy_runtime_settings(
            {"GBAW_OPERATIONS_AUTONOMY_ENABLED": "true", "GBAW_OPERATIONS_MODE": "observe"}
        )


def test_settings_unrecognized_flag_token_fails_closed() -> None:
    with pytest.raises(ValueError):
        resolve_autonomy_runtime_settings({"GBAW_OPERATIONS_AUTONOMY_ENABLED": "maybe"})


# -- Disabled gate never permits -----------------------------------------


def test_disabled_gate_denies_pre_dispatch() -> None:
    gate = _gate(settings=_settings(enabled=False, mode="operate"))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_dispatch()


def test_gate_denies_when_mode_not_exactly_operate() -> None:
    # Defense in depth: even a tampered settings object with a non-operate mode
    # is denied by the gate's own static-authority guard.
    gate = _gate(settings=_settings(enabled=True, mode="remediate"))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_dispatch()


# -- Pre-dispatch composite ----------------------------------------------


def test_pre_dispatch_permits_when_all_sources_allow() -> None:
    switch, kill, durable = _SwitchGate(), _KillGate(), _DurableGate()
    gate = _gate(switch=switch, kill=kill, durable=durable)
    gate.require_pre_dispatch()
    assert switch.calls == 1
    assert kill.phases == ["dispatch"]
    assert durable.phases == ["dispatch"]


def test_pre_dispatch_denies_when_switch_off() -> None:
    gate = _gate(switch=_SwitchGate(allowed=False))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_dispatch()


def test_pre_dispatch_denies_when_switch_unavailable() -> None:
    gate = _gate(switch=_SwitchGate(unavailable=True))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_dispatch()


def test_pre_dispatch_denies_when_kill_switch_denies() -> None:
    gate = _gate(kill=_KillGate(denied=True))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_dispatch()


def test_pre_dispatch_denies_when_durable_denies() -> None:
    gate = _gate(durable=_DurableGate(denied=True))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_dispatch()


# -- Pre-provider-write composite ----------------------------------------


def test_pre_provider_write_permits_and_reserves_when_authorized() -> None:
    switch, kill, durable = _SwitchGate(), _KillGate(), _DurableGate()
    evaluator, reservation = _Evaluator(decision="authorized"), _Reservation(ok=True)
    gate = _gate(switch=switch, kill=kill, durable=durable, evaluator=evaluator, reservation=reservation)
    result = gate.require_pre_provider_write(**_write_inputs())
    assert result["decision"] == "authorized"
    # Emergency disablement re-checked at execute phase.
    assert kill.phases == ["execute"]
    assert durable.phases == ["execute"]
    assert evaluator.calls == 1
    # Atomic reservation happened exactly once, after authorization.
    assert len(reservation.calls) == 1


def test_pre_provider_write_denies_when_switch_off_before_write() -> None:
    evaluator, reservation = _Evaluator(), _Reservation()
    gate = _gate(switch=_SwitchGate(allowed=False), evaluator=evaluator, reservation=reservation)
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())
    # Emergency disablement is checked BEFORE the evaluator and reservation.
    assert evaluator.calls == 0
    assert reservation.calls == []


def test_pre_provider_write_denies_when_execute_phase_denied() -> None:
    evaluator, reservation = _Evaluator(), _Reservation()
    gate = _gate(kill=_KillGate(denied=True), evaluator=evaluator, reservation=reservation)
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())
    assert reservation.calls == []


def test_pre_provider_write_denies_when_durable_execute_denied() -> None:
    reservation = _Reservation()
    gate = _gate(durable=_DurableGate(denied=True), reservation=reservation)
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())
    assert reservation.calls == []


def test_pre_provider_write_denies_when_evaluator_denies() -> None:
    reservation = _Reservation()
    gate = _gate(evaluator=_Evaluator(decision="denied", reasons=["BUDGET_EXCEEDED"]), reservation=reservation)
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())
    # No reservation when the deterministic decision denies.
    assert reservation.calls == []


def test_pre_provider_write_denies_when_reservation_not_granted() -> None:
    gate = _gate(reservation=_Reservation(ok=False))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())


def test_pre_provider_write_fails_closed_when_reservation_errors() -> None:
    gate = _gate(reservation=_Reservation(error=RuntimeError("dynamo down")))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())


def test_pre_provider_write_denies_when_disabled() -> None:
    gate = _gate(settings=_settings(enabled=False, mode="operate"))
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())
