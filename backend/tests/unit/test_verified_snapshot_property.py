#!/usr/bin/env python3
"""Property-based test for the verified source snapshot / read-before-write gate.

Covers Correctness Property V4 from the source-control-connector-v2 design: a
Verified_Source_Snapshot (``base_revision``) is required before a proposed change is
accepted. ``connector.service.propose_change`` accepts a proposal **if and only if** the
``base_revision`` is present AND still equals the current head of the target branch at
propose time; an absent or stale snapshot rejects the proposal without creating any branch,
commit, or Change_Proposal, and an accepted proposal branch is anchored to the verified
revision (never to a later "latest").

For any ``base_revision`` drawn from {absent (""), matching the current head, stale (a value
that differs from the current head)} crossed with generated head SHAs, driven through an
otherwise-authorized propose (an authenticated caller whose groups intersect the authorized
groups, valid CloudFormation content, and a cleared rate-limit window), this proves:

  1. ACCEPT iff ``base_revision`` is present AND equals the current head → status
     ``"created"``, exactly one ``create_branch`` and one ``open_change_proposal``, and the
     created branch is based on the verified revision (``from_sha == base_revision == head``).
  2. ABSENT ("") → status ``"rejected"`` (reason ``missing_snapshot``), with NO
     ``create_branch``/``commit_files``/``open_change_proposal`` performed.
  3. STALE (``base_revision`` differs from the current head) → status ``"rejected"`` (reason
     ``stale_snapshot``), with NO branch/commit/proposal created.
  4. The accepted proposal branch is anchored to the verified revision, not to a later head.

Identity/groups come only from the trusted request contextvar (``utils.request_context``),
never from model/tool input. The provider is a ``FakeProvider`` injected via ``provider=``
whose target-branch head is seeded with ``set_head``; the audit sink is the confirming
in-memory sink from ``conftest`` so no AWS call occurs. On every reject path the provider's
``created_branches``/``commits``/``pull_requests`` capture lists and the mutating-op call
records are asserted empty, proving no source-control state was created.

Validates: Requirements 7.1, 7.2
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# Local modules
import utils.security as security
from connector import service as service_module
from connector.config import AllowlistEntry, SourceControlConfig
from connector.models import ProposedFile
from connector.service import propose_change, read_iac_files
from support.config_factory import make_source_control_config
from support.fake_provider import DEFAULT_HEAD_SHA, FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# --- Fixtures / constants --------------------------------------------------

# The single configured repository/branch the propose path defaults to (allowlist[0]); the
# request omits repo/branch so it lands an exact allowlist match and reaches the snapshot
# gate. The target head is seeded on this repo+branch via ``set_head``.
_REPO = "org/iac-repo"
_BRANCH = "main"

# An authorized requesting user whose group intersects the configured authorized groups, so
# authentication + the five authorization dimensions all pass and the snapshot gate is the
# only decision under test.
_AUTHORIZED_CONTEXT = {"user_id": "user-123", "groups": ["scm-writers"], "session_id": "s-v4"}

# A benign intent/title/description that passes input validation + prompt-injection
# detection, and structurally valid CloudFormation so IaC validation passes.
_INTENT = "Update the storage bucket configuration in the infrastructure template"
_TITLE = "Update bucket configuration"
_DESCRIPTION = "Adjust the storage bucket settings to match the requested configuration."
_VALID_CFN = "Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n"

# The mutating provider operations that a rejected proposal must NEVER invoke.
_MUTATION_OPS = ("create_branch", "commit_files", "open_change_proposal")

# Source revisions: opaque provider-produced SHA-like strings.
_shas = st.from_regex(r"[0-9a-f]{7,40}", fullmatch=True)


def _make_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig backed by a single-entry allowlist."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,)),),
        authorized_groups=("scm-writers",),
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _proposed_files() -> list[ProposedFile]:
    return [ProposedFile(path="infra/template.yaml", content=_VALID_CFN, iac_format="cloudformation")]


def _call_propose(fake: FakeProvider, base_revision: str):
    """Invoke ``propose_change`` with an authorized user and a cleared rate-limit window."""
    # Neutralize the per-user rate-limit sliding window so a permitted request always reaches
    # the snapshot gate and the snapshot decision is isolated.
    security._rate_limit_windows.clear()
    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        return propose_change(
            _INTENT,
            _proposed_files(),
            iac_format="cloudformation",
            title=_TITLE,
            description=_DESCRIPTION,
            base_revision=base_revision,
            config=_make_config(),
            provider=fake,
        )
    finally:
        reset_request_context(token)


def _rejection_reasons(mock_logger) -> list[str]:
    """Collect the ``reason`` values from any rejection warning audits.

    A rejected proposal performs no mutation, so under the intent/outcome model it emits a
    single ``event="scm_outcome"`` record (with no preceding intent) rather than the former
    ``scm_rejected`` label.
    """
    return [
        call.kwargs.get("reason")
        for call in mock_logger.warning.call_args_list
        if call.kwargs.get("event") == "scm_outcome"
    ]


def _assert_no_source_state(fake: FakeProvider) -> None:
    """Assert no branch/commit/proposal was created (capture lists + call records empty)."""
    assert fake.created_branches == []
    assert fake.commits == []
    assert fake.pull_requests == []
    for op in _MUTATION_OPS:
        assert fake.calls_for(op) == [], f"rejected propose unexpectedly invoked {op}"


# --- Hypothesis strategy ---------------------------------------------------


@st.composite
def _scenarios(draw):
    """Generate ``base_revision`` ∈ {absent, matching head, stale} × head SHAs."""
    category = draw(st.sampled_from(["absent", "match", "stale"]))
    head_sha = draw(_shas)

    if category == "absent":
        base_revision = ""
    elif category == "match":
        base_revision = head_sha
    else:  # stale — a value that differs from the current head
        other = draw(_shas)
        assume(other != head_sha)
        base_revision = other

    return {"category": category, "head_sha": head_sha, "base_revision": base_revision}


# Feature: source-control-connector-v2, Property V4: verified source snapshot required before a proposal is accepted
@settings(max_examples=100)
@given(scenario=_scenarios())
def test_property_v4_verified_snapshot_required(scenario):
    """A proposal is accepted iff base_revision is present and equals the current head;
    an absent/stale snapshot rejects it with no branch/commit/proposal created, and an
    accepted branch is anchored to the verified revision (Req 7.1, 7.2)."""
    category = scenario["category"]
    head_sha = scenario["head_sha"]
    base_revision = scenario["base_revision"]

    fake = FakeProvider()
    # Seed the current head of the target branch (the value ``latest_commit_sha`` reports).
    fake.set_head(_REPO, _BRANCH, head_sha)

    with mock.patch.object(service_module, "logger") as mock_logger:
        result = _call_propose(fake, base_revision)

    accepted = category == "match"

    if accepted:
        # (1) ACCEPT: exactly one proposal created.
        assert result.status == "created", result.message
        assert result.proposal_id is not None

        create_calls = fake.calls_for("create_branch")
        commit_calls = fake.calls_for("commit_files")
        pr_calls = fake.calls_for("open_change_proposal")
        assert len(create_calls) == 1, "exactly one branch must be created"
        assert len(commit_calls) == 1, "exactly one commit must be made"
        assert len(pr_calls) == 1, "exactly one change proposal must be opened"

        # (1)/(4) The created branch is based on the VERIFIED revision, which equals both the
        # supplied base_revision and the current head — never a later "latest".
        assert create_calls[0]["from_sha"] == base_revision
        assert create_calls[0]["from_sha"] == head_sha
        assert create_calls[0]["repo"] == _REPO
        assert pr_calls[0]["base"] == _BRANCH
    else:
        # (2)/(3) REJECT: no proposal, no source-control state created.
        assert result.status == "rejected", result.message
        assert result.proposal_id is None
        assert result.proposal_url is None
        _assert_no_source_state(fake)

        # The rejection audit names the expected reason for each category.
        expected_reason = "missing_snapshot" if category == "absent" else "stale_snapshot"
        assert expected_reason in _rejection_reasons(
            mock_logger
        ), f"{category} reject did not name reason {expected_reason!r}"

        if category == "absent":
            # A missing snapshot is rejected BEFORE any adapter op at all (Gate 4b).
            assert fake.calls == [], f"missing-snapshot reject touched the provider: {fake.call_operations}"


# --- Read -> propose happy path + staleness (example coverage) -------------
#
# These example tests exercise the full read-before-write handshake end-to-end: a read
# captures the Verified_Source_Snapshot, which passed straight back into propose_change (with
# the head unchanged) is accepted, and advancing the head first makes that same revision
# stale and rejected.


def _read_revision(fake: FakeProvider) -> str | None:
    """Read the seeded file through the authorized read path and return its revision token."""
    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        result = read_iac_files(
            ["infra/template.yaml"],
            config=_make_config(),
            provider=fake,
        )
    finally:
        reset_request_context(token)
    return result.revision


def test_read_then_propose_with_unchanged_head_is_accepted():
    """The revision a read returns, passed straight into propose with an unchanged head, is
    accepted and the branch is anchored to that revision."""
    head_sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    fake = FakeProvider()
    fake.set_head(_REPO, _BRANCH, head_sha)
    fake.add_file(_REPO, _BRANCH, "infra/template.yaml", _VALID_CFN)

    revision = _read_revision(fake)
    assert revision == head_sha, "read must surface the current head as the snapshot revision"

    result = _call_propose(fake, revision)

    assert result.status == "created", result.message
    assert result.proposal_id is not None
    create_calls = fake.calls_for("create_branch")
    assert len(create_calls) == 1
    assert create_calls[0]["from_sha"] == revision


def test_read_then_advance_head_makes_snapshot_stale_and_rejected():
    """Advancing the target head after a read makes the captured revision stale, so the same
    revision passed into propose is rejected with no branch/commit/proposal."""
    head_sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    fake = FakeProvider()
    fake.set_head(_REPO, _BRANCH, head_sha)
    fake.add_file(_REPO, _BRANCH, "infra/template.yaml", _VALID_CFN)

    revision = _read_revision(fake)
    assert revision == head_sha

    # A push lands between the read and the propose: the head advances, so the captured
    # revision is now stale.
    new_head = fake.advance_head(_REPO, _BRANCH)
    assert new_head != revision

    with mock.patch.object(service_module, "logger") as mock_logger:
        result = _call_propose(fake, revision)

    assert result.status == "rejected", result.message
    assert result.proposal_id is None
    _assert_no_source_state(fake)
    assert "stale_snapshot" in _rejection_reasons(mock_logger)
