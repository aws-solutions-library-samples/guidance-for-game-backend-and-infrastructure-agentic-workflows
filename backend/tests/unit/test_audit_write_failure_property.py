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

The audit trail now flows through the durable, confirmed CloudWatch Logs sink
(``connector.audit.AuditSink``). This test drives that sink directly by patching
``connector.service._get_audit_sink`` with a programmable fake whose ``write`` returns the
confirmed/unconfirmed outcome under test:

- an **unconfirmed** write (``write`` returns ``False``) models a durable-audit-sink failure
  at the exact point the success would be recorded; the connector's ``_audit`` helper
  returns ``False`` and ``_finalize`` converts the intended ``status="created"`` result into
  an audit-persistence error (``status="error"``). Success is NEVER reported.
- a **confirmed** write (``write`` returns ``True``) records the success durably and the
  proposal is reported as ``created`` — success requires a confirmed durable write.

Hypothesis varies the proposed file set, the (injection-free) intent/title/description text,
and the confirmed/unconfirmed write outcome so the property holds across many inputs.

Validates: Requirements 6.4, 13.2, 13.3
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
from connector import service
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


class _ProgrammableAuditSink:
    """A fake :class:`connector.audit.AuditSink` whose ``write`` returns a fixed outcome.

    ``confirmed=False`` models a durable-audit-sink failure (an unconfirmed write): every
    ``write`` returns ``False`` so the connector aborts the action and returns an
    audit-persistence error. ``confirmed=True`` models a confirmed durable write so the
    success can be reported. The events written are captured for optional inspection.
    """

    def __init__(self, confirmed: bool):
        self.events: list[dict] = []
        self._confirmed = confirmed

    def write(self, event: dict) -> bool:
        self.events.append(dict(event))
        return self._confirmed


def _make_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig whose allowlist matches the requested repo/branch."""
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


# --- Property 22 -----------------------------------------------------------


def _run(files, intent_words, *, confirmed: bool):
    """Drive an otherwise-successful ``propose_change`` with a sink of the given outcome."""
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    provider = FakeProvider()
    sink = _ProgrammableAuditSink(confirmed=confirmed)

    user_id = f"user-{next(_user_ids)}"
    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-1"})
    try:
        with patch("connector.service.get_secret", return_value="ghp_fake_token_value"), patch.object(
            service, "_get_audit_sink", return_value=sink
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
    return result


# Feature: source-control-connector, Property 22: Audit-write failure aborts the action atomically
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
)
def test_property22_unconfirmed_audit_write_aborts_atomically(files, intent_words):
    """When the durable audit write is unconfirmed, propose_change returns an error, not created.

    For a scenario that would otherwise succeed (enabled, authorized, allowlist-matching,
    valid IaC, provider succeeds), an unconfirmed sink write (``write`` returns ``False``)
    causes the connector to abort the action atomically: the returned result is an
    audit-persistence error (``status="error"``) and is NEVER reported as ``created`` — a
    proposal is never reported successful without a confirmed durable audit record
    (Req 6.4, 13.2, 13.3).
    """
    result = _run(files, intent_words, confirmed=False)

    # Req 13.3: success is NEVER reported when the durable audit write is not confirmed.
    assert result.status != "created", result.message
    assert result.status == "error", result.message

    # The result is specifically the audit-persistence error and carries no proposal handle.
    assert "audit record could not be persisted" in result.message
    assert result.proposal_id is None
    assert result.proposal_url is None

    # Defense-in-depth: the credential value never leaks into the agent-visible result.
    assert "ghp_fake_token_value" not in result.message


# Feature: source-control-connector, Property 22: Success requires a confirmed durable write
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
)
def test_property22_confirmed_audit_write_reports_success(files, intent_words):
    """When the durable audit write is confirmed, an otherwise-successful proposal is created.

    The complement of the abort property: with the same otherwise-successful scenario a
    confirmed sink write (``write`` returns ``True``) lets the connector report the proposal
    as ``created`` — demonstrating that success requires, and follows from, a confirmed
    durable audit record (Req 13.2, 13.3).
    """
    result = _run(files, intent_words, confirmed=True)

    assert result.status == "created", result.message
    assert result.proposal_id is not None
    assert result.proposal_url is not None
