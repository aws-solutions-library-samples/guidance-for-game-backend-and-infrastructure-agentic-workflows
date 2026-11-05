"""Per-instance state that must be created after an AgentCore V2 snapshot restore.

Amazon Bedrock AgentCore Runtime platform version V2 starts the agent by
restoring a memory snapshot that it takes once per runtime version, after the
startup work in ``agentcore_main`` (imports, ``prewarm_container()``) completes
and the first ``/ping`` reports healthy. Every value produced during that
startup is copied into the snapshot and is identical in every restored
instance:

* the state of the process-wide ``random`` generator, which is seeded at
  import, so every instance would repeat one pseudo-random sequence. The
  OpenTelemetry trace and span ID generators configured by the AWS
  OpenTelemetry distro, and botocore's retry jitter, draw from it;
* any key or identifier minted at startup, such as the log-redaction HMAC key
  in ``utils.security``; and
* the OpenTelemetry resource attribute ``service.instance.id``, a random UUID
  the SDK generates at initialization and regenerates only when the process ID
  changes. Restored instances share the same PID, so they share the same ID.

AgentCore takes the snapshot before the runtime serves any invocation, so the
first invocation a process handles is the earliest point that always runs
after the restore (on platform version V1 it is simply after process start).
``ensure_instance_initialized`` runs once per process at that point, from the
top of the entrypoint, and:

* reseeds the process-wide ``random`` generator from operating-system entropy;
* rotates the log-redaction HMAC key so correlation tokens stay per instance,
  as SECURITY.md documents; and
* mints a random per-instance identifier that ``InstanceIdSpanProcessor``
  attaches to every span started afterwards as ``gbaw.runtime.instance_id``.

Spans can start before that point: the inbound server span of the first
request is opened before the handler runs. ``harden_trace_id_generator``,
applied at import, therefore makes the tracer provider's ID generator draw every
trace and span ID from os.urandom (keeping the X-Ray trace ID format), so no ID
depends on the snapshot's ``random`` state.

Startup code must not call ``ensure_instance_initialized``: anything it
produced before the snapshot would be captured in the snapshot and shared again.
"""

# Standard library
import random
import secrets
import threading
import time
import weakref
from typing import Any, Callable, Optional

# Span attribute carrying the per-instance identifier. The OpenTelemetry
# resource attribute ``service.instance.id`` cannot be changed after the SDK is
# initialized, so the per-instance value is a separate, namespaced attribute.
INSTANCE_ID_SPAN_ATTRIBUTE = "gbaw.runtime.instance_id"

# 64 random bits, hex-encoded: enough to tell instances apart. The value is not
# derived from the host, the process, the caller, or any content.
_INSTANCE_ID_BYTES = 8

_lock = threading.Lock()
_initialized = False
_instance_id: Optional[str] = None

# Tracer providers that already carry the span processor, so a repeated install
# never stacks a second processor. Weak references keep this from pinning a
# provider that a test or a reconfiguration discards.
_instrumented_providers: "weakref.WeakSet[Any]" = weakref.WeakSet()


def ensure_instance_initialized() -> bool:
    """Run the per-instance initialization once per process.

    Called at the top of every invocation; only the first call does any work, so
    later calls cost one attribute read. Returns True on the call that performed
    the initialization, False otherwise.
    """
    global _initialized, _instance_id
    if _initialized:
        return False
    with _lock:
        if _initialized:
            return False
        # ``random.seed()`` without an argument seeds from os.urandom, so every
        # restored instance leaves the shared snapshot sequence here.
        random.seed()

        # Local modules
        from utils.security import rotate_log_redaction_key

        # Discard any redaction key created before the first invocation (for
        # example by startup logging); the next token uses a key generated now,
        # after the restore, from os.urandom.
        rotate_log_redaction_key()
        _instance_id = secrets.token_hex(_INSTANCE_ID_BYTES)
        _initialized = True
        instance = _instance_id

    # Local modules
    from utils.logger import logger

    # The identifier is random and content-free, so it may be logged as-is. It
    # lets an operator tie this instance's log lines to its spans.
    logger.info(f"🆔 Runtime instance initialized (instance id: {instance})")
    return True


def instance_id() -> Optional[str]:
    """Return this instance's identifier, or None before the first invocation."""
    return _instance_id


def is_instance_initialized() -> bool:
    """Return True once ``ensure_instance_initialized`` has run in this process."""
    return _initialized


def reset_instance_state() -> None:
    """Return to the uninitialized state. For tests only."""
    global _initialized, _instance_id
    with _lock:
        _initialized = False
        _instance_id = None


_span_processor_class: Optional[type] = None


def _instance_id_span_processor_class() -> Optional[type]:
    """Build (once) the span processor class, or return None without the OTel SDK."""
    global _span_processor_class
    if _span_processor_class is not None:
        return _span_processor_class
    try:
        # Third-party packages
        from opentelemetry.sdk.trace import SpanProcessor
    except Exception:  # pragma: no cover - the SDK ships with the runtime
        return None

    class InstanceIdSpanProcessor(SpanProcessor):  # type: ignore[misc]
        """Attach the per-instance identifier to spans started after initialization.

        Spans started before the first invocation (startup work captured in the
        snapshot) carry no identifier rather than a value that would be shared
        by every restored instance. The processor only adds an attribute; it
        never reads or changes span content, so it does not interact with the
        trace-content redaction in ``config.telemetry_guard``.
        """

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            identifier = _instance_id
            if identifier is None:
                return
            try:
                span.set_attribute(INSTANCE_ID_SPAN_ATTRIBUTE, identifier)
            except Exception:
                # Telemetry enrichment must never fail the traced operation.
                pass

    _span_processor_class = InstanceIdSpanProcessor
    return _span_processor_class


def install_instance_id_span_processor() -> bool:
    """Register the per-instance span processor on the global tracer provider.

    A no-op that returns False when no OpenTelemetry SDK tracer provider is
    configured (for example a local run that is not started under
    ``opentelemetry-instrument``). Idempotent per provider. Never raises.
    """
    try:
        # Third-party packages
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        add_span_processor = getattr(provider, "add_span_processor", None)
        if add_span_processor is None:
            return False
        # A provider that cannot be tracked (not weak-referenceable or not
        # hashable) is left alone rather than risk registering twice; the
        # membership test raises for an unhashable provider before any change.
        weakref.ref(provider)
        if provider in _instrumented_providers:
            return True
        processor_class = _instance_id_span_processor_class()
        if processor_class is None:
            return False
        add_span_processor(processor_class())
        _instrumented_providers.add(provider)
        return True
    except Exception:
        # Telemetry enrichment must never prevent the runtime from starting.
        return False


_HARDENED_MARKER = "_gbaw_snapshot_safe_ids"


def _nonzero_random_bits(bits: int) -> int:
    """Return a non-zero integer of ``bits`` random bits from os.urandom (0 is an invalid ID)."""
    value = secrets.randbits(bits)
    while value == 0:
        value = secrets.randbits(bits)
    return value


def _xray_trace_id() -> int:
    """An X-Ray trace ID: epoch seconds in the top 32 bits, then 96 bits from os.urandom."""
    return (int(time.time()) << 96) + secrets.randbits(96)


def _random_trace_id() -> int:
    return _nonzero_random_bits(128)


def _random_span_id() -> int:
    return _nonzero_random_bits(64)


def _with_fallback(fresh: Callable[[], int], original: Callable[[], int]) -> Callable[[], int]:
    """Return a generator method that uses ``fresh`` and falls back to ``original`` on error."""

    def generate() -> int:
        try:
            return fresh()
        except Exception:
            # Never fail span creation; the original generator still returns an ID.
            return original()

    return generate


def harden_trace_id_generator() -> bool:
    """Make the global tracer provider's trace and span IDs come from os.urandom.

    The AWS OpenTelemetry distro configures the X-Ray ID generator, and the SDK
    default is its random generator; both draw from the shared ``random``
    generator, whose state a V2 snapshot copies into every instance. Every tracer
    a provider creates shares the provider's generator object, so replacing that
    object's two methods covers tracers created earlier too. The ID format is
    kept: X-Ray trace IDs still start with the current epoch seconds. A generator
    of any other type is left unchanged. Idempotent; never raises; returns True
    when the provider's generator draws from os.urandom.
    """
    try:
        # Third-party packages
        from opentelemetry import trace
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

        xray_generator_type: Optional[type] = None
        try:
            # Third-party packages
            from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

            xray_generator_type = AwsXRayIdGenerator
        except Exception:  # pragma: no cover - the extension ships with the distro
            pass

        generator = getattr(trace.get_tracer_provider(), "id_generator", None)
        if generator is None:
            return False
        if getattr(generator, _HARDENED_MARKER, False):
            return True

        fresh_trace_id: Callable[[], int]
        if xray_generator_type is not None and type(generator) is xray_generator_type:
            fresh_trace_id = _xray_trace_id
        elif type(generator) is RandomIdGenerator:
            fresh_trace_id = _random_trace_id
        else:
            return False

        original_trace_id = generator.generate_trace_id
        original_span_id = generator.generate_span_id
        generator.generate_trace_id = _with_fallback(fresh_trace_id, original_trace_id)
        generator.generate_span_id = _with_fallback(_random_span_id, original_span_id)
        setattr(generator, _HARDENED_MARKER, True)
        return True
    except Exception:
        # Telemetry hardening must never prevent the runtime from starting.
        return False


__all__ = [
    "INSTANCE_ID_SPAN_ATTRIBUTE",
    "ensure_instance_initialized",
    "harden_trace_id_generator",
    "install_instance_id_span_processor",
    "instance_id",
    "is_instance_initialized",
    "reset_instance_state",
]
