"""Control-plane availability uses a dedicated mode (issue #416, E4).

Admin controls (list/detail/kill-switch read + the CAS control write) must stay
available to RECOVER operations even while static execution
(``GBAW_OPERATIONS_MODE``) is disabled. The control plane's availability is
therefore governed by a dedicated ``GBAW_OPERATIONS_CONTROL_MODE``
(enabled|disabled), independent of the global operations mode, and the control
deployment settings resolve without requiring ``GBAW_OPERATIONS_MODE``.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.settings import resolve_control_plane_deployment_settings

pytestmark = pytest.mark.unit

_BASE = {
    "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant-default",
    "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-default",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "trusted-app-client",
    "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "game-agent-operations",
    "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
    "GBAW_OPERATIONS_APPCONFIG_PROFILE": "operations-kill-switch",
    "GBAW_OPERATIONS_APPCONFIG_GRADUAL_STRATEGY_ID": "strategy-gradual",
    "GBAW_OPERATIONS_APPCONFIG_IMMEDIATE_STRATEGY_ID": "strategy-immediate",
    "GBAW_OPERATIONS_CURSOR_SIGNING_KEY": "a-sufficiently-long-signing-key",
    # Deliberately NO GBAW_OPERATIONS_MODE.
}


def test_control_settings_resolve_without_operations_mode() -> None:
    env = dict(_BASE)
    env["GBAW_OPERATIONS_CONTROL_MODE"] = "enabled"
    settings = resolve_control_plane_deployment_settings(env)
    # The global operations mode stays disabled (static execution off) ...
    assert settings.observation.mode == "disabled"
    # ... while the dedicated control mode keeps admin controls available.
    assert settings.control_mode == "enabled"
    assert settings.control_enabled is True


def test_control_mode_defaults_disabled() -> None:
    settings = resolve_control_plane_deployment_settings(dict(_BASE))
    assert settings.control_mode == "disabled"
    assert settings.control_enabled is False


def test_control_mode_independent_of_execution_enable() -> None:
    env = dict(_BASE)
    env["GBAW_OPERATIONS_CONTROL_MODE"] = "enabled"
    env["GBAW_OPERATIONS_MODE"] = "disabled"  # static execution disabled
    settings = resolve_control_plane_deployment_settings(env)
    assert settings.control_enabled is True
    assert settings.observation.operations.operations_enabled is False


def test_control_entry_fails_closed_when_control_mode_disabled(monkeypatch: "pytest.MonkeyPatch") -> None:
    # Standard library
    import importlib

    for key, value in _BASE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GBAW_OPERATIONS_MODE", "remediate")
    monkeypatch.delenv("GBAW_OPERATIONS_CONTROL_MODE", raising=False)  # defaults disabled

    module = importlib.import_module("operations.control.control_entry")
    module._runtime.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        module.handler(
            {"requestContext": {"http": {"method": "GET", "path": "/operations/capabilities"}}},
            None,
        )
    module._runtime.cache_clear()  # type: ignore[attr-defined]
