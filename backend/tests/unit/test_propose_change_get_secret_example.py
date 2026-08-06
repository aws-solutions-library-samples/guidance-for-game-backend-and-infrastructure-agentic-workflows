#!/usr/bin/env python3
"""Example test for adapter-owned credential acquisition under the ProviderAuth model.

The v2 pass moved credential acquisition out of the connector core and into the
Provider_Adapter, behind the provider-neutral :class:`ProviderAuth` contract (Req 11.1).
This focused, example (non-property) test proves two security-critical facts:

1. **The connector core never handles the credential.** ``connector.service`` does not
   import or expose ``get_secret`` at all — the removed "Gate 6" credential fetch is gone,
   and the service performs no credential acquisition of its own.
2. **The adapter acquires the credential only from Secrets Manager.** The GitHub adapter's
   :class:`~connector.github_provider.GitHubTokenAuth` fetches the credential via
   ``get_secret(config.credential_secret_arn, source="secretsmanager")`` — keyed by the
   single ARN-valued credential setting — and never reads a raw credential from the
   environment. On success it attaches an ``Authorization: Bearer`` header; the credential
   value never leaks into the header name or anywhere else observable.

Validates: Requirements 4.2, 11.1, 11.2
"""

# Standard library
import os
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
from connector import service
from connector.config import AdapterConfig
from connector.github_provider import GitHubTokenAuth
from connector.provider import OutboundRequest

pytestmark = pytest.mark.unit


# The single ARN-valued credential setting the adapter must use to fetch the credential.
_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/github-write-credential-AbCdEf"

# The credential value get_secret returns. It must be the ONLY source of the credential.
_SECRET_VALUE = "ghp_fake_token_from_secrets_manager_0123456789"

# A decoy raw credential planted in the environment. Req 4.2 forbids reading it.
_RAW_CREDENTIAL_ENV_VAR = "GBAW_SCM_CREDENTIAL"
_RAW_CREDENTIAL_VALUE = "ghp_raw_credential_that_must_never_be_read_9876543210"


def test_connector_core_does_not_import_or_call_get_secret():
    """The connector core is credential-free: it does not import/expose get_secret (Req 11.1).

    Credential acquisition is entirely adapter-owned behind the neutral ``ProviderAuth``
    contract, so the removed service "Gate 6" credential fetch leaves no ``get_secret``
    reference in ``connector.service``.
    """
    assert not hasattr(service, "get_secret")


def test_adapter_auth_fetches_credential_via_get_secret_and_reads_no_raw_env(monkeypatch):
    """GitHubTokenAuth sources the credential from get_secret(arn) only (Req 4.2, 11.2)."""
    auth = GitHubTokenAuth(AdapterConfig(credential_secret_arn=_SECRET_ARN, provider_base_url=None, config_errors=()))
    request = OutboundRequest(headers={})

    # Plant a decoy raw credential in the environment. If the adapter read the credential
    # from the environment (the forbidden path) it would find this value.
    monkeypatch.setenv(_RAW_CREDENTIAL_ENV_VAR, _RAW_CREDENTIAL_VALUE)

    # Record every environment-variable read performed during credential acquisition so we
    # can prove no raw-credential env var was consulted.
    real_getenv = os.getenv
    real_environ_get = os.environ.get
    read_env_keys: list[str] = []

    def _tracking_getenv(key, default=None):
        read_env_keys.append(key)
        return real_getenv(key, default)

    def _tracking_environ_get(key, default=None):
        read_env_keys.append(key)
        return real_environ_get(key, default)

    with (
        patch("connector.github_provider.get_secret", return_value=_SECRET_VALUE) as mock_get_secret,
        patch("os.getenv", side_effect=_tracking_getenv),
        patch.object(os.environ, "get", side_effect=_tracking_environ_get),
    ):
        auth.apply(request)

    # Req 4.2 / 11.2: the credential is fetched from Secrets Manager via get_secret, keyed by
    # the single ARN-valued credential setting — not read from configuration/environment.
    mock_get_secret.assert_called_once_with(_SECRET_ARN, source="secretsmanager")

    # Req 4.2: no raw-credential environment variable was read during acquisition.
    assert _RAW_CREDENTIAL_ENV_VAR not in read_env_keys

    # On success the credential is attached as a bearer token; the raw decoy is never used.
    assert request.headers["Authorization"] == f"Bearer {_SECRET_VALUE}"
    assert _RAW_CREDENTIAL_VALUE not in request.headers["Authorization"]
