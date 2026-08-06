#!/usr/bin/env python3
"""Property-based tests for the durable intent/outcome audit model — NOT atomic
(`connector.service.propose_change`).

This file replaces the shipped baseline Property 22 ("audit-write failure aborts the action
atomically"). Issue #268 (R9.2) deliberately **reverses** that cross-system atomicity claim:
the connector now records a durable **INTENT** event before the first mutating provider op and
a durable **OUTCOME** event after, correlated by the stable idempotency key, and reconciles
ambiguous outcomes instead of claiming atomicity between the audit store and the provider.

The two audit-write-failure behaviors this proves (the full success/fail × intent × outcome
matrix is covered by the dedicated Property V5 test, task 6.2):

- **Unconfirmed INTENT → abort before any mutation.** If the durable INTENT write is not
  confirmed, the connector aborts *before* any branch/commit/proposal is created. This is a
  genuinely safe fail-closed abort — nothing has been mutated yet — and is **not** an
  atomicity claim: it only means "do not start work we cannot audit". The result is a safe
  error and the provider performs no mutation.
- **Unconfirmed OUTCOME after a successful mutation → reconcilable, not rolled back.** If the
  mutation succeeds but the durable OUTCOME write is not confirmed, the connector does **not**
  report a false success and does **not** roll back the mutation. It returns a distinct
  ``status="reconcilable"`` result: the proposal exists on the provider and is reconcilable
  from the recorded INTENT plus provider state. No cross-system atomicity is claimed.

The complement (INTENT confirmed AND OUTCOME confirmed) reports the proposal as ``created``.

The service is exercised with a scenario that would otherwise succeed: a ``FakeProvider``
injected via ``provider=`` whose default behavior makes every provider operation succeed, an
enabled :class:`SourceControlConfig` whose allowlist matches the requested repository/branch,
an authorized ``user_id`` supplied through the request contextvar, and a programmable audit
sink patched over ``connector.service._get_audit_sink`` that returns a chosen confirmed/
unconfirmed result **per event type** (``scm_intent`` vs ``scm_outcome``).

Validates: Requirements 9.1, 9.2
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
from connector.models import ProposedFile
from connector.service import propose_change
from support.config_factory import make_source_control_config
from support.fake_provider import DEFAULT_HEAD_SHA, FakeProvider
from utils.request_context import reset_request_context, set_request_context
from utils.security import _rate_limit_windows

pytestmark = pytest.mark.unit


# --- Test constants --------------------------------------------------------

_REPO = "org/iac-repo"
_BRANCH = "main"
_GROUP = "scm-writers"
_IAC_FORMAT = "cloudformation"
_FAKE_CREDENTIAL = "ghp_fake_token_value"

# Benign words used to build varying, injection-free intents/titles so the input-validation
# and prompt-injection gates always pass and every request reaches the mutation stage.
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
    """A fake :class:`connector.audit.AuditSink` returning a chosen result per event type.

    The intent/outcome model writes two distinct durable events: ``scm_intent`` (before the
    first mutation) and ``scm_outcome`` (after the provider ops resolve). This sink lets a test
    independently confirm/unconfirm each: ``intent_confirmed`` gates the ``scm_intent`` write
    and ``outcome_confirmed`` gates the ``scm_outcome`` write. Every event written is captured
    (in order) for correlation/ordering/secret assertions.
    """

    def __init__(self, *, intent_confirmed: bool, outcome_confirmed: bool):
        self.events: list[dict] = []
        self._intent_confirmed = intent_confirmed
        self._outcome_confirmed = outcome_confirmed

    def write(self, event: dict) -> bool:
        self.events.append(dict(event))
        kind = event.get("event")
        if kind == "scm_intent":
            return self._intent_confirmed
        if kind == "scm_outcome":
            return self._outcome_confirmed
        return True


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
    """Generate 1..N distinct, structurally valid CloudFormation ``ProposedFile``s."""
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


def _run(files, intent_words, *, intent_confirmed: bool, outcome_confirmed: bool):
    """Drive an otherwise-successful ``propose_change`` with a programmable audit sink.

    Returns ``(result, provider, sink)`` so callers can assert on the terminal result, the
    provider's applied state (mutation or none), and the captured audit events.
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    provider = FakeProvider()
    sink = _ProgrammableAuditSink(intent_confirmed=intent_confirmed, outcome_confirmed=outcome_confirmed)

    user_id = f"user-{next(_user_ids)}"
    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    token = set_request_context({"user_id": user_id, "groups": [_GROUP], "session_id": "s-1"})
    try:
        with patch.object(service, "_get_audit_sink", return_value=sink):
            result = propose_change(
                intent,
                files,
                _IAC_FORMAT,
                title,
                description,
                base_revision=DEFAULT_HEAD_SHA,
                config=config,
                provider=provider,
            )
    finally:
        reset_request_context(token)
    return result, provider, sink


def _assert_no_mutation(provider: FakeProvider) -> None:
    """Assert the provider created no branch, commit, or change proposal."""
    assert provider.created_branches == []
    assert provider.commits == []
    assert provider.pull_requests == []


def _assert_no_secret(sink: _ProgrammableAuditSink) -> None:
    """Assert no audit event (of any type) carries the credential value."""
    for event in sink.events:
        for value in event.values():
            assert _FAKE_CREDENTIAL not in str(value)


# --- Unconfirmed INTENT aborts before any mutation (Req 9.2) ---------------


# Feature: source-control-connector-v2, intent/outcome audit: unconfirmed INTENT aborts before any mutation
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
)
def test_unconfirmed_intent_aborts_before_any_mutation(files, intent_words):
    """An unconfirmed INTENT write aborts before any provider mutation (safe, not atomic).

    For a scenario that would otherwise succeed, if the durable INTENT write is unconfirmed the
    connector performs NO branch/commit/proposal, returns a safe (non-created) error result,
    and never reports success. Because nothing was mutated, this abort is a safe fail-closed
    decline, not a cross-system atomicity claim (Req 9.2).
    """
    result, provider, sink = _run(files, intent_words, intent_confirmed=False, outcome_confirmed=True)

    # No mutation occurred at all — the abort happened before the first mutating op.
    _assert_no_mutation(provider)

    # Success is never reported; the result is a safe, non-created error with no proposal.
    assert result.status != "created", result.message
    assert result.status != "reconcilable", result.message
    assert result.status == "error", result.message
    assert result.proposal_id is None
    assert result.proposal_url is None

    # Exactly one INTENT event was attempted and NO OUTCOME event was written (we aborted).
    intent_events = [e for e in sink.events if e.get("event") == "scm_intent"]
    outcome_events = [e for e in sink.events if e.get("event") == "scm_outcome"]
    assert len(intent_events) == 1
    assert outcome_events == []

    # Defense-in-depth: no secret leaks into the result or the audit events.
    assert _FAKE_CREDENTIAL not in result.message
    _assert_no_secret(sink)


# --- Unconfirmed OUTCOME after a successful mutation is reconcilable, not rolled back -------


# Feature: source-control-connector-v2, intent/outcome audit: unconfirmed OUTCOME after success is reconcilable, not atomic
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
)
def test_unconfirmed_outcome_after_success_is_reconcilable_not_atomic(files, intent_words):
    """A successful mutation with an unconfirmed OUTCOME yields a reconcilable result.

    When the INTENT is confirmed and the provider mutation succeeds but the durable OUTCOME
    write is unconfirmed, the connector does NOT report a false success and does NOT roll back
    the mutation: the change proposal remains on the provider and the result is a distinct
    ``status="reconcilable"`` handle (with the proposal id/url) reconcilable from the recorded
    intent + provider state. No cross-system atomicity is claimed (Req 9.1, 9.2).
    """
    result, provider, sink = _run(files, intent_words, intent_confirmed=True, outcome_confirmed=False)

    # The mutation actually happened and was NOT rolled back: the proposal exists on provider.
    assert len(provider.pull_requests) == 1
    assert provider.created_branches
    assert provider.commits

    # No false success: the result is the distinct reconcilable status, never "created".
    assert result.status != "created", result.message
    assert result.status == "reconcilable", result.message
    # The reconcilable result still surfaces the proposal handle so it can be reconciled.
    assert result.proposal_id is not None
    assert result.proposal_url is not None
    assert "reconcilable" in result.message.lower()

    # INTENT preceded the mutation and correlates with the (attempted) OUTCOME by the key.
    intent_events = [e for e in sink.events if e.get("event") == "scm_intent"]
    outcome_events = [e for e in sink.events if e.get("event") == "scm_outcome"]
    assert len(intent_events) == 1
    assert len(outcome_events) == 1
    assert sink.events[0].get("event") == "scm_intent"  # INTENT written first, before mutation
    key = intent_events[0].get("idempotency_key")
    assert key and outcome_events[0].get("idempotency_key") == key
    # The INTENT carries proposed paths only (never file contents) — a positive no-secret check.
    assert "paths" in intent_events[0]
    _assert_no_secret(sink)


# --- Both writes confirmed reports success (complement) --------------------


# Feature: source-control-connector-v2, intent/outcome audit: confirmed intent + outcome reports created
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
)
def test_confirmed_intent_and_outcome_reports_created(files, intent_words):
    """With both durable writes confirmed, an otherwise-successful proposal is created.

    The complement of the failure paths: a confirmed INTENT (before mutation) and a confirmed
    OUTCOME (after) let the connector report the proposal as ``created``. Two correlated events
    are recorded — INTENT then OUTCOME — sharing the idempotency key (Req 9.1).
    """
    result, provider, sink = _run(files, intent_words, intent_confirmed=True, outcome_confirmed=True)

    assert result.status == "created", result.message
    assert result.proposal_id is not None
    assert result.proposal_url is not None
    assert len(provider.pull_requests) == 1

    intent_events = [e for e in sink.events if e.get("event") == "scm_intent"]
    outcome_events = [e for e in sink.events if e.get("event") == "scm_outcome"]
    assert len(intent_events) == 1
    assert len(outcome_events) == 1
    # INTENT is written before OUTCOME, and both correlate by the stable idempotency key.
    assert sink.events.index(intent_events[0]) < sink.events.index(outcome_events[0])
    key = intent_events[0].get("idempotency_key")
    assert key and outcome_events[0].get("idempotency_key") == key
    assert outcome_events[0].get("outcome") == "created"
    _assert_no_secret(sink)
