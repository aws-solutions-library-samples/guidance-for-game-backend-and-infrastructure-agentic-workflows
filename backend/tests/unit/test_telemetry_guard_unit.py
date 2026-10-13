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
    monkeypatch.setenv(GENAI_CAPTURE_CONTENT_VAR, "true")
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)

    decision = apply_trace_content_guard()

    # Standard library
    import os as _os

    assert _os.environ[GENAI_CAPTURE_CONTENT_VAR] == "false"
    assert _os.environ[SEMCONV_OPT_IN_VAR] == "gen_ai_unredacted_attributes="
    assert decision.capture_enabled is False


def test_apply_respects_opt_in(monkeypatch):
    monkeypatch.setenv(CAPTURE_TRACE_CONTENT_VAR, CAPTURE_OPT_IN_VALUE)
    monkeypatch.delenv(SEMCONV_OPT_IN_VAR, raising=False)

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
    monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
    monkeypatch.setenv(DISABLED_INSTRUMENTATIONS_VAR, "urllib3,requests")

    apply_trace_content_guard()
    apply_trace_content_guard()

    # Standard library
    import os as _os

    tokens = _os.environ[DISABLED_INSTRUMENTATIONS_VAR].split(",")
    assert tokens == ["urllib3", "requests", MCP_INSTRUMENTOR_ENTRY_POINT]


def test_apply_leaves_disabled_list_unset_when_unset(monkeypatch):
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

        monkeypatch.delenv(CAPTURE_TRACE_CONTENT_VAR, raising=False)
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
        monkeypatch.setenv(CAPTURE_TRACE_CONTENT_VAR, CAPTURE_OPT_IN_VALUE)
        apply_trace_content_guard()

        # The opt-in must leave MCP instrumentation in place.
        assert hasattr(BaseSession.send_request, "__wrapped__")
    finally:
        _reset_mcp_instrumentor()
