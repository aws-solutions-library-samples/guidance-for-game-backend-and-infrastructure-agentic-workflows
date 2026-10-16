"""Unit tests for the exported-telemetry content guard."""

# Standard library
import importlib
import logging
import sys
from unittest.mock import MagicMock

# Third-party packages
import pytest

# Local modules
from _guard_state import (
    memory_client_filter_count,
    remove_memory_client_filter,
    restore_guard_state,
    snapshot_guard_state,
)
from config.telemetry_guard import (
    CAPTURE_OPT_IN_VALUE,
    CAPTURE_TRACE_CONTENT_VAR,
    DISABLED_INSTRUMENTATIONS_VAR,
    GENAI_CAPTURE_CONTENT_VAR,
    HOSTED_RUNTIME_VAR,
    MCP_INSTRUMENTOR_ENTRY_POINT,
    MEMORY_CLIENT_LOGGER_NAME,
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


# --- AgentCore Memory SDK client logger -----------------------------------------
#
# ``MemoryClient.retrieve_memories`` logs the long-term-memory namespace, which
# embeds the memory actor ID, at INFO on every successful lookup. Under agent
# observability the distro turns root-logger records into exported OpenTelemetry
# log records, so the line would leave the process. While redacting in a hosted
# runtime the guard filters that one logger to WARNING and above. A non-hosted run
# and the content-capture opt-in do not install the filter.

_MEMORY_NAMESPACE = "/actors/synthetic-actor-0001/"


@pytest.fixture
def _memory_client_logger(_clean_mcp_log_filters):
    """The SDK client logger with its own level unset and no guard filter.

    The guard changes this logger process-wide, so each test starts clean and
    leaves it clean. Depending on ``_clean_mcp_log_filters`` also strips the MCP
    logging filter that every guard call installs.
    """
    target = logging.getLogger(MEMORY_CLIENT_LOGGER_NAME)
    previous_level = target.level
    target.setLevel(logging.NOTSET)
    remove_memory_client_filter()
    yield target
    remove_memory_client_filter()
    target.setLevel(previous_level)


def _memory_client_info_is_emitted(caplog) -> bool:
    """Log one INFO record on the SDK client logger and report whether it was handled."""
    caplog.clear()
    with caplog.at_level(logging.INFO):
        logging.getLogger(MEMORY_CLIENT_LOGGER_NAME).info("synthetic info")
    return any(r.name == MEMORY_CLIENT_LOGGER_NAME and r.levelno == logging.INFO for r in caplog.records)


def _exported_memory_log_text(service_error_code=None) -> str:
    """Run a real ``MemoryClient.retrieve_memories`` and return what would be exported.

    A root-logger OpenTelemetry handler stands in for the distro's, which is what
    turns a library log call into an exported log record. The data-plane client is
    a stub, so nothing touches the network; with ``service_error_code`` it raises a
    service error with that code.
    """
    # Third-party packages
    from bedrock_agentcore.memory.client import MemoryClient
    from botocore.exceptions import ClientError
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor

    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)

    control_plane, data_plane = MagicMock(), MagicMock()
    if service_error_code is None:
        data_plane.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
    else:
        data_plane.retrieve_memory_records.side_effect = ClientError(
            {"Error": {"Code": service_error_code, "Message": "synthetic service message"}}, "RetrieveMemoryRecords"
        )
    session = MagicMock()
    session.client.side_effect = [control_plane, data_plane]
    client = MemoryClient(region_name="us-west-2", boto3_session=session)

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        client.retrieve_memories(memory_id="memory-0001", namespace=_MEMORY_NAMESPACE, query="synthetic query")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
    provider.force_flush()
    return "\n".join(
        f"{record.log_record.body} {dict(record.log_record.attributes or {})}"
        for record in exporter.get_finished_logs()
    )


def test_memory_client_logger_name_is_pinned_to_the_sdk_client():
    # If the SDK renames or moves its client module, the guard would filter a
    # logger nothing writes to and the namespace line would leak again; fail here.
    # Third-party packages
    import bedrock_agentcore.memory.client as memory_client

    assert memory_client.logger.name == MEMORY_CLIENT_LOGGER_NAME


def test_hosted_guard_keeps_the_memory_namespace_out_of_exported_logs(_memory_client_logger):
    pytest.importorskip("opentelemetry.sdk._logs")

    # Control: with the guard not applied, the SDK's INFO line carries the
    # namespace into exported records, so the assertion below cannot pass vacuously.
    assert _MEMORY_NAMESPACE in _exported_memory_log_text()

    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    assert _MEMORY_NAMESPACE not in _exported_memory_log_text()


def test_hosted_guard_passes_memory_client_failure_warnings(_memory_client_logger):
    # The filter passes WARNING and above so SDK failures stay visible. On a
    # resource-not-found error the client's WARNING includes the resolved
    # namespace, which SECURITY.md lists as a residual risk. This pins that
    # boundary: if it moves, the documented residual risk has to move with it.
    pytest.importorskip("opentelemetry.sdk._logs")

    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    exported = _exported_memory_log_text(service_error_code="ResourceNotFoundException")
    assert "Memory or namespace not found" in exported
    assert _MEMORY_NAMESPACE in exported


def test_hosted_guard_passes_memory_client_event_failure_errors(_memory_client_logger):
    # When creating or listing events fails, the client logs the service's error
    # text at ERROR, which the filter passes. SECURITY.md lists this with the
    # other failure paths; if the boundary moves, the documentation has to move.
    pytest.importorskip("opentelemetry.sdk._logs")
    # Third-party packages
    from bedrock_agentcore.memory.client import MemoryClient
    from botocore.exceptions import ClientError
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor

    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    data_plane = MagicMock()
    error = ClientError({"Error": {"Code": "ValidationException", "Message": "synthetic service message"}}, "x")
    data_plane.create_event.side_effect = error
    data_plane.list_events.side_effect = error
    session = MagicMock()
    session.client.side_effect = [MagicMock(), data_plane]
    client = MemoryClient(region_name="us-west-2", boto3_session=session)

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        with pytest.raises(ClientError):
            client.create_event(memory_id="memory-0001", actor_id="a", session_id="s", messages=[("hi", "USER")])
        with pytest.raises(ClientError):
            client.list_events(memory_id="memory-0001", actor_id="a", session_id="s")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
    provider.force_flush()

    exported = "\n".join(str(record.log_record.body) for record in exporter.get_finished_logs())
    assert "Failed to create event" in exported
    assert "Failed to list events" in exported


@pytest.mark.parametrize(
    ("level", "emitted"),
    [
        (logging.DEBUG, False),
        (logging.INFO, False),
        (logging.WARNING, True),
        (logging.ERROR, True),
        (logging.CRITICAL, True),
    ],
)
def test_hosted_guard_drops_exactly_the_records_below_warning(level, emitted, caplog, _memory_client_logger):
    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    with caplog.at_level(logging.DEBUG):
        _memory_client_logger.log(level, "synthetic record")

    seen = [record for record in caplog.records if record.name == MEMORY_CLIENT_LOGGER_NAME]
    assert bool(seen) is emitted


def test_hosted_guard_holds_when_the_root_level_is_raised_after_it_runs(caplog, _memory_client_logger):
    # The runtime configures logging on its own schedule. A filter on the logger
    # must keep dropping INFO whatever the root level is at guard time, which a
    # level set on the logger from the root's value at that moment would not.
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.WARNING)
    try:
        apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})
    finally:
        root.setLevel(previous_level)

    assert _memory_client_info_is_emitted(caplog) is False


def test_hosted_guard_applies_through_the_process_environment(monkeypatch, caplog, _memory_client_logger):
    # The entrypoint calls the guard with no argument, so it reads os.environ.
    _set_guard_env(monkeypatch, opt_in=False)
    monkeypatch.setenv(HOSTED_RUNTIME_VAR, "true")

    apply_trace_content_guard()

    assert _memory_client_info_is_emitted(caplog) is False


def test_hosted_guard_leaves_sibling_sdk_loggers_alone(caplog, _memory_client_logger):
    # The filter belongs to the client logger alone. The Strands session manager's
    # logger carries the SDK's operational lines ("Created session", "Flushed N
    # message events"), and it must keep emitting them.
    # Third-party packages
    from bedrock_agentcore.memory.integrations.strands import session_manager

    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    with caplog.at_level(logging.INFO):
        session_manager.logger.info("synthetic operational line")

    emitted = [record.getMessage() for record in caplog.records if record.name == session_manager.logger.name]
    assert emitted == ["synthetic operational line"]


_HOSTED_FLAG_VALUES = ["true", "TRUE", "True", " true", "true ", "1", "yes", "false", ""]


def test_hosted_flag_parsing_matches_the_runtime_settings():
    # The guard and config.settings must agree on what "hosted" means. Ask
    # config.settings itself, in a clean process so reloading it cannot disturb
    # this one, and compare with the guard's parser over the same values.
    # Standard library
    import json
    import os
    import subprocess

    # Local modules
    from config.telemetry_guard import _hosted_runtime

    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src")
    # config.settings loads ui/.env.local when it exists; stub that out so a
    # developer's deployment file cannot influence or be read by this test.
    script = (
        "import importlib, json, os, sys\n"
        "import dotenv\n"
        "dotenv.load_dotenv = lambda *args, **kwargs: False\n"
        "import config.settings as settings\n"
        "verdicts = {}\n"
        "for value in json.loads(sys.argv[1]):\n"
        "    os.environ['GBAW_HOSTED_RUNTIME'] = value\n"
        "    importlib.reload(settings)\n"
        "    verdicts[value] = settings.HOSTED_RUNTIME\n"
        "print('VERDICTS ' + json.dumps(verdicts))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != HOSTED_RUNTIME_VAR}
    env["PYTHONPATH"] = src_dir
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(_HOSTED_FLAG_VALUES)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    marker_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("VERDICTS ")]
    assert (
        result.returncode == 0 and len(marker_lines) == 1
    ), f"rc={result.returncode} stdout tail={result.stdout[-300:]!r} stderr tail={result.stderr[-300:]!r}"
    verdicts = json.loads(marker_lines[0][len("VERDICTS ") :])

    for value in _HOSTED_FLAG_VALUES:
        assert _hosted_runtime({HOSTED_RUNTIME_VAR: value}) is verdicts[value], repr(value)
    assert _hosted_runtime({}) is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True"])
def test_hosted_flag_spellings_install_the_filter(value, caplog, _memory_client_logger):
    apply_trace_content_guard({HOSTED_RUNTIME_VAR: value})

    assert _memory_client_info_is_emitted(caplog) is False


@pytest.mark.parametrize(
    "env", [{}, {HOSTED_RUNTIME_VAR: "false"}, {HOSTED_RUNTIME_VAR: ""}, {HOSTED_RUNTIME_VAR: "1"}]
)
def test_non_hosted_guard_leaves_the_memory_client_logger_alone(env, caplog, _memory_client_logger):
    apply_trace_content_guard(dict(env))

    assert _memory_client_info_is_emitted(caplog) is True


def test_content_capture_opt_in_leaves_the_memory_client_logger_alone(caplog, _memory_client_logger):
    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true", CAPTURE_TRACE_CONTENT_VAR: CAPTURE_OPT_IN_VALUE})

    assert _memory_client_info_is_emitted(caplog) is True


def test_memory_client_filter_is_installed_once(_memory_client_logger):
    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})
    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    assert memory_client_filter_count() == 1


def test_memory_client_filter_does_not_change_the_logger_level(_memory_client_logger):
    # A filter only removes records; it must not move the level an operator chose.
    _memory_client_logger.setLevel(logging.ERROR)

    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    assert _memory_client_logger.level == logging.ERROR
    assert _memory_client_logger.isEnabledFor(logging.WARNING) is False


def test_guard_state_helpers_restore_the_memory_client_filter(_memory_client_logger):
    # Importing the entrypoint runs the guard against the real environment. The
    # shared isolation helpers must undo every process-wide install, including
    # this filter, or it would leak into the rest of the test session.
    state = snapshot_guard_state()
    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})
    assert memory_client_filter_count() == 1

    restore_guard_state(state)

    assert memory_client_filter_count() == 0


# These two tests run in file order and prove the repository-wide fixture in
# tests/conftest.py removes a filter a test leaves behind. The first one
# deliberately uses no cleanup fixture of its own.
def test_leftover_memory_client_filter_setup_installs_it_without_cleanup():
    apply_trace_content_guard({HOSTED_RUNTIME_VAR: "true"})

    assert memory_client_filter_count() == 1


def test_leftover_memory_client_filter_is_removed_before_the_next_test():
    assert memory_client_filter_count() == 0


# --- Fail-visible logging when a redaction guard path is skipped ----------------
#
# Each guard path (the Strands span wrappers, the MCP uninstrument, the MCP and
# memory logging filters) catches a broad exception so a missing or renamed
# internal, or attribute drift on a wrapped method, cannot crash startup. A
# skipped path leaves a content export open, so it must be logged at WARNING, and
# the log must carry no exception message or stack trace, which can carry content.

_GUARD_SKIP_EXC_MARKER = "UNIQUE-GUARD-SKIP-EXC-MARKER-9f31"


def _warning_records(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "config.telemetry_guard"]


def test_strands_wrapper_import_failure_logs_warning_without_exception_text(monkeypatch, caplog):
    # Force the Strands tracer import inside the install path to fail with a
    # message carrying a unique marker.
    # Standard library
    import types

    # Local modules
    from config.telemetry_guard import _install_strands_span_wrappers

    broken = types.ModuleType("strands.telemetry.tracer")

    def _raise_on_access(name):  # module-level __getattr__: any attribute access fails
        raise ImportError(_GUARD_SKIP_EXC_MARKER)

    broken.__getattr__ = _raise_on_access  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strands.telemetry.tracer", broken)

    with caplog.at_level(logging.WARNING, logger="config.telemetry_guard"):
        _install_strands_span_wrappers()

    warnings = _warning_records(caplog)
    assert warnings, "a skipped guard path must log at WARNING"
    text = "\n".join(r.getMessage() for r in warnings)
    # The open path is named and the exception type is kept, but no exception
    # message or stack trace (which can carry content) is in the record.
    assert "strands span wrappers" in text
    assert "ImportError" in text
    assert _GUARD_SKIP_EXC_MARKER not in text
    assert all(r.exc_info is None for r in warnings)
    assert all(r.exc_text is None for r in warnings)


def test_strands_wrapper_attribute_drift_logs_warning_without_exception_text(monkeypatch, caplog):
    # Attribute drift on the wrapped Tracer method must be caught in the same
    # path and surfaced at WARNING, not crash startup or pass silently.
    # Standard library
    import types

    # Third-party packages
    from strands.telemetry.tracer import Tracer

    # Local modules
    from config.telemetry_guard import _install_strands_span_wrappers

    fake_module = types.ModuleType("strands.telemetry.tracer")

    class _DriftedTracer:
        # The real method name is gone; accessing it raises AttributeError whose
        # message carries the marker.
        def __getattr__(self, name):
            raise AttributeError(f"{_GUARD_SKIP_EXC_MARKER}:{name}")

    fake_module.Tracer = _DriftedTracer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strands.telemetry.tracer", fake_module)

    original_start = Tracer.start_agent_span
    original_end = Tracer._end_span
    try:
        with caplog.at_level(logging.WARNING, logger="config.telemetry_guard"):
            _install_strands_span_wrappers()
    finally:
        Tracer.start_agent_span = original_start
        Tracer._end_span = original_end

    warnings = _warning_records(caplog)
    assert warnings, "attribute drift must log at WARNING"
    text = "\n".join(r.getMessage() for r in warnings)
    assert "strands span wrappers" in text
    assert _GUARD_SKIP_EXC_MARKER not in text


def test_uninstrument_mcp_import_failure_logs_warning_without_exception_text(monkeypatch, caplog):
    # Standard library
    import types

    # Local modules
    from config.telemetry_guard import _uninstrument_mcp

    broken = types.ModuleType("amazon.opentelemetry.distro.instrumentation.mcp")

    def _raise_on_access(name):
        raise ImportError(_GUARD_SKIP_EXC_MARKER)

    broken.__getattr__ = _raise_on_access  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "amazon.opentelemetry.distro.instrumentation.mcp", broken)

    with caplog.at_level(logging.WARNING, logger="config.telemetry_guard"):
        _uninstrument_mcp()

    warnings = _warning_records(caplog)
    assert warnings, "a skipped uninstrument path must log at WARNING"
    text = "\n".join(r.getMessage() for r in warnings)
    assert "MCP instrumentor" in text
    assert _GUARD_SKIP_EXC_MARKER not in text


def test_memory_client_filter_failure_logs_warning_without_exception_text(monkeypatch, caplog):
    # Make addFilter on the memory client logger raise with a marker message, and
    # confirm the skip is a content-free WARNING rather than a crash.
    # Local modules
    import config.telemetry_guard as guard

    real_get_logger = logging.getLogger

    def _fake_get_logger(name=None):
        lg = real_get_logger(name)
        if name == guard.MEMORY_CLIENT_LOGGER_NAME:

            def _raise(_flt):
                raise RuntimeError(_GUARD_SKIP_EXC_MARKER)

            monkeypatch.setattr(lg, "addFilter", _raise, raising=False)
            monkeypatch.setattr(lg, "filters", [], raising=False)
        return lg

    monkeypatch.setattr(guard.logging, "getLogger", _fake_get_logger)

    with caplog.at_level(logging.WARNING, logger="config.telemetry_guard"):
        guard._install_memory_client_filter()

    warnings = _warning_records(caplog)
    assert warnings, "a skipped memory-filter path must log at WARNING"
    text = "\n".join(r.getMessage() for r in warnings)
    assert "memory client logging filter" in text
    assert _GUARD_SKIP_EXC_MARKER not in text
