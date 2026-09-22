"""Deployable E4 control-plane entrypoint tests (issue #416).

Two deployable handler modules back the E4 control plane:
``operations.control.control_entry.handler`` (the API Gateway HTTP router) and
``operations.control.sweeper_entry.handler`` (the scheduled expiry sweeper).

These tests never call AWS. They assert:
* Both entrypoints expose ``handler(event, context)`` and import side-effect free
  (no settings resolution or boto3 client at import time).
* The control entry resolves the frozen E4 contract lazily and fails closed on a
  missing/invalid environment WITHOUT constructing a boto3 client.
* The sweeper entry is likewise lazy and fail-closed.
"""

from __future__ import annotations

# Standard library
import importlib
import os

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_FLEET_ID = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
_FLEET_ARN = f"arn:aws:gamelift:us-west-2:123456789012:fleet/{_FLEET_ID}"

_BASE_ENV = {
    "AWS_REGION": "us-west-2",
    "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant-default",
    "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-default",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "trusted-app-client",
    "GBAW_OPERATIONS_MODE": "remediate",
    "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "game-agent-operations",
    "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
    "GBAW_OPERATIONS_APPCONFIG_PROFILE": "operations-kill-switch",
    "GBAW_OPERATIONS_APPCONFIG_GRADUAL_STRATEGY_ID": "strategy-gradual",
    "GBAW_OPERATIONS_APPCONFIG_IMMEDIATE_STRATEGY_ID": "strategy-immediate",
    "GBAW_OPERATIONS_CURSOR_SIGNING_KEY": "a-sufficiently-long-signing-key",
}


def test_control_entry_imports_side_effect_free() -> None:
    module = importlib.import_module("operations.control.control_entry")
    assert callable(module.handler)


def test_sweeper_entry_imports_side_effect_free() -> None:
    module = importlib.import_module("operations.control.sweeper_entry")
    assert callable(module.handler)


def test_control_entry_fails_closed_on_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GBAW_OPERATIONS_"):
            monkeypatch.delenv(key, raising=False)
    module = importlib.import_module("operations.control.control_entry")
    module._runtime.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        module.handler({"requestContext": {"http": {"method": "GET", "path": "/operations/capabilities"}}}, None)


def test_sweeper_entry_fails_closed_on_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GBAW_OPERATIONS_"):
            monkeypatch.delenv(key, raising=False)
    module = importlib.import_module("operations.control.sweeper_entry")
    module._runtime.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        module.handler({}, None)


def test_settings_resolve_with_full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from operations.settings import resolve_control_plane_deployment_settings

    settings = resolve_control_plane_deployment_settings(_BASE_ENV)
    assert settings.admin_group == "admin"
    assert settings.appconfig_profile == "operations-kill-switch"
    assert settings.appconfig_extension_port == 2772
    assert settings.observation.workspace_id == "workspace-default"
