"""Serialize start and last-consumer stop of a shared cached MCP client.

``utils.mcp_client_factory.create_mcp_client`` returns ONE cached ``MCPClient``
per server for the whole process, and every specialist call builds a fresh
Strands ``Agent`` that appends that same client (optionally wrapped by a guard
provider). A Strands ``ToolRegistry`` calls ``add_consumer`` and then
``load_tools`` on each provider; ``MCPClient.load_tools`` calls ``start()`` when
it has not started yet and only sets the started flag after ``start()`` returns.
``start()`` raises ``the client session is currently running`` when the session
is already active. Two specialist calls on one cached client in the same turn
therefore race in that window and one fails to load its tools. Symmetrically,
``MCPClient.remove_consumer`` stops the client when the last consumer leaves,
which can race with another call's start.

``SerializedStartProvider`` wraps the raw cached client with a per-client lock
and holds it across ``load_tools`` (the start window) and ``add_consumer`` so
consumer accounting and the start window serialize together. The lock is keyed
on the raw client object and shared by every wrapper of that client, so two
wrappers of the same client serialize while different clients never block each
other.

``remove_consumer`` is reached from ``Agent.__del__`` → ``ToolRegistry.cleanup``
during garbage collection, on an arbitrary thread. That includes the loader
worker thread that already holds this lock (``load_tools`` runs on a fresh
``ThreadPoolExecutor`` worker and GC can run on it) and the MCP background thread
that ``MCPClient.start``/``stop`` wait on. ``remove_consumer`` therefore MUST
never block on the lock: it appends the removal to a per-client ``deque`` and
drains it only via ``lock.acquire(blocking=False)``. Every lock holder
(``load_tools``, ``add_consumer``, and the drain) drains pending removals before
releasing the lock, and every path then re-checks the queue with a non-blocking
drain loop so a removal appended during the release window is not stranded.
Deferred removals log a bounded message and swallow their errors, matching
Strands, which already ignores finalizer exceptions.

Holding a ``threading.Lock`` inside the async ``load_tools`` is safe here: a
Strands registry loads a provider via ``strands._async.run_async``, which runs
each provider's ``load_tools`` coroutine with ``asyncio.run`` on a fresh event
loop in its own ``ThreadPoolExecutor`` worker thread. The lock therefore blocks
only that worker thread, never a shared event loop, and concurrent agent builds
run on distinct threads.

Apply this in ``base_specialist`` to the RAW client BEFORE ``mcp_client_transform``
so the guard wrappers (EKS, Cost) sit outside the lock and keep working
unchanged, delegating ``load_tools``/``add_consumer``/``remove_consumer`` through
the serialized wrapper.
"""

# Standard library
import threading
import weakref
from collections import deque
from typing import Any, Deque, Sequence, Tuple

# Third-party packages
from strands.tools.tool_provider import ToolProvider
from strands.types.tools import AgentTool

# Local modules
from utils.logger import logger

# Each pending finalizer removal is the consumer id plus its keyword arguments.
_PendingRemoval = Tuple[Any, dict]


class _ClientState:
    """Per-raw-client serialization state shared by every wrapper of a client.

    Holds the start/stop lock and a queue of removals deferred from finalizers
    that could not take the lock.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # Removals that arrived from Agent.__del__ while the lock was held. They
        # are drained by the next lock holder; this deque is appended to and
        # popped from under no lock, so only the atomic deque operations are used.
        self.pending_removals: "Deque[_PendingRemoval]" = deque()


# One state object per raw client, shared by every wrapper of that client. Keyed
# weakly so the state is released once its client is garbage collected. The
# registry is itself guarded by a module lock because WeakKeyDictionary mutation
# is not atomic across threads.
_states: "weakref.WeakKeyDictionary[Any, _ClientState]" = weakref.WeakKeyDictionary()
_states_guard = threading.Lock()


def _state_for(client: Any) -> _ClientState:
    """Return the shared serialization state for ``client``, creating it once."""
    with _states_guard:
        state = _states.get(client)
        if state is None:
            state = _ClientState()
            _states[client] = state
        return state


class SerializedStartProvider(ToolProvider):
    """Serialize start and last-consumer stop of one shared cached MCP client.

    Delegates the full ``ToolProvider`` lifecycle to the raw client while holding
    a per-client lock across the start window (``load_tools``) and
    ``add_consumer``. ``remove_consumer`` never blocks because it is reached from
    ``Agent.__del__`` on arbitrary threads.
    """

    def __init__(self, delegate: ToolProvider) -> None:
        self._delegate = delegate
        self._state = _state_for(delegate)
        # Exposed for tests and shared-lock assertions.
        self.start_lock = self._state.lock

    def _drain_pending_removals(self) -> None:
        """Run every deferred finalizer removal. Caller must hold the lock.

        Finalizer removals must never raise into their caller (Strands ignores
        finalizer exceptions), so each deferred removal logs and swallows errors.
        """
        state = self._state
        while True:
            try:
                consumer_id, kwargs = state.pending_removals.popleft()
            except IndexError:
                return
            try:
                self._delegate.remove_consumer(consumer_id, **kwargs)
            except Exception as exc:  # noqa: BLE001 - deferred finalizer work
                logger.warning(f"deferred MCP consumer removal failed: {type(exc).__name__}")

    def _drain_after_release(self) -> None:
        """Drain removals queued during the lock-release window, non-blocking.

        A removal appended between a holder's final drain and its ``release`` is
        otherwise stranded until the next holder arrives. Loop so a removal that
        arrives between this check and the release is caught too: re-check the
        queue, take the lock without blocking, drain, release, and repeat while
        the queue is non-empty and the lock is free. If another holder has taken
        the lock it will drain instead, so a failed non-blocking acquire ends the
        loop safely.
        """
        state = self._state
        while state.pending_removals and state.lock.acquire(blocking=False):
            try:
                self._drain_pending_removals()
            finally:
                state.lock.release()

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        # Serialize the delegate's start()-before-flag window so a concurrent
        # caller cannot call start() while this one is still inside it.
        state = self._state
        state.lock.acquire()
        try:
            self._drain_pending_removals()
            try:
                return await self._delegate.load_tools(**kwargs)
            finally:
                self._drain_pending_removals()
        finally:
            state.lock.release()
            self._drain_after_release()

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        # add_consumer is only ever called from ToolRegistry.process_tools, never
        # from a finalizer, so blocking on the lock is safe. Holding it keeps the
        # delegate's consumer set from being erased by a concurrent stop.
        state = self._state
        state.lock.acquire()
        try:
            self._drain_pending_removals()
            self._delegate.add_consumer(consumer_id, **kwargs)
            self._drain_pending_removals()
        finally:
            state.lock.release()
            self._drain_after_release()

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        # Reached from Agent.__del__ → ToolRegistry.cleanup during GC on an
        # arbitrary thread, including the loader worker that already holds this
        # lock and the MCP background thread that start()/stop() wait on. It MUST
        # NOT block: queue the removal and drain only if the lock is free. A lock
        # holder drains the queue before releasing, so a removal queued here is
        # always run.
        state = self._state
        state.pending_removals.append((consumer_id, kwargs))
        self._drain_after_release()


def serialize_mcp_client_start(client: ToolProvider) -> SerializedStartProvider:
    """Wrap a raw cached MCP client so its start and last-consumer stop serialize.

    The returned wrapper shares one lock with every other wrapper of the same raw
    ``client`` object. Apply it to the raw client before any guard transform.
    """
    return SerializedStartProvider(client)
