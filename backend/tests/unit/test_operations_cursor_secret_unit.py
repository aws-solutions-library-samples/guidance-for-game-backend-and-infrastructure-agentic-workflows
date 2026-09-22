"""Production cursor HMAC key loading from Secrets Manager (issue #416, E4).

The list-cursor HMAC signing key is a real secret in production and MUST be
loaded from AWS Secrets Manager by ARN (``GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN``)
— read from the exact secret id, never logged. The raw
``GBAW_OPERATIONS_CURSOR_SIGNING_KEY`` env value remains a LOCAL-TEST-ONLY
convenience. Exactly one source must be configured:

* neither configured  -> fail closed at settings resolution;
* both configured      -> fail closed (ambiguous);
* secret ARN only      -> resolved at bootstrap via a Secrets Manager GetSecretValue
                          on the exact ARN;
* raw key only         -> used directly (local/test).
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.settings import (
    load_cursor_signing_key,
    resolve_control_plane_deployment_settings,
)

pytestmark = pytest.mark.unit

_FLEET = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"

_BASE = {
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
}

_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:gbaw/ops/cursor-AbCdEf"


class _FakeSecretsManager:
    def __init__(self, *, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.requested: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.requested.append(SecretId)
        if SecretId not in self._mapping:
            raise RuntimeError("ResourceNotFoundException")
        return {"SecretString": self._mapping[SecretId]}


def test_neither_source_fails_closed() -> None:
    with pytest.raises(ValueError):
        resolve_control_plane_deployment_settings(dict(_BASE))


def test_both_sources_fail_closed() -> None:
    env = dict(_BASE)
    env["GBAW_OPERATIONS_CURSOR_SIGNING_KEY"] = "a-sufficiently-long-signing-key"
    env["GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN"] = _SECRET_ARN
    with pytest.raises(ValueError):
        resolve_control_plane_deployment_settings(env)


def test_raw_key_only_used_directly() -> None:
    env = dict(_BASE)
    env["GBAW_OPERATIONS_CURSOR_SIGNING_KEY"] = "a-sufficiently-long-signing-key"
    settings = resolve_control_plane_deployment_settings(env)
    assert settings.cursor_signing_key == "a-sufficiently-long-signing-key"
    assert settings.cursor_signing_key_secret_arn is None
    # With no secrets client the raw key resolves as-is.
    assert load_cursor_signing_key(settings, secretsmanager_client=None) == "a-sufficiently-long-signing-key"


def test_secret_arn_only_resolves_from_secrets_manager() -> None:
    env = dict(_BASE)
    env["GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN"] = _SECRET_ARN
    settings = resolve_control_plane_deployment_settings(env)
    assert settings.cursor_signing_key is None
    assert settings.cursor_signing_key_secret_arn == _SECRET_ARN

    client = _FakeSecretsManager(mapping={_SECRET_ARN: "the-production-hmac-signing-key"})
    key = load_cursor_signing_key(settings, secretsmanager_client=client)
    assert key == "the-production-hmac-signing-key"
    # The exact secret ARN was requested, nothing else.
    assert client.requested == [_SECRET_ARN]


def test_secret_arn_short_value_fails_closed() -> None:
    env = dict(_BASE)
    env["GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN"] = _SECRET_ARN
    settings = resolve_control_plane_deployment_settings(env)
    client = _FakeSecretsManager(mapping={_SECRET_ARN: "short"})
    with pytest.raises(ValueError):
        load_cursor_signing_key(settings, secretsmanager_client=client)


def test_secret_arn_without_client_fails_closed() -> None:
    env = dict(_BASE)
    env["GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN"] = _SECRET_ARN
    settings = resolve_control_plane_deployment_settings(env)
    with pytest.raises(ValueError):
        load_cursor_signing_key(settings, secretsmanager_client=None)
