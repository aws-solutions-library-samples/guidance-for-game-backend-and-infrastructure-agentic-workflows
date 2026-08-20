#!/usr/bin/env python3
"""Property-based test for the read path's seven-dimension authorization.

# Feature: source-control-connector-readonly-split, Property 4: every served read is authorized across all seven dimensions

This is a non-optional MR security-posture property. It proves that ``read_iac_files``
serves a read **iff** the request passes all seven authorization dimensions — tenant,
workspace, repository, branch, path prefix, file extension, and authorized-group membership
— and that when a read is served the effective repository/branch come from the *matched*
allowlist entry (never free-form input) and only allowlisted content is ever read.

The service is driven with a ``FakeProvider`` (read-only ``SourceControlReader`` double)
injected via ``reader=`` and a purpose-built :class:`SourceControlConfig` injected via
``config=``. Identity, tenant, and workspace are sourced only through the #278 read-path
context seam (``set_request_context``) — never from tool/model arguments. Per-request file
limit and the per-requester rate limit are set high (and the sliding window is cleared per
example) so this test isolates the authorization behaviour.

An independent in-test oracle recomputes the seven-dimension decision so the assertion is a
true specification check, not a tautological call back into the code under test.

Validates: Requirements 5.2, 7.1, 7.2, 7.4
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
from connector.config import AllowlistEntry
from connector.service import read_iac_files
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit

# The only provider operations the read-only reader exposes; the read path must never touch
# anything outside this set (there is no provider-write operation in the shipped package).
_READ_OPS = ("get_file", "get_files")

# Small pools so the generator produces frequent matches AND misses across every dimension.
_TENANTS = ["acme", "globex", "initech"]
_WORKSPACES = ["prod", "staging", "dev"]
_REPOS = ["org/alpha", "org/beta", "org/gamma"]
_BRANCHES = ["main", "release", "feature"]
_GROUPS = ["scm-readers", "iac-admins", "ops"]
_PREFIXES = ["infra/", "modules/"]
_EXTENSIONS = [".yaml", ".tf"]

# Already-normalized, repo-relative POSIX paths spanning the prefix/extension space so the
# path and extension dimensions are meaningfully exercised while path normalization stays
# the identity (normalization has its own dedicated property test).
_PATHS = [
    "infra/vpc.yaml",
    "infra/db.tf",
    "modules/net.yaml",
    "modules/queue.tf",
    "app/main.py",
    "top.yaml",
    "notes.txt",
]


# --- Hypothesis strategies -------------------------------------------------


@st.composite
def _allowlist_entries(draw):
    """Generate 1..4 allowlist entries with optional constraints on every dimension.

    Each dimension is independently either unconstrained (empty tuple = "any") or a
    non-empty subset drawn from its pool, so the generated policy exercises the full
    matrix of dimension-permissive and dimension-restrictive entries.
    """
    n = draw(st.integers(min_value=1, max_value=4))
    entries = []
    for _ in range(n):
        repo = draw(st.sampled_from(_REPOS))
        branches = tuple(draw(st.lists(st.sampled_from(_BRANCHES), min_size=1, max_size=3, unique=True)))
        prefixes = tuple(draw(st.lists(st.sampled_from(_PREFIXES), min_size=0, max_size=2, unique=True)))
        extensions = tuple(draw(st.lists(st.sampled_from(_EXTENSIONS), min_size=0, max_size=2, unique=True)))
        tenants = tuple(draw(st.lists(st.sampled_from(_TENANTS), min_size=0, max_size=2, unique=True)))
        workspaces = tuple(draw(st.lists(st.sampled_from(_WORKSPACES), min_size=0, max_size=2, unique=True)))
        entries.append(
            AllowlistEntry(
                repo=repo,
                target_branches=branches,
                path_prefixes=prefixes,
                extensions=extensions,
                tenants=tenants,
                workspaces=workspaces,
            )
        )
    return tuple(entries)


@st.composite
def _authz_case(draw):
    """A full policy + request + identity context to authorize a read against."""
    entries = draw(_allowlist_entries())
    authorized_groups = tuple(draw(st.lists(st.sampled_from(_GROUPS), min_size=1, max_size=3, unique=True)))

    req_repo = draw(st.sampled_from(_REPOS))
    req_branch = draw(st.sampled_from(_BRANCHES))
    paths = tuple(draw(st.lists(st.sampled_from(_PATHS), min_size=1, max_size=3, unique=True)))

    tenant = draw(st.sampled_from(_TENANTS))
    workspace = draw(st.sampled_from(_WORKSPACES))
    groups = tuple(draw(st.lists(st.sampled_from(_GROUPS), min_size=0, max_size=3, unique=True)))
    return {
        "entries": entries,
        "authorized_groups": authorized_groups,
        "req_repo": req_repo,
        "req_branch": req_branch,
        "paths": paths,
        "tenant": tenant,
        "workspace": workspace,
        "groups": groups,
    }


def _oracle(case) -> AllowlistEntry | None:
    """Independently compute the matched entry, or ``None`` when any dimension fails.

    Mirrors the seven-dimension contract (tenant -> workspace -> repo -> branch -> path ->
    extension -> group) so the test asserts the service behaviour against an explicit
    specification rather than the implementation itself.
    """
    entries = case["entries"]
    tenant, workspace = case["tenant"], case["workspace"]
    repo, branch = case["req_repo"], case["req_branch"]
    paths, groups = case["paths"], case["groups"]
    authorized_groups = case["authorized_groups"]

    tenant_entries = [e for e in entries if not e.tenants or tenant in e.tenants]
    if not tenant_entries:
        return None
    workspace_entries = [e for e in tenant_entries if not e.workspaces or workspace in e.workspaces]
    if not workspace_entries:
        return None
    repo_entries = [e for e in workspace_entries if e.repo == repo]
    if not repo_entries:
        return None
    matched = next((e for e in repo_entries if branch in e.target_branches), None)
    if matched is None:
        return None
    if matched.path_prefixes and not all(any(p.startswith(pre) for pre in matched.path_prefixes) for p in paths):
        return None
    if matched.extensions and not all(any(p.endswith(ext) for ext in matched.extensions) for p in paths):
        return None
    if not (set(groups) & set(authorized_groups)):
        return None
    return matched


def _run(case, fake):
    """Invoke ``read_iac_files`` inside the #278 identity context, isolating rate limits."""
    security._rate_limit_windows.clear()
    config = make_source_control_config(
        enabled=True,
        provider="github",
        read_credential_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/read-AbCdEf",
        allowlist=case["entries"],
        authorized_groups=case["authorized_groups"],
        rate_limit_max=1000,
        rate_limit_window_seconds=3600,
        max_files_per_request=50,
        audit_log_group="scm-audit",
    )
    context = {
        "user_id": "reader-1",
        "groups": list(case["groups"]),
        "tenant": case["tenant"],
        "workspace": case["workspace"],
        "session_id": "s-authz",
    }
    token = set_request_context(context)
    try:
        return read_iac_files(
            list(case["paths"]),
            repository=case["req_repo"],
            target_branch=case["req_branch"],
            config=config,
            reader=fake,
        )
    finally:
        reset_request_context(token)


# --- Property 4 ------------------------------------------------------------


# Feature: source-control-connector-readonly-split, Property 4: every served read is authorized across all seven dimensions
@settings(max_examples=100)
@given(case=_authz_case())
def test_property4_served_iff_all_seven_dimensions_pass(case):
    """A read is served iff all seven dimensions pass; effective repo/branch come from the
    matched entry and only allowlisted content is read (Req 5.2, 7.1, 7.2, 7.4)."""
    matched = _oracle(case)

    fake = FakeProvider()
    # Seed each requested path as present content on EVERY (repo, branch) pair so a served
    # read returns content, and seed decoys on non-matched repos/branches so any read of
    # non-allowlisted scope would be detectable.
    for repo in _REPOS:
        for branch in _BRANCHES:
            for path in case["paths"]:
                fake.add_file(repo, branch, path, f"content::{repo}::{branch}::{path}")

    with mock.patch.object(service_module, "logger"):
        result = _run(case, fake)

    # The read path must never invoke a non-read operation.
    assert set(fake.call_operations) <= set(_READ_OPS)

    if matched is None:
        # Rejected: NO provider read occurred and nothing is served.
        assert fake.calls == []
        assert result.files == ()
        assert result.missing == ()
        assert result.limit_exceeded is False
        return

    # Served: exactly one provider fetch, scoped to the MATCHED entry's repo and the
    # requested branch (the effective selectors), for exactly the requested paths.
    get_files_calls = fake.calls_for("get_files")
    assert len(get_files_calls) == 1
    call = get_files_calls[0]
    assert call["repo"] == matched.repo
    assert call["branch"] == case["req_branch"]
    assert call["paths"] == list(case["paths"])

    # The effective repository equals the requested repository (repo dimension is exact).
    assert matched.repo == case["req_repo"]

    # Only allowlisted content was read: every returned file's content is the copy stored on
    # the matched repo + effective branch, never a decoy from another scope.
    for fc in result.files:
        assert fc.content == f"content::{matched.repo}::{case['req_branch']}::{fc.path}"
    assert {fc.path for fc in result.files} <= set(case["paths"])
