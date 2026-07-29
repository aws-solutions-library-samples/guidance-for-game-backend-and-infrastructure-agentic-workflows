#!/usr/bin/env python3
"""Property-based test for atomic abort on audit-write failure (`connector.service.propose_change`).

Covers Correctness Property 22 from the source-control-connector design: a change proposal
is only ever reported as successful when a durable audit record has been written. If the
audit sink fails while recording the success of an otherwise-complete proposal, the
connector MUST NOT report success. Instead it aborts the action atomically and returns an
audit-persistence error result (Req 6.4).

The service is exercised with a scenario that would otherwise succeed:

- a ``FakeProvider`` injected via ``provider=`` whose default behavior makes every provider
  operation succeed,
- an enabled :class:`ConnectorConfig` injected via ``config=`` whose allowlist matches the
  requested repository/branch,
- ``connector.service.get_secret`` mocked so no AWS call occurs, and
- an authorized ``user_id`` supplied through the request contextvar.

The audit sink is programmed to fail by patching ``connector.service.logger`` with a fake
whose success-audit method (``info``) raises. Because the connector's ``_audit`` helper
catches the exception and returns ``False``, ``_finalize`` converts the intended
``status="created"`` result into an audit-persistence error (``status="error"``). The test
asserts the returned result is that error and is never reported as ``created``.

Hypothesis varies the proposed file set, the (injection-free) intent/title/description text,
and the exception raised by the audit sink so the property holds across many inputs.

Validates: Requirements 6.4
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

# Benign words used to build varying, injection-free intents/titles so the input-validation
# and prompt-injection gates always pass and every request reaches the success audit.
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


class _RaisingLogger:
    """A fake ``logger`` whose success-audit method (``info``) raises the given exception.

    Only ``info`` — the level used by the success audit in :func:`propose_change` — raises,
    modeling a durable-audit-sink failure at the exact point the proposal would be reported
    as created. Every other level is a no-op so any incidental audit call cannot mask the
    behavior under test.
    """

    def __init__(self, exc: Exception):
        self._exc = exc

    def info(self, *args, **kwargs):
        raise self._exc

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


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
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


@st.composite
def _valid_cfn_files(draw):
    """Generate 1..N distinct, structurally valid CloudFormation ``ProposedFile``s.

    Each file has a unique path (index-suffixed) and a JSON body (valid YAML too) with a
    non-empty ``Resources`` mapping whose single resource declares a ``Type`` — exactly what
    the IaC validation gate requires so the request reaches the success audit.
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


@st.composite
def _audit_failures(draw):
    """Generate a varied exception instance for the audit sink to raise."""
    exc_type = draw(
        st.sampled_from([RuntimeError, OSError, ValueError, ConnectionError, Exception])
    )
    message = draw(
        st.sampled_from(
            [
                "audit sink unavailable",
                "disk full",
                "log backend unreachable",
                "failed to persist audit record",
                "unexpected audit error",
            ]
        )
    )
    return exc_type(message)


# --- Property 22 -----------------------------------------------------------


# Feature: source-control-connector, Property 22: Audit-write failure aborts the action atomically
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
    audit_exc=_audit_failures(),
)
def test_property22_audit_write_failure_aborts_atomically(files, intent_words, audit_exc):
    """When the success-audit write raises, propose_change returns an error, not ``created``.

    For a scenario that would otherwise succeed (enabled, authorized, allowlist-matching,
    valid IaC, provider succeeds), programming the audit sink to fail on the success write
    causes the connector to abort the action atomically: the returned result is an
    audit-persistence error (``status="error"``) and is NEVER reported as ``created`` — a
    proposal is never reported successful without a durable audit record (Req 6.4).
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    provider = FakeProvider()

    user_id = f"user-{next(_user_ids)}"
    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-1"})
    try:
        with patch("connector.service.get_secret", return_value="ghp_fake_token_value"), patch(
            "connector.service.logger", _RaisingLogger(audit_exc)
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

    # Req 6.4: success is NEVER reported when the durable audit record could not be written.
    assert result.status != "created", result.message
    assert result.status == "error", result.message

    # The result is specifically the audit-persistence error and carries no proposal handle.
    assert "audit record could not be persisted" in result.message
    assert result.proposal_id is None
    assert result.proposal_url is None

    # Defense-in-depth: the credential value never leaks into the agent-visible result.
    assert "ghp_fake_token_value" not in result.message
