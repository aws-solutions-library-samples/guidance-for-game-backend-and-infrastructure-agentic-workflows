"""Behavioral test: a real Strands Tracer redacts message content under the guard.

Builds a Strands ``Tracer`` behind an in-memory span exporter and drives a model
span through its public API. Under the hosted default (the guard applied before
the Tracer is constructed) no prompt or response text appears in exported span
attributes or events, while token-count attributes remain.
"""

# Standard library
import json

# Third-party packages
import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Local modules
from config.telemetry_guard import CAPTURE_OPT_IN_VALUE, CAPTURE_TRACE_CONTENT_VAR, apply_trace_content_guard

pytestmark = pytest.mark.unit

PROMPT_MARKER = "UNIQUE-PROMPT-MARKER-7f3a"
RESPONSE_MARKER = "UNIQUE-RESPONSE-MARKER-9c2b"
SYSTEM_MARKER = "UNIQUE-SYSTEM-MARKER-1d4e"


def _collect_text(exporter: InMemorySpanExporter) -> str:
    """Serialize all exported spans (attributes and events) to one text blob."""
    chunks = []
    for span in exporter.get_finished_spans():
        chunks.append(json.dumps(dict(span.attributes or {}), default=str))
        for event in span.events or []:
            chunks.append(event.name)
            chunks.append(json.dumps(dict(event.attributes or {}), default=str))
    return "\n".join(chunks)


def _drive_model_span(monkeypatch, *, opt_in: bool):
    """Apply the guard, build a Tracer behind an in-memory exporter, emit a span."""
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
    if opt_in:
        monkeypatch.setenv(CAPTURE_TRACE_CONTENT_VAR, CAPTURE_OPT_IN_VALUE)
    # Guard runs before any Tracer is constructed.
    apply_trace_content_guard()

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
