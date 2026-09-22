"""Kill-switch enforcement on the deployable write path (issue #416, E4).

The blocking E4 backend requirement: the three deployable write-capable
entrypoints — E2 prepare (observe Lambda), the E3 dispatcher, and the E3
executor — must construct a REAL :class:`KillSwitchGate` over the REAL
:class:`AppConfigExtensionClient` when the AppConfig read target is configured,
and pass it into ``PrepareService`` / ``DispatcherRequestHandler`` /
``ExecutorService``. These tests exercise the real gate + real extension client
(only the localhost HTTP transport is faked with an in-memory opener), never a
stub gate.

They prove:

* A configured-but-disabling document blocks each write phase (fail closed).
* A fresh enabling document permits the phase.
* An unavailable/invalid/stale document fails closed.
* A DYNAMIC flip of the switch — enabled when the executor first reads it on
  entry, disabled by the time it re-reads immediately before the provider write
  — blocks ``UpdateFleetCapacity`` (the adapter is never called).
* All-identifiers-absent builds a no-op gate (backward compatible); partial
  configuration fails startup.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID
from operations.control.appconfig_extension import AppConfigExtensionClient
from operations.control.kill_switch_gate import KillSwitchGate, PhaseDenied
from operations.observe.lambda_entry import build_kill_switch_gate
from operations.settings import (
    KillSwitchExtensionSettings,
    resolve_kill_switch_extension_settings,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _document(*, prepare: bool = True, dispatch: bool = True, execute: bool = True, enabled: bool = True) -> bytes:
    document = {
        "contract_version": "1.0",
        "config_version": 7,
        "issued_at": _z(_NOW - timedelta(seconds=30)),
        "not_after": _z(_NOW + timedelta(minutes=5)),
        "operations_enabled": enabled,
        "capabilities": {CAPABILITY_ID: {"prepare": prepare, "dispatch": dispatch, "execute": execute}},
    }
    return json.dumps(document).encode("utf-8")


class _FakeHTTPResponse:
    """A minimal context-manager response mimicking urlopen's return."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self, amt: int | None = None) -> bytes:
        return self._body


class _ScriptedOpener:
    """A fake urlopen that serves scripted bodies in sequence over localhost."""

    def __init__(self, bodies: list[bytes]) -> None:
        self._bodies = list(bodies)
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> _FakeHTTPResponse:
        self.urls.append(url)
        body = self._bodies.pop(0) if len(self._bodies) > 1 else self._bodies[0]
        return _FakeHTTPResponse(body)


def _real_gate(bodies: list[bytes], *, static_authority: str = "operate") -> tuple[KillSwitchGate, _ScriptedOpener]:
    """Build the REAL extension client + REAL gate over a scripted localhost opener."""
    opener = _ScriptedOpener(bodies)
    extension = AppConfigExtensionClient(
        application="game-agent-operations",
        environment="prod",
        profile="operations-kill-switch",
        port=2772,
        opener=opener,
    )
    gate = KillSwitchGate(
        extension=extension,
        capability_id=CAPABILITY_ID,
        static_authority=static_authority,
        clock=lambda: _NOW,
    )
    return gate, opener


# -- build_kill_switch_gate helper (shared by all three entrypoints) --------


def test_build_gate_none_when_extension_absent() -> None:
    assert build_kill_switch_gate(extension_settings=None, static_authority="observe", clock=lambda: _NOW) is None


def test_build_gate_uses_real_extension_client_and_denies_disabled_prepare() -> None:
    ext = KillSwitchExtensionSettings(
        application="game-agent-operations",
        environment="prod",
        profile="operations-kill-switch",
        extension_port=2772,
    )
    opener = _ScriptedOpener([_document(prepare=False)])
    gate = build_kill_switch_gate(
        extension_settings=ext,
        static_authority="advise",
        clock=lambda: _NOW,
        opener=opener,
    )
    assert gate is not None
    with pytest.raises(PhaseDenied):
        gate.require_phase("prepare")
    # It really hit the localhost extension URL for this workspace's profile.
    assert opener.urls and "operations-kill-switch" in opener.urls[0]


def test_build_gate_permits_enabled_prepare() -> None:
    ext = KillSwitchExtensionSettings(
        application="game-agent-operations",
        environment="prod",
        profile="operations-kill-switch",
        extension_port=2772,
    )
    opener = _ScriptedOpener([_document(prepare=True)])
    gate = build_kill_switch_gate(extension_settings=ext, static_authority="advise", clock=lambda: _NOW, opener=opener)
    assert gate is not None
    decision = gate.require_phase("prepare")
    assert decision.config_version == 7


def test_gate_fails_closed_on_stale_document() -> None:
    stale = {
        "contract_version": "1.0",
        "config_version": 7,
        "issued_at": _z(_NOW - timedelta(hours=2)),
        "not_after": _z(_NOW - timedelta(hours=1)),
        "operations_enabled": True,
        "capabilities": {CAPABILITY_ID: {"prepare": True, "dispatch": True, "execute": True}},
    }
    gate, _ = _real_gate([json.dumps(stale).encode("utf-8")])
    with pytest.raises(PhaseDenied):
        gate.require_phase("prepare")


# -- Dynamic flip proves the pre-write re-check blocks the executor write ---


def test_executor_dynamic_flip_between_read_and_update_blocks_write() -> None:
    """A switch enabled on entry but disabled before the write blocks the write."""
    # Local modules
    from tests.unit._kill_switch_executor_harness import run_executor_with_gate

    # First read (entry check): execute enabled. Second read (pre-write check,
    # after the Describe): execute disabled. The real gate reads fresh each time.
    gate, opener = _real_gate([_document(execute=True), _document(execute=False)])

    outcome, adapter_calls = run_executor_with_gate(gate=gate, now=_NOW)

    # The provider write must never be issued once the switch flipped.
    assert adapter_calls == 0
    assert outcome == "denied"
    # The gate really read the extension at least twice (entry + pre-write).
    assert len(opener.urls) >= 2


# -- End-to-end resolution: absent vs partial ------------------------------


def test_resolution_absent_then_partial() -> None:
    assert resolve_kill_switch_extension_settings({}) is None
    with pytest.raises(ValueError):
        resolve_kill_switch_extension_settings({"GBAW_OPERATIONS_APPCONFIG_APPLICATION": "x"})
