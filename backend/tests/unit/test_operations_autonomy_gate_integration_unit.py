"""End-to-end wiring of the E5 runtime gate over the REAL pure evaluator and the
REAL autonomy switch parser (E5 track C of #439).

Unlike ``test_operations_autonomy_gate_unit`` (which uses a fake evaluator), this
proves the composite gate authorizes a genuinely contract-valid autonomous write
through ``evaluate_autonomy_policy`` and reserves it, and that flipping the real,
separate autonomy switch document denies before the evaluator or reservation runs.
Money, hashes, and freshness are exercised through the published v2 fixtures.
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
from operations.autonomy_gate import (
    AutonomyGateDenied,
    AutonomyRuntimeGate,
    AutonomyRuntimeSettings,
)
from operations.autonomy_switch import AUTONOMY_SWITCH_CAPABILITY_ID, AutonomySwitchGate
from operations.contracts.autonomy import evaluate_autonomy_policy
from operations.contracts.canonical import load_json

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "operations" / "v2"
_NOW_EPOCH = 1_790_017_972
_NOW = datetime.fromtimestamp(_NOW_EPOCH, tz=timezone.utc)

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


def _load(name: str) -> dict[str, Any]:
    return load_json(_FIXTURES / f"{name}.valid.json")


def _switch_bytes(*, enabled: bool) -> bytes:
    document = {
        "autonomy_switch_version": "1.0",
        "config_version": 3,
        "issued_at": "2026-01-01T00:00:00Z",
        "not_after": "2100-01-01T00:00:00Z",
        "autonomy_enabled": enabled,
        "capabilities": {AUTONOMY_SWITCH_CAPABILITY_ID: {"autonomous_write": enabled}},
    }
    return json.dumps(document).encode("utf-8")


class _Extension:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def fetch_configuration(self) -> bytes:
        return self._raw


class _KillDecision:
    config_version = 4


class _KillGate:
    def require_phase(self, phase: str) -> Any:
        return _KillDecision()


class _DurableGate:
    def require_phase(self, phase: str, *, deployed_decision: Any) -> None:
        return None


class _Reservation:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reserve(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


def _build_gate(*, switch_enabled: bool, reservation: _Reservation) -> AutonomyRuntimeGate:
    switch_gate = AutonomySwitchGate(
        extension=_Extension(_switch_bytes(enabled=switch_enabled)),
        clock=lambda: _NOW,
    )
    return AutonomyRuntimeGate(
        settings=AutonomyRuntimeSettings(enabled=True, static_deployment_mode="operate"),
        switch_gate=switch_gate,
        kill_switch_gate=_KillGate(),
        durable_control_gate=_DurableGate(),
        evaluator=evaluate_autonomy_policy,
        reservation_port=reservation,
    )


def _write_inputs() -> dict[str, Any]:
    return {
        "policy": _load("gamelift-capacity-autonomy-policy"),
        "authority_inputs": dict(_AUTHORITY),
        "automation_principal": dict(_PRINCIPAL),
        "observation": _load("gamelift-capacity-autonomy-observation-evidence"),
        "requested": {"desired": 1, "minimum": 0, "maximum": 1},
        "window_state": _load("gamelift-capacity-autonomy-window-state"),
        "now_epoch_seconds": _NOW_EPOCH,
    }


def test_real_evaluator_authorizes_and_reserves_through_the_gate() -> None:
    reservation = _Reservation()
    gate = _build_gate(switch_enabled=True, reservation=reservation)
    result = gate.require_pre_provider_write(**_write_inputs())
    assert result["decision"] == "authorized"
    assert result["reason_codes"] == ["APPROVED_AUTONOMOUS"]
    assert result["effective_authority"] == "operate"
    assert len(reservation.calls) == 1


def test_real_switch_disabled_denies_before_evaluator_and_reservation() -> None:
    reservation = _Reservation()
    gate = _build_gate(switch_enabled=False, reservation=reservation)
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_provider_write(**_write_inputs())
    assert reservation.calls == []


def test_real_pre_dispatch_permits_with_enabled_switch() -> None:
    gate = _build_gate(switch_enabled=True, reservation=_Reservation())
    gate.require_pre_dispatch()


def test_real_pre_dispatch_denies_with_disabled_switch() -> None:
    gate = _build_gate(switch_enabled=False, reservation=_Reservation())
    with pytest.raises(AutonomyGateDenied):
        gate.require_pre_dispatch()
