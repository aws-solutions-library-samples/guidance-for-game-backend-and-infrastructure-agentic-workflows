#!/usr/bin/env python3
"""Property-based test for safe, non-destructive provider unavailability/timeout.

Covers Correctness Property 16 from the source-control-connector design: for any provider
operation that reports the provider unreachable or exceeds the configured provider timeout,
``connector.service.propose_change`` returns an availability-error indication, records the
failure in the audit log, and creates or modifies no proposal branch or proposal.

The connector maps both "provider unreachable" and "provider timed out" onto
:class:`connector.provider.ProviderUnavailableError` (see the design's failure-handling
table for Req 10.1). This test injects that error at *each* provider operation reached
during a proposal (``latest_commit_sha``, ``branch_exists``, ``create_branch``,
``commit_files``, ``open_pull_request``) — chosen by Hypothesis — and asserts the outcome
is always safe and non-destructive:

- the result status is ``"error"`` (an availability-error indication),
- the result never claims success and carries no pull-request id/url, and
- no pull request is created (the provider never records an opened PR).

The service is exercised with a ``FakeProvider`` injected via ``provider=`` (with the chosen
operation programmed to raise ``ProviderUnavailableError`` on every call), an enabled
:class:`ConnectorConfig` injected via ``config=`` whose allowlist matches the requested
repository/branch, and ``connector.service.get_secret`` mocked so no AWS call occurs.
Identity is supplied through the request contextvar with an authorized ``user_id``.

The per-user rate limiter keeps state in a shared, module-level store
(``utils.security._rate_limit_windows``). To keep examples independent, that store is
cleared before each example and a unique ``user_id`` is used per example, so the rate-limit
gate never rejects a request under test.

Validates: Requirements 10.1
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
from connector.provider import ProviderUnavailableError
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
# ProviderUnavailableError at any one of these must yield a safe, non-destructive outcome.
_PROVIDER_OPS = (
    "latest_commit_sha",
    "branch_exists",
    "create_branch",
    "commit_files",
    "open_pull_request",
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
    """Build an enabled ConnectorConfig whose allowlist matches the requested repo/branch."""
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


# --- Property 16 -----------------------------------------------------------


# Feature: source-control-connector, Property 16: Provider unavailability/timeout is safe and non-destructive
@settings(max_examples=100)
@given(
    failing_op=st.sampled_from(_PROVIDER_OPS),
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
)
def test_property16_provider_unavailable_is_safe_and_non_destructive(
    failing_op, files, intent_words
):
    """Provider unavailability at any operation yields a safe, non-destructive error.

    For any authorized, valid request in which the provider reports unavailable/timed out at
    any single provider operation, ``propose_change`` returns a safe error result
    (status ``"error"``), never reports success (no PR id/url), and no pull request is
    created (Req 10.1).
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    provider = FakeProvider()
    # Inject the availability error at the Hypothesis-chosen provider operation. This models
    # both "provider unreachable" and "provider timed out" (Req 10.1) which the connector
    # surfaces as ProviderUnavailableError. It is raised on every call to that operation.
    provider.fail(failing_op, ProviderUnavailableError)

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

    # Req 10.1: the connector returns an availability-error indication — a safe error result,
    # never a success. The status is "error" and no PR id/url is reported.
    assert result.status == "error"
    assert result.pull_request_id is None
    assert result.pull_request_url is None

    # Req 10.1: no proposal is created — the provider never records an opened pull request,
    # regardless of which operation failed.
    assert provider.pull_requests == []
    assert provider.calls_for("open_pull_request") == [] or failing_op == "open_pull_request"
    # Even when open_pull_request itself is the failing op, no PR artifact is recorded.
    assert len(provider.pull_requests) == 0

    # Defense-in-depth: the credential value never leaks into the agent-visible result.
    assert "ghp_fake_token_value" not in result.message
