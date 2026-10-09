"""Unit tests for prompt/identity/session log data minimization (#472).

Production INFO/WARN/ERROR records must never carry prompt excerpts, extracted
names, semantic-memory content, email addresses, display names, raw actor IDs,
or raw session/thread IDs. Externally influenced fields must not be able to
inject extra log lines (CR/LF normalization). These tests capture the real
backend loguru sink, drive the actual call sites with synthetic forbidden
markers, and assert the markers never reach the sink.

The markers are synthetic placeholders only (a fake prompt marker, a fake name,
a fake email at example.com, a fake subject, a fake thread id, and a value
containing a CR/LF sequence).
"""

# Standard library
import io
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from loguru import logger

pytestmark = pytest.mark.unit


# Synthetic forbidden markers. None of these may appear in any captured record.
PROMPT_MARKER = "ZZPROMPTSECRETZZ-help-me-with-fleet-planning"
NAME_MARKER = "Zephyrina"
EMAIL_MARKER = "player@example.com"
SUBJECT_MARKER = "sub-ZZ-aaaaaaaa-0000-0000-0000-000000000000"
THREAD_MARKER = "thread-ZZ-bbbbbbbb-0000-0000-0000-000000000000"
CRLF_MARKER = "line-one\r\nINJECTED-SECOND-LINE"


class _CaptureSink:
    """A loguru sink configured like the production stdout handler.

    Production uses backtrace=False, diagnose=False. The sink captures DEBUG and
    above so a test still fails if sensitive data is demoted to DEBUG rather than
    removed.
    """

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._sink_id = logger.add(
            self._buffer,
            level="DEBUG",
            format="{level} | {name}:{function}:{line} | {message}",
            backtrace=False,
            diagnose=False,
        )

    def __enter__(self) -> "io.StringIO":
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink_id)


# ---------------------------------------------------------------------------
# Control-character normalization (sanitizer + redaction helpers)
# ---------------------------------------------------------------------------


def test_sanitize_log_data_normalizes_crlf():
    """sanitize_log_data must strip CR/LF so one value cannot become two lines."""
    # Local modules
    from utils.security import sanitize_log_data

    out = sanitize_log_data(CRLF_MARKER)
    assert "\r" not in out
    assert "\n" not in out
    # The injected second-line content must not survive on its own line.
    assert "\r\nINJECTED-SECOND-LINE" not in out


def test_normalize_log_value_strips_control_characters():
    """A dedicated normalizer removes CR, LF, tab, and other C0 control chars."""
    # Local modules
    from utils.security import normalize_log_value

    out = normalize_log_value("a\r\nb\tc\x07d")
    assert "\r" not in out
    assert "\n" not in out
    assert "\x07" not in out


def test_redact_identifier_is_bounded_and_non_reversible():
    """redact_identifier returns a short, stable, non-plaintext token."""
    # Local modules
    from utils.security import redact_identifier

    token = redact_identifier(SUBJECT_MARKER)
    assert SUBJECT_MARKER not in token
    assert len(token) <= 24
    # Stable for the same input within a process.
    assert token == redact_identifier(SUBJECT_MARKER)
    # Different inputs produce different tokens.
    assert token != redact_identifier(THREAD_MARKER)
    # Empty / missing values do not raise and are not reversible-looking.
    assert redact_identifier(None)
    assert redact_identifier("")


# ---------------------------------------------------------------------------
# agentcore_main invocation path
# ---------------------------------------------------------------------------


def _run_invoke(monkeypatch, prompt_payload):
    """Import agentcore_main and run invoke_agent with the orchestrator stubbed."""
    # Local modules
    import agentcore_main

    monkeypatch.setattr(agentcore_main, "_CREDENTIALS_OK", True, raising=False)
    monkeypatch.setattr(agentcore_main, "run_orchestrator", lambda *a, **k: "ok", raising=False)
    # Local dev identity bypass so no Cognito verification is required.
    monkeypatch.setattr(agentcore_main, "ALLOW_LOCAL_IDENTITY_BYPASS", True, raising=False)
    monkeypatch.setattr(agentcore_main, "HOSTED_RUNTIME", False, raising=False)
    monkeypatch.setattr(agentcore_main, "COST_SNAPSHOT_STORE_REQUIRED", False, raising=False)
    monkeypatch.setattr(agentcore_main, "COST_SNAPSHOT_TABLE_NAME", None, raising=False)
    return agentcore_main.invoke_agent(prompt_payload)


def test_invoke_agent_does_not_log_prompt_or_identity(monkeypatch):
    """invoke_agent must not emit the prompt, raw thread/session/actor IDs."""
    payload = {
        "prompt": PROMPT_MARKER,
        "thread_id": THREAD_MARKER,
        "user_context": {
            "user_id": SUBJECT_MARKER,
            "session_id": THREAD_MARKER,
            "display_name": NAME_MARKER,
            "email": EMAIL_MARKER,
            "username": NAME_MARKER,
        },
    }
    with _CaptureSink() as buffer:
        _run_invoke(monkeypatch, payload)
        output = buffer.getvalue()

    for marker in (PROMPT_MARKER, NAME_MARKER, EMAIL_MARKER, SUBJECT_MARKER, THREAD_MARKER):
        assert marker not in output, f"forbidden marker leaked into logs: {marker!r}"
    assert "\r\nINJECTED" not in output


def test_invoke_agent_crlf_thread_id_cannot_inject_log_line(monkeypatch):
    """A caller-controlled thread id with CR/LF must not create a second line."""
    payload = {
        "prompt": "a normal question",
        "thread_id": CRLF_MARKER,
        "user_context": {"user_id": "u-1", "session_id": CRLF_MARKER},
    }
    with _CaptureSink() as buffer:
        _run_invoke(monkeypatch, payload)
        output = buffer.getvalue()

    assert "INJECTED-SECOND-LINE" not in output


# ---------------------------------------------------------------------------
# semantic_memory: outcomes and counts only
# ---------------------------------------------------------------------------


def test_semantic_memory_save_logs_no_content_or_actor(monkeypatch):
    """save_semantic_memory must log outcomes/counts, never content or raw actor."""
    # Local modules
    import utils.semantic_memory as sm

    fake_client = MagicMock()
    fake_client.batch_create_memory_records.return_value = {"successfulRecords": [], "failedRecords": []}
    monkeypatch.setattr(sm, "_get_memory_client", lambda: fake_client)
    monkeypatch.setattr(sm, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-test", raising=False)

    with _CaptureSink() as buffer:
        sm.save_semantic_memory(SUBJECT_MARKER, f"User's name is {NAME_MARKER}")
        output = buffer.getvalue()

    assert NAME_MARKER not in output
    assert SUBJECT_MARKER not in output


def test_extract_and_save_logs_no_name_or_content(monkeypatch):
    """extract_and_save_user_info must not log extracted names or memory content."""
    # Local modules
    import utils.semantic_memory as sm

    monkeypatch.setattr(sm, "save_semantic_memory", lambda *a, **k: True)

    with _CaptureSink() as buffer:
        sm.extract_and_save_user_info(SUBJECT_MARKER, f"my name is {NAME_MARKER}", "ai response")
        output = buffer.getvalue()

    assert NAME_MARKER not in output
    assert SUBJECT_MARKER not in output
    assert PROMPT_MARKER not in output
