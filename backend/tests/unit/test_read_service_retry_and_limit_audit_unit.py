#!/usr/bin/env python3
"""Unit tests for two connector read-service hardening findings (PR #319 review).

Finding #4 — **use the configured retry count**: ``read_iac_files`` retries a
``ProviderTransientError`` from the reader up to ``retry_max_attempts`` total calls
(provider rate limits, temporary 5xx, read timeouts), but never retries a
``ProviderAuthError`` or a permanent validation failure.

Finding #8 — **write file-count rejections to the durable audit log**: a request whose path
count exceeds ``max_files_per_request`` is recorded through the durable audit sink
(requester/tenant/workspace/result/reason), not merely a local ``logger.warning``. The audit
carries no credential or file content.

Both are exercised with a purpose-built config injected via ``config=`` and a programmable
``FakeProvider`` injected via ``reader=``; the durable sink is replaced with an in-memory
fake so no AWS call occurs.

Validates: PR #319 review findings 4 and 8.
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
from connector.provider import ProviderAuthError, ProviderTransientError
from connector.service import read_iac_files
from support.config_factory import make_source_control_config
from support.fake_provider import FakeProvider
from utils.request_context import reset_request_context, set_request_context

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"
_AUTHORIZED_CONTEXT = {"user_id": "reader-1", "groups": ["scm-writers"], "tenant": "acme", "workspace": "prod"}


class _FakeAuditSink:
    """In-memory audit sink capturing every event written (confirmed write)."""

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


def _config(*, max_files: int = 20, retry_max_attempts: int = 3):
    """Build an enabled config whose single allowlist entry is org/iac@main (any path)."""
    return make_source_control_config(
        enabled=True,
        read_credential_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf",
        allowlist=[AllowlistEntry(repo=_REPO, target_branches=(_BRANCH,))],
        authorized_groups=["scm-writers"],
        max_files_per_request=max_files,
        retry_max_attempts=retry_max_attempts,
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


# --- Finding #4: configured retry count ---------------------------------------------------


def test_transient_error_retried_then_succeeds(captured_audit):
    """A transient failure on the first get_files is retried and the retry's result is served."""
    served = FileFetchResult(
        files=(FileContent(path="a.yaml", content="Resources: {}"),),
        missing=(),
        limit_exceeded=False,
    )
    reader = FakeProvider()
    # First call raises transient, the second returns the served result.
    reader.program("get_files", side_effects=[ProviderTransientError("429 rate limited"), served])

    result = _read(["a.yaml"], config=_config(retry_max_attempts=3), reader=reader)

    assert result is served
    assert len(reader.calls_for("get_files")) == 2, "expected exactly one retry after the transient failure"


def test_transient_error_exhausts_attempts_then_raises(captured_audit):
    """A transient failure on every attempt propagates after exactly retry_max_attempts calls."""
    reader = FakeProvider()
    reader.fail("get_files", ProviderTransientError("still throttled"))

    with pytest.raises(ProviderTransientError):
        _read(["a.yaml"], config=_config(retry_max_attempts=3), reader=reader)

    assert len(reader.calls_for("get_files")) == 3, "must attempt exactly retry_max_attempts times"


def test_auth_error_is_not_retried(captured_audit):
    """A ProviderAuthError is a permanent failure: it propagates on the first call, no retry."""
    reader = FakeProvider()
    reader.fail("get_files", ProviderAuthError("credential rejected"))

    with pytest.raises(ProviderAuthError):
        _read(["a.yaml"], config=_config(retry_max_attempts=3), reader=reader)

    assert len(reader.calls_for("get_files")) == 1, "auth failures must never be retried"


# --- Finding #8: durable audit of a file-count rejection ----------------------------------


def test_file_count_rejection_writes_durable_audit(captured_audit):
    """An over-limit request writes a durable rejection audit and performs no provider read."""
    reader = FakeProvider()
    # max_files=1 but two paths requested -> limit exceeded before any provider read.
    result = _read(["a.yaml", "b.yaml"], config=_config(max_files=1), reader=reader)

    assert result.limit_exceeded is True
    assert reader.calls == [], "no provider read may occur on an over-limit request"

    limit_events = [e for e in captured_audit.events if e.get("reason") == "limit_exceeded"]
    assert len(limit_events) == 1, "the file-count rejection must be durably audited exactly once"
    event = limit_events[0]
    assert event["event"] == "scm_read"
    assert event["outcome"] == "rejected"
    assert event["requester"] == "reader-1"
    assert event["tenant"] == "acme"
    assert event["workspace"] == "prod"
    # Defense-in-depth: no credential or file-content field is present in the audit event.
    assert "content" not in event
    assert not any("secret" in str(k).lower() for k in event), "audit must not carry a secret field"
