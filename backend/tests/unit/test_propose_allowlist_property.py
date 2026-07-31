#!/usr/bin/env python3
"""Property-based tests for allowlist exact-match gating (`connector.service.propose_change`).

Covers Correctness Property 5 from the source-control-connector design: a source-control
operation is issued *if and only if* the requested ``(repository, target_branch)`` pair is a
case-sensitive, full-string match of a single allowlist entry — with no partial, prefix,
suffix, substring, or wildcard matching, and regardless of any repository/branch values
contained in agent or user input.

Concretely, this proves the safety-critical gate:

- **Exact match** → the connector performs provider operations (``create_branch`` +
  ``commit_files`` + ``open_change_proposal``), and the effective repository/branch used for
  those operations come from the matched allowlist entry (never free-form model input).
- **Any non-match** (case difference, prefix/suffix/substring, wrong branch for a listed
  repo, or an absent repo) → the connector performs **zero** provider operations and returns
  a rejection, recording a rejection audit entry that names the requesting user, requested
  repository, requested branch, and reason.

The service is exercised with a ``FakeProvider`` injected via ``provider=`` and a
purpose-built enabled :class:`ConnectorConfig` injected via ``config=``. ``get_secret`` is
mocked to return a token so the credential gate passes on the exact-match path, an authorized
user is placed in the request context, and valid CloudFormation is proposed. Per-user rate
limiting (Property 7) is neutralized here so this test isolates the allowlist behavior.

Validates: Requirements 4.4, 5.2, 5.3, 11.5, 11.6
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
from connector.models import ProposedFile
from connector import service
from connector.service import propose_change
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit


# --- Fixed, known allowlist ------------------------------------------------
#
# A concrete operator-configured allowlist. Repository identifiers and branches are
# lowercase and contain letters so a case mutation (``.upper()``) is guaranteed to differ,
# and no repo/branch is a prefix/substring of another entry, keeping the constructed
# non-match cases genuinely non-matching.
_ALLOWLIST = (
    AllowlistEntry(repo="org/iac-repo", target_branches=("main", "release")),
    AllowlistEntry(repo="team/infra-repo", target_branches=("prod",)),
)

# Every exact (repo, branch) pair the connector must accept.
_EXACT_PAIRS = [(entry.repo, branch) for entry in _ALLOWLIST for branch in entry.target_branches]

# The provider operations that constitute a source-control "write".
_WRITE_OPS = ("create_branch", "commit_files", "open_change_proposal")

# All provider operations (used to assert ZERO calls on a rejected path).
_ALL_OPS = ("get_file", "get_files", "branch_exists", "latest_commit_sha", *_WRITE_OPS)

# An authorized requesting user whose group intersects the configured authorized groups.
_AUTHORIZED_CONTEXT = {"user_id": "user-123", "groups": ["scm-writers"], "session_id": "s-1"}

# A structurally valid CloudFormation template (passes the IaC validation gate).
_VALID_CFN = '{"Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}}'


def _make_config() -> SourceControlConfig:
    """Build an enabled SourceControlConfig backed by the fixed known allowlist."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=_ALLOWLIST,
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
    """A single valid CloudFormation file to propose."""
    return [ProposedFile(path="template.yaml", content=_VALID_CFN, iac_format="cloudformation")]


def _is_exact_match(repo: str, branch: str) -> bool:
    """Case-sensitive, full-string match of (repo, branch) against the known allowlist."""
    for entry in _ALLOWLIST:
        if entry.repo == repo and branch in entry.target_branches:
            return True
    return False


def _call_propose(repository: str, target_branch: str, fake: FakeProvider):
    """Invoke ``propose_change`` with an authorized user and the credential + rate-limit
    gates neutralized.

    Identity is derived by the service strictly from the request context (never from
    model/tool input), so an authorized context is set (and reset) here per invocation.
    ``get_secret`` is patched (so the credential gate passes on the exact-match path) and
    ``check_rate_limit`` is a no-op (so this test isolates allowlist gating from the
    separate per-user rate-limit property). None of these affect the allowlist gate, which
    runs *before* the credential and rate-limit gates.
    """
    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        with (
            mock.patch.object(service, "get_secret", return_value="ghs_faketoken1234567890abcd"),
            mock.patch.object(service, "check_rate_limit", return_value=None),
        ):
            return propose_change(
                intent="update the storage bucket configuration",
                files=_proposed_files(),
                iac_format="cloudformation",
                title="Update bucket configuration",
                description="Enable versioning on the storage bucket.",
                repository=repository,
                target_branch=target_branch,
                config=_make_config(),
                provider=fake,
            )
    finally:
        reset_request_context(token)


# --- Non-matching input strategy -------------------------------------------


@st.composite
def _non_matching_pairs(draw):
    """A ``(repository, target_branch)`` pair guaranteed NOT to exactly match the allowlist.

    Covers the ways a request can differ from an allowlist entry: case difference,
    suffix/prefix/substring of a listed repo or branch, a listed repo paired with a branch
    from a *different* entry (wrong branch), an entirely absent repository, and fully random
    values. An ``assume`` guard makes the non-match invariant explicit and robust.
    """
    repo, branch = draw(st.sampled_from(_EXACT_PAIRS))
    kind = draw(
        st.sampled_from(
            [
                "repo_case",
                "branch_case",
                "repo_suffix",
                "repo_prefix",
                "branch_suffix",
                "branch_substr",
                "wrong_branch",
                "absent_repo",
                "random",
            ]
        )
    )

    if kind == "repo_case":
        candidate = (repo.upper(), branch)
    elif kind == "branch_case":
        candidate = (repo, branch.upper())
    elif kind == "repo_suffix":
        candidate = (repo + "-extra", branch)
    elif kind == "repo_prefix":
        candidate = (repo[:-1], branch)
    elif kind == "branch_suffix":
        candidate = (repo, branch + "-x")
    elif kind == "branch_substr":
        candidate = (repo, branch[:-1])
    elif kind == "wrong_branch":
        # A branch that belongs to some entry but not to this repository's entry.
        other_branches = [
            b for r, b in _EXACT_PAIRS if not _is_exact_match(repo, b)
        ]
        assume(other_branches)
        candidate = (repo, draw(st.sampled_from(other_branches)))
    elif kind == "absent_repo":
        candidate = ("absent/" + repo, branch)
    else:  # random
        candidate = (
            draw(st.from_regex(r"[A-Za-z0-9._/-]{1,25}", fullmatch=True)),
            draw(st.from_regex(r"[A-Za-z0-9._/-]{1,25}", fullmatch=True)),
        )

    # Make the non-match invariant explicit and defend against any accidental collision.
    assume(not _is_exact_match(candidate[0], candidate[1]))
    return candidate


# --- Property 5 ------------------------------------------------------------


# Feature: source-control-connector, Property 5: Source-control operations occur only on an exact allowlist match
@settings(max_examples=100)
@given(pair=st.sampled_from(_EXACT_PAIRS))
def test_property5_exact_match_issues_operation_scoped_to_allowlist(pair):
    """Exact match: provider write operations occur, scoped to the matched allowlist entry.

    A case-sensitive, full-string match of both repository and branch is the ONLY condition
    under which the connector issues a source-control operation. The repository and base
    branch used for those operations come from the matched allowlist entry (Req 4.4, 5.2,
    11.5).
    """
    requested_repo, requested_branch = pair
    fake = FakeProvider()

    result = _call_propose(requested_repo, requested_branch, fake)

    # A proposal was created (the pipeline reached and completed the provider ops).
    assert result.status == "created", result.message
    assert result.proposal_id is not None
    assert result.proposal_url is not None

    # Every write operation ran exactly the expected number of times.
    for op in _WRITE_OPS:
        assert fake.calls_for(op), f"exact match should have invoked {op}"

    # The effective repository/branch for the write come from the matched allowlist entry.
    assert _is_exact_match(requested_repo, requested_branch)
    create_call = fake.calls_for("create_branch")[0]
    commit_call = fake.calls_for("commit_files")[0]
    pr_call = fake.calls_for("open_change_proposal")[0]

    assert create_call["repo"] == requested_repo
    assert commit_call["repo"] == requested_repo
    assert pr_call["repo"] == requested_repo
    # The pull request is opened against the matched (allowlisted) target branch.
    assert pr_call["base"] == requested_branch
    # latest_commit_sha is read against the matched repo+branch (base for the proposal).
    base_call = fake.calls_for("latest_commit_sha")[0]
    assert base_call["repo"] == requested_repo
    assert base_call["branch"] == requested_branch


# Feature: source-control-connector, Property 5: Source-control operations occur only on an exact allowlist match
@settings(max_examples=100)
@given(pair=_non_matching_pairs())
def test_property5_non_match_performs_no_operation_and_audits(pair):
    """Non-match: ZERO provider operations, a rejection result, and a rejection audit entry.

    For any request that is not a case-sensitive full-string match of a single allowlist
    entry (case difference, prefix/suffix/substring, wrong branch, or absent repo), the
    connector performs no source-control operation and records a rejection naming the
    requesting user, requested repository, requested branch, and reason (Req 5.2, 5.3, 11.5,
    11.6).
    """
    requested_repo, requested_branch = pair
    assert not _is_exact_match(requested_repo, requested_branch)

    fake = FakeProvider()
    with mock.patch.object(service, "logger") as mock_logger:
        result = _call_propose(requested_repo, requested_branch, fake)

    # The request is rejected and no proposal is produced.
    assert result.status == "rejected"
    assert result.proposal_id is None
    assert result.proposal_url is None

    # ZERO provider operations of any kind were issued.
    assert fake.calls == []
    for op in _ALL_OPS:
        assert fake.calls_for(op) == [], f"non-match unexpectedly invoked {op}"

    # A rejection audit entry was recorded with the required fields (Req 5.3).
    assert mock_logger.warning.called
    rejection_calls = [
        call
        for call in mock_logger.warning.call_args_list
        if call.kwargs.get("event") == "scm_rejected"
        and call.kwargs.get("reason") == "allowlist_miss"
    ]
    assert rejection_calls, "expected a scm_rejected/allowlist_miss audit entry"
    audit = rejection_calls[0].kwargs
    assert audit.get("requesting_user") == _AUTHORIZED_CONTEXT["user_id"]
    assert audit.get("repository") == requested_repo
    assert audit.get("target_branch") == requested_branch
    assert audit.get("outcome") == "rejected"
