#!/usr/bin/env python3
"""Unit test: ConnectorConfig.load() ignores non-``GBAW_``-prefixed config sources.

Requirement 12.1: the connector reads all configuration *exclusively* from
``GBAW_``-prefixed environment variables and ignores any connector configuration
supplied through sources that are not ``GBAW_``-prefixed environment variables.

Because ``connector/config.py`` reads only the ``GBAW_SCM_*`` values exposed on
``config/settings.py``, supplying bare (non-prefixed) env vars such as
``SCM_CONNECTOR_ENABLED`` must have no effect on ``load()`` — the connector stays in
its default disabled off-state when the ``GBAW_``-prefixed variables are unset.
"""

# Standard library
import importlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


# The connector's configuration keys, in both their bare and GBAW_-prefixed forms.
# Bare names are what an operator might mistakenly set; the prefixed names are the
# only source load() honors.
_SCM_KEYS = (
    "SCM_CONNECTOR_ENABLED",
    "SCM_PROVIDER",
    "SCM_PROVIDER_BASE_URL",
    "SCM_CREDENTIAL_SECRET_ARN",
    "SCM_REPO_ALLOWLIST",
    "SCM_AUTHORIZED_GROUPS",
    "SCM_AUDIT_LOG_GROUP",
    "SCM_RATE_LIMIT_MAX",
    "SCM_RATE_LIMIT_WINDOW_SECONDS",
    "SCM_PROVIDER_TIMEOUT_SECONDS",
    "SCM_RETRY_MAX_ATTEMPTS",
    "SCM_MAX_FILES_PER_REQUEST",
)

# A fully-valid connector configuration expressed WITHOUT the GBAW_ prefix. If load()
# were (incorrectly) reading these, it would enable the connector.
_BARE_CONFIG = {
    "SCM_CONNECTOR_ENABLED": "true",
    "SCM_PROVIDER": "github",
    "SCM_CREDENTIAL_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/github-token-AbCdEf",
    "SCM_REPO_ALLOWLIST": "org/iac-repo=main",
    "SCM_AUTHORIZED_GROUPS": "sre",
    "SCM_RATE_LIMIT_MAX": "999",
    "SCM_RATE_LIMIT_WINDOW_SECONDS": "120",
    "SCM_PROVIDER_TIMEOUT_SECONDS": "300",
    "SCM_RETRY_MAX_ATTEMPTS": "9",
    "SCM_MAX_FILES_PER_REQUEST": "77",
}


def _reload_config():
    """Reload settings then connector.config so both reflect the current environment."""
    # Local modules
    import config.settings

    importlib.reload(config.settings)
    # Local modules
    import connector.config

    importlib.reload(connector.config)
    return connector.config.SourceControlConfig


def test_non_gbaw_env_vars_are_ignored_by_load(monkeypatch):
    """Bare (non-``GBAW_``) env vars must not influence ConnectorConfig.load() (Req 12.1)."""
    # Ensure no GBAW_-prefixed connector config is present, so the ONLY thing that could
    # possibly enable the connector is the bare env vars we set below.
    for key in _SCM_KEYS:
        monkeypatch.delenv(f"GBAW_{key}", raising=False)

    # Supply a complete, valid-looking configuration through the WRONG source: bare,
    # non-GBAW_-prefixed environment variables.
    for key, value in _BARE_CONFIG.items():
        monkeypatch.setenv(key, value)

    source_control_config = _reload_config()
    config = source_control_config.load()

    # The bare env vars were ignored: the connector is in its default disabled off-state,
    # exactly as if nothing had been configured (Req 12.1, 1.1, 1.5).
    assert config.enabled is False
    assert config.connector.provider is None
    assert config.adapter.credential_secret_arn is None
    assert config.domain.authorization_policy == ()
    assert config.domain.authorized_groups == ()
    # Off-state (no GBAW_ flag) produces NO configuration errors.
    assert config.config_errors == ()
    # Numeric tuning values reflect the documented defaults, not the bare-env overrides.
    assert config.connector.rate_limit_max == 5
    assert config.connector.rate_limit_window_seconds == 3600
    assert config.connector.provider_timeout_seconds == 30
    assert config.connector.retry_max_attempts == 3
    assert config.connector.max_files_per_request == 20


def test_gbaw_prefixed_vars_are_the_only_honored_source(monkeypatch):
    """Positive control: the GBAW_-prefixed source IS honored while bare vars are not.

    Sets a truthy enablement flag under BOTH sources but the rest of a valid config only
    under the GBAW_-prefixed names. This confirms load() acts on the prefixed values (the
    connector becomes enabled) and demonstrates the bare vars are not what drove it.
    """
    prefixed = {
        "GBAW_SCM_CONNECTOR_ENABLED": "true",
        "GBAW_SCM_PROVIDER": "github",
        "GBAW_SCM_CREDENTIAL_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/github-token-AbCdEf",
        "GBAW_SCM_REPO_ALLOWLIST": "org/iac-repo=main",
        "GBAW_SCM_AUTHORIZED_GROUPS": "sre",
        "GBAW_SCM_AUDIT_LOG_GROUP": "scm-audit-logs",
    }
    for key, value in prefixed.items():
        monkeypatch.setenv(key, value)
    # Clear optional numeric GBAW_ knobs so documented defaults apply deterministically.
    for key in (
        "GBAW_SCM_RATE_LIMIT_MAX",
        "GBAW_SCM_RATE_LIMIT_WINDOW_SECONDS",
        "GBAW_SCM_PROVIDER_TIMEOUT_SECONDS",
        "GBAW_SCM_RETRY_MAX_ATTEMPTS",
        "GBAW_SCM_MAX_FILES_PER_REQUEST",
    ):
        monkeypatch.delenv(key, raising=False)

    # Bare vars set to values that, if honored, would corrupt the config.
    monkeypatch.setenv("SCM_PROVIDER", "gitlab")
    monkeypatch.setenv("SCM_RATE_LIMIT_MAX", "999")

    source_control_config = _reload_config()
    config = source_control_config.load()

    assert config.enabled is True
    assert config.config_errors == ()
    # The provider came from the GBAW_-prefixed value, not the bare SCM_PROVIDER=gitlab.
    assert config.connector.provider == "github"
    # The default (not the bare SCM_RATE_LIMIT_MAX=999) is in effect.
    assert config.connector.rate_limit_max == 5
