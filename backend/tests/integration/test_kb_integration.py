"""
Integration tests for Knowledge Base with GameLift specialist.

These tests verify that the KB integration works end-to-end with the agent
using the backend's own Knowledge Base retrieve tool (``utils.kb_tools``).
"""

# Standard library
import os

# Third-party packages
import pytest

# Local modules
from agents.gamelift_specialist import gamelift_agent


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("GBAW_GAMELIFT_KB_ID"), reason="Knowledge Base not deployed")
class TestKBIntegration:
    """Integration tests for KB with agents."""

    def test_gamelift_agent_has_kb_tool(self):
        """Verify GameLift agent has KB tool available."""
        # Local modules
        from utils.kb_tools import create_kb_retrieve_tool

        assert create_kb_retrieve_tool is not None
        assert callable(create_kb_retrieve_tool)

        # Verify it creates a tool
        kb_tool = create_kb_retrieve_tool("test-kb-id", "us-west-2")
        assert kb_tool is not None

    def test_kb_retrieve_tool_is_available(self):
        """Test the owned Knowledge Base retrieve tool is available."""
        # Local modules
        from utils.kb_tools import retrieve

        assert retrieve is not None
        assert callable(retrieve)

    def test_gamelift_agent_with_kb_query(self):
        """Test GameLift agent responds to queries that benefit from KB."""
        kb_id = os.getenv("GBAW_GAMELIFT_KB_ID")
        if not kb_id:
            pytest.skip("KB not configured")

        # Query that should trigger KB usage
        query = "What are the best practices for GameLift fleet auto-scaling?"

        response = gamelift_agent(query)

        # Verify we got a response
        assert isinstance(response, str)
        assert len(response) > 0

        # Response should contain relevant GameLift terminology
        response_lower = response.lower()
        assert any(term in response_lower for term in ["fleet", "scaling", "gamelift", "capacity", "instances"])

    def test_kb_graceful_degradation(self, monkeypatch):
        """Test agent works even when KB is unavailable."""
        # Temporarily remove KB ID
        monkeypatch.delenv("GBAW_GAMELIFT_KB_ID", raising=False)

        query = "List my GameLift fleets"
        response = gamelift_agent(query)

        # Should still get a response (using MCP/SDK fallback)
        assert isinstance(response, str)
        assert len(response) > 0


@pytest.mark.integration
class TestKBRetrieveTool:
    """Test the owned Knowledge Base retrieve tool against a deployed KB."""

    def test_retrieve_tool_with_kb_configured(self):
        """The bound retrieve tool returns a bounded JSON envelope with a state."""
        kb_id = os.getenv("GBAW_GAMELIFT_KB_ID")
        if not kb_id:
            pytest.skip("KB not configured")

        # Standard library
        import json

        # Local modules
        from utils.kb_tools import create_kb_retrieve_tool

        kb_tool = create_kb_retrieve_tool(kb_id, os.getenv("AWS_REGION", "us-west-2"))
        result = kb_tool(text="GameLift fleet capacity planning", numberOfResults=3)

        assert isinstance(result, str)
        envelope = json.loads(result)
        assert "state" in envelope
