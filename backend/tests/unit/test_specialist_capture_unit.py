"""Unit tests for request-scoped specialist output capture and its wiring."""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from agents.specialist_capture import (
    begin_specialist_capture,
    finish_specialist_capture,
    record_specialist_output,
)

pytestmark = pytest.mark.unit


class TestSpecialistCapture:
    def test_records_and_returns_in_fixed_service_order(self):
        capture = begin_specialist_capture()
        try:
            record_specialist_output("GameLift", "gamelift text")
            record_specialist_output("EKS", "eks text")
        finally:
            recorded = finish_specialist_capture(capture)

        assert recorded == [("GameLift", "gamelift text"), ("EKS", "eks text")]

    def test_completion_order_reversal_does_not_change_composition_order(self):
        """A faster EKS recording before a slower GameLift still composes GameLift first."""
        capture = begin_specialist_capture()
        try:
            # Simulate EKS completing (recording) before GameLift.
            record_specialist_output("EKS", "eks text")
            record_specialist_output("GameLift", "gamelift text")
        finally:
            recorded = finish_specialist_capture(capture)

        assert recorded == [("GameLift", "gamelift text"), ("EKS", "eks text")]

    def test_fixed_order_places_cost_last_and_unknown_services_sorted(self):
        capture = begin_specialist_capture()
        try:
            record_specialist_output("Zeta", "zeta text")
            record_specialist_output("Cost", "cost text")
            record_specialist_output("Alpha", "alpha text")
            record_specialist_output("EKS", "eks text")
            record_specialist_output("GameLift", "gamelift text")
        finally:
            recorded = finish_specialist_capture(capture)

        assert [name for name, _ in recorded] == ["GameLift", "EKS", "Cost", "Alpha", "Zeta"]

    def test_duplicate_service_call_is_last_write_wins(self):
        capture = begin_specialist_capture()
        try:
            record_specialist_output("EKS", "first eks")
            record_specialist_output("eks", "second eks")  # different casing, same service
        finally:
            recorded = finish_specialist_capture(capture)

        # One section for the service, holding the most recent output.
        assert recorded == [("eks", "second eks")]

    def test_record_without_active_capture_is_noop(self):
        # Must not raise and must not leak into the next capture.
        record_specialist_output("GameLift", "orphan text")

        capture = begin_specialist_capture()
        recorded = finish_specialist_capture(capture)
        assert recorded == []

    def test_finish_is_idempotent_and_cleans_state(self):
        capture = begin_specialist_capture()
        record_specialist_output("EKS", "eks text")
        first = finish_specialist_capture(capture)
        # A genuine second finish for the same handle returns nothing (state
        # already popped) and does not raise.
        second = finish_specialist_capture(capture)

        assert first == [("EKS", "eks text")]
        assert second == []

    def test_concurrent_captures_are_isolated_by_context(self):
        """Two threads with independent captures do not observe each other's outputs."""
        # Standard library
        import contextvars
        import threading

        results: dict[str, list[tuple[str, str]]] = {}
        started = threading.Barrier(2)

        def worker(tag: str, service: str, text: str) -> None:
            capture = begin_specialist_capture()
            started.wait()
            record_specialist_output(service, text)
            results[tag] = finish_specialist_capture(capture)

        # Each thread runs in its own copied context so the ContextVar capture id
        # is isolated per request.
        t1 = threading.Thread(target=lambda: contextvars.copy_context().run(worker, "a", "GameLift", "gl-a"))
        t2 = threading.Thread(target=lambda: contextvars.copy_context().run(worker, "b", "EKS", "eks-b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["a"] == [("GameLift", "gl-a")]
        assert results["b"] == [("EKS", "eks-b")]


class TestBaseSpecialistRecording:
    def test_specialist_output_recorded_into_active_capture(self):
        """A specialist created via the factory records its finalized output."""
        # Local modules
        from agents.base_specialist import create_specialist_agent

        with (
            patch("agents.base_specialist.Agent") as mock_agent,
            patch("agents.base_specialist.create_specialist_bedrock_model", return_value=MagicMock()),
        ):
            instance = MagicMock()
            instance.return_value = "## TestSvc\n\noperational detail"
            mock_agent.return_value = instance

            agent = create_specialist_agent(
                service_name="TestSvc",
                emoji="🧪",
                mcp_server_names=None,
                kb_id=None,
                prompt_fn=lambda: "system prompt",
            )

            capture = begin_specialist_capture()
            try:
                result = agent("status?")
            finally:
                recorded = finish_specialist_capture(capture)

        assert result == "## TestSvc\n\noperational detail"
        assert recorded == [("TestSvc", "## TestSvc\n\noperational detail")]

    def test_specialist_failure_fallback_is_recorded(self):
        """When the model raises and a fallback exists, the fallback text is recorded."""
        # Local modules
        from agents.base_specialist import create_specialist_agent

        with (
            patch("agents.base_specialist.Agent") as mock_agent,
            patch("agents.base_specialist.create_specialist_bedrock_model", return_value=MagicMock()),
        ):
            instance = MagicMock()
            instance.side_effect = RuntimeError("model blew up")
            mock_agent.return_value = instance

            agent = create_specialist_agent(
                service_name="TestSvc",
                emoji="🧪",
                mcp_server_names=None,
                kb_id=None,
                prompt_fn=lambda: "system prompt",
                fallback_fn=lambda region: f"fallback for {region}",
            )

            capture = begin_specialist_capture()
            try:
                result = agent("status?")
            finally:
                recorded = finish_specialist_capture(capture)

        assert result.startswith("fallback for ")
        assert recorded == [("TestSvc", result)]


class TestRealStrandsToolBoundary:
    """Exercise the actual Strands tool dispatch boundary (``asyncio.to_thread``).

    Strands runs a synchronous tool function via
    ``await asyncio.to_thread(self._tool_func, ...)`` inside
    ``DecoratedFunctionTool.stream`` (strands/tools/decorator.py). ``to_thread``
    copies the active :mod:`contextvars` context into the worker thread, which is
    the exact mechanism the request-scoped capture relies on. These tests drive
    the real decorated tool through ``stream`` — not a plain ``__call__`` — so a
    regression in contextvar propagation (or a switch to a non-context-copying
    dispatch) would fail here.
    """

    @staticmethod
    def _run_tool_stream(decorated_tool, query: str) -> str:
        # Standard library
        import asyncio

        async def drive() -> None:
            tool_use = {"toolUseId": "tool-1", "input": {"query": query}}
            async for _event in decorated_tool.stream(tool_use, {}):
                pass

        asyncio.run(drive())
        # The tool result is a side effect (record_specialist_output); the return
        # value is asserted via the capture below.
        return query

    def _make_specialist(self, service_name: str, output: str):
        # Local modules
        from agents.base_specialist import create_specialist_agent

        instance = MagicMock()
        instance.return_value = output
        patcher_agent = patch("agents.base_specialist.Agent", return_value=instance)
        patcher_model = patch(
            "agents.base_specialist.create_specialist_bedrock_model",
            return_value=MagicMock(),
        )
        patcher_agent.start()
        patcher_model.start()
        self.addfinalizer_stack.extend([patcher_agent.stop, patcher_model.stop])
        return create_specialist_agent(
            service_name=service_name,
            emoji="🧪",
            mcp_server_names=None,
            kb_id=None,
            prompt_fn=lambda: "system prompt",
        )

    @pytest.fixture(autouse=True)
    def _finalizers(self):
        self.addfinalizer_stack: list = []
        yield
        for stop in reversed(self.addfinalizer_stack):
            stop()

    def test_output_recorded_across_asyncio_to_thread_worker(self):
        """A tool run through the real ``stream``/``to_thread`` path is captured."""
        specialist = self._make_specialist("TestSvc", "## TestSvc\n\nworker-thread detail")

        capture = begin_specialist_capture()
        try:
            self._run_tool_stream(specialist, "status?")
        finally:
            recorded = finish_specialist_capture(capture)

        # Proves the capture id set in the calling context reached the worker
        # thread that executed the synchronous tool function.
        assert recorded == [("TestSvc", "## TestSvc\n\nworker-thread detail")]

    def test_two_specialists_through_real_boundary_compose_in_fixed_order(self):
        """EKS driven before GameLift through the real boundary still composes GameLift first."""
        eks = self._make_specialist("EKS", "## EKS\n\neks detail")
        gamelift = self._make_specialist("GameLift", "## GameLift\n\ngamelift detail")

        capture = begin_specialist_capture()
        try:
            # Drive EKS first, GameLift second — completion order is EKS→GameLift.
            self._run_tool_stream(eks, "clusters?")
            self._run_tool_stream(gamelift, "fleets?")
        finally:
            recorded = finish_specialist_capture(capture)

        assert [name for name, _ in recorded] == ["GameLift", "EKS"]


class TestConcurrentBarrierCapture:
    """True-threaded concurrency with a barrier forcing interleaved recording."""

    def test_barrier_interleaved_completion_preserves_fixed_order(self):
        # Standard library
        import contextvars
        import threading

        result_holder: dict[str, list] = {}
        # Both worker threads reach the barrier before either records, so their
        # record order is genuinely interleaved/nondeterministic.
        both_ready = threading.Barrier(2)

        def run_request() -> None:
            capture = begin_specialist_capture()

            def record_service(service: str, text: str) -> None:
                both_ready.wait()
                record_specialist_output(service, text)

            # Two worker threads sharing this request's capture id (mirrors
            # parallel specialist tool calls). Each thread gets its OWN context
            # copy — a single Context object cannot be entered by two threads —
            # but both copies carry the same capture id set by begin() above, so
            # they write into the same request-scoped bucket.
            ctx_eks = contextvars.copy_context()
            ctx_gl = contextvars.copy_context()
            t_eks = threading.Thread(target=lambda: ctx_eks.run(record_service, "EKS", "eks text"))
            t_gl = threading.Thread(target=lambda: ctx_gl.run(record_service, "GameLift", "gl text"))
            # Start EKS first to bias completion toward EKS-before-GameLift.
            t_eks.start()
            t_gl.start()
            t_eks.join()
            t_gl.join()
            result_holder["recorded"] = finish_specialist_capture(capture)

        run_request()

        # Regardless of which worker won the barrier race, fixed order holds.
        assert result_holder["recorded"] == [("GameLift", "gl text"), ("EKS", "eks text")]

    def test_many_concurrent_requests_stay_isolated(self):
        # Standard library
        import contextvars
        import threading

        request_count = 12
        start = threading.Barrier(request_count)
        results: dict[int, list] = {}
        lock = threading.Lock()

        def worker(i: int) -> None:
            capture = begin_specialist_capture()
            start.wait()  # maximize overlap across requests
            record_specialist_output("EKS", f"eks-{i}")
            record_specialist_output("GameLift", f"gl-{i}")
            recorded = finish_specialist_capture(capture)
            with lock:
                results[i] = recorded

        threads = [
            threading.Thread(target=lambda i=i: contextvars.copy_context().run(worker, i)) for i in range(request_count)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each request sees only its own two sections, in fixed order.
        for i in range(request_count):
            assert results[i] == [("GameLift", f"gl-{i}"), ("EKS", f"eks-{i}")]


class TestSpecialistSetupParticipation:
    def test_factory_failure_still_records_cost_participation(self):
        """A setup failure before the agent block must remain visible to orchestration."""
        # Local modules
        from agents.base_specialist import create_specialist_agent

        def failing_factory():
            raise RuntimeError("tool setup failed")

        agent = create_specialist_agent(
            service_name="Cost",
            emoji="💰",
            mcp_server_names=None,
            kb_id=None,
            prompt_fn=lambda: "system prompt",
            additional_tools_factory=failing_factory,
        )

        capture = begin_specialist_capture()
        try:
            with pytest.raises(RuntimeError, match="tool setup failed"):
                agent("Give cost optimization advice")
        finally:
            recorded = finish_specialist_capture(capture)

        assert recorded == [("Cost", "")]
