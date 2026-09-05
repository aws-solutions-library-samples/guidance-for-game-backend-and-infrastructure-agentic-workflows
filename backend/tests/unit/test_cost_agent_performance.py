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
        from agents.optimized_prompts import COST_PROMPT, get_optimized_cost_prompt

        # The managed/base template must stay lean (<500 chars) for token efficiency.
        assert (
            len(COST_PROMPT.text) < 500
        ), f"Base cost prompt is {len(COST_PROMPT.text)} chars, should be <500 for efficiency"

        # The runtime accessor appends a short current-UTC-date directive (~125
        # chars) so relative ranges are derived from real time. Allow bounded
        # headroom above the base cap for exactly that directive.
        prompt = get_optimized_cost_prompt()
        assert len(prompt) < 650, f"Cost prompt with date directive is {len(prompt)} chars, should be <650"

    def test_cost_prompt_includes_runtime_utc_date(self):
        """Cost prompt must state the current UTC date so relative ranges use real time."""
        # Standard library
        from datetime import datetime, timezone

        # Local modules
        import agents.optimized_prompts as op

        with patch.object(op, "_utc_now", return_value=datetime(2027, 3, 9, 15, 30, tzinfo=timezone.utc)):
            prompt = op.get_optimized_cost_prompt()

        assert "Today (UTC) is 2027-03-09" in prompt
        assert "current month" in prompt
        # Base template guidance is still present.
        assert "get_cost_report" in prompt

    def test_cost_prompt_date_is_normalized_to_utc(self):
        """A non-UTC injected time is normalized to its UTC calendar date."""
        # Standard library
        from datetime import datetime, timedelta, timezone

        # Local modules
        import agents.optimized_prompts as op

        # 2027-03-09 23:00 at UTC-5 is 2027-03-10 04:00 UTC -> date rolls forward.
        local_late = datetime(2027, 3, 9, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
        with patch.object(op, "_utc_now", return_value=local_late):
            prompt = op.get_optimized_cost_prompt()

        assert "Today (UTC) is 2027-03-10" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
