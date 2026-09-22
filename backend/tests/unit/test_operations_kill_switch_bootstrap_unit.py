"""Bootstrap wiring for the fail-closed kill-switch on the write path (#416).

These tests pin the *settings* contract the three deployable write-capable
entrypoints rely on to construct a real :class:`KillSwitchGate`:

* ``resolve_kill_switch_extension_settings`` returns ``None`` when ALL three
  AppConfig identifiers are absent (a pre-E4 deployment stays backward
  compatible and builds a no-op gate);
* it FAILS CLOSED (raises) on partial configuration (some identifiers present,
  others missing), so a half-configured deployment can never start with the
  kill-switch silently disabled;
* it returns a validated settings object (application/environment/profile/port)
  when every identifier is present; and
* the dedicated control-mode (``GBAW_OPERATIONS_CONTROL_MODE``) is independent of
  the global operations mode, so admin controls remain reachable to recover
  operations while static execution is disabled.
"""

from __future__ import annotations

# Third-party packages
import pytest

# Local modules
from operations.settings import (
    resolve_control_mode,
    resolve_kill_switch_extension_settings,
)

pytestmark = pytest.mark.unit

_FULL_APPCONFIG = {
    "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "game-agent-operations",
    "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
    "GBAW_OPERATIONS_APPCONFIG_PROFILE": "operations-kill-switch",
    "GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT": "2772",
}


def test_all_identifiers_absent_returns_none() -> None:
    assert resolve_kill_switch_extension_settings({}) is None


def test_full_configuration_resolves() -> None:
    settings = resolve_kill_switch_extension_settings(dict(_FULL_APPCONFIG))
    assert settings is not None
    assert settings.application == "game-agent-operations"
    assert settings.environment == "prod"
    assert settings.profile == "operations-kill-switch"
    assert settings.extension_port == 2772


def test_default_extension_port_when_omitted() -> None:
    env = {k: v for k, v in _FULL_APPCONFIG.items() if not k.endswith("EXTENSION_PORT")}
    settings = resolve_kill_switch_extension_settings(env)
    assert settings is not None
    assert settings.extension_port == 2772


@pytest.mark.parametrize(
    "present",
    [
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE",
    ],
)
def test_partial_configuration_fails_closed(present: str) -> None:
    env = {present: "some-value"}
    with pytest.raises(ValueError):
        resolve_kill_switch_extension_settings(env)


def test_control_mode_defaults_disabled() -> None:
    assert resolve_control_mode({}) == "disabled"


def test_control_mode_enabled_independent_of_operations_mode() -> None:
    env = {"GBAW_OPERATIONS_CONTROL_MODE": "enabled", "GBAW_OPERATIONS_MODE": "disabled"}
    assert resolve_control_mode(env) == "enabled"


def test_control_mode_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        resolve_control_mode({"GBAW_OPERATIONS_CONTROL_MODE": "on"})
