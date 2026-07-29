#!/usr/bin/env python3
"""Property-based test that credential values never appear in the connector's output.

Covers Correctness Property 8 from the source-control-connector design: the value of the
source-control credential (``SCM_Credential``) is fetched only from Secrets Manager via
``get_secret`` and must **never** surface anywhere the agent or an operator can observe it.
Two observable surfaces exist for a ``connector.service.propose_change`` invocation:

  1. the returned :class:`ProposalResult` — its ``status``, ``message``,
     ``proposal_id`` and ``proposal_url``; and
  2. the audit log — every field of every ``connector.service.logger`` call.

The property proven here is universal: for *any* credential value that ``get_secret``
returns (generated as random, high-entropy, secret-looking strings), that exact value never
appears in the returned result nor in any audit log call's args/kwargs — across **both** the
success path (a proposal is created) and every failure path (input rejected, empty file set,
invalid IaC, and each typed provider failure). This holds because the pipeline never places
the credential in a result field or an audit field; the credential lives only long enough to
authenticate the provider.

The service is exercised with a ``FakeProvider`` injected via ``provider=`` and a
purpose-built enabled :class:`ConnectorConfig` injected via ``config=``. An authorized user
is supplied through the request contextvar, valid CloudFormation and an allowlist-matching
repo/branch drive the success path, and ``connector.service.get_secret`` is mocked to return
the generated credential. Audit output is captured by patching ``connector.service.logger``
with a :class:`~unittest.mock.MagicMock` and asserting the credential string appears in no
recorded call. Each example clears the shared rate-limit store and uses a unique ``user_id``
so examples stay independent.

Validates: Requirements 4.7, 6.6
"""

# Standard library
import itertools
import string
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
import utils.security as security
from connector.config import AllowlistEntry, ConnectorConfig
from connector.models import ProposedFile
from connector.provider import (
    ProviderAuthError,
    ProviderConflictError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from connector.service import propose_change
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# --- Fixed request/config fixtures -----------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"

# A benign, injection-free intent/title/description so the success/failure outcome is decided
# by the scenario under test, not by the input-validation gate.
_INTENT = "update the storage bucket configuration in the service infrastructure template"
_TITLE = "Update bucket configuration"
_DESCRIPTION = "Adjust the storage bucket settings to match the requested configuration."

# Structurally valid CloudFormation (passes IaC validation on the paths that reach it).
_VALID_CFN = "Resources:\n  MyBucket:\n    Type: AWS::S3::Bucket\n"

# Structurally INVALID IaC (no top-level Resources map) to drive the validation-decline path.
_INVALID_CFN = "NotResources:\n  junk: true\n"

# Monotonic source of unique user ids so each example starts with a fresh rate-limit budget.
_user_ids = itertools.count(1)


def _make_config() -> ConnectorConfig:
    """Build an enabled ConnectorConfig for the propose path.

    ``rate_limit_max`` is high and the shared window store is cleared per example, so the
    rate-limit gate never masks the scenario under test. ``retry_max_attempts=1`` keeps the
    transient-failure scenario fast (no backoff sleeps) while still exercising the failure
    path where a credential value could theoretically leak.
    """
    return ConnectorConfig(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=(_GROUP,),
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=1,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


# --- Strategies ------------------------------------------------------------

# Random, high-entropy, secret-looking credential values. A distinctive prefix plus a long
# alphanumeric body makes each value look like a real provider token and makes an accidental
# collision with ordinary output words (e.g. "created", "rejected") effectively impossible,
# so a match in the output would be a genuine leak rather than a false positive.
_secret_values = st.builds(
    lambda prefix, body: prefix + body,
    st.sampled_from(["ghp_", "ghs_", "github_pat_11A", "xoxb-", "AKIA", "glpat-"]),
    st.text(alphabet=string.ascii_letters + string.digits, min_size=24, max_size=48),
)

# The scenarios exercised. "success" drives the full create path; every other value drives a
# distinct decline/failure/error path that still fetches the credential first.
_scenarios = st.sampled_from(
    [
        "success",
        "empty_files",
        "invalid_iac",
        "provider_auth",
        "provider_unavailable",
        "provider_conflict",
        "provider_transient",
    ]
)


def _provider_for(scenario: str) -> FakeProvider:
    """Return a FakeProvider programmed for ``scenario``.

    Provider-failure scenarios inject a typed exception on the first provider operation so
    the propose pipeline takes the corresponding error branch (all of which audit and return
    a secret-free result).
    """
    fake = FakeProvider()
    fake.set_head(_REPO, _BRANCH, "basesha0000000000000000000000000000000000")
    if scenario == "provider_auth":
        fake.fail("latest_commit_sha", ProviderAuthError("auth denied"))
    elif scenario == "provider_unavailable":
        fake.fail("latest_commit_sha", ProviderUnavailableError("unreachable"))
    elif scenario == "provider_conflict":
        fake.fail("open_change_proposal", ProviderConflictError("merge conflict"))
    elif scenario == "provider_transient":
        fake.fail("latest_commit_sha", ProviderTransientError("temporary"))
    return fake


def _files_for(scenario: str) -> list[ProposedFile]:
    """Return the proposed files that drive ``scenario``."""
    if scenario == "empty_files":
        return []
    content = _INVALID_CFN if scenario == "invalid_iac" else _VALID_CFN
    return [ProposedFile(path="template.yaml", content=content, iac_format="cloudformation")]


def _logger_call_blob(mock_logger: MagicMock) -> str:
    """Flatten every recorded logger call (name + args + kwargs) into one searchable string.

    Captures the full audit surface: for each recorded call we include the invoked attribute
    name, the positional message/args, and every keyword field value, so a credential value
    hidden in any audit field would be found.
    """
    parts: list[str] = []
    for name, args, kwargs in mock_logger.mock_calls:
        parts.append(str(name))
        parts.extend(repr(arg) for arg in args)
        parts.extend(f"{key}={value!r}" for key, value in kwargs.items())
    return "\n".join(parts)


# --- Property 8 ------------------------------------------------------------


# Feature: source-control-connector, Property 8: Credential values never appear in output
@settings(max_examples=100)
@given(credential=_secret_values, scenario=_scenarios)
def test_property8_credential_never_appears_in_output(credential, scenario):
    """The credential value never appears in the ProposalResult nor any audit log field.

    For any credential returned by ``get_secret`` and across the success path and every
    decline/failure path, the exact credential string is absent from ``result.status``,
    ``result.message``, ``result.proposal_id``, ``result.proposal_url`` and from
    every ``connector.service.logger`` call's args/kwargs (Req 4.7, 6.6).
    """
    # Isolate this example: fresh rate-limit window and a unique authorized user id.
    security._rate_limit_windows.clear()
    user_id = f"user-cred-{next(_user_ids)}"

    config = _make_config()
    provider = _provider_for(scenario)
    files = _files_for(scenario)

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-8-6"})
    try:
        with (
            patch("connector.service.get_secret", return_value=credential),
            patch("connector.service.logger", new=MagicMock()) as mock_logger,
        ):
            result = propose_change(
                _INTENT,
                files,
                iac_format="cloudformation",
                title=_TITLE,
                description=_DESCRIPTION,
                config=config,
                provider=provider,
            )
            # Capture the audit surface while the logger is still patched.
            audit_blob = _logger_call_blob(mock_logger)
    finally:
        reset_request_context(token)

    # Sanity: the scenario actually exercised the intended terminal state so the assertions
    # below are meaningful (the credential gate was passed and each path was reached).
    if scenario == "success":
        assert result.status == "created", result.message
        assert result.proposal_id is not None
    else:
        assert result.status in {"declined", "error"}, (scenario, result.status)

    # Req 4.7 / 6.6: the credential value appears in NO field of the returned result.
    for field_value in (
        result.status,
        result.message,
        result.proposal_id,
        result.proposal_url,
    ):
        if field_value is not None:
            assert credential not in field_value, (
                f"credential leaked into ProposalResult ({scenario}): {field_value!r}"
            )

    # Req 6.6: the credential value appears in NO audit log field (args or kwargs).
    assert credential not in audit_blob, (
        f"credential leaked into an audit log entry ({scenario})"
    )
