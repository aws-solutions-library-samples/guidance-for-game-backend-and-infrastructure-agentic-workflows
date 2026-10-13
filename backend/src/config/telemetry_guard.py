"""Startup guard that keeps model content out of exported telemetry.

The runtime runs under ``opentelemetry-instrument`` with the AWS OpenTelemetry
distro and AgentCore observability. Three independent paths decide whether
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
* The AWS OpenTelemetry distro's MCP instrumentor (entry point ``aws_mcp``)
  writes ``gen_ai.tool.call.arguments`` and ``gen_ai.tool.call.result`` -- the
  full MCP tool inputs and outputs -- onto MCP protocol spans unconditionally;
  no capture switch turns them off. The guard closes this path in two places:
  it uninstruments the distro's ``McpInstrumentor`` singleton in the runtime
  process, and, when the distro has set ``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS``
  (its default exclusion list under agent observability), it appends ``aws_mcp``
  to that list so the auto-instrumented MCP server subprocesses -- which inherit
  a copy of this process's environment -- exclude MCP instrumentation too. The
  distro leaves that variable unset only when it is not running with agent
  observability, in which case the children are not auto-instrumented with the
  default list either, so leaving it unset is safe. Strands' own
  ``execute_tool`` spans keep tool name, call id, status, and timing while
  redacting tool inputs/outputs under the redaction token, so dropping the MCP
  protocol spans loses no non-content signal.

The hosted default is to carry no content: capture is off, the Strands
redaction token is present, and MCP tool content spans are suppressed. Latency,
token-count, model id, finish reason, error type, and span structure are
unaffected because they are separate, non-sensitive attributes.

``apply_trace_content_guard`` is called once at module import of the entrypoint,
before any Strands ``Tracer`` is constructed and before any MCP client is
created, so the default does not depend on the deploy environment passing the
switches. An operator can opt into content capture only by setting
``GBAW_CAPTURE_TRACE_CONTENT`` to the exact opt-in token; this is for
non-production debugging and fails closed on any other value. The opt-in leaves
MCP instrumentation and the disabled-instrumentations list untouched.
"""

# Standard library
import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# The Strands redaction token name and the botocore content-capture switch.
SEMCONV_OPT_IN_VAR = "OTEL_SEMCONV_STABILITY_OPT_IN"
GENAI_CAPTURE_CONTENT_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

# Strands enables redaction when this token is present; an empty attribute list
# ("...=") redacts every sensitive attribute and legacy per-message event.
UNREDACTED_ATTRIBUTES_PREFIX = "gen_ai_unredacted_attributes="
REDACT_ALL_TOKEN = UNREDACTED_ATTRIBUTES_PREFIX  # equals "gen_ai_unredacted_attributes="

# OpenTelemetry auto-instrumentation exclusion list and the ADOT MCP instrumentor
# entry-point name. The distro sets the list with its own default under agent
# observability; the guard appends this entry so MCP tool content spans (which
# carry gen_ai.tool.call.arguments / gen_ai.tool.call.result) are not produced in
# subprocesses, which inherit a copy of this process's environment.
DISABLED_INSTRUMENTATIONS_VAR = "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"
MCP_INSTRUMENTOR_ENTRY_POINT = "aws_mcp"

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
    # Value for OTEL_PYTHON_DISABLED_INSTRUMENTATIONS, or None to leave it unset.
    disabled_instrumentations: Optional[str] = None


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


def _append_disabled_instrumentation(existing: str) -> str:
    """Append the MCP instrumentor entry point to the disabled list, once.

    Preserves the existing entries and their order and never duplicates
    ``aws_mcp``. The caller only invokes this when the variable is already set,
    so an unset variable stays unset.
    """
    tokens = [token.strip() for token in existing.split(",") if token.strip()]
    if MCP_INSTRUMENTOR_ENTRY_POINT not in tokens:
        tokens.append(MCP_INSTRUMENTOR_ENTRY_POINT)
    return ",".join(tokens)


def resolve_trace_content_env(env: Mapping[str, str]) -> TraceContentDecision:
    """Decide the exported-telemetry content switches from the environment.

    The default (no exact opt-in) redacts all sensitive span attributes, disables
    GenAI message-content capture, and -- when the auto-instrumentation disabled
    list is already set -- appends the MCP instrumentor so MCP tool arguments and
    results stay out of exported spans in subprocesses. The opt-in is honored only
    for the exact token value, so a hosted runtime without the opt-in never
    captures content regardless of other values, and the opt-in leaves the
    disabled list untouched.
    """
    capture_enabled = env.get(CAPTURE_TRACE_CONTENT_VAR, "") == CAPTURE_OPT_IN_VALUE
    existing_opt_in = env.get(SEMCONV_OPT_IN_VAR, "")
    existing_disabled = env.get(DISABLED_INSTRUMENTATIONS_VAR)
    if capture_enabled:
        return TraceContentDecision(
            capture_enabled=True,
            capture_message_content="true",
            semconv_opt_in=_merge_semconv_opt_in(existing_opt_in, redact=False),
            disabled_instrumentations=existing_disabled,
        )
    disabled = None if existing_disabled is None else _append_disabled_instrumentation(existing_disabled)
    return TraceContentDecision(
        capture_enabled=False,
        capture_message_content="false",
        semconv_opt_in=_merge_semconv_opt_in(existing_opt_in, redact=True),
        disabled_instrumentations=disabled,
    )


def _uninstrument_mcp() -> None:
    """Uninstrument the ADOT MCP instrumentor singleton if it is present.

    The MCP instrumentor is a ``BaseInstrumentor`` singleton whose
    ``uninstrument`` restores the wrapped MCP SDK methods, so no exported span
    carries ``gen_ai.tool.call.arguments`` or ``gen_ai.tool.call.result`` in this
    process. A missing or changed ADOT package is a debug-logged no-op, never a
    startup failure.
    """
    try:
        # Third-party packages
        from amazon.opentelemetry.distro.instrumentation.mcp import McpInstrumentor
    except Exception as exc:  # pragma: no cover - defensive import guard
        logger.debug("ADOT MCP instrumentor not available; skipping uninstrument: %s", exc)
        return
    try:
        McpInstrumentor().uninstrument()
    except Exception as exc:  # pragma: no cover - defensive, never fail startup
        logger.debug("MCP instrumentor uninstrument skipped: %s", exc)


def apply_trace_content_guard(env: "os._Environ[str] | dict[str, str] | None" = None) -> TraceContentDecision:
    """Apply the resolved content switches to the environment in place.

    Must run before any Strands ``Tracer`` is constructed and before any MCP
    client is created. The botocore Bedrock instrumentation reads the capture
    switch at call time, so applying it here also governs GenAI events and the
    OpenTelemetry log records derived from them. When redacting, the MCP
    instrumentor singleton is uninstrumented in this process and ``aws_mcp`` is
    appended to an already-set disabled-instrumentations list so subprocesses
    inherit the exclusion. Returns the decision for logging and tests.
    """
    target = os.environ if env is None else env
    decision = resolve_trace_content_env(target)
    target[GENAI_CAPTURE_CONTENT_VAR] = decision.capture_message_content
    target[SEMCONV_OPT_IN_VAR] = decision.semconv_opt_in
    if decision.disabled_instrumentations is not None:
        target[DISABLED_INSTRUMENTATIONS_VAR] = decision.disabled_instrumentations
    if not decision.capture_enabled:
        _uninstrument_mcp()
    return decision
