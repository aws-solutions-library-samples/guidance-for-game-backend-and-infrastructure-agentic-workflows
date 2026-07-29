#!/usr/bin/env python3
"""Property-based test for PR-creation failure after branch creation.

Covers Correctness Property 18 from the source-control-connector design: if a proposal's
branch (and commit) are created successfully but opening the pull request fails with a
provider error, the connector SHALL report failure and NEVER report success. In particular,
for *any* provider error raised by ``open_change_proposal`` (after ``create_branch`` and
``commit_files`` have already succeeded), ``connector.service.propose_change``:

- returns an error result (``status == "error"``) — never ``"created"`` (Req 10.3),
- returns **no** pull-request id and **no** pull-request url, and
- does so *even though a branch was created* — ``create_branch`` was invoked and the
  ``FakeProvider`` recorded the created branch, yet no successful proposal is reported and
  no pull request exists.

The service is exercised with a ``FakeProvider`` injected via ``provider=`` (its default
behavior makes ``latest_commit_sha``/``branch_exists``/``create_branch``/``commit_files``
succeed and records each op) programmed so *only* ``open_change_proposal`` raises. The failure
is varied by Hypothesis across the abstraction's provider error types
(``ProviderConflictError``, ``ProviderUnavailableError``, ``ProviderTransientError``
exhausted, and the base ``ProviderError``). An enabled :class:`ConnectorConfig` injected via
``config=`` matches the requested repository/branch on its allowlist, ``retry_max_attempts``
is ``1`` so a transient error is immediately exhausted (no sleeps), an authorized user is
supplied through the request contextvar, valid CloudFormation is proposed, and
``connector.service.get_secret`` is mocked so no AWS call occurs.

The per-user rate limiter keeps state in a shared, module-level store
(``utils.security._rate_limit_windows``). To keep examples independent, that store is
cleared before each example and a unique ``user_id`` is used per example, so the rate-limit
gate never rejects a request under test.

Validates: Requirements 10.3
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
from connector.provider import (
    ProviderConflictError,
    ProviderError,
    ProviderTransientError,
    ProviderUnavailableError,
)
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

# Benign, injection-free words used to build varying intents/titles so the input-validation
# and prompt-injection gates always pass and every request reaches the provider operations.
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

    ``retry_max_attempts`` is 1 so a ``ProviderTransientError`` raised by
    ``open_change_proposal`` is immediately exhausted (a single attempt, no backoff sleeps).
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


@st.composite
def _valid_cfn_files(draw):
    """Generate 1..N distinct, structurally valid CloudFormation ``ProposedFile``s."""
    specs = draw(
        st.lists(
            st.tuples(
                st.from_regex(r"[A-Za-z][A-Za-z0-9]{2,15}", fullmatch=True),
                st.sampled_from(_RESOURCE_TYPES),
            ),
            min_size=1,
            max_size=4,
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


@st.composite
def _pr_failures(draw):
    """Generate a provider error for ``open_change_proposal`` to raise.

    Varies across every provider error type the pipeline can surface after a branch has
    been created: a conflict, provider unavailability, an exhausted transient failure, and
    the base ``ProviderError`` (covering an unexpected/unclassified provider failure).
    """
    exc_type = draw(
        st.sampled_from(
            [
                ProviderConflictError,
                ProviderUnavailableError,
                ProviderTransientError,
                ProviderError,
            ]
        )
    )
    message = draw(
        st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40)
    )
    return exc_type(message)


# --- Property 18 -----------------------------------------------------------


# Feature: source-control-connector, Property 18: Proposal-creation failure after branch creation never reports success
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
    failure=_pr_failures(),
)
def test_property18_pr_failure_after_branch_never_reports_success(files, intent_words, failure):
    """A PR-creation failure after the branch is created reports failure, never success.

    For any provider error raised by ``open_change_proposal`` after ``create_branch`` (and
    ``commit_files``) succeeded, ``propose_change`` reports an error, never ``"created"``,
    and returns no pull-request id/url — even though a branch was created (Req 10.3).
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    provider = FakeProvider()
    # Only PR creation fails; every prior provider op (latest_commit_sha, branch_exists,
    # create_branch, commit_files) uses the fake's default success behavior.
    provider.fail("open_change_proposal", failure)

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

    # A branch WAS created before the PR failed (proving we reached the post-branch stage).
    create_calls = provider.calls_for("create_branch")
    assert len(create_calls) >= 1, "expected create_branch to be invoked before the PR step"
    assert provider.created_branches, "expected a branch to have been created"

    # open_change_proposal was actually attempted (and failed).
    assert provider.calls_for("open_change_proposal"), "expected open_change_proposal to be attempted"

    # Req 10.3: failure is reported and success is NEVER reported.
    assert result.status == "error"
    assert result.status != "created"

    # No pull request id/url is returned, and no pull request exists on the provider.
    assert result.proposal_id is None
    assert result.proposal_url is None
    assert provider.pull_requests == [], "no pull request should have been created"

    # Defense-in-depth: the credential value never leaks into the agent-visible result.
    assert "ghp_fake_token_value" not in result.message
