#!/usr/bin/env python3
"""Property-based test for Property V5 — durable intent/outcome audit with reconciliation,
with **no cross-system atomicity** (`connector.service.propose_change`).

Issue #268 (R9.1, R9.2) records a durable **INTENT** event (`event="scm_intent"`) before the
first mutating provider op and a durable **OUTCOME** event (`event="scm_outcome"`) after,
correlated by the stable idempotency key, and reconciles ambiguous outcomes instead of
claiming atomicity between the audit store and the provider. This test drives the full
behavior matrix:

    mutation ∈ {success, fail} × intent-write ∈ {ok, fail} × outcome-write ∈ {ok, fail}

against an otherwise-authorized propose (intersecting groups, valid CloudFormation, under the
rate limit, ``base_revision`` == the current target head so the snapshot verifies), using a
``FakeProvider`` and a programmable ``FakeAuditSink`` whose ``write()`` confirms or fails per
event type. It asserts:

1. **INTENT precedes the first mutation.** On any path that reaches the provider mutation
   stage, an ``scm_intent`` event is written before the first
   ``create_branch``/``commit_files``/``open_change_proposal`` call (checked on a shared,
   interleaved timeline the sink and provider both append to).
2. **Correlation + no secrets.** INTENT and OUTCOME share the same ``idempotency_key`` and no
   event field carries a credential value or any proposed file's contents.
3. **Unconfirmed INTENT → abort before any mutation.** The provider records zero mutating ops,
   the result is a safe error (not ``created``, not ``reconcilable``), and no OUTCOME claims
   success.
4. **Successful mutation + unconfirmed OUTCOME → reconcilable.** The result status is
   ``reconcilable`` carrying the proposal id/url, the mutation is NOT rolled back (the
   branch/commit/proposal still exist on the provider), and success is never falsely reported.
5. **No cross-system atomicity.** A successful mutation with an unconfirmed OUTCOME is never
   rolled back, and a failed mutation yields a clean typed error with no attempt to undo
   provider state.
6. **Reject/decline emits a single OUTCOME and no INTENT** (no mutation attempted).

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
from connector.provider import ProviderUnavailableError
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
    (in order) for correlation/secret assertions, and each write is also appended to a shared
    ``timeline`` so its ordering relative to the provider's mutating ops can be asserted.
    """

    def __init__(
        self,
        *,
        intent_confirmed: bool,
        outcome_confirmed: bool,
        timeline: list[tuple[str, str]],
    ):
        self.events: list[dict] = []
        self._intent_confirmed = intent_confirmed
        self._outcome_confirmed = outcome_confirmed
        self._timeline = timeline

    def write(self, event: dict) -> bool:
        self.events.append(dict(event))
        kind = event.get("event")
        self._timeline.append(("audit", str(kind)))
        if kind == "scm_intent":
            return self._intent_confirmed
        if kind == "scm_outcome":
            return self._outcome_confirmed
        return True


class _OrderRecordingProvider(FakeProvider):
    """A :class:`FakeProvider` that records each mutating op onto a shared timeline.

    Only the three mutating operations (``create_branch``, ``commit_files``,
    ``open_change_proposal``) are recorded (before delegating to the base behavior, so even a
    programmed failure records the attempt). Interleaving these with the programmable sink's
    audit writes on one ``timeline`` lets the test assert the INTENT event is written before
    the first provider mutation.
    """

    def __init__(self, timeline: list[tuple[str, str]]):
        super().__init__()
        self._timeline = timeline

    def create_branch(self, repo, new_branch, from_sha):  # type: ignore[override]
        self._timeline.append(("mutation", "create_branch"))
        return super().create_branch(repo, new_branch, from_sha)

    def commit_files(self, repo, branch, files, message):  # type: ignore[override]
        self._timeline.append(("mutation", "commit_files"))
        return super().commit_files(repo, branch, files, message)

    def open_change_proposal(self, repo, head, base, title, body):  # type: ignore[override]
        self._timeline.append(("mutation", "open_change_proposal"))
        return super().open_change_proposal(repo, head, base, title, body)


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


def _run(
    files,
    intent_words,
    *,
    intent_confirmed: bool,
    outcome_confirmed: bool,
    mutation_succeeds: bool,
    groups=None,
    user_id: str | None = None,
):
    """Drive ``propose_change`` with a programmable sink and an order-recording provider.

    Returns ``(result, provider, sink, timeline)`` so callers can assert on the terminal
    result, the provider's applied state (mutation or none), the captured audit events, and
    the interleaved ordering of audit writes vs provider mutations. When ``mutation_succeeds``
    is ``False`` the provider is programmed to fail ``open_change_proposal`` with a
    non-retried :class:`ProviderUnavailableError`, so the branch + commit land first (proving
    the connector never rolls them back) and the proposal never opens.
    """
    # Isolate this example: clear the shared sliding-window store and use a fresh user id.
    _rate_limit_windows.clear()

    config = _make_config()
    timeline: list[tuple[str, str]] = []
    provider = _OrderRecordingProvider(timeline)
    if not mutation_succeeds:
        provider.fail("open_change_proposal", ProviderUnavailableError("provider down"))
    sink = _ProgrammableAuditSink(
        intent_confirmed=intent_confirmed,
        outcome_confirmed=outcome_confirmed,
        timeline=timeline,
    )

    resolved_user = user_id if user_id is not None else f"user-{next(_user_ids)}"
    resolved_groups = groups if groups is not None else [_GROUP]
    intent = " ".join(intent_words)
    title = f"Update {intent_words[0]} configuration"
    description = f"Adjust the {intent_words[-1]} in the infrastructure template."

    token = set_request_context({"user_id": resolved_user, "groups": resolved_groups, "session_id": "s-1"})
    try:
        with (
            patch.object(service, "_get_audit_sink", return_value=sink),
            patch.object(service.time, "sleep", lambda *_a, **_k: None),
        ):
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
    return result, provider, sink, timeline


def _assert_no_mutation(provider: FakeProvider) -> None:
    """Assert the provider created no branch, commit, or change proposal."""
    assert provider.created_branches == []
    assert provider.commits == []
    assert provider.pull_requests == []


def _assert_no_secret(sink: _ProgrammableAuditSink, files) -> None:
    """Assert no audit event (of any type) carries the credential value or any file content."""
    file_contents = [f.content for f in files]
    for event in sink.events:
        for value in event.values():
            text = str(value)
            assert _FAKE_CREDENTIAL not in text
            for content in file_contents:
                assert content not in text


def _intents(sink: _ProgrammableAuditSink) -> list[dict]:
    return [e for e in sink.events if e.get("event") == "scm_intent"]


def _outcomes(sink: _ProgrammableAuditSink) -> list[dict]:
    return [e for e in sink.events if e.get("event") == "scm_outcome"]


# --- Property V5: intent/outcome audit, non-atomic, full matrix ------------


# Feature: source-control-connector-v2, Property V5: intent/outcome audit with reconciliation, no cross-system atomicity
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
    intent_confirmed=st.booleans(),
    outcome_confirmed=st.booleans(),
    mutation_succeeds=st.booleans(),
)
def test_intent_outcome_audit_non_atomic(files, intent_words, intent_confirmed, outcome_confirmed, mutation_succeeds):
    """Durable INTENT/OUTCOME with reconciliation and no cross-system atomicity (Req 9.1, 9.2).

    Crosses mutation ∈ {success, fail} × intent-write ∈ {ok, fail} × outcome-write ∈ {ok, fail}
    over an otherwise-successful propose and asserts INTENT-before-mutation ordering, INTENT/
    OUTCOME correlation with no secrets, a safe pre-mutation abort on an unconfirmed INTENT, a
    reconcilable (never falsely successful, never rolled back) result on an unconfirmed OUTCOME
    after a successful mutation, and a clean typed error with no undo when the mutation fails.
    """
    result, provider, sink, timeline = _run(
        files,
        intent_words,
        intent_confirmed=intent_confirmed,
        outcome_confirmed=outcome_confirmed,
        mutation_succeeds=mutation_succeeds,
    )

    intents = _intents(sink)
    outcomes = _outcomes(sink)

    # Assertion 2 (no secrets): no credential value or file content leaks into any event.
    _assert_no_secret(sink, files)

    # --- Case A: unconfirmed INTENT → abort before any mutation (Assertion 3) -----------
    if not intent_confirmed:
        _assert_no_mutation(provider)
        assert result.status == "error", result.message
        assert result.status not in ("created", "reconcilable"), result.message
        assert result.proposal_id is None
        assert result.proposal_url is None
        # Exactly one INTENT was attempted; no OUTCOME (we aborted before the mutation stage).
        assert len(intents) == 1
        assert outcomes == []
        # No provider mutation was recorded on the timeline at all.
        assert not any(kind == "mutation" for kind, _ in timeline)
        return

    # INTENT confirmed: it is written, and it precedes the first provider mutation.
    assert len(intents) == 1
    mutation_indices = [i for i, (kind, _) in enumerate(timeline) if kind == "mutation"]
    intent_indices = [i for i, (kind, name) in enumerate(timeline) if kind == "audit" and name == "scm_intent"]
    # Assertion 1 (ordering): if any mutation ran, an INTENT was written before the first one.
    if mutation_indices:
        assert intent_indices, "no scm_intent recorded on the timeline"
        assert intent_indices[0] < mutation_indices[0]

    if mutation_succeeds:
        # The mutation landed on the provider: exactly one branch, commit, and proposal.
        assert len(provider.pull_requests) == 1
        assert provider.created_branches
        assert provider.commits
        assert len(outcomes) == 1

        if outcome_confirmed:
            # Both writes confirmed → created.
            assert result.status == "created", result.message
            assert result.proposal_id is not None
            assert result.proposal_url is not None
            assert outcomes[0].get("outcome") == "created"
        else:
            # Assertion 4/5: unconfirmed OUTCOME after success → reconcilable, not rolled back,
            # never a false success, and no cross-system atomicity is claimed.
            assert result.status == "reconcilable", result.message
            assert result.status != "created"
            assert result.proposal_id is not None
            assert result.proposal_url is not None
            # The mutation was NOT rolled back: the proposal still exists on the provider.
            assert len(provider.pull_requests) == 1
            assert provider.created_branches
            assert provider.commits

        # Assertion 2 (correlation): INTENT and OUTCOME share the idempotency key.
        key = intents[0].get("idempotency_key")
        assert key
        assert outcomes[0].get("idempotency_key") == key
        # The INTENT carries proposed paths only (a positive no-file-content check).
        assert "paths" in intents[0]
    else:
        # Assertion 5: the mutation failed (open_change_proposal) → clean typed error with no
        # attempt to undo the branch/commit that already landed (no rollback).
        assert result.status == "error", result.message
        assert result.status not in ("created", "reconcilable"), result.message
        assert result.proposal_id is None
        assert result.proposal_url is None
        # No proposal was opened, but the earlier successful mutations are left in place.
        assert provider.pull_requests == []
        assert provider.created_branches, "branch was rolled back — atomicity was wrongly claimed"
        assert provider.commits, "commit was rolled back — atomicity was wrongly claimed"
        # A single terminal OUTCOME was recorded, correlated to the INTENT by the key.
        assert len(outcomes) == 1
        key = intents[0].get("idempotency_key")
        assert key
        assert outcomes[0].get("idempotency_key") == key


# --- Property V5 (Assertion 6): reject/decline emits one OUTCOME and no INTENT ---------


# Feature: source-control-connector-v2, Property V5: intent/outcome audit with reconciliation, no cross-system atomicity
@settings(max_examples=100)
@given(
    files=_valid_cfn_files(),
    intent_words=st.lists(st.sampled_from(_SAFE_WORDS), min_size=2, max_size=6),
    scenario=st.sampled_from(["authz_reject", "empty_decline"]),
)
def test_reject_or_decline_emits_single_outcome_no_intent(files, intent_words, scenario):
    """A rejected/declined proposal records a single OUTCOME, no INTENT, and no mutation.

    Reject/decline paths never reach the mutation stage, so no durable INTENT is recorded and
    the provider performs zero mutating ops. Exactly one ``scm_outcome`` event is written for
    the terminal decision. Drives two paths: a five-dimension authorization reject (requesting
    groups do not intersect the authorized groups) and a decline (no IaC files to change).
    """
    if scenario == "authz_reject":
        proposed_files = files
        groups = ["some-other-group"]  # authenticated but non-intersecting → group reject
    else:
        proposed_files = []  # empty file set → declined at the IaC gate
        groups = [_GROUP]

    result, provider, sink, _timeline = _run(
        proposed_files,
        intent_words,
        intent_confirmed=True,
        outcome_confirmed=True,
        mutation_succeeds=True,
        groups=groups,
        user_id="reviewer-1",
    )

    # No mutation was attempted on the provider.
    _assert_no_mutation(provider)

    # A safe, non-success terminal result.
    assert result.status in ("rejected", "declined"), result.message
    assert result.proposal_id is None
    assert result.proposal_url is None

    # Exactly one OUTCOME event and NO preceding INTENT event.
    assert _intents(sink) == []
    outcomes = _outcomes(sink)
    assert len(outcomes) == 1
    assert outcomes[0].get("outcome") in ("rejected", "declined")

    # Defense-in-depth: no secret / file content in any event.
    _assert_no_secret(sink, files)
