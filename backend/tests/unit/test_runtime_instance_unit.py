"""Per-instance initialization for AgentCore Runtime V2 snapshot restores.

Platform version V2 restores every instance from one snapshot taken after
startup, so the snapshot's memory (the ``random`` generator state, any key
created during startup) is identical in every instance. These tests simulate
that in-process: capture the state a snapshot would copy, "restore" it twice,
and compare what the two instances produce with and without
``ensure_instance_initialized``.
"""

# Standard library
import random
import threading
import time

# Third-party packages
import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Local modules
from utils import kb_tools, resilience, runtime_instance, security

pytestmark = pytest.mark.unit

SUBJECT = "synthetic-subject-0001"


@pytest.fixture(autouse=True)
def _isolated_instance_state():
    """Start every test uninitialized and leave the process as it was."""
    saved_random_state = random.getstate()
    saved_key = security._LOG_REDACTION_KEY
    saved_initialized = runtime_instance._initialized
    saved_instance_id = runtime_instance._instance_id
    runtime_instance.reset_instance_state()
    security.rotate_log_redaction_key()
    yield
    random.setstate(saved_random_state)
    security._LOG_REDACTION_KEY = saved_key
    runtime_instance._initialized = saved_initialized
    runtime_instance._instance_id = saved_instance_id


def _snapshot() -> dict:
    """Capture the process state a V2 startup snapshot copies into every instance."""
    return {"random": random.getstate(), "key": security._LOG_REDACTION_KEY}


def _restore(snapshot: dict) -> None:
    """Start an instance from the snapshot: identical memory, not yet initialized."""
    random.setstate(snapshot["random"])
    security._LOG_REDACTION_KEY = snapshot["key"]
    runtime_instance.reset_instance_state()


def _startup_work() -> None:
    """Startup that touches the shared state, as logging or a library might."""
    security.redact_identifier("startup-log-line")
    random.random()


def test_restored_instances_repeat_startup_state_without_initialization():
    # Control: this is the V2 hazard the initialization exists to remove.
    _startup_work()
    snapshot = _snapshot()

    _restore(snapshot)
    first = (random.getrandbits(64), security.redact_identifier(SUBJECT))
    _restore(snapshot)
    second = (random.getrandbits(64), security.redact_identifier(SUBJECT))

    assert first == second


def test_initialization_gives_each_restored_instance_fresh_values():
    _startup_work()
    snapshot = _snapshot()

    produced = []
    for _ in range(2):
        _restore(snapshot)
        assert runtime_instance.ensure_instance_initialized() is True
        produced.append((random.getrandbits(64), security.redact_identifier(SUBJECT), runtime_instance.instance_id()))

    (first_bits, first_token, first_id), (second_bits, second_token, second_id) = produced
    assert first_bits != second_bits
    assert first_token != second_token
    assert first_id is not None and second_id is not None
    assert first_id != second_id


def test_a_key_created_during_startup_is_never_used_after_the_first_invocation():
    startup_token = security.redact_identifier(SUBJECT)
    startup_key = security._LOG_REDACTION_KEY
    assert startup_key is not None

    runtime_instance.ensure_instance_initialized()
    serving_token = security.redact_identifier(SUBJECT)

    assert security._LOG_REDACTION_KEY not in (None, startup_key)
    assert serving_token != startup_token
    # Tokens stay stable within the instance after initialization.
    assert security.redact_identifier(SUBJECT) == serving_token


def test_the_redaction_key_is_created_on_first_use_not_before():
    assert security._LOG_REDACTION_KEY is None

    token = security.redact_identifier(SUBJECT)

    assert security._LOG_REDACTION_KEY is not None
    assert len(security._LOG_REDACTION_KEY) == 32
    assert token == security.redact_identifier(SUBJECT)


def test_initialization_runs_once_per_process():
    assert runtime_instance.ensure_instance_initialized() is True
    instance = runtime_instance.instance_id()
    token = security.redact_identifier(SUBJECT)
    random_state = random.getstate()

    assert runtime_instance.ensure_instance_initialized() is False

    assert runtime_instance.instance_id() == instance
    assert security.redact_identifier(SUBJECT) == token
    assert random.getstate() == random_state
    assert runtime_instance.is_instance_initialized() is True


def test_concurrent_first_invocations_initialize_exactly_once():
    results = []
    barrier = threading.Barrier(8)

    def first_invocation():
        barrier.wait()
        results.append(runtime_instance.ensure_instance_initialized())

    threads = [threading.Thread(target=first_invocation) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False] * 7 + [True]


def test_instance_id_is_absent_before_and_random_hex_after():
    assert runtime_instance.instance_id() is None

    runtime_instance.ensure_instance_initialized()

    instance = runtime_instance.instance_id()
    assert instance is not None
    assert len(instance) == 16
    int(instance, 16)


def _local_provider(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    return provider, exporter


def test_spans_carry_the_instance_id_only_after_initialization(monkeypatch):
    provider, exporter = _local_provider(monkeypatch)
    assert runtime_instance.install_instance_id_span_processor() is True
    tracer = provider.get_tracer("snapshot-safety-test")

    with tracer.start_as_current_span("startup-span"):
        pass
    runtime_instance.ensure_instance_initialized()
    with tracer.start_as_current_span("request-span"):
        pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert runtime_instance.INSTANCE_ID_SPAN_ATTRIBUTE not in spans["startup-span"].attributes
    assert spans["request-span"].attributes[runtime_instance.INSTANCE_ID_SPAN_ATTRIBUTE] == (
        runtime_instance.instance_id()
    )


def test_span_processor_install_is_idempotent_per_provider(monkeypatch):
    provider, _exporter = _local_provider(monkeypatch)
    registered = []
    original_add = provider.add_span_processor
    monkeypatch.setattr(
        provider, "add_span_processor", lambda processor: (registered.append(processor), original_add(processor))
    )

    assert runtime_instance.install_instance_id_span_processor() is True
    assert runtime_instance.install_instance_id_span_processor() is True

    assert len(registered) == 1


def test_span_processor_install_is_a_no_op_without_an_sdk_provider(monkeypatch):
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: trace_api.NoOpTracerProvider())

    assert runtime_instance.install_instance_id_span_processor() is False


def test_span_processor_never_fails_the_traced_operation():
    runtime_instance.ensure_instance_initialized()
    processor = runtime_instance._instance_id_span_processor_class()()

    class _RejectingSpan:
        def set_attribute(self, key, value):
            raise RuntimeError("span rejected the attribute")

    processor.on_start(_RejectingSpan())


def test_retry_jitter_does_not_draw_from_the_shared_generator(monkeypatch):
    # The jitter must come from os.urandom, not the module-level generator a
    # V2 snapshot shares across instances.
    monkeypatch.setattr(kb_tools, "RETRY_BASE_DELAY", 1.0)
    monkeypatch.setattr(kb_tools.time, "sleep", lambda seconds: None)
    random_state = random.getstate()

    kb_tools._sleep_backoff(2)

    assert random.getstate() == random_state

    calls = []

    @resilience.retry_with_backoff(max_attempts=2, base_delay=0.5)
    def flaky():
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise type("ThrottlingException", (Exception,), {})()
        return "ok"

    monkeypatch.setattr(resilience.time, "sleep", lambda seconds: None)
    assert flaky() == "ok"
    assert random.getstate() == random_state


def _provider_with(monkeypatch, generator):
    provider = SDKTracerProvider(id_generator=generator)
    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: provider)
    return provider


def _ids_from(generator, random_state):
    """IDs produced by an instance whose ``random`` state is ``random_state``."""
    random.setstate(random_state)
    return generator.generate_trace_id(), generator.generate_span_id()


def test_distro_id_generator_repeats_ids_across_restored_instances_without_hardening():
    # Control: the AWS distro's X-Ray generator draws from ``random``.
    # Third-party packages
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

    generator = AwsXRayIdGenerator()
    snapshot = random.getstate()

    first = _ids_from(generator, snapshot)
    second = _ids_from(generator, snapshot)

    # Same random bits; only the epoch-seconds prefix can differ between calls.
    assert first[1] == second[1]
    assert first[0] & ((1 << 96) - 1) == second[0] & ((1 << 96) - 1)


def test_hardened_xray_ids_do_not_depend_on_the_snapshot_state(monkeypatch):
    # Third-party packages
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

    provider = _provider_with(monkeypatch, AwsXRayIdGenerator())
    tracer = provider.get_tracer("created-before-hardening")
    assert runtime_instance.harden_trace_id_generator() is True
    snapshot = random.getstate()

    first = _ids_from(provider.id_generator, snapshot)
    second = _ids_from(provider.id_generator, snapshot)

    assert first[1] != second[1]
    assert first[0] & ((1 << 96) - 1) != second[0] & ((1 << 96) - 1)
    # The X-Ray format is kept: the top 32 bits are the current epoch seconds.
    assert abs((first[0] >> 96) - int(time.time())) <= 2
    # A tracer created before hardening shares the hardened generator.
    with tracer.start_as_current_span("first-request-server-span") as span:
        context = span.get_span_context()
    assert context.trace_id != 0 and context.span_id != 0
    assert tracer.id_generator is provider.id_generator


def test_hardened_random_ids_do_not_depend_on_the_snapshot_state(monkeypatch):
    # Third-party packages
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    provider = _provider_with(monkeypatch, RandomIdGenerator())
    assert runtime_instance.harden_trace_id_generator() is True
    snapshot = random.getstate()

    first = _ids_from(provider.id_generator, snapshot)
    second = _ids_from(provider.id_generator, snapshot)

    assert first != second
    assert all(value != 0 for value in first + second)


def test_hardening_is_idempotent_and_leaves_unknown_generators_alone(monkeypatch):
    # Third-party packages
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    provider = _provider_with(monkeypatch, RandomIdGenerator())
    assert runtime_instance.harden_trace_id_generator() is True
    hardened = provider.id_generator.generate_trace_id
    assert runtime_instance.harden_trace_id_generator() is True
    assert provider.id_generator.generate_trace_id is hardened

    class CustomIdGenerator(RandomIdGenerator):
        pass

    custom = CustomIdGenerator()
    _provider_with(monkeypatch, custom)
    assert runtime_instance.harden_trace_id_generator() is False
    assert "generate_trace_id" not in vars(custom)

    monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: trace_api.NoOpTracerProvider())
    assert runtime_instance.harden_trace_id_generator() is False


def test_hardened_ids_fall_back_to_the_original_generator_on_entropy_errors(monkeypatch):
    # Third-party packages
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    provider = _provider_with(monkeypatch, RandomIdGenerator())
    runtime_instance.harden_trace_id_generator()

    def failing_randbits(bits):
        raise OSError("entropy unavailable")

    monkeypatch.setattr(runtime_instance.secrets, "randbits", failing_randbits)

    assert provider.id_generator.generate_trace_id() != 0
    assert provider.id_generator.generate_span_id() != 0
