#!/usr/bin/env python3
"""Property-based test that non-file-expressible requests are declined cleanly.

Covers Correctness Property 13 from the source-control-connector design: if the Agent's
requested change cannot be expressed as a modification to files in the IaC repository — the
canonical case being a request that carries **no** IaC file modifications (an empty ``files``
list) — the Connector SHALL decline the request without creating a Proposal_Branch or
Change_Proposal and SHALL return a message explaining why (Req 2.7).

Concretely, this proves the safety-critical decline path:

- The result is a ``declined`` :class:`ProposalResult` whose message explains the change
  cannot be expressed as a pull request.
- **No** source-control operation is issued — the ``FakeProvider`` records zero calls, so no
  branch is created, nothing is committed, and no pull request is opened.
- Success is **never** reported: the status is never ``created`` and no pull-request id/url
  is returned.

The service is exercised with a ``FakeProvider`` injected via ``provider=`` and a
purpose-built enabled :class:`ConnectorConfig` injected via ``config=``. Hypothesis varies the
``intent``/``title``/``description`` (benign, injection-free text) while the ``files`` list is
always empty. An authorized user is placed in the request context (set and reset per example so
identity never leaks), ``connector.service.get_secret`` is mocked so the credential gate passes,
and per-user rate limiting is neutralized so this test isolates the empty-file-set decline that
runs after the earlier gates.

Validates: Requirements 2.7
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# Local modules
from connector.config import AllowlistEntry, SourceControlConfig
from support.config_factory import make_source_control_config
from connector import service
from connector.service import propose_change
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# --- Fixed, known configuration --------------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"

# An authorized requesting user whose group intersects the configured authorized groups, so
# the pipeline advances past the authorization gate to the empty-file-set decline.
_AUTHORIZED_CONTEXT = {"user_id": "user-123", "groups": [_GROUP], "session_id": "s-1"}

# Every provider operation (used to assert ZERO calls on the declined path).
_ALL_OPS = (
    "get_file",
    "get_files",
    "branch_exists",
    "latest_commit_sha",
    "create_branch",
    "commit_files",
    "open_change_proposal",
)


def _make_config() -> SourceControlConfig:
    """Build an enabled config whose default allowlist entry matches the request."""
    return make_source_control_config(
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
    )


# --- Benign, injection-free free-text generator ----------------------------
#
# The pipeline runs input validation and prompt-injection detection *before* the
# empty-file-set decline. To isolate the decline behavior, generated text is drawn from a
# benign vocabulary and joined with spaces so it never matches an ``INJECTION_PATTERNS``
# entry and is never empty after ``validate_prompt`` strips it. An ``assume`` guard makes the
# non-injection invariant explicit and robust against any accidental phrasing.

_WORDS = [
    "update", "bucket", "stack", "resource", "memory", "configuration", "service",
    "cluster", "policy", "template", "infrastructure", "enable", "versioning",
    "scaling", "capacity", "instance", "network", "adjust", "the", "for", "and",
]


@st.composite
def _benign_text(draw, *, min_words: int = 1, max_words: int = 12) -> str:
    """A non-empty, injection-free phrase built from a benign vocabulary."""
    words = draw(st.lists(st.sampled_from(_WORDS), min_size=min_words, max_size=max_words))
    text = " ".join(words).strip()
    assume(text)
    assume(not service._looks_injected(text))
    return text


# --- Property 13 -----------------------------------------------------------


# Feature: source-control-connector, Property 13: Non-file-expressible requests are declined cleanly
@settings(max_examples=100)
@given(
    intent=_benign_text(),
    title=_benign_text(),
    description=_benign_text(),
)
def test_property13_empty_files_declined_cleanly(intent, title, description):
    """Empty file set: declined result, ZERO provider operations, never reports success.

    For any (benign) intent/title/description, a request carrying no IaC file modifications is
    declined without contacting the provider: no branch is created, nothing is committed, and
    no pull request is opened, and the connector never reports success (Req 2.7).
    """
    fake = FakeProvider()

    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        with (
            mock.patch.object(service, "check_rate_limit", return_value=None),
        ):
            result = propose_change(
                intent=intent,
                files=[],  # non-file-expressible: no IaC modifications
                iac_format="cloudformation",
                title=title,
                description=description,
                config=_make_config(),
                provider=fake,
            )
    finally:
        reset_request_context(token)

    # The request is declined cleanly with an explanatory message (Req 2.7).
    assert result.status == "declined"
    assert result.proposal_id is None
    assert result.proposal_url is None
    assert result.message
    # The message explains the change cannot be expressed as a pull request.
    lowered = result.message.lower()
    assert "no" in lowered and "proposal" in lowered

    # Success is never reported.
    assert result.status != "created"

    # ZERO provider operations of any kind were issued — no branch/commit/PR was created.
    assert fake.calls == []
    for op in _ALL_OPS:
        assert fake.calls_for(op) == [], f"declined path unexpectedly invoked {op}"
    assert fake.created_branches == []
    assert fake.commits == []
    assert fake.pull_requests == []
