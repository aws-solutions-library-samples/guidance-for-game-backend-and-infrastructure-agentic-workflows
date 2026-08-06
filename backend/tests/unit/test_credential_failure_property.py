#!/usr/bin/env python3
"""Property-based test for fail-closed credential acquisition under the ProviderAuth model.

The v2 pass moved credential acquisition out of the connector core and into the
Provider_Adapter, behind the provider-neutral :class:`ProviderAuth` contract (Req 11.1).
The connector service (``connector.service``) no longer imports or calls ``get_secret`` at
all; the adapter's :class:`~connector.github_provider.GitHubTokenAuth` acquires the
credential on its first provider operation and raises :class:`ProviderAuthError` if
acquisition fails. That typed error is caught by the propose pipeline and mapped to a safe,
no-retry error result, so the fail-closed credential behavior the removed service "Gate 6"
provided is preserved.

This test proves two universally-quantified facts:

1. **Adapter auth is fail-closed.** For any credential value the adapter's credential
   source cannot produce (``get_secret`` returns ``None`` / ``""``), ``GitHubTokenAuth.apply``
   raises :class:`ProviderAuthError` and attaches no ``Authorization`` header.
2. **The core fails closed without retry and without ever fetching a credential.** For any
   provider operation that surfaces a :class:`ProviderAuthError` (the adapter's
   credential-acquisition failure), ``propose_change`` performs no retry of that operation,
   creates no Change_Proposal, returns a secret-free error result, and the connector core
   issues **zero** ``get_secret`` calls (it does not even import ``get_secret``).

Validates: Requirements 4.6, 11.1
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector import service
from connector.config import AdapterConfig, AllowlistEntry, SourceControlConfig
from connector.github_provider import GitHubTokenAuth
from connector.models import ProposedFile
from connector.provider import OutboundRequest, ProviderAuthError
from connector.service import propose_change
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# --- Fixed, valid request context ------------------------------------------
#
# A single allowlist entry the request matches exactly, so the allowlist gate passes and the
# pipeline advances to the provider operations where the adapter acquires its credential.
_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"

# A valid Secrets Manager ARN — the single ARN-valued credential setting the adapter fetches.
_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/github-token-AbCdEf"

# The seeded target-branch head; propose passes it as the Verified_Source_Snapshot so the
# read-before-write check passes and the credential-failure path under test is reached.
_BASE_SHA = "basesha0000000000000000000000000000000000"

# An authenticated user in an authorized group so the authorization gate passes. Identity is
# derived strictly from the request context, never from tool/model input.
_AUTHORIZED_CONTEXT = {"user_id": "user-9", "groups": [_GROUP], "session_id": "sess-9"}

# A benign, injection-free intent/title/description that clears input validation and
# prompt-injection detection so every request reaches the provider operations.
_INTENT = "update the storage bucket configuration for the service stack"
_TITLE = "Update service stack configuration"
_DESCRIPTION = "Adjust the configured storage bucket in the infrastructure template."

# Structurally valid CloudFormation so IaC validation passes and the pipeline reaches the
# provider ops — proving the request fails *only* because of the credential-acquisition
# failure surfaced by the adapter.
_VALID_CFN = "Resources:\n  MyBucket:\n    Type: AWS::S3::Bucket\n"

# The provider operations reached by the propose pipeline at which the adapter acquires its
# credential (each builds auth headers). A ProviderAuthError on any of these is the shape a
# credential-acquisition failure takes once acquisition is adapter-owned.
_AUTH_BEARING_OPS = (
    "latest_commit_sha",
    "branch_exists",
    "create_branch",
    "commit_files",
    "open_change_proposal",
)


def _make_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig backed by a single allowlist entry."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_arn=_ARN,
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=(_GROUP,),
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _proposed_files() -> list[ProposedFile]:
    """A single valid CloudFormation file to propose."""
    return [ProposedFile(path="template.yaml", content=_VALID_CFN, iac_format="cloudformation")]


# --- Part 1: the adapter's ProviderAuth is fail-closed ---------------------


# Feature: source-control-connector-v2, Property V2: credential acquisition is adapter-owned behind a neutral auth contract
@settings(max_examples=100)
@given(secret=st.sampled_from([None, ""]))
def test_adapter_auth_fails_closed_when_credential_unavailable(secret):
    """GitHubTokenAuth.apply raises ProviderAuthError when the credential is unavailable.

    Credential acquisition is owned by the adapter behind the neutral ``ProviderAuth``
    contract: when the underlying ``get_secret`` yields nothing (``None`` / ``""``), the
    adapter fails closed with a :class:`ProviderAuthError` and attaches no credential header
    (Req 11.1). The credential is fetched from the single ARN-valued setting.
    """
    auth = GitHubTokenAuth(AdapterConfig(credential_secret_arn=_ARN, provider_base_url=None, config_errors=()))
    request = OutboundRequest(headers={})

    with mock.patch("connector.github_provider.get_secret", return_value=secret) as mock_get_secret:
        with pytest.raises(ProviderAuthError):
            auth.apply(request)

    # The credential is acquired from the single ARN-valued setting.
    mock_get_secret.assert_called_once_with(_ARN, source="secretsmanager")
    # No credential material is attached on the fail-closed path.
    assert "Authorization" not in request.headers


# --- Part 2: the core fails closed (no retry, no proposal, no get_secret) --


# Feature: source-control-connector-v2, Property V2: credential acquisition is adapter-owned behind a neutral auth contract
@settings(max_examples=100)
@given(
    op=st.sampled_from(_AUTH_BEARING_OPS),
    message=st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40),
)
def test_credential_acquisition_failure_is_fail_closed_no_retry(op, message):
    """A ProviderAuthError from the adapter → no retry, no proposal, secret-free error.

    For any provider operation that surfaces the adapter's credential-acquisition failure as
    a :class:`ProviderAuthError`, ``propose_change`` returns an error result, never reports a
    created proposal, does not retry the failing operation, and the connector core performs
    no ``get_secret`` of its own (Req 4.6, 11.1).
    """
    fake = FakeProvider()
    fake.set_head(_REPO, _BRANCH, _BASE_SHA)
    fake.fail(op, ProviderAuthError(message or "credential acquisition failed"))

    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        with mock.patch.object(service, "logger") as mock_logger:
            result = propose_change(
                intent=_INTENT,
                files=_proposed_files(),
                iac_format="cloudformation",
                title=_TITLE,
                description=_DESCRIPTION,
                base_revision=_BASE_SHA,
                repository=_REPO,
                target_branch=_BRANCH,
                config=_make_config(),
                provider=fake,
            )
    finally:
        reset_request_context(token)

    # The connector core does not (and cannot) acquire credentials itself: it does not even
    # import ``get_secret``. Credential acquisition is entirely adapter-owned (Req 11.1).
    assert not hasattr(service, "get_secret"), (
        "connector.service must not import or call get_secret; credential acquisition is "
        "adapter-owned behind ProviderAuth"
    )

    # An error result is returned and no successful proposal is reported (Req 4.6).
    assert result.status == "error"
    assert result.proposal_id is None
    assert result.proposal_url is None

    # A ProviderAuthError is never retried — the failing op was attempted exactly once.
    assert len(fake.calls_for(op)) == 1, f"credential auth failure retried {op}"

    # No Change_Proposal was created (fail closed).
    assert fake.pull_requests == []

    # A provider-auth error audit entry was recorded, attributing the requesting user.
    assert mock_logger.error.called
    auth_error_calls = [
        call for call in mock_logger.error.call_args_list if call.kwargs.get("reason") == "provider_auth_error"
    ]
    assert auth_error_calls, "expected a provider_auth_error audit entry"
    audit = auth_error_calls[0].kwargs
    assert audit.get("requesting_user") == _AUTHORIZED_CONTEXT["user_id"]
    assert audit.get("outcome") == "error"
