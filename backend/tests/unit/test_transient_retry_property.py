#!/usr/bin/env python3
"""Property-based test that transient provider errors are retried up to the maximum.

Covers Correctness Property 20 from the source-control-connector design: a transient
provider error (``connector.provider.ProviderTransientError``) is retried up to
``config.retry_max_attempts`` before the connector reports failure. This exercises the
propose pipeline's transient-only retry behavior (Req 10.5) and the exhausted-retry
reporting (Req 10.6) through ``connector.service.propose_change``.

For any single provider operation reached during a proposal (``latest_commit_sha``,
``branch_exists``, ``create_branch``, ``commit_files``, ``open_change_proposal``), the fake
provider is programmed to raise ``ProviderTransientError`` for the next ``k`` calls (via
``FakeProvider.fail_times``) and then fall back to its default success behavior. Two
facets, selected by comparing the Hypothesis-generated ``k`` against the generated
``retry_max_attempts`` (``max``):

- **(a) k < max** — the operation fails transiently fewer times than the maximum, so a
  later attempt succeeds. The chosen op is therefore invoked exactly ``k + 1`` times and
  the proposal ultimately succeeds (``status == "created"`` with a pull request created).
- **(b) k >= max** — the operation keeps failing transiently, so retries are exhausted.
  The chosen op is attempted **exactly** ``max`` times and the result is a non-success
  error (``status == "error"`` with no pull request and an exhausted-retry audit entry).

Each op reached on the success path is invoked exactly once (the fake's default behavior),
so ``provider.calls_for(op)`` cleanly counts the attempts made against the failing op.

To keep real backoff sleeps from slowing the suite, ``connector.service.time.sleep`` is
patched to a no-op so retries do not actually wait. The service is exercised with a
``FakeProvider`` injected via ``provider=``, an enabled :class:`ConnectorConfig` injected
via ``config=`` whose allowlist matches the requested repository/branch and whose
``retry_max_attempts`` is the generated ``max``, an authorized identity supplied through
the request contextvar, structurally valid CloudFormation, and
``connector.service.get_secret`` mocked so no AWS call occurs.

The per-user rate limiter keeps state in a shared, module-level store
(``utils.security._rate_limit_windows``). To keep examples independent, that store is
cleared before each example and a unique ``user_id`` is used per example, so the
rate-limit gate never rejects a request under test.

Validates: Requirements 10.5, 10.6
"""

# Standard library
import itertools
import json
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, ConnectorConfig
from connector.models import ProposedFile
from connector import service
from connector.provider import ProviderTransientError
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

# The provider operations reached on the proposal path, each invoked exactly once on the
# success path. Injecting a bounded number of transient failures at any one of them
# exercises that op's retry loop; Hypothesis picks which op fails.
# NOTE (hardening spec, task 6.1/6.3): the propose pipeline now uses reconcile-before-retry
# for the MUTATING ops (create_branch, commit_files, open_change_proposal) so they are no
# longer blindly repeated after an ambiguous transient failure. Their retry/idempotency
# coverage moves to hardening Property H4 (test_reconcile_before_retry_property, task 6.2).
# This baseline Property 20 test is scoped to the READ-ONLY ops, which keep simple transient
# retry (safe to repeat) and whose attempt-count semantics are unchanged.
_PROVIDER_OPS = (
    "latest_commit_sha",
    "branch_exists",
)

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


def _make_config(retry_max_attempts: int) -> ConnectorConfig:
    """Build an enabled ConnectorConfig whose allowlist matches the requested repo/branch.

    ``retry_max_attempts`` is the Hypothesis-generated maximum so the property can assert
    the number of attempts against exactly the configured value.
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
        retry_max_attempts=retry_max_attempts,
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


# --- Property 20 -----------------------------------------------------------


# Feature: source-control-connector, Property 20: Transient errors are retried up to the configured maximum
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
    failing_op=st.sampled_from(_PROVIDER_OPS),
    retry_max_attempts=st.integers(min_value=1, max_value=4),
    transient_failures=st.integers(min_value=0, max_value=8),
)
def test_property20_transient_errors_are_retried_up_to_the_maximum(
    files, intent_words, failing_op, retry_max_attempts, transient_failures
):
    """Transient errors are retried up to ``config.retry_max_attempts`` (Req 10.5, 10.6).

    Facet (a) ``k < max``: the failing op succeeds after ``k`` transient failures, so it is
    invoked exactly ``k + 1`` times and the proposal succeeds. Facet (b) ``k >= max``: the
    failing op is attempted exactly ``max`` times and the result is a non-success error with
    an exhausted-retry audit entry.
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config(retry_max_attempts)
    provider = FakeProvider()
    # Program the chosen op to raise a transient error for the next ``k`` calls, then fall
    # back to the fake's default success behavior.
    provider.fail_times(failing_op, ProviderTransientError("temporary provider failure"), transient_failures)

    user_id = f"user-{next(_user_ids)}"
    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-1"})
    try:
        with (
            mock.patch.object(service, "get_secret", return_value="ghp_fake_token_value"),
            # Patch time.sleep so retry backoff does not actually wait (fast test).
            mock.patch("connector.service.time.sleep", return_value=None),
            mock.patch.object(service, "logger") as mock_logger,
        ):
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

    attempts_made = len(provider.calls_for(failing_op))

    if transient_failures < retry_max_attempts:
        # Facet (a): the op fails k times, then the (k+1)-th attempt succeeds. Because the op
        # is reached exactly once on the success path, it is invoked exactly k + 1 times.
        assert attempts_made == transient_failures + 1

        # The proposal ultimately succeeds and a pull request is created (Req 10.5).
        assert result.status == "created", result.message
        assert result.proposal_id is not None
        assert result.proposal_url is not None
        assert len(provider.pull_requests) == 1
    else:
        # Facet (b): retries are exhausted. The op is attempted EXACTLY max times — never
        # more (retries capped) and never fewer (each attempt retried until the cap).
        assert attempts_made == retry_max_attempts

        # The result is a non-success error and no pull request is created (Req 10.6).
        assert result.status == "error"
        assert result.status != "created"
        assert result.proposal_id is None
        assert result.proposal_url is None
        assert provider.pull_requests == []

        # Req 10.6: the exhausted-retry outcome is recorded in the audit log.
        exhausted_audits = [
            call
            for call in mock_logger.error.call_args_list
            if call.kwargs.get("event") == "scm_proposal"
            and call.kwargs.get("outcome") == "error"
            and call.kwargs.get("reason") == "provider_operation_failed"
        ]
        assert exhausted_audits, "expected an exhausted-retry audit entry"
        assert exhausted_audits[0].kwargs.get("requesting_user") == user_id

    # Defense-in-depth: the credential value never leaks into the agent-visible result.
    assert "ghp_fake_token_value" not in result.message
