"""Regression for PR #319 re-review — a served read is not gated on audit-write success.

The connector documents that reads are non-mutating and already complete before the durable
audit is written, so ``read_iac_files`` records the served-read audit best-effort and does
**not** gate the returned files on the sink's confirmation (``_audit``'s boolean result is
discarded). Existing service tests use an audit sink that always returns ``True``, so the
non-gating guarantee was only covered indirectly (documentation grep).

This test drives the read with an audit sink whose ``write()`` returns ``False`` (a durable
write that was not confirmed) and asserts the authorized read still returns the fetched
files, and that the sink was actually exercised. It also confirms a failed audit write does
not block the not-found outcome.

Validates: PR #319 re-review finding 2 (best-effort read auditing).
"""

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
import utils.security as security
from connector import service as service_module
from connector.config import AllowlistEntry
from connector.models import FileContent, FileFetchResult
from connector.service import read_iac_files
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"
_AUTHORIZED_CONTEXT = {"user_id": "reader-1", "groups": ["scm-writers"], "tenant": "acme", "workspace": "prod"}


class _FailingAuditSink:
    """Audit sink whose durable write is never confirmed (returns ``False``)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return False


@pytest.fixture
def failing_audit(monkeypatch) -> _FailingAuditSink:
    """Replace the durable sink lookup with one whose write() returns False."""
    sink = _FailingAuditSink()
    monkeypatch.setattr(service_module, "_get_audit_sink", lambda _config: sink)
    return sink


def _config():
    """Enabled config whose single allowlist entry is org/iac@main (any path)."""
    return make_source_control_config(
        enabled=True,
        read_credential_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf",
        allowlist=[AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,))],
        authorized_groups=["scm-writers"],
        audit_log_group="/scm/audit",
    )


def _read(paths, *, config, reader):
    """Invoke read_iac_files inside the authorized request context with a clean rate window."""
    security._rate_limit_windows.clear()
    token = set_request_context(dict(_AUTHORIZED_CONTEXT))
    try:
        return read_iac_files(paths, config=config, reader=reader)
    finally:
        reset_request_context(token)


def test_served_read_returns_files_when_audit_write_unconfirmed(failing_audit):
    """An authorized read still returns fetched files even though the audit write returns False."""
    served = FileFetchResult(
        files=(FileContent(path="infra/main.tf", content="resource {}"),),
        missing=(),
        limit_exceeded=False,
    )
    reader = FakeProvider()
    reader.program("get_files", side_effects=[served])

    result = _read(["infra/main.tf"], config=_config(), reader=reader)

    # The read is not gated on audit confirmation: the fetched files are returned as-is.
    assert result is served
    assert [f.path for f in result.files] == ["infra/main.tf"]
    # The best-effort durable audit was genuinely attempted (and reported an unconfirmed write).
    assert failing_audit.events, "the served-read audit must be attempted even though it returns False"
    assert failing_audit.events[-1]["outcome"] == "served"


def test_not_found_read_still_returns_when_audit_write_unconfirmed(failing_audit):
    """A not-found outcome is likewise unaffected by an unconfirmed audit write."""
    empty = FileFetchResult(files=(), missing=("infra/missing.tf",), limit_exceeded=False)
    reader = FakeProvider()
    reader.program("get_files", side_effects=[empty])

    result = _read(["infra/missing.tf"], config=_config(), reader=reader)

    assert result is empty
    assert result.missing == ("infra/missing.tf",)
    assert failing_audit.events, "the read audit must be attempted even though it returns False"
