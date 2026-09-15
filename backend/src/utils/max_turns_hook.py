"""
Max-turns guardrail hook for Strands agents.

Implements a BeforeToolCallEvent hook that counts event-loop cycles and cancels
further tool calls once the configured limit is reached.  This prevents runaway
reasoning loops and bounds per-request cost.

Addresses Well-Architected GenAI Lens findings:
  - Cost Optimization 3.5  (stopping conditions for agent workflows)
  - Reliability 5.3        (remediation for loops, retries, failures)
"""

# Standard library
from typing import Any

# Third-party packages
from strands.hooks.events import BeforeToolCallEvent
from strands.hooks.registry import HookProvider, HookRegistry

# Local modules
from utils.logger import logger


class MaxTurnsHook(HookProvider):
    """Cancel tool execution once the agent exceeds *max_turns* cycles."""

    def __init__(self, max_turns: int):
        self.max_turns = max_turns
        self._cycle_count = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._check_limit)

    # ------------------------------------------------------------------
    def _check_limit(self, event: BeforeToolCallEvent, **kwargs: Any) -> None:
        self._cycle_count += 1
        if self._cycle_count > self.max_turns:
            logger.warning(
                f"⚠️ Agent exceeded max_turns ({self.max_turns}). "
                f"Cancelling tool call: {event.tool_use.get('name', 'unknown')}"
            )
            event.cancel_tool = (
                f"Maximum reasoning turns ({self.max_turns}) exceeded. "
                "Please provide your best answer with the information gathered so far."
            )
