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

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
import utils.security as security
from connector import service as service_module
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
        ("infra/./main.tf", "infra/main.tf"),  # "." segment collapsed
        ("infra//nested///main.tf", "infra/nested/main.tf"),  # duplicate slashes collapsed
    ],
)
def test_normalize_path_accepts_and_canonicalizes_safe_paths(safe_input, expected):
    """A safe repo-relative path is canonicalized (only "." / duplicate slashes collapsed)."""
    assert _normalize_path(safe_input) == expected


@pytest.mark.parametrize(
    "unsafe_input",
    [
        # Absolute paths are REJECTED outright (no longer stripped to repo-relative).
        "/main.tf",
        "/etc/passwd",  # absolute path
        "/etc/passwd/../../root",  # absolute + interior .. (previously collapsed to "root")
        # ANY ".." segment is rejected on presence (previously interior ".." was collapsed).
        "infra/sub/../main.tf",  # interior ..
        "../secrets.tf",  # plain ../ escape
        "../../etc/passwd",
        "infra/../../etc/passwd",
        "a/../../b",  # nested parent traversal
        "..",
        # Illegal characters (pre-existing rejections, preserved).
        "a\\b.tf",  # backslash
        "a\x00b.tf",  # NUL
    ],
)
def test_normalize_path_rejects_absolute_traversal_or_illegal_paths(unsafe_input):
    """Absolute paths, any ".." segment, and illegal characters are REJECTED, not rewritten."""
    with pytest.raises(PathTraversalError):
        _normalize_path(unsafe_input)


class _FakeAuditSink:
    """In-memory audit sink capturing every event written."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return True


@pytest.fixture
def captured_audit(monkeypatch) -> _FakeAuditSink:
    """Replace the durable sink lookup so audit events are captured in memory."""
    sink = _FakeAuditSink()
    monkeypatch.setattr(service_module, "_get_audit_sink", lambda _config: sink)
    return sink


def _read_authorized(paths, *, config, reader):
    security._rate_limit_windows.clear()
    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        return read_iac_files(paths, config=config, reader=reader)
    finally:
        reset_request_context(token)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../../etc/passwd",  # plain ../ escape
        "a/../../b",  # nested parent traversal
        "/etc/passwd",  # absolute path
    ],
)
def test_read_iac_files_rejects_unsafe_path_fails_closed_with_audit(unsafe_path, captured_audit):
    """A rejected path fails closed: empty result, a ``path_invalid`` audit, and NO provider read."""
    config = make_source_control_config()
    reader = FakeProvider()

    result = _read_authorized([unsafe_path], config=config, reader=reader)

    # Fail-closed empty result.
    assert result.files == ()
    assert result.missing == ()
    assert result.limit_exceeded is False
    # The provider must never be contacted for a request containing an unsafe path.
    assert reader.calls == []

    # The rejection is durably audited exactly once with the path_invalid reason.
    invalid_events = [e for e in captured_audit.events if e.get("reason") == "path_invalid"]
    assert len(invalid_events) == 1, "an unsafe-path rejection must be durably audited once"
    event = invalid_events[0]
    assert event["event"] == "scm_read"
    assert event["outcome"] == "rejected"
    # Defense-in-depth: the audit carries no credential or file-content field.
    assert "content" not in event
    assert not any("secret" in str(k).lower() for k in event), "audit must not carry a secret field"


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
