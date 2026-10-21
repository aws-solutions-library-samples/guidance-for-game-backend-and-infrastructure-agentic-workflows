"""Unit tests for serializing start/stop of a shared cached MCP client.

These tests reproduce the strands ``MCPClient`` start sequence without a real
MCP server process: ``load_tools`` calls ``start()`` when the provider has not
started yet and only sets the started flag after ``start()`` returns, while
``start()`` raises ``MCPClientInitializationError`` if the session is already
active. Two Strands registries built concurrently around one cached client
therefore overlap in that window unless the start is serialized.

The fake providers below model that exact ordering with a barrier/event so the
overlap is deterministic rather than timing luck. No network and no real MCP
server subprocess is used.
"""

# Standard library
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Sequence, Tuple

# Third-party packages
import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams
from strands.tools.mcp.mcp_client import MCPClient
from strands.tools.registry import ToolRegistry
from strands.tools.tool_provider import ToolProvider
from strands.types.exceptions import MCPClientInitializationError
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

# Local modules
from utils.mcp_provider_lock import SerializedStartProvider, serialize_mcp_client_start

pytestmark = pytest.mark.unit


class _FakeTool(AgentTool):
    """A minimal AgentTool so a loaded provider yields a registrable tool."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {
            "name": self._name,
            "description": "fake tool",
            "inputSchema": {"json": {"type": "object"}},
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        # Not exercised in these tests; providers only load tools here.
        raise NotImplementedError


class _FakeSlowStartClient(ToolProvider):
    """Reproduces the real ``MCPClient`` start/stop lifecycle.

    ``load_tools`` starts the client when it has not started yet and sets the
    started flag only after ``start()`` returns. ``start()`` raises when the
    session is already active, mirroring ``MCPClient.start``'s
    ``_is_session_active`` guard. ``stop()`` resets ``consumers`` to an empty set
    exactly as ``MCPClient.stop`` does, so an unlocked ``add_consumer`` can be
    erased by a concurrent stop. A start gate makes a second caller enter
    ``load_tools`` while the first is inside the slow ``start()``.
    """

    def __init__(
        self,
        start_gate: "threading.Event | None" = None,
        start_delay: float = 0.2,
        stop_delay: float = 0.0,
        stop_gate: "threading.Event | None" = None,
        rendezvous: "threading.Barrier | None" = None,
    ) -> None:
        self._tool_provider_started = False
        self._session_active = False
        self._start_delay = start_delay
        self._stop_delay = stop_delay
        # When set by the first starter, the gate lets a second (overlapping)
        # caller proceed into its own start while the first is still inside its
        # slow start window. Serialization must make that overlap impossible.
        self._start_gate = start_gate
        # Signalled at stop() entry so a test can drive a concurrent add_consumer
        # into the window where the real MCPClient.stop resets _consumers.
        self._stop_gate = stop_gate
        # A barrier two different clients both wait on inside start(). With a
        # per-client lock both reach it concurrently; a global lock would block
        # the second client's start() and the barrier would time out.
        self._rendezvous = rendezvous
        self.start_calls = 0
        self.consumers: set[Any] = set()
        self._lock = threading.Lock()

    def _is_session_active(self) -> bool:
        return self._session_active

    def start(self) -> None:
        # Mirror MCPClient.start on a fresh, never-started client: the background
        # thread becomes alive at the beginning of start(), so
        # _is_session_active() is True for the whole first-start window, not only
        # after it finishes. A second overlapping start therefore raises "the
        # client session is currently running". (The restart window after a stop
        # differs and is modeled by _RestartWindowClient below.)
        if self._is_session_active():
            raise MCPClientInitializationError("the client session is currently running")
        with self._lock:
            self.start_calls += 1
        self._session_active = True
        # Signal that we are mid-start so an unsynchronized second caller overlaps.
        if self._start_gate is not None:
            self._start_gate.set()
        if self._rendezvous is not None:
            self._rendezvous.wait()
        time.sleep(self._start_delay)

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        if not self._tool_provider_started:
            self.start()
            self._tool_provider_started = True
        return [_FakeTool("fake_list")]

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.discard(consumer_id)
        if not self.consumers and self._tool_provider_started:
            self.stop()

    def stop(self) -> None:
        if self._stop_gate is not None:
            self._stop_gate.set()
        # MCPClient.stop resets the started flag while the background thread is
        # still being joined, so there is a teardown window where the provider is
        # "not started" yet the session is still active. A start that reads the
        # cleared started flag then calls start() and hits the active-session
        # guard. Model that ordering: clear started first, keep the session
        # active across the slow teardown, then deactivate and reset consumers.
        self._tool_provider_started = False
        if self._stop_delay:
            time.sleep(self._stop_delay)
        self._session_active = False
        # MCPClient.stop ends with ``self._consumers = set()``; model that reset
        # so a consumer registered during the stop is erased unless add_consumer
        # holds the same lock.
        self.consumers = set()


def test_concurrent_registries_on_one_client_both_load_without_running_error() -> None:
    """Two registries built concurrently around ONE cached client must both load.

    The wrapper serializes the start window, so the second caller does not call
    ``start()`` while the first is still inside it and neither registry sees
    ``the client session is currently running``.
    """
    gate = threading.Event()
    raw = _FakeSlowStartClient(start_gate=gate, start_delay=0.3)
    wrapped = serialize_mcp_client_start(raw)

    errors: List[BaseException] = []

    def _first() -> None:
        try:
            ToolRegistry().process_tools([wrapped])
        except BaseException as exc:  # noqa: BLE001 - record for the assertion
            errors.append(exc)

    def _second() -> None:
        # Enter only once the first caller is mid-start, forcing a deterministic
        # overlap of the start window rather than relying on timing.
        assert gate.wait(timeout=5), "first starter never entered its start window"
        try:
            ToolRegistry().process_tools([wrapped])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_first), threading.Thread(target=_second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads), "registry build threads did not finish"
    assert errors == [], f"concurrent start raised: {errors!r}"
    assert raw.start_calls == 1, f"start() ran {raw.start_calls} times; expected exactly one serialized start"


def test_last_consumer_stop_does_not_interleave_with_concurrent_start() -> None:
    """A last-consumer stop must not race a concurrent start on the same client.

    The fake ``stop()`` signals entry, then holds the session active for a slow
    teardown window before resetting ``_session_active`` and ``consumers`` (as the
    real ``MCPClient.stop`` resets ``_consumers``). A start that overlaps that
    window on an unserialized ``remove_consumer`` calls ``start()`` while the
    session is still active and raises ``the client session is currently
    running``. Holding the stop window under the lock prevents the overlap.
    """
    stop_gate = threading.Event()
    raw = _FakeSlowStartClient(start_delay=0.0, stop_delay=0.3, stop_gate=stop_gate)
    wrapped = serialize_mcp_client_start(raw)

    # Prime one consumer so a later remove_consumer is a last-consumer stop.
    priming_registry = ToolRegistry()
    priming_registry.process_tools([wrapped])
    assert raw._session_active is True

    errors: List[BaseException] = []

    def _stop_last_consumer() -> None:
        try:
            priming_registry.cleanup()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _start_new_consumer() -> None:
        # Enter only once the stop teardown window is open (session still active),
        # forcing the overlap the lock must prevent.
        assert stop_gate.wait(timeout=5), "stop() never entered its teardown window"
        try:
            registry = ToolRegistry()
            registry.process_tools([serialize_mcp_client_start(raw)])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    stopper = threading.Thread(target=_stop_last_consumer)
    starter = threading.Thread(target=_start_new_consumer)
    stopper.start()
    starter.start()
    stopper.join(timeout=10)
    starter.join(timeout=10)

    assert not stopper.is_alive() and not starter.is_alive(), "stop/start threads did not finish"
    assert errors == [], f"stop/start interleave raised: {errors!r}"


def test_lock_is_per_client_different_clients_start_concurrently() -> None:
    """Two different clients must be able to start at the same time (per-client lock)."""
    rendezvous = threading.Barrier(2, timeout=5)
    results: List[str] = []
    errors: List[BaseException] = []

    def _make_and_build() -> None:
        try:
            # Each thread gets its OWN raw client but shares the rendezvous barrier
            # inside start(); with a per-client lock both reach it, but a global
            # lock would block the second start() and the barrier would time out.
            raw = _FakeSlowStartClient(start_delay=0.1, rendezvous=rendezvous)
            wrapped = serialize_mcp_client_start(raw)
            registry = ToolRegistry()
            registry.process_tools([wrapped])
            results.append("ok")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_make_and_build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], f"independent clients failed to start concurrently: {errors!r}"
    assert results == ["ok", "ok"]


def test_shared_lock_across_wrappers_of_same_raw_client() -> None:
    """Every wrapper of the same raw client shares one lock object."""
    raw = _FakeSlowStartClient()
    first = serialize_mcp_client_start(raw)
    second = serialize_mcp_client_start(raw)
    assert first.start_lock is second.start_lock

    other_raw = _FakeSlowStartClient()
    other = serialize_mcp_client_start(other_raw)
    assert other.start_lock is not first.start_lock


# --------------------------------------------------------------------------- #
# Finalizer: remove_consumer must never block                                 #
# --------------------------------------------------------------------------- #
class _FinalizerReentrantClient(ToolProvider):
    """Drives a second wrapper's ``remove_consumer`` from inside ``start()``.

    This models ``Agent.__del__`` → ``ToolRegistry.cleanup`` → ``remove_consumer``
    running during garbage collection while a loader holds the per-client lock.
    If ``remove_consumer`` blocked on that lock, the reentrant call would hang
    forever; a non-blocking ``remove_consumer`` returns immediately.
    """

    def __init__(self, on_start) -> None:
        self._on_start = on_start
        self.removed: List[Any] = []
        self.consumers: set[Any] = set()

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        # The callback runs while the loader holds the lock (load_tools holds it
        # across the delegate call), reproducing a GC finalizer on this thread.
        self._on_start()
        return [_FakeTool("fake_list")]

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.removed.append(consumer_id)
        self.consumers.discard(consumer_id)


def test_remove_consumer_from_same_thread_inside_start_does_not_deadlock() -> None:
    """A finalizer removal issued on the loader thread (holding the lock) returns.

    ``remove_consumer`` queues the removal and only tries a non-blocking acquire,
    so calling it while the same thread holds the lock cannot deadlock. The
    queued removal is drained by the lock holder before it releases.
    """
    done = threading.Event()

    def _finalizer_during_start() -> None:
        # Called while the loader thread holds the per-client lock.
        wrapped.remove_consumer("finalizer-consumer")
        done.set()

    raw = _FinalizerReentrantClient(on_start=_finalizer_during_start)
    wrapped = serialize_mcp_client_start(raw)

    worker = threading.Thread(
        target=lambda: asyncio.run(wrapped.load_tools()),
        daemon=True,  # a regression deadlocks only this daemon thread, so the
        # test process still fails cleanly and exits instead of hanging
    )
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "loader thread deadlocked on a same-thread finalizer removal"
    assert done.is_set(), "finalizer remove_consumer never returned"
    # The queued removal is drained by the lock holder before releasing.
    assert "finalizer-consumer" in raw.removed, "deferred finalizer removal was never drained"


def test_remove_consumer_from_helper_thread_while_start_waits_does_not_deadlock() -> None:
    """A finalizer removal on another thread, while start() waits on it, returns.

    This models the MCP background thread (which ``start()``/``stop()`` wait on)
    reaching ``Agent.__del__`` → ``remove_consumer``. The loader holds the lock
    and is waiting for the helper; the helper calls ``remove_consumer``. A
    blocking removal would deadlock both threads; the non-blocking one returns at
    once, so the helper comes back while the loader is still holding the lock.
    """
    helper_entered = threading.Event()
    helper_returned = threading.Event()
    # Did the helper return WHILE the loader was still holding the lock? A
    # non-blocking removal returns immediately, so this records True. A blocking
    # removal cannot return until the loader releases the lock, so it records
    # False and the assertion fails — while the bounded waits still let the
    # daemon worker finish and the process exit instead of hanging.
    helper_returned_during_hold: List[bool] = []

    def _start_waits_on_helper() -> None:
        # Fire a helper thread that calls remove_consumer, then wait a bounded
        # time for it — exactly the shape of start() waiting on the background
        # thread. With a blocking removal the helper cannot acquire the held lock
        # within the window, so it has not returned by the time this records.
        def _helper() -> None:
            helper_entered.set()
            wrapped.remove_consumer("bg-consumer")  # must not block on the held lock
            helper_returned.set()

        threading.Thread(target=_helper, daemon=True).start()
        assert helper_entered.wait(timeout=5), "helper thread never started"
        helper_returned_during_hold.append(helper_returned.wait(timeout=5))

    raw = _FinalizerReentrantClient(on_start=_start_waits_on_helper)
    wrapped = serialize_mcp_client_start(raw)

    worker = threading.Thread(
        target=lambda: asyncio.run(wrapped.load_tools()),
        daemon=True,  # a regression deadlocks only this daemon thread, so the
        # test process still fails cleanly and exits instead of hanging
    )
    worker.start()
    worker.join(timeout=12)

    assert not worker.is_alive(), "loader thread never completed"
    assert helper_returned_during_hold == [
        True
    ], "helper-thread finalizer removal did not return while the loader held the lock"
    assert "bg-consumer" in raw.removed, "deferred finalizer removal was never drained"


# --------------------------------------------------------------------------- #
# Consumer accounting: add_consumer under the lock survives a concurrent stop  #
# --------------------------------------------------------------------------- #
def test_consumer_registered_during_stop_survives() -> None:
    """A consumer registered during a last-consumer stop must not be erased.

    The real ``MCPClient.stop`` resets ``_consumers`` to an empty set. With
    ``add_consumer`` taken under the same lock as the stop, a registration that
    races the stop is applied after the reset and survives.
    """
    stop_gate = threading.Event()
    raw = _FakeSlowStartClient(start_delay=0.0, stop_delay=0.3, stop_gate=stop_gate)
    first = serialize_mcp_client_start(raw)

    # Prime one consumer and start the client.
    priming_registry = ToolRegistry()
    priming_registry.process_tools([first])
    assert raw.consumers, "priming consumer was not registered"

    errors: List[BaseException] = []

    def _stop() -> None:
        try:
            priming_registry.cleanup()  # last-consumer stop; slow, resets consumers
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _register_during_stop() -> None:
        # Enter add_consumer only once stop() is in progress (past the reset point
        # it is heading toward), so an unlocked add_consumer would be erased.
        assert stop_gate.wait(timeout=5), "stop() never entered"
        try:
            serialize_mcp_client_start(raw).add_consumer("late-consumer")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    stopper = threading.Thread(target=_stop)
    registrar = threading.Thread(target=_register_during_stop)
    stopper.start()
    registrar.start()
    stopper.join(timeout=10)
    registrar.join(timeout=10)

    assert not stopper.is_alive() and not registrar.is_alive(), "stop/register threads did not finish"
    assert errors == [], f"stop/register raised: {errors!r}"
    assert "late-consumer" in raw.consumers, "a consumer registered during stop was erased"


# --------------------------------------------------------------------------- #
# Restart window: only one start even across a stop/restart overlap            #
# --------------------------------------------------------------------------- #
class _RestartWindowClient(ToolProvider):
    """Models the real restart window after a stop.

    The real ``MCPClient.stop`` resolves the current ``_close_future`` and lets
    the background thread wind down, and a later ``start()`` creates a brand new
    background thread that installs a fresh ``_close_future``. Until that new
    thread installs its future, ``_is_session_active()`` reports False, so a
    second overlapping restart does NOT see a running session and launches its
    own session instead of raising. Two unserialized restarts therefore launch
    two sessions. The wrapper must admit only one launch at a time, so two
    overlapping restarts yield exactly one launch.
    """

    def __init__(self, start_delay: float = 0.2) -> None:
        # Fully active only once a launch finishes installing its session; False
        # during the launch window, matching the real restart gap.
        self._session_active = False
        self._tool_provider_started = False
        self._start_delay = start_delay
        self.start_calls = 0
        self.concurrent_starts = 0
        self.max_concurrent_starts = 0
        self.consumers: set[Any] = set()
        self._lock = threading.Lock()

    def _is_session_active(self) -> bool:
        return self._session_active

    def start(self) -> None:
        # During a restart the previous session has been torn down and the new
        # background thread has not installed its close future yet, so the
        # active-session guard does not fire. A concurrent unserialized start in
        # this window launches a second session rather than raising.
        if self._is_session_active():
            raise MCPClientInitializationError("the client session is currently running")
        with self._lock:
            self.start_calls += 1
            self.concurrent_starts += 1
            self.max_concurrent_starts = max(self.max_concurrent_starts, self.concurrent_starts)
        # Session is not reported active until the launch completes.
        time.sleep(self._start_delay)
        with self._lock:
            self.concurrent_starts -= 1
        self._session_active = True

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        if not self._tool_provider_started:
            self.start()
            self._tool_provider_started = True
        return [_FakeTool("fake_list")]

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.discard(consumer_id)
        if not self.consumers and self._tool_provider_started:
            self.stop()

    def stop(self) -> None:
        # Reusable after stop: the next start launches a new session. Clearing
        # the started flag and session together models the gap where the old
        # session is gone and the new background thread has not yet replaced the
        # close future.
        self._session_active = False
        self._tool_provider_started = False
        self.consumers = set()


def test_overlapping_restart_launches_only_one_session_at_a_time() -> None:
    """Two overlapping restarts after a stop launch exactly one session.

    Prime one start and stop first so the two concurrent builds are genuine
    restarts: the shared client was started, then torn down, and now two
    specialist calls restart it at once. During the restart window the client
    does not report an active session, so without the lock both builds launch
    their own session (``start_calls == 3``, counting the priming start). The wrapper serializes the start
    window, so the second build sees the first build's completed launch and does
    not launch again — exactly one launch.
    """
    raw = _RestartWindowClient(start_delay=0.3)

    # Prime: start the client and then tear it down (last consumer leaves).
    priming_registry = ToolRegistry()
    priming_registry.process_tools([serialize_mcp_client_start(raw)])
    assert raw.start_calls == 1, "priming start did not launch a session"
    priming_registry.cleanup()
    assert raw._tool_provider_started is False, "priming stop did not reset the client"

    errors: List[BaseException] = []
    overlap = threading.Barrier(2)

    def _build() -> None:
        overlap.wait()
        try:
            ToolRegistry().process_tools([serialize_mcp_client_start(raw)])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_build) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads), "restart build threads did not finish"
    assert errors == [], f"overlapping restart raised: {errors!r}"
    assert (
        raw.start_calls == 2
    ), f"restart made {raw.start_calls} launches total; expected priming + exactly one restart launch"
    assert (
        raw.max_concurrent_starts == 1
    ), f"start() ran {raw.max_concurrent_starts} times concurrently; the wrapper must admit one at a time"


# --------------------------------------------------------------------------- #
# Real MCPClient control test (in-memory transport, no subprocess, no network) #
# --------------------------------------------------------------------------- #
def _in_memory_transport(start_delay: float):
    """Build a zero-arg transport factory for a real ``MCPClient``.

    ``MCPClient`` calls this with no arguments on its own background event loop
    and enters the returned async context manager, expecting it to yield
    ``(read_stream, write_stream)``. A minimal FastMCP server runs on the
    server-side streams inside an anyio task group within the same context
    manager, so it lives on ``MCPClient``'s background loop with no subprocess or
    socket. A small startup delay makes two concurrent loaders overlap the start
    window deterministically.
    """

    @asynccontextmanager
    async def _transport() -> AsyncIterator[Tuple[Any, Any]]:
        await anyio.sleep(start_delay)
        server = FastMCP("control")

        @server.tool()
        def ping() -> str:
            return "pong"

        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    lambda: server._mcp_server.run(
                        server_read,
                        server_write,
                        server._mcp_server.create_initialization_options(),
                        raise_exceptions=False,
                    )
                )
                yield client_read, client_write
                task_group.cancel_scope.cancel()

    return _transport


def _load_concurrently(provider_for_thread) -> Tuple[List[List[str]], List[BaseException]]:
    """Run two concurrent ``load_tools`` calls, returning (results, errors)."""
    results: List[List[str]] = []
    errors: List[BaseException] = []
    gate = threading.Barrier(2, timeout=10)

    def _load() -> None:
        try:
            gate.wait()
            provider = provider_for_thread()
            tools = asyncio.run(provider.load_tools())
            results.append([tool.tool_name for tool in tools])
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=_load, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "load_tools threads did not finish"
    return results, errors


def test_real_mcp_client_raw_races_but_wrapper_serializes_start() -> None:
    """Pin the real ``MCPClient`` start race and the wrapper's fix for it.

    Two loaders that overlap the start window of ONE real ``MCPClient`` race its
    ``start()``-before-flag sequence. The raw client fails the overlapping loader
    with "the client session is currently running"; the serialized wrapper lets
    both load their tools from one start. This exercises the actual Strands
    lifecycle the wrapper exists to serialize, not a fake, so a Strands change
    that alters the start sequence would surface here.
    """
    # Raw client: drive loader 2 into start() only once loader 1's background
    # thread is alive, so loader 2 deterministically hits the active-session
    # guard and loses the race.
    raw = MCPClient(_in_memory_transport(start_delay=0.5), startup_timeout=5)
    raw_results: List[List[str]] = []
    raw_errors: List[BaseException] = []

    def _raw_first() -> None:
        try:
            tools = asyncio.run(raw.load_tools())
            raw_results.append([tool.tool_name for tool in tools])
        except BaseException as exc:  # noqa: BLE001
            raw_errors.append(exc)

    def _raw_second() -> None:
        # Wait until loader 1's background session thread is alive (the transport
        # is still inside its startup delay), then race start().
        deadline = time.time() + 10
        while not raw._is_session_active() and time.time() < deadline:
            time.sleep(0.005)
        try:
            tools = asyncio.run(raw.load_tools())
            raw_results.append([tool.tool_name for tool in tools])
        except BaseException as exc:  # noqa: BLE001
            raw_errors.append(exc)

    try:
        first = threading.Thread(target=_raw_first, daemon=True)
        second = threading.Thread(target=_raw_second, daemon=True)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive(), "raw loader threads did not finish"
    finally:
        if raw._is_session_active():
            raw.stop(None, None, None)

    assert any(
        "currently running" in str(exc) for exc in raw_errors
    ), f"expected the raw client to fail the overlapping loader with a 'currently running' error, got {raw_errors!r}"
    assert ["ping"] in raw_results, "the winning raw loader should still have loaded its tools"

    # Wrapped client: both loaders load from one serialized start, no race error.
    wrapped_raw = MCPClient(_in_memory_transport(start_delay=0.4), startup_timeout=5)
    try:
        wrapped_results, wrapped_errors = _load_concurrently(lambda: serialize_mcp_client_start(wrapped_raw))
    finally:
        if wrapped_raw._is_session_active():
            wrapped_raw.stop(None, None, None)

    assert wrapped_errors == [], f"the wrapper should serialize the start, but a loader raised: {wrapped_errors!r}"
    assert wrapped_results == [["ping"], ["ping"]], f"both wrapped loaders should load tools, got {wrapped_results!r}"


# --------------------------------------------------------------------------- #
# Guard delegation through the lock wrapper                                   #
# --------------------------------------------------------------------------- #
class _NamedToolClient(ToolProvider):
    """A simple provider that yields one tool with a chosen name, no start race."""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self.consumers: set[Any] = set()

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        return [_FakeTool(self._tool_name)]

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.discard(consumer_id)


def _load_tool_names(provider: ToolProvider) -> list[str]:
    tools = asyncio.run(provider.load_tools())
    return [tool.tool_name for tool in tools]


def test_eks_guard_sees_tools_through_the_lock_wrapper() -> None:
    """The EKS guard wraps the lock wrapper and still sees the delegate tools."""
    # Local modules
    from agents.eks_mcp_guard import EksMcpGuardedProvider, guard_eks_mcp_client

    raw = _NamedToolClient("list_k8s_resources")
    locked = serialize_mcp_client_start(raw)
    guarded = guard_eks_mcp_client("eks-mcp-server", locked)

    assert isinstance(guarded, EksMcpGuardedProvider)
    # Tool survives the allowlist, proving the guard loaded through the lock wrapper.
    assert "list_k8s_resources" in _load_tool_names(guarded)


def test_cost_guard_sees_tools_through_the_lock_wrapper() -> None:
    """The Cost guard wraps the lock wrapper and still sees the delegate tools."""
    # Local modules
    from agents.cost_mcp_guard import CostReportGuardedProvider, guard_cost_mcp_client

    raw = _NamedToolClient("cost-explorer")
    locked = serialize_mcp_client_start(raw)
    guarded = guard_cost_mcp_client("billing-cost-management-mcp-server", locked)

    assert isinstance(guarded, CostReportGuardedProvider)
    assert "cost-explorer" in _load_tool_names(guarded)


# --------------------------------------------------------------------------- #
# Wiring: base_specialist wraps the raw client before the guard transform      #
# --------------------------------------------------------------------------- #
def test_create_specialist_agent_wires_lock_wrapper_inside_each_guard() -> None:
    """The provider reaching the Agent is the guard around a SerializedStartProvider.

    This pins the ``base_specialist`` wiring: the raw cached client is wrapped by
    ``serialize_mcp_client_start`` and only then by the guard transform, for both
    the EKS and Cost transforms. Reverting the wiring (passing the raw client to
    the transform) makes the delegate chain wrong and this test fails.
    """
    # Local modules
    import agents.base_specialist as base_specialist
    from agents.cost_mcp_guard import CostReportGuardedProvider, guard_cost_mcp_client
    from agents.eks_mcp_guard import EksMcpGuardedProvider, guard_eks_mcp_client

    sentinel_clients = {
        "eks-mcp-server": _NamedToolClient("list_k8s_resources"),
        "billing-cost-management-mcp-server": _NamedToolClient("cost-explorer"),
    }
    captured: dict[str, Any] = {}

    class _CapturingAgent:
        def __init__(self, *args: Any, tools: Any = None, **kwargs: Any) -> None:
            captured["tools"] = list(tools or [])

        def __call__(self, query: str) -> str:
            return "ok"

    def _transform(server_name: str, provider: Any) -> Any:
        guarded = guard_eks_mcp_client(server_name, provider)
        return guard_cost_mcp_client(server_name, guarded)

    original_create = base_specialist.create_mcp_client
    original_agent = base_specialist.Agent
    base_specialist.create_mcp_client = lambda name: sentinel_clients.get(name)  # type: ignore[assignment]
    base_specialist.Agent = _CapturingAgent  # type: ignore[assignment]
    try:
        for server_name, guard_type in (
            ("eks-mcp-server", EksMcpGuardedProvider),
            ("billing-cost-management-mcp-server", CostReportGuardedProvider),
        ):
            captured.clear()
            agent_tool = base_specialist.create_specialist_agent(
                service_name=f"Probe-{server_name}",
                emoji="🧪",
                mcp_server_names=[server_name],
                kb_id=None,
                prompt_fn=lambda: "system",
                fallback_fn=lambda region: "fallback",
                mcp_client_transform=_transform,
            )
            # Invoke the @tool-wrapped function body.
            agent_tool._tool_func("hello")  # type: ignore[attr-defined]

            tools = captured["tools"]
            provider = next(t for t in tools if isinstance(t, guard_type))
            inner = provider._delegate  # the lock wrapper
            assert isinstance(
                inner, SerializedStartProvider
            ), f"{guard_type.__name__} should wrap a SerializedStartProvider, got {type(inner).__name__}"
            assert (
                inner._delegate is sentinel_clients[server_name]
            ), "the SerializedStartProvider should wrap the raw cached client"
    finally:
        base_specialist.create_mcp_client = original_create  # type: ignore[assignment]
        base_specialist.Agent = original_agent  # type: ignore[assignment]
