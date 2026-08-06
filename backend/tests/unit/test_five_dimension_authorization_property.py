#!/usr/bin/env python3
"""Property-based test for five-dimension authorization on reads and writes.

Covers Correctness Property V3 from the source-control-connector-v2 design: the
Service_Layer authorizes an operation on **repository, branch, path, extension, and
group** and enforces the identical policy on both the read entry point
(``connector.service.read_iac_files``) and the change-proposal entry point
(``connector.service.propose_change``) before any Provider_Adapter operation.

For any authorization policy (a set of ``AllowlistEntry`` with ``path_prefixes`` and
``extensions``) and any requested ``(repository, branch, paths, groups)`` — including
case, prefix, extension, and group near-misses — driven through either entry point, this
proves:

  1. The operation is PERMITTED iff all five dimensions pass: an entry matches the
     repository and branch by exact case-sensitive full-string comparison, every requested
     path lies under one of that entry's ``path_prefixes`` and carries one of its
     ``extensions`` (absent ``path_prefixes`` means any path; absent ``extensions`` means
     any extension), and the requesting groups intersect the authorized groups.
  2. On ANY rejection, no Provider_Adapter operation is performed (the ``FakeProvider``
     records zero calls).
  3. The rejection audit NAMES the failed dimension.
  4. The effective repository/branch come from the matched allowlist entry, never from
     free-form input.
  5. Reads and writes enforce the SAME five dimensions: both entry points reach the
     identical authorization outcome for the same policy + request.

Identity/groups are derived only from the trusted request contextvar
(``utils.request_context``), never from model/tool input. The caller is always
authenticated (a non-empty ``user_id``) so the only authorization variable under test is
the five-dimension policy — the read path has no separate authentication gate, so an
authenticated context isolates the five-dimension comparison. The provider is a
``FakeProvider`` injected via ``provider=`` and never touched on a rejected path; the
audit sink is the confirming in-memory sink from ``conftest`` so no AWS call occurs.

Validates: Requirements 6.1, 6.2, 6.3
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
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


# --- Fixed pools -----------------------------------------------------------
#
# Disjoint pools keep constructed near-misses genuinely non-matching:
# - authorized vs "other" groups never intersect, so a group near-miss is guaranteed;
# - allowed prefixes/extensions never overlap the "bad" ones, so a path/extension
#   near-miss cannot accidentally satisfy the policy.
_AUTH_GROUPS_POOL = ["writers", "admins", "sre"]
_OTHER_GROUPS_POOL = ["viewers", "guests", "readonly"]
_REPOS = ["org/iac", "team/infra", "org/app", "svc/platform"]
_BRANCHES = ["main", "release", "prod", "dev", "stage"]
_PREFIXES = ["infra/", "modules/", "stacks/"]
_EXTS = [".yaml", ".tf", ".json"]
_BAD_PREFIXES = ["other/", "srcdir/", "docs/"]
_BAD_EXTS = [".txt", ".md", ".png"]

# A benign intent/title/description that passes input validation + prompt-injection
# detection so the five-dimension authorization gate is the only decision under test.
_INTENT = "Update the storage bucket configuration in the infrastructure template"
_TITLE = "Update bucket configuration"
_DESCRIPTION = "Adjust the storage bucket settings to match the requested configuration."

# Structurally valid CloudFormation so the IaC validation gate passes on a permitted
# propose and never masks the authorization outcome.
_VALID_CFN = "Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n"

# An always-authenticated requesting user; the group dimension is varied via the context
# groups, never via this identity (identity comes only from the request contextvar).
_USER_ID = "user-123"


# --- Authorization oracle (independent re-derivation of the five-dimension rule) ------
#
# This mirrors the design's five-dimension rule directly (not by calling the production
# AuthorizationPolicy) so the test independently decides the expected outcome, then
# asserts BOTH entry points match it. Evaluation order is repo -> branch -> path ->
# extension -> group; the first failing dimension is the reported one.


def _evaluate(entries, authorized_groups, repo, branch, paths, groups):
    """Return ``(allowed, failed_dimension, effective_repo, effective_branch)``."""
    repo_entries = [e for e in entries if e.repo == repo]
    if not repo_entries:
        return (False, "repo", None, None)

    matched = None
    for entry in repo_entries:
        if branch in entry.target_branches:
            matched = entry
            break
    if matched is None:
        return (False, "branch", None, None)

    if matched.path_prefixes:
        for path in paths:
            if not any(path.startswith(pre) for pre in matched.path_prefixes):
                return (False, "path", None, None)

    if matched.extensions:
        for path in paths:
            if not any(path.endswith(ext) for ext in matched.extensions):
                return (False, "extension", None, None)

    if not (set(groups) & set(authorized_groups)):
        return (False, "group", None, None)

    return (True, None, matched.repo, branch)


# --- Hypothesis strategy ---------------------------------------------------


@st.composite
def _scenarios(draw):
    """Generate an authorization policy plus a requested (repo, branch, paths, groups).

    A ``category`` biases the request toward a specific permit or near-miss, but the
    expected outcome is always computed by the independent :func:`_evaluate` oracle, so
    the assertions never depend on the category label being perfectly constructed.
    """
    category = draw(
        st.sampled_from(
            [
                "permit",
                "permit_unconstrained",
                "repo_miss",
                "repo_case",
                "branch_miss",
                "branch_case",
                "path_miss",
                "extension_miss",
                "group_miss",
            ]
        )
    )

    authorized_groups = tuple(draw(st.lists(st.sampled_from(_AUTH_GROUPS_POOL), min_size=1, unique=True)))

    repo = draw(st.sampled_from(_REPOS))
    branches = tuple(draw(st.lists(st.sampled_from(_BRANCHES), min_size=1, max_size=3, unique=True)))
    branch = draw(st.sampled_from(list(branches)))

    if category == "permit_unconstrained":
        # Absent path_prefixes => any path; absent extensions => any extension.
        prefixes: tuple[str, ...] = ()
        extensions: tuple[str, ...] = ()
    else:
        prefixes = tuple(draw(st.lists(st.sampled_from(_PREFIXES), min_size=1, max_size=2, unique=True)))
        extensions = tuple(draw(st.lists(st.sampled_from(_EXTS), min_size=1, max_size=2, unique=True)))

    primary = AllowlistEntry(
        repo=repo,
        target_branches=branches,
        path_prefixes=prefixes,
        extensions=extensions,
    )
    entries = [primary]

    # Optionally add a decoy entry with a different repository so the policy has more than
    # one entry and the "single matching entry" behavior is exercised.
    other_repos = [r for r in _REPOS if r != repo]
    if draw(st.booleans()) and other_repos:
        decoy_repo = draw(st.sampled_from(other_repos))
        entries.append(AllowlistEntry(repo=decoy_repo, target_branches=("main",)))

    def _good_path(name: str) -> str:
        pre = prefixes[0] if prefixes else "any/"
        ext = extensions[0] if extensions else ".yaml"
        return f"{pre}{name}{ext}"

    def _good_groups():
        overlap = draw(st.lists(st.sampled_from(list(authorized_groups)), min_size=1, unique=True))
        extras = draw(st.lists(st.sampled_from(_OTHER_GROUPS_POOL), unique=True))
        return list(dict.fromkeys(overlap + extras))

    # Defaults for the "matching" dimensions; each category perturbs exactly one.
    req_repo = repo
    req_branch = branch
    req_groups = _good_groups()

    if category == "permit_unconstrained":
        req_paths = ["deep/nested/thing.txt", "readme"]
    else:
        count = draw(st.integers(min_value=1, max_value=3))
        req_paths = [_good_path(f"res{i}") for i in range(count)]

    if category == "repo_miss":
        req_repo = "absent/" + repo
    elif category == "repo_case":
        req_repo = repo.upper()
    elif category == "branch_miss":
        available = [b for b in _BRANCHES if b not in branches]
        req_branch = available[0] if available else branch + "-x"
    elif category == "branch_case":
        req_branch = branch.upper()
    elif category == "path_miss":
        bad_prefix = draw(st.sampled_from(_BAD_PREFIXES))
        ext = extensions[0] if extensions else ".yaml"
        req_paths = [_good_path("ok"), f"{bad_prefix}bad{ext}"]
    elif category == "extension_miss":
        pre = prefixes[0] if prefixes else "any/"
        bad_ext = draw(st.sampled_from(_BAD_EXTS))
        req_paths = [_good_path("ok"), f"{pre}bad{bad_ext}"]
    elif category == "group_miss":
        req_groups = draw(st.lists(st.sampled_from(_OTHER_GROUPS_POOL), unique=True))

    return {
        "entries": tuple(entries),
        "authorized_groups": authorized_groups,
        "req_repo": req_repo,
        "req_branch": req_branch,
        "req_paths": req_paths,
        "req_groups": req_groups,
    }


def _make_config(scenario) -> SourceControlConfig:
    """Build an enabled composed config from the generated policy."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=scenario["entries"],
        authorized_groups=scenario["authorized_groups"],
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _proposed_files(paths) -> list[ProposedFile]:
    return [ProposedFile(path=p, content=_VALID_CFN, iac_format="cloudformation") for p in paths]


def _rejection_dimensions(mock_logger) -> list[str]:
    """Collect the ``failed_dimension`` values from any rejection warning audits.

    The read path records a rejection as ``event="scm_rejected"`` while the propose path now
    records it as a single ``event="scm_outcome"`` (the intent/outcome model — a rejected
    proposal performs no mutation, so it emits one OUTCOME event with no preceding intent).
    Both are collected so the five-dimension policy is asserted identically on reads and
    writes.
    """
    return [
        call.kwargs.get("failed_dimension")
        for call in mock_logger.warning.call_args_list
        if call.kwargs.get("event") in ("scm_rejected", "scm_outcome")
    ]


# Feature: source-control-connector-v2, Property V3: five-dimension authorization enforced identically on reads and writes
@settings(max_examples=100)
@given(scenario=_scenarios())
def test_property_v3_five_dimension_authorization(scenario):
    """Reads and writes permit iff all five dimensions pass; reject names the dimension,
    performs no provider op, and the effective repo/branch come from the matched entry."""
    config = _make_config(scenario)
    req_repo = scenario["req_repo"]
    req_branch = scenario["req_branch"]
    req_paths = scenario["req_paths"]
    req_groups = scenario["req_groups"]

    allowed, failed_dim, eff_repo, eff_branch = _evaluate(
        scenario["entries"],
        scenario["authorized_groups"],
        req_repo,
        req_branch,
        req_paths,
        req_groups,
    )

    request_ctx = {"user_id": _USER_ID, "groups": list(req_groups), "session_id": "s-v3"}

    # --- Read entry point ----------------------------------------------------------
    read_fake = FakeProvider()
    token = set_request_context(dict(request_ctx))
    try:
        with mock.patch.object(service_module, "logger") as read_logger:
            read_result = read_iac_files(
                list(req_paths),
                repository=req_repo,
                target_branch=req_branch,
                config=config,
                provider=read_fake,
            )
    finally:
        reset_request_context(token)

    # --- Change-proposal entry point ----------------------------------------------
    # Neutralize the per-user rate-limit window (a later, orthogonal gate) so a permitted
    # request always reaches the provider ops and the authorization decision is isolated.
    security._rate_limit_windows.clear()
    propose_fake = FakeProvider()
    token = set_request_context(dict(request_ctx))
    try:
        with mock.patch.object(service_module, "logger") as propose_logger:
            propose_result = propose_change(
                _INTENT,
                _proposed_files(req_paths),
                iac_format="cloudformation",
                title=_TITLE,
                description=_DESCRIPTION,
                base_revision=DEFAULT_HEAD_SHA,
                repository=req_repo,
                target_branch=req_branch,
                config=config,
                provider=propose_fake,
            )
    finally:
        reset_request_context(token)

    if allowed:
        # (1)/(5) Both entry points permit the operation for the same policy + request.
        assert read_result.limit_exceeded is False
        read_calls = read_fake.calls_for("get_files")
        assert len(read_calls) == 1, f"permitted read expected exactly one get_files, got {read_fake.call_operations}"
        # (4) Effective repo/branch come from the matched allowlist entry.
        assert read_calls[0]["repo"] == eff_repo
        assert read_calls[0]["branch"] == eff_branch

        assert propose_result.status == "created", (
            f"permitted propose expected 'created', got {propose_result.status}: " f"{propose_result.message}"
        )
        assert propose_result.proposal_id is not None
        create_calls = propose_fake.calls_for("create_branch")
        pr_calls = propose_fake.calls_for("open_change_proposal")
        assert len(create_calls) == 1 and len(pr_calls) == 1
        # (4) Effective repo/branch come from the matched allowlist entry, not raw input.
        assert create_calls[0]["repo"] == eff_repo
        assert pr_calls[0]["repo"] == eff_repo
        assert pr_calls[0]["base"] == eff_branch
    else:
        # (5) Both entry points reject the operation for the same policy + request.
        # (2) No provider operation was performed on either path.
        assert read_result.files == ()
        assert read_result.missing == ()
        assert read_result.limit_exceeded is False
        assert read_fake.calls == [], f"rejected read unexpectedly called provider: {read_fake.call_operations}"

        assert propose_result.status == "rejected", (
            f"rejected propose expected 'rejected', got {propose_result.status}: " f"{propose_result.message}"
        )
        assert propose_result.proposal_id is None
        assert propose_result.proposal_url is None
        assert (
            propose_fake.calls == []
        ), f"rejected propose unexpectedly called provider: {propose_fake.call_operations}"

        # (3) The rejection audit NAMES the failed dimension, identically on read + write.
        assert failed_dim in _rejection_dimensions(read_logger), f"read rejection did not name dimension {failed_dim!r}"
        assert failed_dim in _rejection_dimensions(
            propose_logger
        ), f"propose rejection did not name dimension {failed_dim!r}"
