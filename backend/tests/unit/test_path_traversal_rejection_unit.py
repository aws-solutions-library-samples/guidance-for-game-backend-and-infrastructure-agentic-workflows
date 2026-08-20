#!/usr/bin/env python3
"""Unit tests: the read path rejects (does not silently rewrite) unsafe paths.

Addresses the PR #319 review finding that ``_normalize_path`` collapsed ``..`` and stripped a
leading ``/`` but did not *reject* a path escaping the repository root — so an LLM-supplied
``../../etc/passwd`` or ``/etc/passwd`` could be authorized/fetched outside the intended tree.
The hardened normalizer raises :class:`PathTraversalError` on absolute paths, ``..`` escapes,
and illegal characters; ``read_iac_files`` converts that into a fail-closed empty result with a
``path_invalid`` audit and NO provider read.

Validates the path-dimension defense of the seven-dimension read authorization.
"""

# Third-party packages
import pytest

# Local modules
import utils.security as security
from connector.service import PathTraversalError, _normalize_path, read_iac_files
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit

_AUTHORIZED_CONTEXT = {"user_id": "reader-1", "groups": ["scm-writers"], "session_id": "s-read"}


@pytest.mark.parametrize(
    "safe_input, expected",
    [
        ("", ""),
        ("   ", ""),
        (".", ""),
        ("main.tf", "main.tf"),
        ("/main.tf", "main.tf"),  # leading slash stripped to repo-relative (pre-existing contract)
        ("infra/./main.tf", "infra/main.tf"),
        ("infra//nested///main.tf", "infra/nested/main.tf"),
        ("infra/sub/../main.tf", "infra/main.tf"),  # interior .. that stays within root
        ("/etc/passwd/../../root", "root"),  # absolute + interior .. resolves within root after strip
    ],
)
def test_normalize_path_accepts_and_canonicalizes_safe_paths(safe_input, expected):
    assert _normalize_path(safe_input) == expected


@pytest.mark.parametrize(
    "unsafe_input",
    [
        "../secrets.tf",
        "../../etc/passwd",
        "infra/../../etc/passwd",  # escapes root after collapsing
        "..",
        "a\\b.tf",  # backslash
        "a\x00b.tf",  # NUL
    ],
)
def test_normalize_path_rejects_escaping_or_illegal_paths(unsafe_input):
    with pytest.raises(PathTraversalError):
        _normalize_path(unsafe_input)


def _read_authorized(paths, *, config, reader):
    security._rate_limit_windows.clear()
    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        return read_iac_files(paths, config=config, reader=reader)
    finally:
        reset_request_context(token)


def test_read_iac_files_fails_closed_on_unsafe_path_without_provider_read():
    """A single unsafe path rejects the whole request with no provider fetch (fail closed)."""
    config = make_source_control_config()
    reader = FakeProvider()

    result = _read_authorized(["../../etc/passwd"], config=config, reader=reader)

    assert result.files == ()
    assert result.missing == ()
    assert result.limit_exceeded is False
    # The provider must never be contacted for a request containing an escaping path.
    assert reader.calls == []
