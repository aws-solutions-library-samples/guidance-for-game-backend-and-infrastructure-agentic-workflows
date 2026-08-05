#!/usr/bin/env python3
"""Property-based tests for the connector read path (`connector/service.read_iac_files`).

Covers Correctness Property 12 from the source-control-connector design: file read
scoping, limit, and missing-file reporting. ``read_iac_files`` is the connector's
read entry point the agent uses to review the current source of truth before proposing
changes, so proving it (a) fetches exactly the requested paths from the *configured*
repository + target branch and names missing paths, (b) rejects an over-limit request
with a limit-exceeded result and no provider fetch, and (c) never creates a proposal,
is what makes the read path safe and predictable.

The service is exercised with a ``FakeProvider`` injected via ``provider=`` and a
purpose-built :class:`ConnectorConfig` injected via ``config=``; the read path performs
no secret retrieval and no provider-factory lookup, so no ``get_secret`` mock is needed.

Validates: Requirements 3.1, 3.2, 3.4
"""

# Standard library
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector import service as service_module
from connector.config import AllowlistEntry, SourceControlConfig
from support.config_factory import make_source_control_config
from connector.service import read_iac_files
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit

# The read path now enforces all five authorization dimensions (repository, branch, path,
# extension, group) identically to the write path (Req 6.1). The group dimension requires an
# authenticated caller whose groups intersect the configured authorized groups, so every read
# under test runs inside this authorized request context. The fixed allowlist entries below
# carry no path_prefixes/extensions (any path / any extension), so the path and extension
# dimensions are permissive here and this test isolates the read scoping/limit behavior.
_AUTHORIZED_CONTEXT = {"user_id": "reader-1", "groups": ["scm-writers"], "session_id": "s-read"}


def _read_authorized(paths, *, config, provider):
    """Invoke ``read_iac_files`` inside an authorized request context.

    Identity/groups are derived only from the request context (never from tool input), so an
    authorized user is set (and reset) per call to satisfy the read path's group dimension.
    """
    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        return read_iac_files(paths, config=config, provider=provider)
    finally:
        reset_request_context(token)


# --- Hypothesis strategies -------------------------------------------------

# Repository / branch identifiers used for the configured allowlist entry. These are the
# ONLY repo+branch the read path may touch (taken from allowlist[0]), never model input.
_repos = st.from_regex(r"[A-Za-z0-9._-]{1,15}/[A-Za-z0-9._-]{1,15}", fullmatch=True)
_branches = st.from_regex(r"[A-Za-z0-9._/-]{1,15}", fullmatch=True)

# File paths within the repository (whitespace-free, distinct per request).
_paths = st.from_regex(r"[A-Za-z0-9._/-]{1,25}", fullmatch=True)

# The mutation operations that the read path must NEVER invoke (no proposal is created).
_MUTATION_OPS = ("create_branch", "commit_files", "open_change_proposal")


def _make_config(*, max_files: int, repo: str, branch: str) -> SourceControlConfig:
    """Build a minimal enabled ConnectorConfig whose allowlist[0] is ``repo``+``branch``.

    Only the fields the read path reads (``max_files_per_request`` and ``allowlist``)
    matter here; the remaining fields carry valid placeholder values so the frozen
    dataclass is well-formed.
    """
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(AllowlistEntry(repo=repo, target_branches=(branch,)),),
        authorized_groups=("scm-writers",),
        rate_limit_max=5,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=max_files,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def _assert_no_proposal(fake: FakeProvider) -> None:
    """Assert the read path created no proposal (no branch/commit/PR provider calls)."""
    for op in _MUTATION_OPS:
        assert fake.calls_for(op) == [], f"read path unexpectedly invoked {op}"


# --- Property 12 ------------------------------------------------------------


@st.composite
def _within_limit_requests(draw):
    """A request whose path count is <= max_files_per_request.

    Returns the config, the FakeProvider (with the *present* paths seeded on the
    configured repo+branch), the ordered request paths, and the present/missing split.
    """
    repo = draw(_repos)
    branch = draw(_branches)
    max_files = draw(st.integers(min_value=1, max_value=25))

    paths = draw(
        st.lists(_paths, min_size=0, max_size=max_files, unique=True)
    )
    # Partition the requested paths into those present in the provider and those missing.
    present = set(draw(st.sets(st.sampled_from(paths), max_size=len(paths)))) if paths else set()
    missing = [p for p in paths if p not in present]

    fake = FakeProvider()
    for path in present:
        fake.add_file(repo, branch, path, f"content::{path}")
    # Seed a decoy file with the same path on a DIFFERENT branch to prove scoping: the
    # read path must fetch from the configured target branch only.
    for path in missing:
        fake.add_file(repo, f"other-{branch}", path, "decoy")

    config = _make_config(max_files=max_files, repo=repo, branch=branch)
    return config, fake, paths, sorted(present), sorted(missing)


# Feature: source-control-connector, Property 12: File read scoping, limit, and missing-file reporting
@settings(max_examples=100)
@given(case=_within_limit_requests())
def test_property12_within_limit_fetches_scoped_and_reports_missing(case):
    """Within the limit: present files are returned, missing paths named, scoped to
    the configured repo+branch, and no proposal is created (Req 3.1, 3.4)."""
    config, fake, paths, present, missing = case

    result = _read_authorized(paths, config=config, provider=fake)

    # Not a limit rejection: a real fetch happened.
    assert result.limit_exceeded is False

    # Exactly the present paths are returned, and exactly the missing paths are named.
    assert sorted(fc.path for fc in result.files) == present
    assert sorted(result.missing) == missing
    # Returned content is the content stored on the configured branch.
    for fc in result.files:
        assert fc.content == f"content::{fc.path}"

    # Req 3.1: exactly one provider fetch, scoped to the configured repo + target branch
    # (allowlist[0]), for exactly the requested paths — never model-controlled values.
    get_files_calls = fake.calls_for("get_files")
    assert len(get_files_calls) == 1
    call = get_files_calls[0]
    assert call["repo"] == config.domain.authorization_policy[0].repo
    assert call["branch"] == config.domain.authorization_policy[0].target_branches[0]
    assert call["paths"] == list(paths)

    # Req 3.4: reading never creates a proposal.
    _assert_no_proposal(fake)


@st.composite
def _over_limit_requests(draw):
    """A request whose path count strictly exceeds max_files_per_request."""
    repo = draw(_repos)
    branch = draw(_branches)
    max_files = draw(st.integers(min_value=1, max_value=15))

    # Strictly more than max_files distinct paths.
    paths = draw(
        st.lists(_paths, min_size=max_files + 1, max_size=max_files + 8, unique=True)
    )

    fake = FakeProvider()
    # Seed every requested path so that, if a fetch *were* wrongly issued, it would
    # return content — making an accidental fetch impossible to hide.
    for path in paths:
        fake.add_file(repo, branch, path, f"content::{path}")

    config = _make_config(max_files=max_files, repo=repo, branch=branch)
    return config, fake, paths


# Feature: source-control-connector, Property 12: File read scoping, limit, and missing-file reporting
@settings(max_examples=100)
@given(case=_over_limit_requests())
def test_property12_over_limit_rejects_without_fetch(case):
    """Over the limit: limit-exceeded result, NO provider fetch, no proposal (Req 3.2)."""
    config, fake, paths = case

    result = _read_authorized(paths, config=config, provider=fake)

    # Req 3.2: a limit-exceeded result with no files and no missing list.
    assert result.limit_exceeded is True
    assert result.files == ()
    assert result.missing == ()

    # Req 3.2: NO provider fetch occurred.
    assert fake.calls_for("get_files") == []
    assert fake.calls == []

    # And certainly no proposal.
    _assert_no_proposal(fake)


# --- Five-dimension enforcement on reads (Req 6.1, 6.3) --------------------
#
# The v2 pass authorizes reads on the same five dimensions as writes. The tests below prove
# the group dimension (previously write-only) and the path/extension dimensions now reject a
# read with NO provider fetch and an audit entry naming the failed dimension.


def _make_constrained_config(*, repo: str, branch: str) -> SourceControlConfig:
    """An enabled config whose single allowlist entry constrains path prefix + extension."""
    return make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(
            AllowlistEntry(
                repo=repo,
                target_branches=(branch,),
                path_prefixes=("infra/",),
                extensions=(".yaml",),
            ),
        ),
        authorized_groups=("scm-writers",),
        rate_limit_max=5,
        rate_limit_window_seconds=3600,
        provider_timeout_seconds=30,
        retry_max_attempts=3,
        max_files_per_request=20,
        provider_base_url=None,
        audit_log_group="scm-audit",
        config_errors=(),
    )


def test_read_rejected_when_group_dimension_not_satisfied():
    """A read by a caller with no intersecting group is rejected with no provider fetch."""
    repo, branch = "org/iac-repo", "main"
    config = make_source_control_config(
        enabled=True,
        provider="github",
        credential_secret_id="scm/credential",
        allowlist=(AllowlistEntry(repo=repo, target_branches=(branch,)),),
        authorized_groups=("scm-writers",),
        audit_log_group="scm-audit",
    )
    fake = FakeProvider()
    fake.add_file(repo, branch, "infra/vpc.yaml", "content")

    # Authenticated but NOT in an authorized group → group dimension fails.
    token = set_request_context({"user_id": "u", "groups": ["other-group"], "session_id": "s"})
    try:
        with mock.patch.object(service_module, "logger") as mock_logger:
            result = read_iac_files(["infra/vpc.yaml"], config=config, provider=fake)
    finally:
        reset_request_context(token)

    assert result.files == ()
    assert result.missing == ()
    assert result.limit_exceeded is False
    # No provider fetch happened.
    assert fake.calls == []
    # A rejection audit entry names the failed dimension.
    rejections = [
        call
        for call in mock_logger.warning.call_args_list
        if call.kwargs.get("event") == "scm_rejected"
        and call.kwargs.get("failed_dimension") == "group"
    ]
    assert rejections, "expected a scm_rejected read audit naming the group dimension"


def test_read_rejected_when_path_prefix_not_allowed():
    """A read of a path outside the entry's path_prefixes is rejected with no fetch."""
    repo, branch = "org/iac-repo", "main"
    config = _make_constrained_config(repo=repo, branch=branch)
    fake = FakeProvider()
    fake.add_file(repo, branch, "modules/vpc.yaml", "content")

    result = _read_authorized(["modules/vpc.yaml"], config=config, provider=fake)

    assert result.files == ()
    assert fake.calls == []


def test_read_rejected_when_extension_not_allowed():
    """A read of a path with a disallowed extension is rejected with no fetch."""
    repo, branch = "org/iac-repo", "main"
    config = _make_constrained_config(repo=repo, branch=branch)
    fake = FakeProvider()
    fake.add_file(repo, branch, "infra/vpc.tf", "content")

    result = _read_authorized(["infra/vpc.tf"], config=config, provider=fake)

    assert result.files == ()
    assert fake.calls == []


def test_read_allowed_when_path_and_extension_satisfy_entry():
    """A read under an allowed prefix with an allowed extension fetches normally."""
    repo, branch = "org/iac-repo", "main"
    config = _make_constrained_config(repo=repo, branch=branch)
    fake = FakeProvider()
    fake.add_file(repo, branch, "infra/vpc.yaml", "content")

    result = _read_authorized(["infra/vpc.yaml"], config=config, provider=fake)

    assert [fc.path for fc in result.files] == ["infra/vpc.yaml"]
    assert len(fake.calls_for("get_files")) == 1
