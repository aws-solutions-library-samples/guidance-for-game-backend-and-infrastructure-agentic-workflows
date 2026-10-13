"""Unit tests for the exported-telemetry content guard."""

# Standard library
import importlib
import sys

# Third-party packages
import pytest

# Local modules
from config.telemetry_guard import (
    CAPTURE_OPT_IN_VALUE,
    CAPTURE_TRACE_CONTENT_VAR,
    DISABLED_INSTRUMENTATIONS_VAR,
    GENAI_CAPTURE_CONTENT_VAR,
    MCP_INSTRUMENTOR_ENTRY_POINT,
    SEMCONV_OPT_IN_VAR,
    apply_trace_content_guard,
    resolve_trace_content_env,
)

pytestmark = pytest.mark.unit


def _set_guard_env(monkeypatch, *, opt_in: bool) -> None:
    """Register the guard's env vars with monkeypatch so changes do not leak.

    ``apply_trace_content_guard`` writes ``os.environ`` in place. monkeypatch
    only restores a variable it has recorded, and ``delenv(..., raising=False)``
    on an absent variable records nothing, so set the two switches first; their
    teardown then deletes whatever the guard wrote.
    """
    monkeypatch.setenv(SEMCONV_OPT_IN_VAR, "")
    monkeypatch.setenv(GENAI_CAPTURE_CONTENT_VAR, "")
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)
    monkeypatch.delenv(GENAI_CAPTURE_CONTENT_VAR, raising=False)
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
    if opt_in:
        monkeypatch.setenv(CAPTURE_TRACE_CONTENT_VAR, CAPTURE_OPT_IN_VALUE)


def test_hosted_default_redacts_and_disables_capture():
    decision = resolve_trace_content_env({"GBAW_HOSTED_RUNTIME": "true"})

    assert decision.capture_enabled is False
    assert decision.capture_message_content == "false"
    assert decision.semconv_opt_in == "gen_ai_unredacted_attributes="


def test_default_preserves_unrelated_semconv_token():
    decision = resolve_trace_content_env({SEMCONV_OPT_IN_VAR: "gen_ai_latest_experimental,gen_ai_tool_definitions"})

    tokens = decision.semconv_opt_in.split(",")
    assert "gen_ai_latest_experimental" in tokens
    assert "gen_ai_tool_definitions" in tokens
    assert "gen_ai_unredacted_attributes=" in tokens


def test_default_overrides_preexisting_unredacted_allowlist():
    # A stray unredacted allowlist must collapse to "redact everything sensitive".
    decision = resolve_trace_content_env(
        {SEMCONV_OPT_IN_VAR: "gen_ai_unredacted_attributes=gen_ai.input.messages;gen_ai.output.messages"}
    )

    assert decision.semconv_opt_in == "gen_ai_unredacted_attributes="


def test_opt_in_exact_value_enables_capture_and_removes_redaction():
    decision = resolve_trace_content_env(
        {
            CAPTURE_TRACE_CONTENT_VAR: CAPTURE_OPT_IN_VALUE,
            SEMCONV_OPT_IN_VAR: "gen_ai_latest_experimental",
        }
    )

    assert decision.capture_enabled is True
    assert decision.capture_message_content == "true"
    tokens = [t for t in decision.semconv_opt_in.split(",") if t]
    assert "gen_ai_latest_experimental" in tokens
    assert not any(t.startswith("gen_ai_unredacted_attributes=") for t in tokens)


@pytest.mark.parametrize("value", ["true", "1", "yes", "", "Capture-Content-Nonprod", "capture-content-nonprod "])
def test_opt_in_fails_closed_on_any_other_value(value):
    decision = resolve_trace_content_env({"GBAW_HOSTED_RUNTIME": "true", CAPTURE_TRACE_CONTENT_VAR: value})

    assert decision.capture_enabled is False
    assert decision.capture_message_content == "false"
    assert decision.semconv_opt_in == "gen_ai_unredacted_attributes="


def test_hosted_without_opt_in_never_captures():
    decision = resolve_trace_content_env({"GBAW_HOSTED_RUNTIME": "true"})
    assert decision.capture_enabled is False


def test_apply_overwrites_distro_setdefault_true(monkeypatch):
    # The ADOT distro turns capture on with setdefault before our module imports;
    # the guard must overwrite that to the redacting default.
    monkeypatch.setenv(SEMCONV_OPT_IN_VAR, "")
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)
    monkeypatch.setenv(GENAI_CAPTURE_CONTENT_VAR, "true")
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)

    decision = apply_trace_content_guard()

    # Standard library
    import os as _os

    assert _os.environ[GENAI_CAPTURE_CONTENT_VAR] == "false"
    assert _os.environ[SEMCONV_OPT_IN_VAR] == "gen_ai_unredacted_attributes="
    assert decision.capture_enabled is False


def test_apply_respects_opt_in(monkeypatch):
    monkeypatch.setenv(SEMCONV_OPT_IN_VAR, "")
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)
    monkeypatch.setenv(GENAI_CAPTURE_CONTENT_VAR, "")
    monkeypatch.delenv(GENAI_CAPTURE_CONTENT_VAR, raising=False)
    monkeypatch.setenv(CAPTURE_TRACE_CONTENT_VAR, CAPTURE_OPT_IN_VALUE)

    decision = apply_trace_content_guard()

    # Standard library
    import os as _os

    assert _os.environ[GENAI_CAPTURE_CONTENT_VAR] == "true"
    assert "gen_ai_unredacted_attributes=" not in _os.environ[SEMCONV_OPT_IN_VAR]
    assert decision.capture_enabled is True


# --- MCP tool-content span path -------------------------------------------------
#
# ADOT's MCP instrumentor writes gen_ai.tool.call.arguments / gen_ai.tool.call.result
# unconditionally (no capture switch). The guard closes the in-process path by
# uninstrumenting the ADOT McpInstrumentor singleton, and the subprocess path by
# appending `aws_mcp` to an already-set OTEL_PYTHON_DISABLED_INSTRUMENTATIONS so
# auto-instrumented MCP server children inherit the exclusion.


def test_default_appends_aws_mcp_to_existing_disabled_list():
    # The ADOT distro sets this with its default exclusion list before our guard
    # runs; the resolver must append aws_mcp while preserving existing entries/order.
    existing = "urllib3,requests,system_metrics"
    decision = resolve_trace_content_env({"GBAW_HOSTED_RUNTIME": "true", DISABLED_INSTRUMENTATIONS_VAR: existing})

    assert decision.disabled_instrumentations is not None
    tokens = decision.disabled_instrumentations.split(",")
    assert tokens[:3] == ["urllib3", "requests", "system_metrics"]
    assert tokens[-1] == MCP_INSTRUMENTOR_ENTRY_POINT
    assert tokens.count(MCP_INSTRUMENTOR_ENTRY_POINT) == 1


def test_default_leaves_disabled_list_unset_when_unset():
    # Unset only happens when the distro is not running with agent observability,
    # so children are not auto-instrumented with the default exclusion list either.
    decision = resolve_trace_content_env({"GBAW_HOSTED_RUNTIME": "true"})
    assert decision.disabled_instrumentations is None


def test_default_append_is_idempotent_when_already_present():
    existing = f"urllib3,{MCP_INSTRUMENTOR_ENTRY_POINT},requests"
    decision = resolve_trace_content_env({DISABLED_INSTRUMENTATIONS_VAR: existing})

    tokens = decision.disabled_instrumentations.split(",")
    assert tokens.count(MCP_INSTRUMENTOR_ENTRY_POINT) == 1
    # Existing entries and their order are preserved.
    assert tokens == ["urllib3", MCP_INSTRUMENTOR_ENTRY_POINT, "requests"]


def test_opt_in_leaves_disabled_list_untouched():
    existing = "urllib3,requests"
    decision = resolve_trace_content_env(
        {CAPTURE_TRACE_CONTENT_VAR: CAPTURE_OPT_IN_VALUE, DISABLED_INSTRUMENTATIONS_VAR: existing}
    )
    assert decision.disabled_instrumentations == existing


def test_apply_appends_once_across_two_calls(monkeypatch):
    monkeypatch.setenv(SEMCONV_OPT_IN_VAR, "")
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)
    monkeypatch.setenv(GENAI_CAPTURE_CONTENT_VAR, "")
    monkeypatch.delenv(GENAI_CAPTURE_CONTENT_VAR, raising=False)
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
    monkeypatch.setenv(DISABLED_INSTRUMENTATIONS_VAR, "urllib3,requests")

    apply_trace_content_guard()
    apply_trace_content_guard()

    # Standard library
    import os as _os

    tokens = _os.environ[DISABLED_INSTRUMENTATIONS_VAR].split(",")
    assert tokens == ["urllib3", "requests", MCP_INSTRUMENTOR_ENTRY_POINT]


def test_apply_leaves_disabled_list_unset_when_unset(monkeypatch):
    monkeypatch.setenv(SEMCONV_OPT_IN_VAR, "")
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)
    monkeypatch.setenv(GENAI_CAPTURE_CONTENT_VAR, "")
    monkeypatch.delenv(GENAI_CAPTURE_CONTENT_VAR, raising=False)
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
    monkeypatch.delenv(DISABLED_INSTRUMENTATIONS_VAR, raising=False)

    apply_trace_content_guard()

    # Standard library
    import os as _os

    assert DISABLED_INSTRUMENTATIONS_VAR not in _os.environ


def _reset_mcp_instrumentor():
    """Fully unwrap the MCP SDK methods so the singleton starts single-layered.

    The ADOT ``McpInstrumentor`` is a process-wide ``BaseInstrumentor`` singleton.
    Other tests (or the distro auto-load) may have left it instrumented, and a
    second ``instrument()`` would stack a second ``wrapt`` layer that a single
    ``uninstrument()`` cannot fully peel. Peel every layer here so each test
    exercises the guard against a known, single-instrumented state.
    """
    # Third-party packages
    from amazon.opentelemetry.distro.instrumentation.mcp import McpInstrumentor
    from mcp.shared.session import BaseSession

    instrumentor = McpInstrumentor()
    for _ in range(5):
        if not hasattr(BaseSession.send_request, "__wrapped__"):
            break
        instrumentor._is_instrumented_by_opentelemetry = True
        instrumentor.uninstrument()
    instrumentor._is_instrumented_by_opentelemetry = False
    return instrumentor


def test_apply_uninstruments_mcp_instrumentor_singleton(monkeypatch):
    # Instrument the real ADOT McpInstrumentor, then confirm the guard restores
    # the wrapped MCP SDK methods (no __wrapped__ attribute remains).
    pytest.importorskip("amazon.opentelemetry.distro.instrumentation.mcp")
    pytest.importorskip("mcp")

    # Third-party packages
    from mcp.shared.session import BaseSession

    instrumentor = _reset_mcp_instrumentor()
    instrumentor.instrument()
    try:
        assert hasattr(BaseSession.send_request, "__wrapped__")

        _set_guard_env(monkeypatch, opt_in=False)
        apply_trace_content_guard()

        assert not hasattr(BaseSession.send_request, "__wrapped__")
    finally:
        # Ensure a clean global state for other tests regardless of outcome.
        _reset_mcp_instrumentor()


def test_apply_opt_in_does_not_uninstrument_mcp(monkeypatch):
    pytest.importorskip("amazon.opentelemetry.distro.instrumentation.mcp")
    pytest.importorskip("mcp")

    # Third-party packages
    from mcp.shared.session import BaseSession

    instrumentor = _reset_mcp_instrumentor()
    instrumentor.instrument()
    try:
        _set_guard_env(monkeypatch, opt_in=True)
        apply_trace_content_guard()

        # The opt-in must leave MCP instrumentation in place.
        assert hasattr(BaseSession.send_request, "__wrapped__")
    finally:
        _reset_mcp_instrumentor()


# --- MCP exception-logging leg --------------------------------------------------
#
# Under agent observability the distro installs an OpenTelemetry logging handler
# on the root logger. The MCP client calls logger.exception(...) on a tool
# failure, so the record's exc_info (exception message and stack trace) becomes
# log-record attributes. MCP tool exceptions can echo tool arguments or provider
# output. While redacting, the guard attaches a filter to the MCP client loggers
# that strips exc_info so only the static message survives.

_MCP_EXC_MARKER = "UNIQUE-MCP-EXC-MARKER-5c7e"


@pytest.fixture
def _clean_mcp_log_filters():
    """Remove the guard's logging filter from the MCP loggers before and after.

    The filter is process-wide by design (installed once at startup), so it must
    be stripped around tests that assert on its presence or absence.
    """
    # Standard library
    import logging

    # Local modules
    from config.telemetry_guard import MCP_EXCEPTION_LOGGER_NAMES, _McpExceptionInfoFilter

    def _strip():
        for name in MCP_EXCEPTION_LOGGER_NAMES:
            lg = logging.getLogger(name)
            for flt in list(lg.filters):
                if getattr(flt, "marker", None) == _McpExceptionInfoFilter.marker:
                    lg.removeFilter(flt)

    _strip()
    yield
    _strip()


def _capture_otel_log_attributes(logger_name: str, monkeypatch):
    """Emit a logger.exception on ``logger_name`` through an OTel LoggingHandler.

    Returns the attributes dict the handler would export for that record.
    """
    # Standard library
    import logging

    # Third-party packages
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor

    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)

    target = logging.getLogger(logger_name)
    target.addHandler(handler)
    monkeypatch.setattr(target, "level", logging.NOTSET, raising=False)
    try:
        try:
            raise RuntimeError(_MCP_EXC_MARKER)
        except RuntimeError:
            target.exception("tool execution failed")
    finally:
        target.removeHandler(handler)
    provider.force_flush()

    blobs = []
    for record in exporter.get_finished_logs():
        attrs = dict(record.log_record.attributes or {})
        blobs.append(str(attrs))
    return "\n".join(blobs)


def test_guard_filters_mcp_exception_text_from_log_records(monkeypatch, _clean_mcp_log_filters):
    pytest.importorskip("opentelemetry.sdk._logs")

    _set_guard_env(monkeypatch, opt_in=False)
    apply_trace_content_guard()

    blob = _capture_otel_log_attributes("strands.tools.mcp.mcp_client", monkeypatch)
    # The static message is kept, but no exception message or stack trace.
    assert _MCP_EXC_MARKER not in blob
    assert "exception.stacktrace" not in blob
    assert "exception.message" not in blob
    # The non-content exception type survives so error reporting is not lost.
    assert "exception.type" in blob
    assert "RuntimeError" in blob


def test_mcp_exception_logger_name_is_pinned_to_strands_client():
    # If Strands renames its MCP client module the filter would silently become a
    # no-op; pin the real logger name so that drift fails here.
    # Third-party packages
    import strands.tools.mcp.mcp_client as mcp_client

    # Local modules
    from config.telemetry_guard import MCP_EXCEPTION_LOGGER_NAMES

    assert mcp_client.logger.name in MCP_EXCEPTION_LOGGER_NAMES
    # The MCP SDK root-logger path is deliberately not in the list (documented as
    # residual risk), so the list contains only the Strands client logger.
    assert MCP_EXCEPTION_LOGGER_NAMES == ("strands.tools.mcp.mcp_client",)


def test_opt_in_leaves_mcp_exception_text_in_log_records(monkeypatch, _clean_mcp_log_filters):
    pytest.importorskip("opentelemetry.sdk._logs")

    _set_guard_env(monkeypatch, opt_in=True)
    apply_trace_content_guard()

    blob = _capture_otel_log_attributes("strands.tools.mcp.mcp_client", monkeypatch)
    # Under the opt-in the exception text is retained for debugging.
    assert _MCP_EXC_MARKER in blob


def test_mcp_logging_filter_is_idempotent(monkeypatch, _clean_mcp_log_filters):
    # Standard library
    import logging

    _set_guard_env(monkeypatch, opt_in=False)
    apply_trace_content_guard()
    apply_trace_content_guard()

    target = logging.getLogger("strands.tools.mcp.mcp_client")
    markers = [getattr(flt, "marker", None) for flt in target.filters]
    assert markers.count("gbaw_trace_content_guard") == 1


# --- MCP uninstrument robustness (ordering, no spurious warnings) ---------------


def test_uninstrument_logs_no_warning_when_not_instrumented(monkeypatch, caplog):
    # Standard library
    import logging

    pytest.importorskip("amazon.opentelemetry.distro.instrumentation.mcp")
    pytest.importorskip("mcp")

    _reset_mcp_instrumentor()  # leaves the singleton uninstrumented
    _set_guard_env(monkeypatch, opt_in=False)

    with caplog.at_level(logging.WARNING):
        apply_trace_content_guard()

    text = caplog.text.lower()
    assert "already uninstrumented" not in text


def test_uninstrument_warns_when_adot_layer_remains_under_a_client(monkeypatch, caplog):
    # Standard library
    import logging

    pytest.importorskip("amazon.opentelemetry.distro.instrumentation.mcp")
    pytest.importorskip("mcp")

    # Third-party packages
    from mcp.shared.session import BaseSession
    from wrapt import wrap_function_wrapper

    instrumentor = _reset_mcp_instrumentor()
    instrumentor.instrument()  # ADOT ClientWrapper is the outermost layer
    try:
        # Simulate a Strands MCPClient wrapping the same method AFTER ADOT, so a
        # single uninstrument() peels only the Strands layer and leaves the ADOT
        # content-writing layer underneath.
        def _outer(wrapped, instance, args, kwargs):  # pragma: no cover - not called
            return wrapped(*args, **kwargs)

        wrap_function_wrapper("mcp.shared.session", "BaseSession.send_request", _outer)

        _set_guard_env(monkeypatch, opt_in=False)
        with caplog.at_level(logging.WARNING):
            apply_trace_content_guard()

        assert "MCP tool-content instrumentation may remain active" in caplog.text
    finally:
        _reset_mcp_instrumentor()
