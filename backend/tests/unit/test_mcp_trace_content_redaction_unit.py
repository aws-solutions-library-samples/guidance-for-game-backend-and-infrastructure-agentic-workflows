"""Behavioral test: MCP tool arguments and results stay out of exported spans.

ADOT's MCP instrumentor writes ``gen_ai.tool.call.arguments`` and
``gen_ai.tool.call.result`` onto MCP protocol spans with no capture switch to
turn them off. This test drives the real ADOT ``ClientWrapper`` behind an
in-memory span exporter, confirms the tool result would otherwise be exported,
and asserts that with the startup guard applied the MCP instrumentation produces
no such spans. Token/name/timing data remain available on the Strands
``execute_tool`` span, which the redaction token already covers for content.
"""

# Standard library
import asyncio
import json

# Third-party packages
import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Local modules
from config.telemetry_guard import CAPTURE_TRACE_CONTENT_VAR, apply_trace_content_guard

pytestmark = pytest.mark.unit

TOOL_ARG_MARKER = "UNIQUE-TOOL-ARG-MARKER-4a1c"
TOOL_RESULT_MARKER = "UNIQUE-TOOL-RESULT-MARKER-8b6d"


def _spans_text(exporter: InMemorySpanExporter) -> str:
    chunks = []
    for span in exporter.get_finished_spans():
        chunks.append(span.name)
        chunks.append(json.dumps(dict(span.attributes or {}), default=str))
    return "\n".join(chunks)


def _make_call_tool_request():
    # Third-party packages
    from mcp import types

    return types.ClientRequest(
        types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(
                name="list_k8s_resources",
                arguments={"namespace": TOOL_ARG_MARKER},
            ),
        )
    )


def _make_tool_result():
    # Third-party packages
    from mcp import types

    return types.CallToolResult(content=[types.TextContent(type="text", text=TOOL_RESULT_MARKER)])


def _drive_mcp_client_span(exporter: InMemorySpanExporter) -> None:
    """Drive the ADOT ClientWrapper's wrapped send against an in-memory exporter."""
    # Third-party packages
    from amazon.opentelemetry.distro.instrumentation.mcp._wrappers import ClientWrapper

    tracer = trace_api.get_tracer_provider().get_tracer("test-mcp")
    wrapper = ClientWrapper(tracer=tracer, propagators=None)

    message = _make_call_tool_request()
    result = _make_tool_result()

    class _Instance:
        _request_id = 1

    async def _wrapped(*_args, **_kwargs):
        return result

    coro = wrapper.wrap_session_send(_wrapped, _Instance(), (message,), {})
    asyncio.run(coro)
    trace_api.get_tracer_provider().force_flush()


@pytest.fixture
def _provider(monkeypatch):
    # ``mcp`` and ``aws-opentelemetry-distro`` are direct dependencies, so the
    # ADOT MCP wrapper must import. Importing it directly (no importorskip) means
    # ADOT MCP drift fails this test rather than skipping it, which is how CI
    # catches a moved or renamed ADOT MCP module before a deploy.
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    return exporter


def test_mcp_tool_result_is_exported_without_guard(_provider):
    # Baseline: the ADOT wrapper does emit the tool arguments and result. This
    # pins the behavior the guard must remove and keeps the mutation honest.
    _drive_mcp_client_span(_provider)
    blob = _spans_text(_provider)
    assert TOOL_RESULT_MARKER in blob
    assert TOOL_ARG_MARKER in blob


def _reset_mcp_instrumentor():
    """Fully unwrap the MCP SDK methods so the singleton starts single-layered."""
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


def test_guard_uninstruments_mcp_so_no_tool_content_span(monkeypatch, _provider):
    instrumentor = _reset_mcp_instrumentor()
    instrumentor.instrument()
    try:
        # Register the switches so the guard's os.environ writes are restored.
        monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "")
        monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "")
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
        monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
        apply_trace_content_guard()

        # After the guard the MCP SDK send methods are unwrapped, so a real MCP
        # call would not create these spans. Confirm the restoration directly.
        # Third-party packages
        from mcp.shared.session import BaseSession

        assert not hasattr(BaseSession.send_request, "__wrapped__")
        assert not hasattr(BaseSession.send_notification, "__wrapped__")
    finally:
        _reset_mcp_instrumentor()
