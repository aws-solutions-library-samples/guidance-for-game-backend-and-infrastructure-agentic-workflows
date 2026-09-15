"""
Wall-clock timeout hook for Strands agents.

Implements a BeforeToolCallEvent hook that checks elapsed wall-clock time and
cancels further tool calls once the configured timeout is exceeded. This catches
scenarios that max_turns alone cannot: hung Bedrock calls, slow MCP servers,
or expensive multi-step reasoning.

Addresses Guardian Security Design Evaluation finding:
  - "Insufficient timeout configurations" (Service integrations)
"""

# Standard library
import time
from typing import Any

# Third-party packages
from strands.hooks.events import BeforeToolCallEvent
from strands.hooks.registry import HookProvider, HookRegistry

# Local modules
from utils.logger import logger


class WallClockTimeoutHook(HookProvider):
    """Cancel tool execution when wall-clock timeout is exceeded."""

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        self._start_time = time.monotonic()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._check_timeout)

    def _check_timeout(self, event: BeforeToolCallEvent, **kwargs: Any) -> None:
        elapsed = time.monotonic() - self._start_time
        if elapsed > self.timeout_seconds:
            tool_name = event.tool_use.get("name", "unknown")
            logger.warning(
                f"⚠️ Agent exceeded wall-clock timeout ({self.timeout_seconds}s, "
                f"elapsed: {elapsed:.1f}s). Cancelling tool call: {tool_name}"
            )
            event.cancel_tool = (
                f"Wall-clock timeout ({self.timeout_seconds}s) exceeded after {elapsed:.0f}s. "
                "Please provide your best answer with the information gathered so far."
            )
