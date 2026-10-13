"""Startup guard that keeps model content out of exported telemetry.

The runtime runs under ``opentelemetry-instrument`` with the AWS OpenTelemetry
distro and AgentCore observability. Two independent switches decide whether
prompt text, response text, tool inputs/outputs, system prompts, and retrieved
passages reach exported spans, span events, and OpenTelemetry log records:

* ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` -- the botocore Bedrock
  instrumentation reads this at call time to decide whether message content is
  attached to GenAI events (which the distro's log-record handler also harvests
  into OpenTelemetry log records). When AgentCore observability is enabled the
  distro turns this on with ``setdefault``, so an explicit value already in the
  environment wins.
* ``OTEL_SEMCONV_STABILITY_OPT_IN`` -- the Strands tracer reads the
  ``gen_ai_unredacted_attributes=<list>`` token when it is constructed. An empty
  list redacts every sensitive span attribute and legacy per-message event
  (``gen_ai.input.messages``, ``gen_ai.output.messages``,
  ``gen_ai.system_instructions``). Omitting the token leaves them unredacted.

The hosted default is to carry no content: capture is off and the Strands
redaction token is present. Latency, token-count, model id, finish reason,
error type, and span structure are unaffected because they are separate,
non-sensitive attributes.

``apply_trace_content_guard`` is called once at module import of the entrypoint,
before any Strands ``Tracer`` is constructed, so the default does not depend on
the deploy environment passing the switches. An operator can opt into content
capture only by setting ``GBAW_CAPTURE_TRACE_CONTENT`` to the exact opt-in
token; this is for non-production debugging and fails closed on any other value.
"""

# Standard library
import os
from dataclasses import dataclass
from typing import Mapping

# The Strands redaction token name and the botocore content-capture switch.
SEMCONV_OPT_IN_VAR = "OTEL_SEMCONV_STABILITY_OPT_IN"
GENAI_CAPTURE_CONTENT_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

# Strands enables redaction when this token is present; an empty attribute list
# ("...=") redacts every sensitive attribute and legacy per-message event.
UNREDACTED_ATTRIBUTES_PREFIX = "gen_ai_unredacted_attributes="
REDACT_ALL_TOKEN = UNREDACTED_ATTRIBUTES_PREFIX  # equals "gen_ai_unredacted_attributes="

# Operator opt-in. Content capture is enabled only when the opt-in variable
# holds exactly this value; any other value (including "true", "1", or empty)
# fails closed to the redacting default.
CAPTURE_TRACE_CONTENT_VAR = "GBAW_CAPTURE_TRACE_CONTENT"
CAPTURE_OPT_IN_VALUE = "capture-content-nonprod"


@dataclass(frozen=True)
class TraceContentDecision:
    """Resolved telemetry-content switches for the current environment."""

    capture_enabled: bool
    capture_message_content: str  # value for OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT
    semconv_opt_in: str  # value for OTEL_SEMCONV_STABILITY_OPT_IN


def _merge_semconv_opt_in(existing: str, *, redact: bool) -> str:
    """Return the semantic-convention opt-in value, preserving unrelated tokens.

    When ``redact`` is True, any existing ``gen_ai_unredacted_attributes=`` token
    is replaced with the empty-list form (redact everything sensitive) and other
    tokens are kept in their original order. When ``redact`` is False, any
    existing redaction token is removed so the Strands tracer emits content
    unredacted for the opt-in operator.
    """
    tokens = [token.strip() for token in existing.split(",") if token.strip()]
    kept = [token for token in tokens if not token.startswith(UNREDACTED_ATTRIBUTES_PREFIX)]
    if redact:
        kept.append(REDACT_ALL_TOKEN)
    return ",".join(kept)


def resolve_trace_content_env(env: Mapping[str, str]) -> TraceContentDecision:
    """Decide the exported-telemetry content switches from the environment.

    The default (no exact opt-in) redacts all sensitive span attributes and
    disables GenAI message-content capture. The opt-in is honored only for the
    exact token value, so a hosted runtime without the opt-in never captures
    content regardless of other values.
    """
    capture_enabled = env.get(CAPTURE_TRACE_CONTENT_VAR, "") == CAPTURE_OPT_IN_VALUE
    existing_opt_in = env.get(SEMCONV_OPT_IN_VAR, "")
    if capture_enabled:
        return TraceContentDecision(
            capture_enabled=True,
            capture_message_content="true",
            semconv_opt_in=_merge_semconv_opt_in(existing_opt_in, redact=False),
        )
    return TraceContentDecision(
        capture_enabled=False,
        capture_message_content="false",
        semconv_opt_in=_merge_semconv_opt_in(existing_opt_in, redact=True),
    )


def apply_trace_content_guard(env: "os._Environ[str] | dict[str, str] | None" = None) -> TraceContentDecision:
    """Apply the resolved content switches to the environment in place.

    Must run before any Strands ``Tracer`` is constructed. The botocore Bedrock
    instrumentation reads the capture switch at call time, so applying it here
    also governs GenAI events and the OpenTelemetry log records derived from
    them. Returns the decision for logging and tests.
    """
    target = os.environ if env is None else env
    decision = resolve_trace_content_env(target)
    target[GENAI_CAPTURE_CONTENT_VAR] = decision.capture_message_content
    target[SEMCONV_OPT_IN_VAR] = decision.semconv_opt_in
    return decision
