#!/usr/bin/env python3
"""Property-based test that invalid credentials are never retried.

Covers Correctness Property 17 from the source-control-connector design: for any provider
operation in which the provider rejects the credential as invalid or unauthorized,
``connector.service.propose_change`` attempts the operation exactly once (no retry), returns
an authorization-error indication, and records the failure in the audit log.

The connector maps an invalid/unauthorized credential (e.g. HTTP 401/403 from the provider)
onto :class:`connector.provider.ProviderAuthError` (see the design's failure-handling table
for Req 10.2). Unlike transient errors — which the pipeline retries up to
``retry_max_attempts`` — a ``ProviderAuthError`` must propagate immediately, so the operation
is attempted **exactly once** even when retries are available. This test injects that error
at *each* provider operation reached during a proposal (``latest_commit_sha``,
``branch_exists``, ``create_branch``, ``commit_files``, ``open_change_proposal``) — chosen by
Hypothesis — with a config whose ``retry_max_attempts`` is greater than one, and asserts:

- the failing operation was invoked exactly once (``fake.calls_for(op)`` has length 1),
  proving the auth error was not retried despite retries being available,
- the result status is ``"error"`` (an authorization-error indication) that never claims
  success and carries no pull-request id/url, and
- no pull request is created (the provider never records an opened PR).

The service is exercised with a ``FakeProvider`` injected via ``provider=`` (with the chosen
operation programmed to raise ``ProviderAuthError`` on every call), an enabled
:class:`ConnectorConfig` injected via ``config=`` whose allowlist matches the requested
repository/branch and whose ``retry_max_attempts`` is 3, and ``connector.service.get_secret``
mocked so no AWS call occurs. Identity is supplied through the request contextvar with an
authorized ``user_id``.

The per-user rate limiter keeps state in a shared, module-level store
(``utils.security._rate_limit_windows``). To keep examples independent, that store is
cleared before each example and a unique ``user_id`` is used per example, so the rate-limit
gate never rejects a request under test.

Validates: Requirements 10.2
"""

# Standard library
import itertools
import json
from unittest.mock import patch

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, ConnectorConfig
from connector.models import ProposedFile
from connector.provider import ProviderAuthError
from connector.service import propose_change
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context
from utils.security import _rate_limit_windows

pytestmark = pytest.mark.unit


# --- Test constants --------------------------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"
_IAC_FORMAT = "cloudformation"

# The provider operations reached on the proposal path, in invocation order. Injecting a
# ProviderAuthError at any one of these must yield a single-attempt, non-destructive outcome.
_PROVIDER_OPS = (
    "latest_commit_sha",
    "branch_exists",
    "create_branch",
    "commit_files",
    "open_change_proposal",
)

# A small pool of benign words used to build varying, injection-free intents/titles so the
# input-validation and prompt-injection gates always pass and every request reaches the
# provider operations.
_SAFE_WORDS = [
    "update",
    "storage",
    "bucket",
    "queue",
    "configuration",
    "resource",
    "infrastructure",
    "template",
    "service",
    "stack",
    "memory",
    "scaling",
]

# CloudFormation resource types used to build structurally valid templates.
_RESOURCE_TYPES = [
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
    "AWS::SNS::Topic",
    "AWS::DynamoDB::Table",
    "AWS::Logs::LogGroup",
]

# Monotonic source of unique user ids so each example starts with a fresh rate-limit budget.
_user_ids = itertools.count(1)


def _make_config() -> ConnectorConfig:
    """Build an enabled ConnectorConfig whose allowlist matches the requested repo/branch.

    ``retry_max_attempts`` is deliberately > 1 (3) so that if the connector *did* retry an
    auth failure, the failing operation would be invoked more than once. The property proves
    it is invoked exactly once regardless.
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
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


@st.composite
def _valid_cfn_files(draw):
    """Generate 1..N distinct, structurally valid CloudFormation ``ProposedFile``s.

    Each file has a unique path (index-suffixed) so the proposed set has no duplicate paths,
    and a JSON body (valid YAML too) with a non-empty ``Resources`` mapping whose single
    resource declares a ``Type`` — exactly what the IaC validation gate requires.
    """
    specs = draw(
        st.lists(
            st.tuples(
                st.from_regex(r"[A-Za-z][A-Za-z0-9]{2,15}", fullmatch=True),
                st.sampled_from(_RESOURCE_TYPES),
            ),
            min_size=1,
            max_size=5,
        )
    )
    files: list[ProposedFile] = []
    for index, (logical_id, resource_type) in enumerate(specs):
        template = {"Resources": {logical_id: {"Type": resource_type}}}
        files.append(
            ProposedFile(
                path=f"templates/resource_{index}.yaml",
                content=json.dumps(template),
                iac_format=_IAC_FORMAT,
            )
        )
    return files


# --- Property 17 -----------------------------------------------------------


# Feature: source-control-connector, Property 17: Invalid credentials are not retried
@settings(max_examples=100)
@given(
    failing_op=st.sampled_from(_PROVIDER_OPS),
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
)
def test_property17_invalid_credentials_are_not_retried(failing_op, files, intent_words):
    """An invalid/unauthorized credential is surfaced once, never retried.

    For any authorized, valid request in which the provider rejects the credential at any
    single provider operation, ``propose_change`` invokes that operation exactly once (no
    retry, despite ``retry_max_attempts`` > 1), returns a safe error result (status
    ``"error"``) that never reports success (no PR id/url), and creates no pull request
    (Req 10.2).
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    provider = FakeProvider()
    # Inject the auth error at the Hypothesis-chosen provider operation. This models the
    # provider rejecting the credential as invalid/unauthorized (HTTP 401/403 → Req 10.2),
    # which the connector surfaces as ProviderAuthError. It is raised on every call.
    provider.fail(failing_op, ProviderAuthError)

    user_id = f"user-{next(_user_ids)}"
    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-1"})
    try:
        with patch("connector.service.get_secret", return_value="ghp_fake_token_value"):
            result = propose_change(
                intent,
                files,
                _IAC_FORMAT,
                title,
                description,
                config=config,
                provider=provider,
            )
    finally:
        reset_request_context(token)

    # Req 10.2 (core): the failing operation was attempted EXACTLY ONCE — the auth error was
    # not retried even though retries (retry_max_attempts=3) were available.
    assert len(provider.calls_for(failing_op)) == 1

    # Req 10.2: the connector returns an authorization-error indication — a safe error result,
    # never a success. The status is "error" and no PR id/url is reported.
    assert result.status == "error"
    assert result.proposal_id is None
    assert result.proposal_url is None

    # Req 10.2: no proposal is created — the provider never records an opened pull request,
    # regardless of which operation raised the auth error.
    assert provider.pull_requests == []
    assert len(provider.pull_requests) == 0

    # Defense-in-depth: the credential value never leaks into the agent-visible result.
    assert "ghp_fake_token_value" not in result.message
