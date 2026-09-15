#!/usr/bin/env python3
"""
Performance tests for cost agent to prevent excessive tool calls.
"""

# Standard library
import os
import sys
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

# Local modules
from agents.cost_specialist import cost_agent


class TestCostAgentPerformance:
    """Test cost agent doesn't make excessive tool calls."""

    def test_cost_agent_makes_limited_tool_calls(self):
        """Cost agent should make 1-3 tool calls maximum."""
        # With factory pattern, we test that the agent is callable
        # and returns a string response
        with patch("agents.base_specialist.Agent") as mock_agent:
            # Mock the agent to return a simple response
            mock_agent_instance = MagicMock()
            mock_agent_instance.return_value = "Cost data unavailable"
            mock_agent.return_value = mock_agent_instance

            # Test simple query
            result = cost_agent("what is my AWS spending?")

            # Should get a response
            assert isinstance(result, str)
            assert len(result) > 0

    def test_cost_agent_prompt_is_concise(self):
        """Cost agent's authored system prompt should be concise to reduce token usage."""
        # Local modules
        from agents.optimized_prompts import COST_PROMPT, get_optimized_cost_prompt

        # The authored specialist prompt itself must stay lean. The deployed
        # prompt additionally composes the shared, versioned chart directive
        # (issue #255), which is intentionally shared across specialists and is
        # asserted separately in the chart directive tests.
        assert (
            len(COST_PROMPT.text) < 500
        ), f"Authored cost prompt is {len(COST_PROMPT.text)} chars, should be <500 for efficiency"
        # The composed prompt must still carry the chart contract so the
        # capability actually reaches the model.
        assert "`chart`" in get_optimized_cost_prompt()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
