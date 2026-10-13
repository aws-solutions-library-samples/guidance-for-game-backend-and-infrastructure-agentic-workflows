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
    GENAI_CAPTURE_CONTENT_VAR,
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
