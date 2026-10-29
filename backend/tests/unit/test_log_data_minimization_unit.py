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


class _StructuredSink:
    """A callable loguru sink that captures each record's structured parts.

    A callable sink receives the loguru ``Message`` whose ``.record`` exposes the
    bound ``extra`` dict and the attached ``exception`` without rendering only
    ``{message}``. This lets a test inspect the structured ``frames`` extra and
    the exception a sink would serialize (for example a ``serialize=True`` sink),
    so a leak hidden in ``bind(...)`` rather than the message line is caught.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []
        self._sink_id = logger.add(self._write, level="DEBUG", format="{message}", backtrace=False, diagnose=False)

    def _write(self, message: "object") -> None:
        self.records.append(dict(message.record))  # type: ignore[attr-defined]

    def __enter__(self) -> "_StructuredSink":
        return self

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
    """The production error path records ``module:function:lineno`` raise-site
    frames so a plain bug (KeyError/TypeError) is triageable, while still never
    emitting the message, locals, or a filesystem path.
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
    """The frame helper caps the number of frames, orders them innermost-first,
    and never reads the message.

    A deeply recursive stack must not produce an unbounded line, the raise site
    must come first, the outermost (test) frame must be dropped once the stack
    exceeds the cap, and the frames must carry only ``module:function:lineno``
    loci — a secret-shaped marker in the exception message must not appear.
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
    # Innermost-first ordering: frames[0] is the actual raise site (the ``raise``
    # inside ``_recurse``), not an outer frame.
    assert frames[0].startswith(f"{__name__}:_recurse:")
    assert all(":_recurse:" in frame for frame in frames)
    # The stack is deeper than the cap, so the outermost frame — this test
    # function, which called ``_recurse`` — is dropped by the innermost-first
    # bound.
    assert not any(
        f"{__name__}:test_sanitized_traceback_frames_are_bounded_and_content_free:" in frame for frame in frames
    )


def test_sanitized_exception_structured_frames_extra_is_content_free():
    """The structured ``frames`` extra (what a serialize=True sink emits) carries
    only ``module:function:lineno`` loci, never the exception message.

    This inspects ``record["extra"]`` and the attached exception directly through
    a callable sink, so a leak bound into ``bind(...)`` — or folded into the
    bound ``frames`` list — is caught even though the stdout format renders only
    ``{message}``.
    """
    # Local modules
    from utils.security import log_sanitized_exception

    def _inner_boom():
        raise KeyError(f"missing {PROMPT_MARKER} for actor {SUBJECT_MARKER}")

    try:
        _inner_boom()
    except KeyError as exc:
        with _StructuredSink() as sink:
            log_sanitized_exception(logger, "operation failed", exc, request_id="req-extra", debug_traceback=False)

    assert sink.records, "expected a captured record"
    record = sink.records[-1]
    extra = record["extra"]

    # The bound frames extra is a list of loci and references the raising
    # function, proving it is populated.
    frames_extra = extra["frames"]
    assert isinstance(frames_extra, list) and frames_extra
    assert any(":_inner_boom:" in frame for frame in frames_extra)

    # No bound extra value — including a leaked error string — carries the
    # secret-shaped message content.
    for key, value in extra.items():
        assert PROMPT_MARKER not in str(value), f"marker leaked into extra[{key!r}]"
        assert SUBJECT_MARKER not in str(value), f"marker leaked into extra[{key!r}]"
        assert "missing" not in str(value), f"message slice leaked into extra[{key!r}]"

    # The production path attaches no exception object, so a serialize=True sink
    # cannot render str(exc) into its "exception" field.
    assert record["exception"] is None


@pytest.mark.parametrize(
    "name_label",
    [
        pytest.param("missing", id="missing-name"),
        pytest.param("non-string", id="non-string-name"),
        pytest.param("str-subclass", id="str-subclass-name"),
    ],
)
def test_sanitized_traceback_frames_total_for_hostile_frames(name_label):
    """The helper never raises and never leaks a file path for a hostile frame.

    A frame whose module ``__name__`` is deleted entirely, is a non-string whose
    ``__str__`` raises, or is a ``str`` subclass supplying its own ``__str__``
    text must not make the helper raise (which would hand the original exception
    to the SDK's unredacted formatter), and must resolve to a code-owned
    ``<unknown>`` sentinel rather than ``co_filename`` or subclass-authored text.
    """
    # Local modules
    from utils.security import _UNKNOWN_FRAME_MODULE, sanitized_traceback_frames

    class _ExplodingName:
        def __str__(self) -> str:
            raise RuntimeError(f"boom {PROMPT_MARKER}")

        def __repr__(self) -> str:
            raise RuntimeError(f"boom {PROMPT_MARKER}")

    class _DisclosingName(str):
        # An ``isinstance(..., str)`` guard would accept this subclass and then
        # read its overridden ``__str__``; an exact ``type(...) is str`` check
        # rejects it, so the subclass-authored text never reaches the log.
        def __str__(self) -> str:
            return f"leaked {PROMPT_MARKER}"

    hostile = {
        "non-string": _ExplodingName(),
        "str-subclass": _DisclosingName("placeholder"),
    }

    def _boom():
        raise ValueError("hostile frame")

    try:
        _boom()
    except ValueError as exc:
        # Make the raising frame's module ``__name__`` hostile. The frame shares
        # this module's globals, so save and restore the key around the call to
        # avoid leaking the mutation into other tests. The missing-name case
        # deletes the key entirely so the ``.get("__name__")`` lookup returns
        # ``None``; the other cases install a non-string or a ``str`` subclass.
        tb = exc.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        assert tb is not None
        globals_dict = tb.tb_frame.f_globals
        sentinel = object()
        saved = globals_dict.get("__name__", sentinel)
        if name_label in hostile:
            globals_dict["__name__"] = hostile[name_label]
        else:
            globals_dict.pop("__name__", None)
        try:
            # The helper must stay total.
            frames = sanitized_traceback_frames(exc)
        finally:
            if saved is sentinel:
                globals_dict.pop("__name__", None)
            else:
                globals_dict["__name__"] = saved

    assert frames, "helper returned no frames for a valid traceback"
    # The hostile frame resolved to the sentinel, not a filesystem path or
    # subclass-authored text.
    innermost = frames[0]
    assert innermost.startswith(f"{_UNKNOWN_FRAME_MODULE}:")
    # No file path or marker leaked from the hostile frame.
    assert ".py" not in innermost
    assert PROMPT_MARKER not in innermost


def test_sanitized_traceback_frames_reject_subclass_coname_and_overridden_globals_get():
    """A ``str``-subclass ``co_name`` and a mapping whose ``get`` is overridden
    cannot inject attacker-chosen text into a frame locus.

    The function component is read through an exact ``type(...) is str`` check,
    so a ``str`` subclass whose ``__str__`` is overridden resolves to the
    code-owned sentinel instead of its own text. The module component is read
    with ``dict.get`` applied directly to ``f_globals``, so a ``dict`` subclass
    whose ``get`` returns other text is bypassed and the genuine ``__name__``
    (or the sentinel) is used.
    """
    # Standard library
    import types

    # Local modules
    from utils.security import _UNKNOWN_FRAME_MODULE, sanitized_traceback_frames

    class _DisclosingCoName(str):
        def __str__(self) -> str:
            return f"leaked {SUBJECT_MARKER}"

    class _DisclosingGlobals(dict):
        def get(self, key, default=None):  # type: ignore[override]
            if key == "__name__":
                return f"leaked {PROMPT_MARKER}"
            return super().get(key, default)

    def _raiser():
        raise ValueError("subclass co_name and overridden get")

    # A code object whose ``co_name`` is a ``str`` subclass, run with a mapping
    # subclass as its globals whose ``get`` returns attacker-chosen text.
    forged_code = _raiser.__code__.replace(co_name=_DisclosingCoName("realname"))
    hostile_globals = _DisclosingGlobals(_raiser.__globals__)
    hostile_globals["__name__"] = "genuine.module"
    forged = types.FunctionType(forged_code, hostile_globals, "forged")

    try:
        forged()
    except ValueError as exc:
        frames = sanitized_traceback_frames(exc)

    assert frames, "helper returned no frames for a valid traceback"
    innermost = frames[0]
    # The overridden ``get`` text never reaches the module component: the real
    # ``__name__`` is read through ``dict.get``.
    assert innermost.startswith("genuine.module:")
    assert PROMPT_MARKER not in innermost
    # The ``str``-subclass ``co_name`` is rejected by the exact-type check and
    # resolves to the code-owned sentinel, not the subclass-authored text.
    function = innermost.split(":")[1]
    assert function == _UNKNOWN_FRAME_MODULE
    assert SUBJECT_MARKER not in innermost


def test_sanitized_traceback_frames_total_when_component_helper_raises(monkeypatch):
    """The ``except`` totality wrapper is exercised: when building a frame
    component raises, the helper swallows it and returns an innermost-first,
    content-free list rather than propagating to the SDK's unredacted formatter.
    """
    # Local modules
    from utils import security
    from utils.security import sanitized_traceback_frames

    def _inner():
        raise ValueError("component helper will raise")

    def _outer():
        _inner()

    calls = {"n": 0}
    real_component = security._frame_component

    def _raising_component(value):
        # Let the two outer frames build normally, then raise while composing the
        # innermost frame, so several frames are already gathered (outermost
        # first) when the walk fails. This drives the ``except`` path with a
        # multi-element partial list whose order the wrapper must still fix.
        calls["n"] += 1
        if calls["n"] >= 5:
            raise RuntimeError("component boom")
        return real_component(value)

    monkeypatch.setattr(security, "_frame_component", _raising_component)

    try:
        _outer()
    except ValueError as exc:
        # Must not raise even though the component helper does.
        frames = sanitized_traceback_frames(exc)

    # Two frames were gathered before the failure. The ``except`` path reverses
    # the partial list, so it stays innermost-first (matching the success-path
    # ordering the docstring promises): the deeper ``_outer`` frame leads the
    # shallower test frame rather than trailing it.
    assert len(frames) >= 2, "expected a multi-frame partial list from the totality wrapper"
    assert frames[0].startswith(f"{__name__}:_outer:")
    assert frames[-1].startswith(f"{__name__}:test_sanitized_traceback_frames_total_when_component_helper_raises:")
    # No message content leaked through the failed walk.
    assert all("component boom" not in frame for frame in frames)


def test_sanitized_traceback_frames_cap_component_length():
    """A pathologically long module ``__name__`` or function ``co_name`` is
    length-capped per component, so one frame cannot produce an unbounded line."""
    # Standard library
    import types

    # Local modules
    from utils.security import _MAX_FRAME_COMPONENT_LENGTH, sanitized_traceback_frames

    def _boom():
        raise ValueError("long name")

    try:
        _boom()
    except ValueError as exc:
        tb = exc.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        assert tb is not None
        globals_dict = tb.tb_frame.f_globals
        saved = globals_dict.get("__name__")
        globals_dict["__name__"] = "m" * 100_000
        try:
            frames = sanitized_traceback_frames(exc)
        finally:
            globals_dict["__name__"] = saved

    module = frames[0].split(":", 1)[0]
    assert len(module) <= _MAX_FRAME_COMPONENT_LENGTH

    # A very long function name is capped the same way, so neither component of a
    # frame locus can grow without bound.
    forged_code = _boom.__code__.replace(co_name="f" * 100_000)
    forged = types.FunctionType(forged_code, _boom.__globals__, "forged")
    try:
        forged()
    except ValueError as exc:
        frames = sanitized_traceback_frames(exc)

    function = frames[0].split(":")[1]
    assert len(function) <= _MAX_FRAME_COMPONENT_LENGTH

    # A function name made entirely of lone surrogates must also stay within the
    # cap. Each surrogate expands to a six-character backslash escape, so the
    # escape has to run before the cap: capping first would leave up to
    # _MAX_FRAME_COMPONENT_LENGTH surrogates that then expand to six times the
    # bound. Asserting the final length pins the escape-before-cap order.
    surrogate_name = "".join(chr(0xD800 + (i % 0x400)) for i in range(_MAX_FRAME_COMPONENT_LENGTH * 4))
    surrogate_code = _boom.__code__.replace(co_name=surrogate_name)
    surrogate_fn = types.FunctionType(surrogate_code, _boom.__globals__, "forged")
    try:
        surrogate_fn()
    except ValueError as exc:
        frames = sanitized_traceback_frames(exc)

    surrogate_function = frames[0].split(":")[1]
    assert len(surrogate_function) <= _MAX_FRAME_COMPONENT_LENGTH
    # No raw lone surrogate survived; each was replaced by its backslash escape
    # before the cap, so the component is strict-UTF-8 encodable.
    surrogate_function.encode("utf-8")


def test_sanitized_traceback_frames_normalize_function_name():
    """CR/LF in a frame's ``co_name`` is normalized, so it cannot forge a second
    line in the structured ``frames`` extra or the message."""
    # Standard library
    import types

    # Local modules
    from utils.security import sanitized_traceback_frames

    def _raiser():
        raise ValueError("crlf co_name")

    # Build a function whose code object carries a CR/LF-bearing co_name, so the
    # raising frame's function name holds control characters without touching the
    # read-only f_code of a live frame.
    forged_code = _raiser.__code__.replace(co_name="victim\r\nINJECTED-FUNC")
    forged = types.FunctionType(forged_code, _raiser.__globals__, "forged")

    try:
        forged()
    except ValueError as exc:
        frames = sanitized_traceback_frames(exc)

    locus = frames[0]
    assert "\r" not in locus
    assert "\n" not in locus
    assert "INJECTED-FUNC" in locus  # content kept, but collapsed to one line


def test_sanitized_traceback_frames_lone_surrogate_is_strict_utf8_encodable():
    """A lone surrogate in a frame component is escaped, so a strict UTF-8 sink
    can encode the composed line.

    A frame ``co_name`` holding a lone surrogate would make a strict UTF-8 sink
    raise ``UnicodeEncodeError``; a sink failure during a log call inside an
    ``except`` block drives Loguru's handler-error path, which prints the
    in-flight exception to stderr. The component must therefore be strict-UTF-8
    encodable.
    """
    # Standard library
    import io
    import types

    # Local modules
    from utils.security import log_sanitized_exception, sanitized_traceback_frames

    def _raiser():
        raise ValueError("lone surrogate co_name")

    # A lone high surrogate (U+D800) cannot be encoded by a strict UTF-8 codec.
    forged_code = _raiser.__code__.replace(co_name="victim\ud800x")
    forged = types.FunctionType(forged_code, _raiser.__globals__, "forged")

    try:
        forged()
    except ValueError as exc:
        frames = sanitized_traceback_frames(exc)
        # Every component is strict-UTF-8 encodable (no raised UnicodeEncodeError).
        for frame in frames:
            frame.encode("utf-8")
        # The raw lone surrogate does not survive; it is replaced by an escape.
        assert "\ud800" not in frames[0]

        # A strict UTF-8 sink writes the composed line without the handler-error
        # path triggering.
        strict_buffer = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict", newline="")
        sink_id = logger.add(strict_buffer, level="DEBUG", format="{message}", backtrace=False, diagnose=False)
        try:
            log_sanitized_exception(logger, "operation failed", exc, request_id="req-surrogate", debug_traceback=False)
        finally:
            logger.remove(sink_id)
        strict_buffer.flush()
        written = strict_buffer.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]

    assert "operation failed" in written
    assert "frames=" in written


def test_sanitized_exception_debug_mode_still_renders_full_traceback():
    """Debug mode attaches the exception so the framework renders the full
    traceback, in addition to the bounded frame loci."""
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
    fake_client.batch_create_memory_records.return_value = {
        "successfulRecords": [{"memoryRecordId": "synthetic-record"}],
        "failedRecords": [],
    }
    fake_client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
    monkeypatch.setattr(sm, "_get_memory_client", lambda: fake_client)
    monkeypatch.setattr(sm, "_get_existence_check_client", lambda: fake_client)
    monkeypatch.setattr(sm, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-test", raising=False)
    monkeypatch.setattr(sm, "_saved_memory_hashes", {})
    monkeypatch.setattr(sm, "_inflight_hashes", set())

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
    fake_client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
    monkeypatch.setattr(sm, "_get_memory_client", lambda: fake_client)
    monkeypatch.setattr(sm, "_get_existence_check_client", lambda: fake_client)
    monkeypatch.setattr(sm, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-test", raising=False)
    monkeypatch.setattr(sm, "_is_duplicate_memory", lambda *a, **k: False)
    monkeypatch.setattr(sm, "_saved_memory_hashes", {})
    monkeypatch.setattr(sm, "_inflight_hashes", set())

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
