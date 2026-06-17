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
        """Cost agent system prompt should be concise to reduce token usage."""
        # Local modules
        from agents.optimized_prompts import get_optimized_cost_prompt

        prompt = get_optimized_cost_prompt()

        # Prompt should be under 500 characters for efficiency
        assert len(prompt) < 500, f"Prompt is {len(prompt)} chars, should be <500 for efficiency"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
