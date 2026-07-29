#!/usr/bin/env python3
"""Property-based test for non-destructive conflict handling (`connector.service.propose_change`).

Covers Correctness Property 19 from the source-control-connector design: whenever a provider
operation raises :class:`connector.provider.ProviderConflictError` during the propose pipeline,
the connector reports the conflict **without any destructive resolution**. For *any* provider
operation in the propose flow that raises a conflict, ``propose_change``:

- returns an **error** result — never ``"created"`` — carrying **no** pull-request id/url
  (Req 10.3, 10.4),
- returns a message that conveys the conflict and that existing content was **preserved**
  (Req 10.4),
- performs **no destructive resolution**: the provider abstraction defines no
  merge/approve/close/force operation, so no such op is ever issued, and — crucially — **no
  provider operation runs after the conflicting op** (the conflict aborts the pipeline; nothing
  attempts to overwrite or force past it), and
- does **not** retry the conflicting operation (a conflict is not a transient error), so the
  failing op is invoked exactly once.

The service is exercised with a ``FakeProvider`` injected via ``provider=`` (programmed to raise
``ProviderConflictError`` on one Hypothesis-chosen operation), an enabled
:class:`ConnectorConfig` injected via ``config=`` whose allowlist matches the requested
repository/branch, and ``connector.service.get_secret`` mocked so no AWS call occurs. An
authorized identity is supplied through the request contextvar. The proposed IaC is
structurally valid and the repo/branch match the allowlist, so every example reaches the
provider operations where the conflict is triggered.

To keep examples independent, the shared sliding-window rate-limit store is cleared before each
example and a unique ``user_id`` is used per example, so the rate-limit gate never rejects a
request under test.

Validates: Requirements 10.4
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
from connector.service import propose_change
from connector.provider import ProviderConflictError
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context
from utils.security import _rate_limit_windows

pytestmark = pytest.mark.unit


# --- Test constants --------------------------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"
_IAC_FORMAT = "cloudformation"

# The propose pipeline's provider operations, in the order they run. A conflict may surface at
# any of them; Hypothesis picks which one raises so the property holds for every op.
_PROPOSE_OPS = (
    "latest_commit_sha",
    "branch_exists",
    "create_branch",
    "commit_files",
    "open_change_proposal",
)

# Operations that mutate source control. None of these is a merge/approve/close/force op — the
# abstraction intentionally has no such operation — so any destructive resolution is
# structurally impossible; we additionally assert nothing runs *after* the conflict.
_MUTATING_OPS = ("create_branch", "commit_files", "open_change_proposal")

# Benign, injection-free words used to build varying intents/titles so input validation and
# prompt-injection detection always pass and every request reaches the provider operations.
_SAFE_WORDS = [
    "update",
    "storage",
    "bucket",
    "queue",
    "configuration",
    "resource",
    "template",
    "stack",
]

_RESOURCE_TYPES = [
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
    "AWS::SNS::Topic",
    "AWS::DynamoDB::Table",
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


# --- Property 19 -----------------------------------------------------------


# Feature: source-control-connector, Property 19: Merge conflicts are reported without destructive resolution
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
    failing_op=st.sampled_from(_PROPOSE_OPS),
)
def test_property19_merge_conflict_is_reported_without_destructive_resolution(
    files, intent_words, failing_op
):
    """A provider conflict is surfaced safely: error result, no PR, no destructive op, no retry.

    For any provider operation in the propose flow that raises ``ProviderConflictError``, the
    connector reports the conflict (error status + preserved-content message), opens no pull
    request, issues no operation after the conflict (no force/overwrite resolution), and does
    not retry the conflicting op (Req 10.4).
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    provider = FakeProvider()
    # Trigger a conflict on the Hypothesis-chosen operation.
    provider.fail(failing_op, ProviderConflictError("merge conflict against target branch"))

    user_id = f"user-{next(_user_ids)}"
    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-1"})
    try:
        with (
            mock.patch.object(service, "get_secret", return_value="ghp_fake_token_value"),
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

    # Req 10.4: the conflict is reported as an error and NEVER as a created proposal.
    assert result.status == "error", result.message
    assert result.status != "created"

    # No pull-request id/url is returned — nothing was successfully proposed.
    assert result.proposal_id is None
    assert result.proposal_url is None

    # The message conveys the conflict and that existing content was preserved (Req 10.4).
    message = result.message.lower()
    assert "conflict" in message
    assert "preserved" in message

    # No pull request was ever opened (whether the conflict was on open_change_proposal or earlier).
    assert provider.pull_requests == []

    # No destructive resolution: the failing op is the LAST provider call — nothing runs after
    # the conflict to overwrite/force/resolve it. (The abstraction also defines no
    # merge/approve/close/force op, so such an op can never be recorded.)
    op_names = provider.call_operations
    assert op_names, "expected the pipeline to reach the provider operations"
    assert op_names[-1] == failing_op

    # The conflicting op is not retried (a conflict is not transient): it is invoked exactly
    # once, and no provider op appears after it.
    assert op_names.count(failing_op) == 1
    assert op_names.index(failing_op) == len(op_names) - 1

    # Whichever mutating ops ran did so strictly BEFORE the conflict; none ran afterward.
    conflict_index = len(op_names) - 1
    for position, name in enumerate(op_names):
        if name in _MUTATING_OPS:
            assert position <= conflict_index

    # A conflict audit entry was recorded (Req 10.4 reporting).
    conflict_audits = [
        call
        for call in mock_logger.error.call_args_list
        if call.kwargs.get("event") == "scm_proposal"
        and call.kwargs.get("reason") == "provider_conflict"
    ]
    assert conflict_audits, "expected a provider_conflict audit entry"
    audit = conflict_audits[0].kwargs
    assert audit.get("outcome") == "error"
    assert audit.get("requesting_user") == user_id

    # Defense-in-depth: the credential value never leaks into the agent-visible result.
    assert "ghp_fake_token_value" not in result.message
