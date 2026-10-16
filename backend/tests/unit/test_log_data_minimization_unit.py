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
    # Empty / missing values resolve to a fixed, non-reversible placeholder.
    assert redact_identifier(None) == "<none>"
    assert redact_identifier("") == "<none>"


def test_redact_identifier_accepts_text_that_cannot_be_encoded_as_utf8():
    """A log helper must never fail the request it is describing.

    ``json.loads`` accepts a lone surrogate, so a request-supplied identifier can
    hold text that ``str.encode`` rejects. Redacting it must still return a bounded,
    stable token, and distinct such values must stay distinct.
    """
    # Local modules
    from utils.security import redact_identifier

    token = redact_identifier("\ud800-synthetic-marker")

    assert token.startswith("id:")
    assert "synthetic-marker" not in token
    assert token == redact_identifier("\ud800-synthetic-marker")
    assert token != redact_identifier("\ud801-synthetic-marker")
    assert token != redact_identifier("-synthetic-marker")


def test_redact_identifier_length_is_clamped():
    """A caller cannot widen the token to the full digest or shrink it away."""
    # Local modules
    from utils.security import redact_identifier

    # An over-large request is clamped: the hex body never reaches the full
    # 64-char SHA-256 digest.
    wide = redact_identifier(SUBJECT_MARKER, length=1000)
    assert len(wide.removeprefix("id:")) <= 32
    # A too-small request is widened to a distinguishing minimum.
    narrow = redact_identifier(SUBJECT_MARKER, length=1)
    assert len(narrow.removeprefix("id:")) >= 8


@pytest.mark.parametrize(
    "code_point",
    [
        "\x00",  # NUL (C0)
        "\x1b",  # ESC (C0)
        "\x7f",  # DEL
        "\x85",  # NEL (C1)
        "\x9b",  # CSI (C1)
        "\u2028",  # line separator
        "\u2029",  # paragraph separator
        "\u202e",  # right-to-left override (bidi)
        "\u2066",  # left-to-right isolate (bidi)
    ],
)
def test_normalize_log_value_covers_control_and_separator_code_points(code_point):
    """Every control / line-separator / bidi code point is collapsed, so no
    Unicode-aware consumer can split one value across lines or reorder it."""
    # Local modules
    from utils.security import normalize_log_value

    out = normalize_log_value(f"a{code_point}b")
    assert code_point not in out
    # The value must still be a single logical line for a splitlines() consumer.
    assert len(out.splitlines()) == 1


def test_classify_exception_returns_sanitized_codes():
    """Exceptions map to a stable code vocabulary with no provider text."""
    # Third-party packages
    from botocore.exceptions import ClientError

    # Local modules
    from utils.security import (
        ERROR_ACCESS_DENIED,
        ERROR_PROVIDER_ERROR,
        ERROR_TIMEOUT,
        classify_exception,
    )

    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:iam::x"}}, "Op")
    assert classify_exception(denied) == ERROR_ACCESS_DENIED
    assert classify_exception(TimeoutError("too slow")) == ERROR_TIMEOUT
    assert classify_exception(RuntimeError("boom")) == ERROR_PROVIDER_ERROR


def test_log_sanitized_exception_hides_message_keeps_class_and_code():
    """The sanitized exception logger emits class + code but never the message,
    even when the message carries a prompt marker and a CR/LF."""
    # Local modules
    from utils.security import log_sanitized_exception

    exc = RuntimeError(f"ValidationException: input '{PROMPT_MARKER}' actor={SUBJECT_MARKER}\nFORGED")
    with _CaptureSink() as buffer:
        log_sanitized_exception(logger, "operation failed", exc, request_id="req-abcd", debug_traceback=False)
        output = buffer.getvalue()

    assert "operation failed" in output
    assert "RuntimeError" in output  # class is kept for diagnosis
    assert PROMPT_MARKER not in output
    assert SUBJECT_MARKER not in output
    assert "FORGED" not in output
    assert len(output.strip().splitlines()) == 1  # no injected second line


def test_sanitized_exception_logs_bounded_raise_site_frames():
    """Red-green: the production error path records ``module:function:lineno``
    raise-site frames so a plain bug (KeyError/TypeError) is triageable, while
    still never emitting the message, locals, or a filesystem path.

    This fails on a build that logs only ``[class=… code=… request_id=…]`` with
    no frames, and passes once bounded frames are composed into the record.
    """
    # Local modules
    from utils.security import log_sanitized_exception

    def _inner_boom():
        # A plain-bug exception whose message embeds a secret-shaped marker.
        raise KeyError(f"missing {PROMPT_MARKER} for actor {SUBJECT_MARKER}")

    def _outer_call():
        _inner_boom()

    try:
        _outer_call()
    except KeyError as exc:
        with _CaptureSink() as buffer:
            log_sanitized_exception(logger, "operation failed", exc, request_id="req-frames", debug_traceback=False)
            output = buffer.getvalue()

    # Raise-site loci are present as module:function:lineno for each frame.
    assert "frames=" in output
    assert f"{__name__}:_inner_boom:" in output
    assert f"{__name__}:_outer_call:" in output
    # The innermost frame is the actual raise site (KeyError in _inner_boom).
    assert "_inner_boom" in output

    # The secret-shaped message content is never emitted, even though the
    # exception carried it and the frame list references the raising function.
    assert PROMPT_MARKER not in output
    assert SUBJECT_MARKER not in output
    assert "missing" not in output  # no slice of the exception message

    # No filesystem path leaks: only the dotted module name, not a .py path or
    # an absolute directory, reaches the sink.
    assert ".py" not in output
    assert "/" not in output.split("frames=")[1]

    # Still a single line (no injected second line, no rendered traceback block).
    assert len(output.strip().splitlines()) == 1
    assert "Traceback (most recent call last)" not in output


def test_sanitized_traceback_frames_are_bounded_and_content_free():
    """The frame helper caps the number of frames and never reads the message.

    A deeply recursive stack must not produce an unbounded line, and the frames
    must carry only ``module:function:lineno`` loci — a secret-shaped marker in
    the exception message must not appear in any frame.
    """
    # Local modules
    from utils.security import _MAX_TRACEBACK_FRAMES, sanitized_traceback_frames

    def _recurse(n):
        if n <= 0:
            raise ValueError(f"deep failure carrying {PROMPT_MARKER}")
        _recurse(n - 1)

    try:
        _recurse(_MAX_TRACEBACK_FRAMES + 25)
    except ValueError as exc:
        frames = sanitized_traceback_frames(exc)

    # Bounded: the recursion is far deeper than the cap, yet the list is clamped.
    assert 0 < len(frames) <= _MAX_TRACEBACK_FRAMES
    # Each frame is a module:function:lineno locus; none carries message content.
    for frame in frames:
        assert frame.count(":") >= 2
        assert PROMPT_MARKER not in frame
    # The innermost frame (raise site) is kept.
    assert any(":_recurse:" in frame for frame in frames)


def test_sanitized_exception_debug_mode_still_renders_full_traceback():
    """Debug mode is unchanged: it still attaches the exception so the framework
    renders the full traceback, in addition to the bounded frame loci."""
    # Local modules
    from utils.security import log_sanitized_exception

    def _boom():
        raise RuntimeError("debug-mode marker")

    try:
        _boom()
    except RuntimeError as exc:
        with _CaptureSink() as buffer:
            log_sanitized_exception(logger, "operation failed", exc, request_id="req-dbg", debug_traceback=True)
            output = buffer.getvalue()

    # Debug attaches the exception, so the rendered traceback block is present.
    assert "Traceback (most recent call last)" in output
    assert "RuntimeError" in output
    # The bounded frame loci are present regardless of debug mode.
    assert "frames=" in output


# ---------------------------------------------------------------------------
# agentcore_main invocation path
# ---------------------------------------------------------------------------


def _run_invoke(monkeypatch, prompt_payload, *, orchestrator=None):
    """Import agentcore_main and run invoke_agent with the orchestrator stubbed.

    The import itself is guarded by patching ``boto3.client`` so a standalone run
    with ambient credentials never performs a real STS ``GetCallerIdentity`` at
    module import time.
    """
    with patch("boto3.client") as mock_boto:
        mock_boto.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
        # Local modules
        import agentcore_main

    monkeypatch.setattr(agentcore_main, "_CREDENTIALS_OK", True, raising=False)
    monkeypatch.setattr(
        agentcore_main,
        "run_orchestrator",
        orchestrator if orchestrator is not None else (lambda *a, **k: "ok"),
        raising=False,
    )
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
        result = _run_invoke(monkeypatch, payload)
        output = buffer.getvalue()

    for marker in (PROMPT_MARKER, NAME_MARKER, EMAIL_MARKER, SUBJECT_MARKER, THREAD_MARKER):
        assert marker not in output, f"forbidden marker leaked into logs: {marker!r}"
    assert "\r\nINJECTED" not in output
    # Positive assertions so an early return cannot pass silently: the request
    # ran through to the stubbed orchestrator and emitted the redacted actor line.
    assert result == "ok"
    assert "🆔 Actor: id:" in output


def test_invoke_agent_error_path_hides_exception_payload(monkeypatch):
    """When the orchestrator raises an exception whose message embeds a prompt
    marker and a CR/LF, the error log keeps the class but not the payload."""

    def _boom(*_a, **_k):
        raise RuntimeError(f"ValidationException: '{PROMPT_MARKER}' actor={SUBJECT_MARKER}\nFORGED-LINE")

    payload = {
        "prompt": "a normal question",
        "thread_id": "thread-ok",
        "user_context": {"user_id": "u-1", "session_id": "s-1"},
    }
    with _CaptureSink() as buffer:
        result = _run_invoke(monkeypatch, payload, orchestrator=_boom)
        output = buffer.getvalue()

    assert PROMPT_MARKER not in output
    assert SUBJECT_MARKER not in output
    assert "FORGED-LINE" not in output
    assert "RuntimeError" in output  # class retained for diagnosis
    # The user-facing string is the generic failure message.
    assert "error processing your request" in result


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
        result = sm.save_semantic_memory(SUBJECT_MARKER, f"User's name is {NAME_MARKER}")
        output = buffer.getvalue()

    assert NAME_MARKER not in output
    assert SUBJECT_MARKER not in output
    # Positive assertion: the save ran and recorded a safe outcome line.
    assert result is True
    assert "Semantic memory saved" in output


def test_semantic_memory_save_failure_hides_exception_payload(monkeypatch):
    """A failed LTM save logs the exception class/code but never the record text
    or actor namespace carried in a provider validation message."""
    # Local modules
    import utils.semantic_memory as sm

    fake_client = MagicMock()
    fake_client.batch_create_memory_records.side_effect = RuntimeError(
        f"ValidationException: namespace={SUBJECT_MARKER} content='{NAME_MARKER}'\nFORGED"
    )
    monkeypatch.setattr(sm, "_get_memory_client", lambda: fake_client)
    monkeypatch.setattr(sm, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-test", raising=False)
    monkeypatch.setattr(sm, "_is_duplicate_memory", lambda *a, **k: False)

    with _CaptureSink() as buffer:
        result = sm.save_semantic_memory(SUBJECT_MARKER, f"User's name is {NAME_MARKER}")
        output = buffer.getvalue()

    assert result is False
    assert NAME_MARKER not in output
    assert SUBJECT_MARKER not in output
    assert "FORGED" not in output
    assert "RuntimeError" in output


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


# ---------------------------------------------------------------------------
# Request-ID correlation and sink-level normalization
# ---------------------------------------------------------------------------


class _RequestIdSink:
    """A sink whose format surfaces the bound request-ID extra."""

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._sink_id = logger.add(
            self._buffer,
            level="DEBUG",
            format="req={extra[request_id]} | {message}",
            backtrace=False,
            diagnose=False,
        )

    def __enter__(self) -> "io.StringIO":
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink_id)


def test_records_carry_a_request_id_extra_without_raising():
    """Every record exposes extra[request_id]; the default is the no-request
    sentinel and a bound value flows through to the sink format."""
    # Local modules
    from utils.logger import _REQUEST_ID_VAR

    with _RequestIdSink() as buffer:
        logger.info("outside a request")  # default sentinel, no KeyError
        token = _REQUEST_ID_VAR.set("req-1234")
        try:
            logger.info("inside a request")
        finally:
            _REQUEST_ID_VAR.reset(token)
        output = buffer.getvalue()

    assert "req=- | outside a request" in output
    assert "req=req-1234 | inside a request" in output


class _RequestIdAndLocationSink:
    """A sink whose format surfaces the request-ID extra and the record source
    location, so a test can assert both correlation and caller attribution."""

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._sink_id = logger.add(
            self._buffer,
            level="DEBUG",
            format="{name}:{function}:{line} | req={extra[request_id]} | {message}",
            backtrace=False,
            diagnose=False,
        )

    def __enter__(self) -> "io.StringIO":
        return self._buffer

    def __exit__(self, *_exc: object) -> None:
        logger.remove(self._sink_id)


def test_sanitized_exception_without_request_id_uses_bound_request_id(monkeypatch):
    """A swallowed error logged without an explicit request_id still correlates.

    A stub orchestrator mirrors a real swallowed-error site: inside
    ``invoke_agent`` (which binds the runtime request ID), it calls
    ``log_sanitized_exception`` with no ``request_id``. The record must carry the
    bound request ID — not the ``<none>`` sentinel — and must be attributed to
    the calling module, not to the helper in ``utils.security``.
    """
    # Local modules
    from utils.security import log_sanitized_exception

    def _orchestrator_that_swallows(*_a, **_k):
        try:
            raise RuntimeError("memory setup failed")
        except Exception as exc:
            # No request_id passed: the helper must fall back to the bound ID.
            log_sanitized_exception(logger, "⚠️ Memory setup failed, using fallback", exc)
        return "ok"

    payload = {
        "prompt": "a normal question",
        "thread_id": "thread-ok",
        "user_context": {"user_id": "u-1", "session_id": "s-1"},
    }

    # Force a known runtime request ID for the invocation.
    with patch("agentcore_main._current_request_id", return_value="req-CAFE0001"):
        with _RequestIdAndLocationSink() as buffer:
            result = _run_invoke(monkeypatch, payload, orchestrator=_orchestrator_that_swallows)
            output = buffer.getvalue()

    assert result == "ok"
    # Find the swallowed-error record among the invocation's lines.
    error_line = next(line for line in output.splitlines() if "Memory setup failed" in line)
    # Correlation: the bound runtime request ID, not the <none> sentinel.
    assert "req=req-CAFE0001" in error_line
    assert "request_id=req-CAFE0001" in error_line
    assert "<none>" not in error_line
    # Attribution: the caller's module, not utils.security.
    assert error_line.startswith("test_log_data_minimization_unit:_orchestrator_that_swallows:")
    assert "utils.security:" not in error_line


def test_orchestrator_model_loop_failure_hides_prompt_bearing_exception(monkeypatch):
    """When ``agent(query)`` fails on a cost-report follow-up, the orchestrator
    records only the sanitized class/code/request-id, never the exception
    message, which can quote the user prompt or a provider validation payload."""
    # Standard library
    import importlib

    # Local modules
    import agents.orchestrator as orch

    # The shared autouse fixture replaces ``agents.orchestrator.run_orchestrator``
    # with a stub. Reload the module to obtain the genuine implementation under
    # test (all of its heavy dependencies are stubbed below).
    orch = importlib.reload(orch)

    # Force the cost-report-followup branch (the simplest route to the two
    # changed sites) and keep memory off so the fallback agent is used.
    monkeypatch.setattr(orch, "_is_cost_report_followup", lambda _q: True)
    monkeypatch.setattr(orch, "USE_BEDROCK_SESSIONS", False, raising=False)
    monkeypatch.setattr(orch, "create_cached_bedrock_model", lambda: object(), raising=False)
    monkeypatch.setattr(orch, "create_bedrock_model_with_overrides", lambda **k: object(), raising=False)
    monkeypatch.setattr(orch, "INFERENCE_CONFIG", {}, raising=False)
    monkeypatch.setattr(orch, "get_optimized_orchestrator_prompt", lambda: "sys", raising=False)
    monkeypatch.setattr(orch, "get_prompt_versions", lambda: {}, raising=False)
    # The capture helpers return "no authoritative report / no specialist output"
    # so the followup branch is taken.
    monkeypatch.setattr(orch, "begin_cost_report_capture", lambda: None, raising=False)
    monkeypatch.setattr(orch, "finish_cost_report_capture", lambda _c: None, raising=False)
    monkeypatch.setattr(orch, "begin_specialist_capture", lambda: None, raising=False)
    monkeypatch.setattr(orch, "finish_specialist_capture", lambda _c: {}, raising=False)

    class _RaisingAgent:
        def __init__(self, *a, **k):
            pass

        def __call__(self, _query):
            raise RuntimeError(f"ValidationException: input '{PROMPT_MARKER}' actor={SUBJECT_MARKER}\nFORGED-LINE")

    monkeypatch.setattr(orch, "Agent", _RaisingAgent, raising=False)

    with _CaptureSink() as buffer:
        response = orch.run_orchestrator(query="show me cost report rpt-123", context={})
        output = buffer.getvalue()

    # The deterministic follow-up failure response is returned, not a raise.
    assert response == orch._COST_REPORT_FOLLOWUP_FAILURE
    # The sanitized record is present; the raw message/prompt/actor are not.
    assert "RuntimeError" in output
    assert "code=provider_error" in output
    assert PROMPT_MARKER not in output
    assert SUBJECT_MARKER not in output
    assert "FORGED-LINE" not in output


def test_sink_patcher_normalizes_crlf_from_any_interpolated_value():
    """A CR/LF carried by an interpolated value is collapsed at the sink, even
    when the originating call site did not sanitize it."""
    with _CaptureSink() as buffer:
        tainted = "value\r\nINJECTED-SINK-LINE"
        logger.info(f"field: {tainted}")
        output = buffer.getvalue()

    assert "INJECTED-SINK-LINE" in output  # content kept, but on the same line
    assert "\r" not in output
    assert len(output.strip().splitlines()) == 1
