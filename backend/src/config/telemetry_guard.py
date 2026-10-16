"""Startup guard that keeps model content out of exported telemetry.

The runtime runs under ``opentelemetry-instrument`` with the AWS OpenTelemetry
distro and AgentCore observability. Several independent paths decide whether
prompt text, response text, tool inputs/outputs, system prompts, retrieved
passages, and error text reach exported spans, span events, span status, and
OpenTelemetry log records:

* ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` -- the botocore Bedrock
  instrumentation reads this at call time to decide whether message content is
  attached to GenAI events (which the distro's log-record handler also harvests
  into OpenTelemetry log records). When AgentCore observability is enabled the
  distro turns this on with ``setdefault``, so an explicit value already in the
  environment wins.
* ``OTEL_SEMCONV_STABILITY_OPT_IN`` -- the Strands tracer reads the
  ``gen_ai_unredacted_attributes=<list>`` token when it is constructed. An empty
  list redacts the sensitive span attributes and legacy per-message events it
  routes through its redaction policy (``gen_ai.input.messages``,
  ``gen_ai.output.messages``, ``gen_ai.system_instructions``). Omitting the token
  leaves them unredacted.
* The Strands tracer also carries two content paths the redaction token does not
  reach. ``start_agent_span`` copies a ``system_prompt`` keyword argument onto the
  ``invoke_agent`` span as a plain attribute, which the distro's log-record
  handler emits as a ``system`` message. ``_end_span`` records tool/model error
  text as the span status description and, for exceptions, as an ``exception``
  span event (message and stack trace). While redacting, the guard installs
  narrow wrappers on these two ``Tracer`` methods that drop the ``system_prompt``
  attribute and reduce an error to its type while keeping the error status.
* Under agent observability the distro installs an OpenTelemetry logging handler
  on the root logger, so a library ``logger.exception(...)`` becomes a log record
  whose attributes carry the exception type, message, and stack trace. The MCP
  client logs tool failures this way, and MCP tool exceptions can echo tool
  arguments or provider output. While redacting, the guard attaches a logging
  filter to the Strands MCP client logger that keeps the exception type as a
  plain ``exception.type`` record attribute but removes the message and stack
  trace, so the fact and class of the failure survive while the content does not.
  The MCP SDK's own validation and receive-loop errors are logged on the root
  logger with the exception interpolated into the message string; a named-logger
  filter cannot reach them, so they are documented as a residual risk rather than
  filtered here.
* The AWS OpenTelemetry distro's MCP instrumentor (entry point ``aws_mcp``)
  writes ``gen_ai.tool.call.arguments`` and ``gen_ai.tool.call.result`` -- the
  full MCP tool inputs and outputs -- onto MCP protocol spans unconditionally;
  no capture switch turns them off. The guard closes this path in two places:
  it uninstruments the distro's ``McpInstrumentor`` singleton in the runtime
  process, and, when the distro has set ``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS``
  (its default exclusion list under agent observability), it appends ``aws_mcp``
  to that list so the auto-instrumented MCP server subprocesses -- which inherit
  a copy of this process's environment -- exclude MCP instrumentation too.
  Uninstrumenting also removes the instrumentor's trace-context injection, so
  botocore spans from the MCP subprocesses start their own traces instead of
  nesting under the tool call, and MCP JSON-RPC error codes are not recorded as
  span attributes; the tool name, call id, status, and timing remain
  on the Strands tool span. The hosted runtime is safe because agent
  observability sets this exclusion list, so the guard appends ``aws_mcp`` to it
  and the MCP server subprocesses inherit the exclusion. When the list is unset
  (a non-observability ``opentelemetry-instrument`` run) the guard leaves it
  unset; an auto-instrumented child would then load ``aws_mcp`` and could emit
  MCP tool-content spans, which is why the exclusion is relied on only under
  agent observability.
* The AgentCore Memory SDK client logger (``bedrock_agentcore.memory.client``)
  writes the long-term-memory namespace, which embeds the memory actor ID, at
  INFO on every successful lookup, and the distro exports root-logger records
  under agent observability. In a hosted runtime, while redacting, the guard
  attaches a filter to that one logger that drops its records below WARNING (see
  ``_MemoryClientInfoFilter`` for what is and is not covered). The gate is
  deliberate: the line leaves the process only where agent observability installs
  the root-logger export handler, which is the hosted runtime. The opt-in and a
  non-hosted run do not install it. SECURITY.md lists the paths this leaves open.

The hosted default is to carry no content: capture is off, the Strands redaction
token is present, the agent-span and error-path wrappers are installed, the MCP
client exception logs are filtered, the MCP tool-content instrumentation is
suppressed, and, in a hosted runtime, the memory SDK client's below-WARNING
records are dropped. Latency, token-count, model id, finish reason, and span structure
remain, as separate non-sensitive attributes. The error *type* also remains, but
where it is recorded depends on the path: a raised error carries its type as the
span status description (the ``exception`` event, with its message and stack
trace, is dropped); an error-status tool result carries a fixed non-content
status string on the span, with its exception type recorded only on the filtered
``exception.type`` log-record attribute.

``apply_trace_content_guard`` is called once at module import of the entrypoint,
before the first Strands ``Tracer`` is constructed and before the first MCP
client is created, so the default does not depend on the deploy environment
passing the switches. An operator can opt into content capture only by setting
``GBAW_CAPTURE_TRACE_CONTENT`` to the exact opt-in token on the runtime; this is
for non-production debugging and fails closed on any other value. The opt-in
leaves the Strands wrappers, the logging filters, MCP instrumentation, and the
disabled-instrumentations list untouched.
"""

# Standard library
import functools
import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# Fixed, bounded, content-free message logged at WARNING when a redaction guard
# path is skipped (a missing or renamed internal, or attribute drift). A skipped
# path leaves a content export open, so it is surfaced at WARNING at deploy time
# rather than hidden at DEBUG. The log carries only this message and the skipped
# path name; it never includes the exception message or stack trace, which can
# carry content, so the signature-pin unit tests remain the detail-level guard
# against drift in CI.
GUARD_PATH_SKIPPED_MESSAGE = (
    "Telemetry redaction guard path skipped; a trace-content export may be open. Path: %s. Exception type: %s."
)


def _warn_guard_path_skipped(path: str, exc: BaseException) -> None:
    """Log at WARNING that a redaction guard path was skipped, content-free.

    Records the guard path name and the exception *type* only. The exception
    message and stack trace are deliberately omitted because they can carry
    prompt, response, or tool content.
    """
    logger.warning(GUARD_PATH_SKIPPED_MESSAGE, path, type(exc).__name__)


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

# The Strands span attribute (set from a plain keyword argument on the agent
# span) that carries the system prompt, and the non-content replacement used for
# a redacted error status.
STRANDS_SYSTEM_PROMPT_KWARG = "system_prompt"
REDACTED_TOOL_ERROR_STATUS = "tool returned error status"

# The non-content exception-type attribute key the OpenTelemetry logging handler
# uses. The guard sets it explicitly before clearing ``exc_info`` so the error
# type survives on the log record while the message and stack trace do not.
OTEL_EXCEPTION_TYPE_ATTR = "exception.type"

# stdlib logger whose tool-execution exceptions the OpenTelemetry logging handler
# would turn into log-record attributes (exception message and stack trace). This
# is the Strands MCP client logger; the MCP SDK's own validation and receive-loop
# messages are logged on the root logger with the exception interpolated into the
# message string, so a named-logger filter cannot reach them (documented as a
# residual risk in SECURITY.md).
MCP_EXCEPTION_LOGGER_NAMES = ("strands.tools.mcp.mcp_client",)

# Hosted-runtime switch: the same variable and parsing as
# ``config.settings.HOSTED_RUNTIME``, read from the mapping handed to the guard
# (the process environment when the entrypoint calls it) so the guard stays free
# of application settings and testable with a dict.
HOSTED_RUNTIME_VAR = "GBAW_HOSTED_RUNTIME"

# stdlib logger of the AgentCore Memory SDK client, whose ``retrieve_memories``
# logs the long-term-memory namespace at INFO. See ``_MemoryClientInfoFilter``.
MEMORY_CLIENT_LOGGER_NAME = "bedrock_agentcore.memory.client"


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


class _McpExceptionInfoFilter(logging.Filter):
    """Strip exception message and stack trace from a log record, keep the type.

    The OpenTelemetry logging handler copies ``record.exc_info`` into log-record
    attributes (exception type, message, and stack trace). MCP tool exceptions can
    echo tool arguments or provider output, so while redacting we remove the
    message and stack trace on the MCP client logger. Before clearing
    ``record.exc_info`` the filter records the exception *type* name as a plain
    ``exception.type`` record attribute, which the handler copies verbatim (it is
    not a reserved attribute). The static log message (for example "tool execution
    failed") is kept, and the error type is preserved as the ``exception.type``
    OpenTelemetry log-record attribute, so the fact and class of the failure are
    still recorded on exported log records; the message and stack trace are not.

    The filter runs before any handler sees the record, so the exception traceback
    is also removed from the stdout application log, including in local
    development. The ``exception.type`` value is only a log-record attribute that
    the OpenTelemetry logging handler exports; the plain stdout/console log
    (loguru's last-resort handler in local development) shows only the static
    message, not the exception type.
    """

    # A sentinel so the guard is idempotent and the filter is identifiable.
    marker = "gbaw_trace_content_guard"

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        if exc_info and exc_info[0] is not None:
            # Keep the non-content exception type; the handler copies this plain
            # record attribute into the log record.
            record.__dict__[OTEL_EXCEPTION_TYPE_ATTR] = exc_info[0].__name__
        record.exc_info = None
        record.exc_text = None
        return True


def _install_mcp_logging_filter() -> None:
    """Attach the exception-stripping filter to the MCP client logger once.

    A failure here leaves the MCP client's exception message and stack trace
    exportable, so it is logged at WARNING (content-free) rather than silently
    skipped; it never fails startup.
    """
    try:
        for name in MCP_EXCEPTION_LOGGER_NAMES:
            target = logging.getLogger(name)
            if any(getattr(flt, "marker", None) == _McpExceptionInfoFilter.marker for flt in target.filters):
                continue
            target.addFilter(_McpExceptionInfoFilter())
    except Exception as exc:  # pragma: no cover - defensive, never fail startup
        _warn_guard_path_skipped("MCP client logging filter", exc)


def _install_strands_span_wrappers() -> None:
    """Wrap the Strands ``Tracer`` methods that export content the token misses.

    * ``start_agent_span`` -- drop the ``system_prompt`` keyword so it is not set
      as a plain span attribute (and so the distro's log-record handler cannot
      turn it into a ``system`` message). The system prompt is still reported,
      redacted, through ``gen_ai.system_instructions`` on model spans.
    * ``_end_span`` -- replace an error's status description with its exception
      type (or a fixed non-content string for an error-status tool result) and
      drop the exception object so no ``exception`` event with message/stack
      trace is recorded. The error *status* and the error *type* are kept.

    The wrappers are installed on the ``Tracer`` class so every instance is
    covered, and they are idempotent (marked once applied). Each wrapper is
    decorated with ``functools.wraps(original)`` so ``inspect.signature`` and the
    ``__wrapped__`` chain resolve to the real Strands method; the signature is
    asserted by the unit tests so a Strands change fails loudly rather than
    silently leaking.

    A missing Strands tracer, or attribute drift on the wrapped ``Tracer``
    methods, leaves these two content paths open. The whole install -- the
    import and the ``Tracer`` attribute access -- is guarded so drift cannot
    crash startup, and any skip is logged at WARNING (content-free) so the open
    path is visible at deploy time rather than silent.
    """
    try:
        # Third-party packages
        from strands.telemetry.tracer import Tracer

        marker = "_gbaw_trace_content_guard"

        original_start_agent_span = Tracer.start_agent_span
        if not getattr(original_start_agent_span, marker, False):

            @functools.wraps(original_start_agent_span)
            def start_agent_span(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                # Drop the system prompt so it is never set as a raw span attribute.
                kwargs.pop(STRANDS_SYSTEM_PROMPT_KWARG, None)
                return original_start_agent_span(self, *args, **kwargs)

            setattr(start_agent_span, marker, True)
            Tracer.start_agent_span = start_agent_span  # type: ignore[method-assign]

        original_end_span = Tracer._end_span
        if not getattr(original_end_span, marker, False):

            @functools.wraps(original_end_span)
            def _end_span(self, span, attributes=None, error=None, error_message=None):  # type: ignore[no-untyped-def]
                # Keep the error status, but strip any content-bearing text. An
                # exception becomes its type only (no record_exception, no stack
                # trace); an error-status tool result (error is None, error_message
                # set) becomes a fixed non-content string.
                if error is not None:
                    return original_end_span(
                        self,
                        span,
                        attributes,
                        error=None,
                        error_message=type(error).__name__,
                    )
                if error_message is not None:
                    return original_end_span(
                        self,
                        span,
                        attributes,
                        error=None,
                        error_message=REDACTED_TOOL_ERROR_STATUS,
                    )
                return original_end_span(self, span, attributes, error=None, error_message=None)

            setattr(_end_span, marker, True)
            Tracer._end_span = _end_span  # type: ignore[method-assign]
    except Exception as exc:
        _warn_guard_path_skipped("strands span wrappers", exc)


def _mcp_client_wrapper_layer_remains() -> bool:
    """Return True if an ADOT MCP ``ClientWrapper`` layer still wraps the SDK.

    ``uninstrument`` removes only the outermost wrapt layer, so if a Strands
    ``MCPClient`` wrapped the method after ADOT did, the content-writing ADOT
    layer can survive underneath. Walk the ``__wrapped__`` chain of the MCP SDK
    send methods and report whether any wrapper belongs to the ADOT MCP module.
    """
    try:
        # Third-party packages
        from mcp.shared.session import BaseSession
    except Exception:  # pragma: no cover - MCP SDK not installed
        return False
    adot_mcp_module = "amazon.opentelemetry.distro.instrumentation.mcp"
    for method_name in ("send_request", "send_notification"):
        func = getattr(BaseSession, method_name, None)
        seen = 0
        while func is not None and hasattr(func, "__wrapped__") and seen < 10:
            # wrapt proxies __module__ to the wrapped target, so identify the ADOT
            # layer by the bound wrapper function it holds in ``_self_wrapper``.
            wrapper_fn = getattr(func, "_self_wrapper", None)
            wrapper_module = getattr(wrapper_fn, "__module__", "") or ""
            if wrapper_module.startswith(adot_mcp_module):
                return True
            func = getattr(func, "__wrapped__", None)
            seen += 1
    return False


def _uninstrument_mcp() -> None:
    """Uninstrument the ADOT MCP instrumentor singleton if it is instrumented.

    The MCP instrumentor is a ``BaseInstrumentor`` singleton whose
    ``uninstrument`` restores the wrapped MCP SDK methods, so no exported span
    carries ``gen_ai.tool.call.arguments`` or ``gen_ai.tool.call.result`` in this
    process. ``uninstrument`` is called only when the instrumentor reports it is
    instrumented, so an uninstrumented startup logs no warning. A missing or
    changed ADOT package, or any failure while uninstrumenting, is a no-op for
    startup but is logged at WARNING (content-free), because it leaves MCP tool
    arguments and results exportable; it never fails startup.
    """
    try:
        # Third-party packages
        from amazon.opentelemetry.distro.instrumentation.mcp import McpInstrumentor
    except Exception as exc:  # pragma: no cover - defensive import guard
        _warn_guard_path_skipped("MCP instrumentor import", exc)
        return
    try:
        instrumentor = McpInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        if _mcp_client_wrapper_layer_remains():
            # An MCP client wrapped the SDK before the guard ran, so an ADOT
            # content-writing layer survives underneath. No content is logged.
            logger.warning(
                "MCP tool-content instrumentation may remain active: an MCP client was "
                "created before telemetry redaction was applied."
            )
    except Exception as exc:  # pragma: no cover - defensive, never fail startup
        _warn_guard_path_skipped("MCP instrumentor uninstrument", exc)


def _hosted_runtime(env: Mapping[str, str]) -> bool:
    """Return True when the environment marks a hosted runtime.

    Same variable and parsing as ``config.settings.HOSTED_RUNTIME``: only the
    case-insensitive text ``true`` counts. The guard reads the process environment
    when it runs, before ``config.settings`` loads any dotenv file, so a flag that
    exists only in ``ui/.env.local`` is not seen here. Hosted deployments set it in
    the runtime environment.
    """
    return env.get(HOSTED_RUNTIME_VAR, "false").lower() == "true"


class _MemoryClientInfoFilter(logging.Filter):
    """Drop records below WARNING from the AgentCore Memory SDK client logger.

    On every successful lookup the client logs the long-term-memory namespace,
    which embeds the memory actor ID, at INFO, and the distro exports root-logger
    records under agent observability. This filter drops that line and the client's
    other sub-WARNING lines (initialization, event creation). It sits on the logger
    rather than setting a level, so it holds whatever the root logger is set to and
    whenever logging is configured, including after this guard runs, and, because
    a filter can only remove records, it never loosens a stricter level.

    Deliberately not covered:

    * WARNING and above from this logger pass, so SDK failures stay visible. On a
      service error the client's WARNING can include the resolved namespace
      (resource not found) or the service's error text, and its ERROR when
      creating or listing events fails carries the service's error text.
    * A logger filter applies only to records created on that logger. The SDK's
      other loggers are untouched, including the Strands session manager's, whose
      operational INFO lines (session and agent creation, flush counts) remain and
      whose ERROR on a non-service failure carries the retrieval template key and
      the exception text. This runtime does not use the SDK's ``MemorySession``,
      which logs the namespace and the query text at INFO from its own logger.
    """

    # A sentinel so the guard is idempotent and the filter is identifiable.
    marker = "gbaw_memory_client_guard"

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


def _install_memory_client_filter() -> None:
    """Attach the below-WARNING filter to the memory SDK client logger once.

    A failure here leaves the memory namespace line (which embeds the actor ID)
    exportable in a hosted runtime, so it is logged at WARNING (content-free)
    rather than silently skipped; it never fails startup.
    """
    try:
        target = logging.getLogger(MEMORY_CLIENT_LOGGER_NAME)
        if any(getattr(flt, "marker", None) == _MemoryClientInfoFilter.marker for flt in target.filters):
            return
        target.addFilter(_MemoryClientInfoFilter())
    except Exception as exc:  # pragma: no cover - defensive, never fail startup
        _warn_guard_path_skipped("memory client logging filter", exc)


def apply_trace_content_guard(env: "os._Environ[str] | dict[str, str] | None" = None) -> TraceContentDecision:
    """Apply the resolved content switches to the environment in place.

    Must run before the first Strands ``Tracer`` is constructed and before the
    first MCP client is created. The botocore Bedrock instrumentation reads the
    capture switch at call time, so applying it here also governs GenAI events
    and the OpenTelemetry log records derived from them. When redacting, the
    guard also installs the Strands agent-span and error-path wrappers, attaches
    the MCP client logging filter, uninstruments the MCP instrumentor singleton
    in this process, and appends ``aws_mcp`` to an already-set
    disabled-instrumentations list so subprocesses inherit the exclusion. In a
    hosted runtime it also filters the AgentCore Memory SDK client logger to
    WARNING and above; a non-hosted run and the opt-in leave it alone. Returns the
    decision for logging and tests.
    """
    target = os.environ if env is None else env
    decision = resolve_trace_content_env(target)
    target[GENAI_CAPTURE_CONTENT_VAR] = decision.capture_message_content
    target[SEMCONV_OPT_IN_VAR] = decision.semconv_opt_in
    if decision.disabled_instrumentations is not None:
        target[DISABLED_INSTRUMENTATIONS_VAR] = decision.disabled_instrumentations
    if not decision.capture_enabled:
        _install_strands_span_wrappers()
        _install_mcp_logging_filter()
        _uninstrument_mcp()
        if _hosted_runtime(target):
            _install_memory_client_filter()
    return decision
