"""Behavioral test: a real Strands Tracer redacts message content under the guard.

Builds a Strands ``Tracer`` behind an in-memory span exporter and drives a model
span through its public API. Under the hosted default (the guard applied before
the Tracer is constructed) no prompt or response text appears in exported span
attributes or events, while token-count attributes remain.

A second group builds a real Strands ``Agent`` with a fake model that answers and
calls two failing tools with a marked argument, exports to an in-memory span
exporter, and runs the real AWS OpenTelemetry distro ``LLOHandler`` into an
in-memory log-record exporter. It asserts that no system prompt, prompt,
response, tool argument, tool result, or error text reaches span attributes,
status descriptions, events, or log-record bodies, while token-count attributes
remain and the tool spans keep an ERROR status.
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
from config.telemetry_guard import (
    CAPTURE_OPT_IN_VALUE,
    CAPTURE_TRACE_CONTENT_VAR,
    GENAI_CAPTURE_CONTENT_VAR,
    SEMCONV_OPT_IN_VAR,
    apply_trace_content_guard,
)

pytestmark = pytest.mark.unit

PROMPT_MARKER = "UNIQUE-PROMPT-MARKER-7f3a"
RESPONSE_MARKER = "UNIQUE-RESPONSE-MARKER-9c2b"
SYSTEM_MARKER = "UNIQUE-SYSTEM-MARKER-1d4e"
TOOL_RAISE_MARKER = "UNIQUE-TOOL-RAISE-MARKER-3e8f"
TOOL_ERRSTATUS_MARKER = "UNIQUE-TOOL-ERRSTATUS-MARKER-6a2d"
TOOL_ARG_MARKER = "UNIQUE-TOOL-ARG-MARKER-2b9c"

_ALL_MARKERS = (
    PROMPT_MARKER,
    RESPONSE_MARKER,
    SYSTEM_MARKER,
    TOOL_RAISE_MARKER,
    TOOL_ERRSTATUS_MARKER,
    TOOL_ARG_MARKER,
)


def _guard_env(monkeypatch, *, opt_in: bool) -> None:
    """Apply the guard with env hygiene so OTEL_* changes do not leak the session.

    The guard writes ``os.environ`` in place. Registering the two switches with
    monkeypatch first means its teardown restores (or deletes) them regardless of
    what the guard writes, so a later test never inherits the redaction token.
    """
    monkeypatch.setenv(SEMCONV_OPT_IN_VAR, "")
    monkeypatch.setenv(GENAI_CAPTURE_CONTENT_VAR, "")
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
    if opt_in:
        monkeypatch.setenv(CAPTURE_TRACE_CONTENT_VAR, CAPTURE_OPT_IN_VALUE)
    apply_trace_content_guard()


def _collect_text(exporter: InMemorySpanExporter) -> str:
    """Serialize all exported spans (attributes, status, events) to one blob."""
    chunks = []
    for span in exporter.get_finished_spans():
        chunks.append(json.dumps(dict(span.attributes or {}), default=str))
        if span.status is not None:
            chunks.append(str(span.status.description))
        for event in span.events or []:
            chunks.append(event.name)
            chunks.append(json.dumps(dict(event.attributes or {}), default=str))
    return "\n".join(chunks)


def _drive_model_span(monkeypatch, *, opt_in: bool):
    """Apply the guard, build a Tracer behind an in-memory exporter, emit a span."""
    _guard_env(monkeypatch, opt_in=opt_in)

    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Strands Tracer reads the global provider and the redaction token at construction.
    # Third-party packages
    from strands.telemetry.tracer import Tracer

    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    tracer = Tracer()

    messages = [{"role": "user", "content": [{"text": PROMPT_MARKER}]}]
    span = tracer.start_model_invoke_span(
        messages=messages,
        model_id="test-model",
        system_prompt=SYSTEM_MARKER,
    )
    tracer.end_model_invoke_span(
        span=span,
        message={"role": "assistant", "content": [{"text": RESPONSE_MARKER}]},
        usage={"inputTokens": 11, "outputTokens": 7, "totalTokens": 18},
        metrics={"latencyMs": 42},
        stop_reason="end_turn",
    )
    provider.force_flush()
    return exporter


def test_default_redacts_prompt_response_and_system_text(monkeypatch):
    exporter = _drive_model_span(monkeypatch, opt_in=False)
    blob = _collect_text(exporter)

    assert PROMPT_MARKER not in blob
    assert RESPONSE_MARKER not in blob
    assert SYSTEM_MARKER not in blob


def test_default_keeps_token_count_attributes(monkeypatch):
    exporter = _drive_model_span(monkeypatch, opt_in=False)
    blob = _collect_text(exporter)

    assert "gen_ai.usage.input_tokens" in blob
    assert "gen_ai.usage.output_tokens" in blob
    assert "gen_ai.usage.total_tokens" in blob
    # Model id and finish reason survive redaction.
    assert "test-model" in blob


def test_opt_in_restores_content(monkeypatch):
    exporter = _drive_model_span(monkeypatch, opt_in=True)
    blob = _collect_text(exporter)

    assert PROMPT_MARKER in blob
    assert RESPONSE_MARKER in blob


# --- Signature-pinning for the Strands Tracer methods the guard wraps ----------
# The guard wraps semi-private Strands signatures. If Strands changes them these
# tests fail loudly rather than letting content leak silently.


def test_strands_end_span_signature_is_pinned():
    # Standard library
    import inspect

    # Third-party packages
    from strands.telemetry.tracer import Tracer

    params = list(inspect.signature(Tracer._end_span).parameters)
    assert params == ["self", "span", "attributes", "error", "error_message"]


def test_strands_start_agent_span_accepts_system_prompt_kwarg():
    # Standard library
    import inspect

    # Third-party packages
    from strands.telemetry.tracer import Tracer

    # start_agent_span takes **kwargs, so a system_prompt keyword is accepted.
    # On Strands versions that copy raw string kwargs onto the agent span, that
    # keyword becomes a span attribute unless the guard drops it; on versions that
    # route the system prompt through the redaction policy instead, dropping the
    # keyword is a no-op. The guard drops it either way, so the pin only asserts
    # the keyword is still accepted (the behavioral tests cover redaction).
    params = inspect.signature(Tracer.start_agent_span).parameters
    assert any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def test_guard_wrappers_chain_to_strands_via_wrapped(monkeypatch):
    # The guard's wrappers are installed with functools.wraps(original), so the
    # signature pins above resolve through __wrapped__ to the real Strands method
    # rather than inspecting the guard's own wrapper. If a future change dropped
    # functools.wraps, inspect.signature would describe the wrapper and the pins
    # would be vacuous; this test fails in that case.
    # Standard library
    import inspect

    _guard_env(monkeypatch, opt_in=False)

    # Third-party packages
    from strands.telemetry.tracer import Tracer

    end_span = Tracer._end_span
    start_agent_span = Tracer.start_agent_span

    # The wrappers are installed (marker set) and expose the original via __wrapped__.
    assert getattr(end_span, "_gbaw_trace_content_guard", False) is True
    assert getattr(start_agent_span, "_gbaw_trace_content_guard", False) is True
    assert hasattr(end_span, "__wrapped__")
    assert hasattr(start_agent_span, "__wrapped__")

    # inspect.signature follows __wrapped__, so the pinned parameter list is the
    # real Strands _end_span signature, not the wrapper's own.
    params = list(inspect.signature(end_span).parameters)
    assert params == ["self", "span", "attributes", "error", "error_message"]
    assert list(inspect.signature(end_span.__wrapped__).parameters) == params


# --- Direct behavioral wrapper checks against a real Tracer --------------------


def _build_tracer(monkeypatch, *, opt_in: bool):
    _guard_env(monkeypatch, opt_in=opt_in)
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Third-party packages
    from strands.telemetry.tracer import Tracer

    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    return Tracer(), provider, exporter


def test_guard_drops_system_prompt_on_agent_span(monkeypatch):
    tracer, provider, exporter = _build_tracer(monkeypatch, opt_in=False)
    messages = [{"role": "user", "content": [{"text": PROMPT_MARKER}]}]
    span = tracer.start_agent_span(messages=messages, agent_name="orchestrator", system_prompt=SYSTEM_MARKER)
    tracer.end_agent_span(span=span)
    provider.force_flush()

    blob = _collect_text(exporter)
    assert SYSTEM_MARKER not in blob
    assert "system_prompt" not in blob


def test_guard_strips_tool_error_text_from_status_and_exception(monkeypatch):
    tracer, provider, exporter = _build_tracer(monkeypatch, opt_in=False)

    # Error-status tool result whose first text block carries a content marker.
    span1 = tracer.start_tool_call_span(tool={"name": "lookup", "toolUseId": "t1", "input": {}})
    tracer.end_tool_call_span(
        span=span1,
        tool_result={"toolUseId": "t1", "status": "error", "content": [{"text": TOOL_ERRSTATUS_MARKER}]},
    )

    # Raised exception whose message carries a content marker.
    span2 = tracer.start_tool_call_span(tool={"name": "run", "toolUseId": "t2", "input": {}})
    tracer.end_tool_call_span(
        span=span2,
        tool_result={"toolUseId": "t2", "status": "error", "content": [{"text": "boom"}]},
        error=RuntimeError(TOOL_RAISE_MARKER),
    )
    provider.force_flush()

    blob = _collect_text(exporter)
    assert TOOL_ERRSTATUS_MARKER not in blob
    assert TOOL_RAISE_MARKER not in blob
    # The error type survives as the status description for the raised exception.
    assert "RuntimeError" in blob

    # Both tool spans keep an ERROR status so error reporting is not lost.
    # Third-party packages
    from opentelemetry.trace import StatusCode

    statuses = {span.name: span.status.status_code for span in exporter.get_finished_spans()}
    assert statuses.get("execute_tool lookup") is StatusCode.ERROR
    assert statuses.get("execute_tool run") is StatusCode.ERROR


# --- Real Agent + the AWS distro LLO handler -----------------------------------


def _make_fake_model():
    """Build a Strands Model that answers, then calls two failing tools, then stops.

    Turn 1 emits two tool-use blocks (one for a tool that raises, one for a tool
    that returns an error status). Turn 2 emits a short text answer. This drives
    the agent span, tool spans, model spans, and their error paths. Subclassing
    the real ``Model`` inherits the non-streaming parts of the interface.
    """
    # Third-party packages
    from strands.models.model import Model

    class _FakeModel(Model):
        def __init__(self):
            self._turn = 0

        def get_config(self):  # pragma: no cover - trivial
            return {}

        def update_config(self, **kwargs):  # pragma: no cover - trivial
            pass

        async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # pragma: no cover
            yield {"output": None}

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            self._turn += 1
            if self._turn == 1:
                # Two tool calls in one assistant turn, each with a marked argument.
                yield {"messageStart": {"role": "assistant"}}
                for idx, name in enumerate(("raise_tool", "errstatus_tool")):
                    yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tu{idx}", "name": name}}}}
                    yield {
                        "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"value": TOOL_ARG_MARKER})}}}
                    }
                    yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                yield {
                    "metadata": {
                        "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
                        "metrics": {"latencyMs": 1},
                    }
                }
            else:
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": RESPONSE_MARKER}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                yield {
                    "metadata": {
                        "usage": {"inputTokens": 6, "outputTokens": 4, "totalTokens": 10},
                        "metrics": {"latencyMs": 1},
                    }
                }

    return _FakeModel()


def _reset_strands_tracer_singleton():
    # Third-party packages
    import strands.telemetry.tracer as tracer_mod

    tracer_mod._tracer_instance = None


def test_real_agent_and_llo_handler_leak_no_content(monkeypatch):
    # The ADOT LLO handler and strands are direct dependencies, imported directly
    # below so a missing or renamed module fails this test rather than skipping it.
    # Third-party packages
    from amazon.opentelemetry.distro.llo_handler import LLOHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
    from strands import Agent
    from strands.tools.decorator import tool

    _guard_env(monkeypatch, opt_in=False)

    span_exporter = InMemorySpanExporter()
    span_provider = SDKTracerProvider()
    span_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: span_provider)

    # The Agent reads the cached global tracer; reset it so it is rebuilt under
    # the redaction token and the patched provider.
    _reset_strands_tracer_singleton()
    monkeypatch.setattr("strands.agent.agent.get_tracer", _rebuild_tracer)

    @tool
    def raise_tool(value: str = "") -> str:
        raise RuntimeError(TOOL_RAISE_MARKER)

    @tool
    def errstatus_tool(value: str = "") -> dict:
        return {"status": "error", "content": [{"text": TOOL_ERRSTATUS_MARKER}]}

    agent = Agent(
        model=_make_fake_model(),
        tools=[raise_tool, errstatus_tool],
        system_prompt=SYSTEM_MARKER,
        callback_handler=None,
    )
    agent(PROMPT_MARKER)
    span_provider.force_flush()

    spans = span_exporter.get_finished_spans()
    assert spans, "expected spans to be exported"

    # Run the real distro LLO handler over the spans into an in-memory log exporter.
    log_exporter = InMemoryLogExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    LLOHandler(logger_provider).process_spans(spans)
    logger_provider.force_flush()

    span_blob = _collect_text(span_exporter)
    log_blob = "\n".join(
        json.dumps({"body": str(r.log_record.body), "attributes": dict(r.log_record.attributes or {})}, default=str)
        for r in log_exporter.get_finished_logs()
    )
    blob = span_blob + "\n" + log_blob

    for marker in _ALL_MARKERS:
        assert marker not in blob, f"content marker leaked: {marker}"

    # Token-count attributes still present on the spans.
    assert "gen_ai.usage.total_tokens" in span_blob

    # Both tool spans keep an ERROR status even though their content is redacted.
    # Third-party packages
    from opentelemetry.trace import StatusCode

    tool_statuses = [s.status.status_code for s in spans if s.name.startswith("execute_tool")]
    assert len(tool_statuses) == 2
    assert all(code is StatusCode.ERROR for code in tool_statuses)


def _rebuild_tracer():
    # Third-party packages
    from strands.telemetry.tracer import Tracer

    return Tracer()
