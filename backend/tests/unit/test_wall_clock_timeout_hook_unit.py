#!/usr/bin/env python3
"""Unit tests for WallClockTimeoutHook."""

# Standard library
import os
import sys
import time
from unittest.mock import MagicMock

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# Local modules
from utils.wall_clock_timeout_hook import WallClockTimeoutHook


def _make_event(tool_name="test_tool"):
    """Create a mock BeforeToolCallEvent."""
    event = MagicMock()
    event.tool_use = {"name": tool_name}
    event.cancel_tool = None
    return event


class TestWallClockTimeoutHook:
    """Test WallClockTimeoutHook behavior."""

    def test_allows_tool_call_within_timeout(self):
        """Tool calls within timeout should not be cancelled."""
        hook = WallClockTimeoutHook(timeout_seconds=60)
        event = _make_event()

        hook._check_timeout(event)

        assert event.cancel_tool is None

    def test_cancels_tool_call_after_timeout(self):
        """Tool calls after timeout should be cancelled."""
        hook = WallClockTimeoutHook(timeout_seconds=0.01)
        # Wait for timeout to expire
        time.sleep(0.02)

        event = _make_event("slow_tool")
        hook._check_timeout(event)

        assert event.cancel_tool is not None
        assert "Wall-clock timeout" in event.cancel_tool
        assert "exceeded" in event.cancel_tool

    def test_cancel_message_includes_timeout_value(self):
        """Cancel message should include the configured timeout."""
        hook = WallClockTimeoutHook(timeout_seconds=90)
        hook._start_time = time.monotonic() - 100  # Simulate 100s elapsed

        event = _make_event()
        hook._check_timeout(event)

        assert "90" in event.cancel_tool

    def test_multiple_calls_within_timeout(self):
        """Multiple tool calls within timeout should all be allowed."""
        hook = WallClockTimeoutHook(timeout_seconds=60)

        for i in range(10):
            event = _make_event(f"tool_{i}")
            hook._check_timeout(event)
            assert event.cancel_tool is None

    def test_registers_before_tool_call_hook(self):
        """Should register callback for BeforeToolCallEvent."""
        hook = WallClockTimeoutHook(timeout_seconds=60)
        mock_hooks = MagicMock()

        hook.register_hooks(mock_hooks)

        mock_hooks.add_callback.assert_called_once()
        # Verify the callback is _check_timeout
        args = mock_hooks.add_callback.call_args
        assert args[0][1] == hook._check_timeout

    def test_uses_monotonic_clock(self):
        """Should use time.monotonic for accurate elapsed time measurement."""
        before = time.monotonic()
        hook = WallClockTimeoutHook(timeout_seconds=60)
        after = time.monotonic()

        assert before <= hook._start_time <= after
