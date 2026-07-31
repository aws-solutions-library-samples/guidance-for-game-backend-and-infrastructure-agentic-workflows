#!/usr/bin/env python3
"""Example test for the connector propose path credential source (task 8.2).

This is a focused, example (non-property) test proving a single, security-critical
behavior of ``connector.service.propose_change``: the credential used to talk to the
source-control provider comes **only** from AWS Secrets Manager via
``get_secret(config.credential_secret_id, ...)`` and is **never** read from a raw
environment variable (Req 4.2, and the credential-isolation posture of Req 4.1/12.2).

To make the guarantee observable the test:

- injects an enabled :class:`ConnectorConfig` (via ``config=``) whose
  ``credential_secret_id`` is a known Secrets-Manager-style id,
- injects a :class:`FakeProvider` (via ``provider=``) so no network/AWS calls occur,
- sets the request context with a ``user_id`` in the authorized group so the
  authorization gate passes,
- supplies valid CloudFormation so IaC validation passes and the pipeline reaches the
  provider ops (a successful ``created`` proposal),
- patches ``connector.service.get_secret`` and asserts it was called exactly once with
  the configured secret id, and
- plants a decoy *raw-credential* environment variable and records every environment
  read during the call, asserting the decoy variable is never consulted — i.e. the
  connector does not accept the credential value through environment configuration.

Validates: Requirements 4.2
"""

# Standard library
import os
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
from connector.config import AllowlistEntry, SourceControlConfig
from support.config_factory import make_source_control_config
from connector.models import ProposedFile
from connector.service import propose_change
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# The Secrets Manager identifier the connector must use to fetch the credential.
_SECRET_ID = "scm/github-write-credential"

# The credential value get_secret returns. It must be the ONLY source of the credential.
_SECRET_VALUE = "ghp_fake_token_from_secrets_manager_0123456789"

# A decoy raw credential planted in the environment. Req 4.2 forbids reading it.
_RAW_CREDENTIAL_ENV_VAR = "GBAW_SCM_CREDENTIAL"
_RAW_CREDENTIAL_VALUE = "ghp_raw_credential_that_must_never_be_read_9876543210"

# A minimal but structurally valid CloudFormation template so validate_iac() passes.
_VALID_CFN = (
    "AWSTemplateFormatVersion: '2010-09-09'\n"
    "Resources:\n"
    "  MyBucket:\n"
    "    Type: AWS::S3::Bucket\n"
)


def _enabled_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig with a Secrets-Manager-style credential id.

    The allowlist's first entry is the configured repository/target branch the propose
    path acts on; ``credential_secret_id`` is the id the pipeline must hand to
    ``get_secret``.
    """
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id=_SECRET_ID,
        allowlist=(AllowlistEntry(repo="org/iac-repo", target_branches=("main",)),),
        authorized_groups=("scm-writers",),
        rate_limit_max=5,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def test_propose_change_fetches_credential_via_get_secret_and_reads_no_raw_env(monkeypatch):
    """propose_change sources the credential from get_secret(secret_id) only (Req 4.2)."""
    config = _enabled_config()
    fake = FakeProvider()
    fake.set_head("org/iac-repo", "main", "basesha0000000000000000000000000000000000")

    proposed_files = [
        ProposedFile(path="templates/bucket.yaml", content=_VALID_CFN, iac_format="cloudformation")
    ]

    # Plant a decoy raw credential in the environment. If propose_change read the
    # credential from the environment (the forbidden path) it would find this value.
    monkeypatch.setenv(_RAW_CREDENTIAL_ENV_VAR, _RAW_CREDENTIAL_VALUE)

    # Record every environment-variable read performed during the propose call so we can
    # prove no raw-credential env var was consulted.
    real_getenv = os.getenv
    real_environ_get = os.environ.get
    read_env_keys: list[str] = []

    def _tracking_getenv(key, default=None):
        read_env_keys.append(key)
        return real_getenv(key, default)

    def _tracking_environ_get(key, default=None):
        read_env_keys.append(key)
        return real_environ_get(key, default)

    # Set the authenticated request context: an authorized user so the auth gate passes.
    # Identity is derived strictly from this context, never from tool/model input.
    token = set_request_context(
        {"user_id": "user-8-2", "groups": ["scm-writers"], "session_id": "sess-8-2"}
    )
    try:
        with (
            patch("connector.service.get_secret", return_value=_SECRET_VALUE) as mock_get_secret,
            patch("os.getenv", side_effect=_tracking_getenv),
            patch.object(os.environ, "get", side_effect=_tracking_environ_get),
        ):
            result = propose_change(
                intent="Add an S3 bucket for build artifacts",
                files=proposed_files,
                iac_format="cloudformation",
                title="Add artifacts bucket",
                description="Adds an S3 bucket to store build artifacts.",
                config=config,
                provider=fake,
            )
    finally:
        reset_request_context(token)

    # The pipeline reached the provider ops and opened exactly one proposal.
    assert result.status == "created", result.message
    assert result.proposal_id is not None
    assert len(fake.pull_requests) == 1

    # Req 4.2: the credential is fetched from Secrets Manager via get_secret, keyed by the
    # configured secret id — not read from configuration/environment.
    mock_get_secret.assert_called_once_with(_SECRET_ID, source="secretsmanager")

    # Req 4.2: no raw-credential environment variable was read during the propose path.
    assert _RAW_CREDENTIAL_ENV_VAR not in read_env_keys

    # Defense-in-depth: the returned message never leaks the credential value.
    assert _SECRET_VALUE not in result.message
    assert _RAW_CREDENTIAL_VALUE not in result.message
