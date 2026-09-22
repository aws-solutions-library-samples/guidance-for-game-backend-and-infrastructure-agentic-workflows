"""Capability discovery service tests (issue #416, E4).

:class:`~operations.control.capability_discovery.CapabilityDiscoveryService`
computes the server-owned ``operations-capability-discovery`` projection the UI
reads before showing any control. It distinguishes:

* **available** — the capability's code path exists in this build (always true
  for the one supported capability);
* **provisioned** — the deployment created the resources (static gate);
* **enabled** — the runtime kill-switch AND the effective authority currently
  permit the capability (dynamic gate);

and it distinguishes static gates (fixed at build/deploy) from dynamic gates
(evaluated per request). A hidden or disabled control is never authorization;
this is a hint only.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import (
    CAPABILITY_DISCOVERY_SCHEMA_NAME,
    CAPABILITY_ID,
    validate_control_contract,
)
from operations.control.capability_discovery import CapabilityDiscoveryService

pytestmark = [pytest.mark.unit, pytest.mark.fast]

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _GateDecision:
    def __init__(self, *, operations_enabled: bool, config_version: int, phases: dict[str, bool]) -> None:
        self.operations_enabled = operations_enabled
        self.config_version = config_version
        self._phases = phases

    def phase_allowed(self, phase: str) -> bool:
        return self._phases.get(phase, False)


class _GateStub:
    def __init__(self, decision: Any = None, *, unavailable: bool = False) -> None:
        self._decision = decision
        self._unavailable = unavailable

    def evaluate(self) -> Any:
        if self._unavailable:
            from operations.control.kill_switch_gate import KillSwitchUnavailable

            raise KillSwitchUnavailable("down")
        return self._decision


def _service(gate: Any, *, mode: str = "remediate", provisioned: bool = True) -> CapabilityDiscoveryService:
    return CapabilityDiscoveryService(
        kill_switch_gate=gate,
        deployment_mode=mode,
        provisioned=provisioned,
        clock=lambda: _NOW,
    )


def test_discovery_is_valid_and_reports_enabled_when_switch_on() -> None:
    decision = _GateDecision(
        operations_enabled=True, config_version=4, phases={"prepare": True, "dispatch": True, "execute": True}
    )
    service = _service(_GateStub(decision))
    doc = service.discover()
    validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, doc)
    assert doc["operations_enabled"] is True
    assert doc["kill_switch_config_version"] == 4
    cap = doc["capabilities"][0]
    assert cap["capability_id"] == CAPABILITY_ID
    assert cap["available"] is True
    assert cap["provisioned"] is True
    assert cap["enabled"] is True
    assert cap["phases"] == {"prepare": True, "dispatch": True, "execute": True}


def test_switch_off_reports_disabled_but_still_available() -> None:
    decision = _GateDecision(
        operations_enabled=False, config_version=2, phases={"prepare": False, "dispatch": False, "execute": False}
    )
    service = _service(_GateStub(decision))
    doc = service.discover()
    validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, doc)
    assert doc["operations_enabled"] is False
    cap = doc["capabilities"][0]
    assert cap["available"] is True
    assert cap["enabled"] is False


def test_unavailable_switch_fails_closed_to_disabled() -> None:
    service = _service(_GateStub(unavailable=True))
    doc = service.discover()
    validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, doc)
    assert doc["operations_enabled"] is False
    cap = doc["capabilities"][0]
    # Fail closed: an unreadable switch reports every phase disabled.
    assert cap["enabled"] is False
    assert cap["phases"] == {"prepare": False, "dispatch": False, "execute": False}
    assert "kill_switch_config_version" not in doc


def test_not_provisioned_reports_provisioned_false_and_disabled() -> None:
    decision = _GateDecision(
        operations_enabled=True, config_version=3, phases={"prepare": True, "dispatch": True, "execute": True}
    )
    service = _service(_GateStub(decision), provisioned=False)
    doc = service.discover()
    validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, doc)
    cap = doc["capabilities"][0]
    assert cap["provisioned"] is False
    # Not provisioned: not enabled regardless of the switch.
    assert cap["enabled"] is False


def test_gates_distinguish_static_and_dynamic() -> None:
    decision = _GateDecision(
        operations_enabled=True, config_version=1, phases={"prepare": True, "dispatch": True, "execute": True}
    )
    service = _service(_GateStub(decision))
    doc = service.discover()
    gates = {gate["gate_id"]: gate for gate in doc["capabilities"][0]["gates"]}
    kinds = {gate["kind"] for gate in gates.values()}
    assert "static" in kinds and "dynamic" in kinds


def test_carries_no_identity_or_provider_payload() -> None:
    decision = _GateDecision(
        operations_enabled=True, config_version=1, phases={"prepare": True, "dispatch": True, "execute": True}
    )
    doc = _service(_GateStub(decision)).discover()
    blob = repr(doc)
    for secret in ("arn:", "account", "fleet-", "token", "@"):
        assert secret not in blob
