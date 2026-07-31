#!/usr/bin/env python3
"""Property-based test for per-user proposal rate limiting (`connector.service.propose_change`).

Covers Correctness Property 7 from the source-control-connector design: the connector
enforces a per-Requesting_User limit on the number of successfully created change
proposals within a configured rolling window. For a per-user limit of ``N``:

- the first ``N`` proposals by a user are allowed (each opens exactly one pull request), and
- the ``(N+1)``-th (and any subsequent) request is rejected with a rate-limit result that
  states the limit and the reset time and creates no branch or proposal, while
- the limit is strictly **per user**: a different ``user_id`` starts with a fresh budget.

The service is exercised with a ``FakeProvider`` injected via ``provider=`` (whose default
behavior makes every proposal succeed) and a purpose-built :class:`ConnectorConfig` injected
via ``config=`` with ``rate_limit_max=N``. ``connector.service.get_secret`` is mocked so no
AWS call occurs, identity is supplied through the request contextvar with an authorized user,
and the real ``utils.security.check_rate_limit`` sliding-window limiter is used.

The rate limiter keeps its window state in a shared, module-level store keyed by user
(``utils.security._rate_limit_windows``). To keep examples independent, the store is cleared
before each example **and** a unique ``user_id`` is used per example so counts always start
clean.

Validates: Requirements 8.1, 8.2
"""

# Standard library
import itertools
from unittest.mock import patch

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, SourceControlConfig
from support.config_factory import make_source_control_config
from connector.models import ProposedFile
from connector.service import propose_change
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context
from utils.security import _rate_limit_windows

pytestmark = pytest.mark.unit


# --- Test constants --------------------------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"

# A benign, injection-free intent/title/description that passes input validation and
# prompt-injection detection so every request reaches the rate-limit gate.
_INTENT = "update the bucket resource memory configuration for the service stack"
_TITLE = "Update service stack configuration"
_DESCRIPTION = "Adjust the configured resource in the infrastructure template."
_IAC_FORMAT = "cloudformation"

# Structurally valid CloudFormation so IaC validation passes and the proposal succeeds.
_VALID_CFN = "Resources:\n  MyBucket:\n    Type: AWS::S3::Bucket\n"

# Monotonic source of unique user ids so each example (and each user within an example)
# starts with a fresh rate-limit budget regardless of store state.
_user_ids = itertools.count(1)


def _make_config(*, rate_limit_max: int) -> SourceControlConfig:
    """Build an enabled ConnectorConfig whose per-user proposal limit is ``rate_limit_max``."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=(_GROUP,),
        rate_limit_max=rate_limit_max,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _propose_as(user_id: str, *, config: SourceControlConfig, provider: FakeProvider):
    """Run one ``propose_change`` as ``user_id`` (authorized) and return the result.

    ``connector.service.get_secret`` is mocked so the credential fetch succeeds without any
    AWS call; the returned value never influences the rate-limit behavior under test.
    """
    token = set_request_context({"user_id": user_id, "groups": [_GROUP]})
    try:
        with patch("connector.service.get_secret", return_value="ghp_fake_token_value"):
            return propose_change(
                _INTENT,
                [ProposedFile(path="template.yaml", content=_VALID_CFN, iac_format=_IAC_FORMAT)],
                _IAC_FORMAT,
                _TITLE,
                _DESCRIPTION,
                config=config,
                provider=provider,
            )
    finally:
        reset_request_context(token)


# --- Property 7 ------------------------------------------------------------


# Feature: source-control-connector, Property 7: Per-user proposal rate limit
@settings(max_examples=100)
@given(
    rate_limit_max=st.integers(min_value=1, max_value=6),
    extra_calls=st.integers(min_value=1, max_value=4),
)
def test_property7_per_user_proposal_rate_limit(rate_limit_max, extra_calls):
    """First N proposals per user are allowed; the (N+1)-th is rate-limited; per-user budget.

    For a per-user limit ``N == rate_limit_max`` and a total of ``N + extra_calls`` sequential
    requests by one authorized user: exactly the first ``N`` return a created proposal and
    every request after that is rejected with a rate-limit message and performs no provider
    mutation. A second, distinct user still gets its own full budget (Req 8.1, 8.2).
    """
    # Isolate this example: clear the shared sliding-window store and use fresh user ids.
    _rate_limit_windows.clear()

    config = _make_config(rate_limit_max=rate_limit_max)
    provider = FakeProvider()

    user_a = f"user-a-{next(_user_ids)}"
    total_calls = rate_limit_max + extra_calls

    results = [_propose_as(user_a, config=config, provider=provider) for _ in range(total_calls)]

    # The first N requests succeed (each opens exactly one pull request).
    allowed = results[:rate_limit_max]
    for result in allowed:
        assert result.status == "created"
        assert result.proposal_id
        assert result.proposal_url

    # Every request beyond the limit is rejected with a rate-limit message stating the
    # limit and the reset time, and creates no branch or proposal (Req 8.2).
    rejected = results[rate_limit_max:]
    for result in rejected:
        assert result.status == "rejected"
        assert result.proposal_id is None
        assert result.proposal_url is None
        assert str(rate_limit_max) in result.message
        assert "resets" in result.message.lower()

    # Exactly N pull requests were opened for user A — the excess requests never reached
    # the provider mutation operations.
    assert len(provider.calls_for("open_change_proposal")) == rate_limit_max
    assert len(provider.calls_for("create_branch")) == rate_limit_max

    # Per-user isolation: a different user has its own budget, so its first proposal is
    # allowed even though user A is already rate-limited (Req 8.1 — "per Requesting_User").
    user_b = f"user-b-{next(_user_ids)}"
    result_b = _propose_as(user_b, config=config, provider=provider)
    assert result_b.status == "created"
    assert result_b.proposal_id

    # User B's success added exactly one more pull request (total N + 1 across both users).
    assert len(provider.calls_for("open_change_proposal")) == rate_limit_max + 1
