"""Server-owned capability discovery projection (issue #416, E4).

:class:`CapabilityDiscoveryService` computes the ``operations-capability-discovery``
document the UI reads before showing any operations control. It folds three
orthogonal facts into a single per-capability projection:

* **available** — the capability's code path exists in this build. E4 ships
  exactly one capability (``gamelift.capacity-adjustment``), so this is a
  build-time constant.
* **provisioned** — the deployment created the resources the capability needs
  (table, state machine, AppConfig profile). A static, deploy-time gate.
* **enabled** — the runtime kill-switch AND the effective authority currently
  permit the capability. A dynamic, per-request gate evaluated by reading the
  kill-switch fresh through the gate; an unavailable/invalid/stale switch fails
  closed to disabled.

The projection distinguishes static gates (fixed at build/deploy time) from
dynamic gates (evaluated per request). It is a hint only — the backend re-checks
every gate at prepare/dispatch/execute — and it carries no identity, credential,
ARN, account, or provider payload.
"""

from __future__ import annotations

# Standard library
from datetime import datetime, timezone
from typing import Any, Callable

# Local modules
from operations.contracts import CONTRACT_VERSION
from operations.contracts.capacity import CAPABILITY_VERSION
from operations.contracts.control_plane import (
    CAPABILITY_DISCOVERY_SCHEMA_NAME,
    CAPABILITY_ID,
    CONTROL_PHASES,
    ControlContractError,
    validate_control_contract,
)
from operations.control.kill_switch_gate import KillSwitchUnavailable

_AUTHORITY_ORDER = {"disabled": 0, "observe": 1, "advise": 2, "remediate": 3, "operate": 4}
# Per-phase minimum static authority (matches the kill-switch gate mapping):
# prepare/dispatch are advise-authority; execute is the remediate write.
_PHASE_MINIMUM = {"prepare": "advise", "dispatch": "advise", "execute": "remediate"}


class CapabilityDiscoveryError(RuntimeError):
    """Discovery could not be produced (fail closed; server-owned output bug)."""


class CapabilityDiscoveryService:
    """Compute the server-owned capability discovery projection."""

    def __init__(
        self,
        *,
        kill_switch_gate: Any,
        deployment_mode: str,
        provisioned: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if deployment_mode not in _AUTHORITY_ORDER:
            raise ValueError("deployment_mode must be a valid ADR 0001 authority")
        self._gate = kill_switch_gate
        self._deployment_mode = deployment_mode
        self._provisioned = bool(provisioned)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def discover(self) -> dict[str, Any]:
        """Return the validated capability discovery document."""
        decision, operations_enabled, config_version, switch_phases = self._read_switch()

        authority_admits = _AUTHORITY_ORDER[self._deployment_mode] >= _AUTHORITY_ORDER["advise"]
        base_enabled = self._provisioned and operations_enabled and authority_admits
        # Effective per-phase enablement folds provisioning + authority + switch,
        # plus the per-phase execute floor (execute requires remediate). A phase
        # reported here is one the backend would actually permit right now.
        effective_phases = {
            phase: base_enabled
            and switch_phases[phase]
            and _AUTHORITY_ORDER[self._deployment_mode] >= _AUTHORITY_ORDER[_PHASE_MINIMUM[phase]]
            for phase in CONTROL_PHASES
        }

        capability = {
            "capability_id": CAPABILITY_ID,
            "capability_version": CAPABILITY_VERSION,
            "available": True,
            "provisioned": self._provisioned,
            "enabled": any(effective_phases.values()),
            "effective_authority": self._deployment_mode,
            "phases": effective_phases,
            "gates": self._gates(operations_enabled, switch_phases),
        }
        document: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "generated_at": self._now_iso(),
            "deployment_mode": self._deployment_mode,
            "operations_enabled": operations_enabled,
            "capabilities": [capability],
        }
        if config_version is not None:
            document["kill_switch_config_version"] = config_version

        try:
            validate_control_contract(CAPABILITY_DISCOVERY_SCHEMA_NAME, document)
        except ControlContractError as exc:  # pragma: no cover - server-owned output
            raise CapabilityDiscoveryError("discovery projection failed its contract") from exc
        return document

    # -- internals -------------------------------------------------------

    def _read_switch(self) -> tuple[Any, bool, int | None, dict[str, bool]]:
        """Read the kill-switch fresh, failing closed to disabled on any error."""
        try:
            decision = self._gate.evaluate()
        except KillSwitchUnavailable:
            # Fail closed: an unreadable/invalid/stale switch reports everything
            # disabled with no config_version.
            return None, False, None, {phase: False for phase in CONTROL_PHASES}
        phases = {phase: bool(decision.phase_allowed(phase)) for phase in CONTROL_PHASES}
        return decision, bool(decision.operations_enabled), int(decision.config_version), phases

    def _gates(self, operations_enabled: bool, phases: dict[str, bool]) -> list[dict[str, Any]]:
        return [
            {"gate_id": "deployment.provisioned", "kind": "static", "satisfied": self._provisioned},
            {
                "gate_id": "deployment.authority",
                "kind": "static",
                "satisfied": _AUTHORITY_ORDER[self._deployment_mode] >= _AUTHORITY_ORDER["advise"],
            },
            {"gate_id": "kill_switch.operations_enabled", "kind": "dynamic", "satisfied": operations_enabled},
            {"gate_id": "kill_switch.prepare", "kind": "dynamic", "satisfied": phases["prepare"]},
            {"gate_id": "kill_switch.dispatch", "kind": "dynamic", "satisfied": phases["dispatch"]},
            {"gate_id": "kill_switch.execute", "kind": "dynamic", "satisfied": phases["execute"]},
        ]

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
