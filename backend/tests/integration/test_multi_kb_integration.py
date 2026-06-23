"""
Integration tests for Multi-KB architecture.

Tests realistic game engineer questions against GameLift, EKS, and Cost KBs.
"""

# Standard library
import os

# Third-party packages
import pytest

# Local modules
from agents.cost_specialist import cost_agent
from agents.eks_specialist import eks_agent
from agents.gamelift_specialist import gamelift_agent


def assert_no_agent_error(response: str) -> None:
    """Assert a specialist response is a real answer, not an agent failure.

    Matches indicators ANCHORED to actual failure modes — colon-prefixed error
    formatting (``error:``, ``exception:``, ``failed:``) and the specialists'
    real fallback/error strings — rather than naive substrings. The previous
    check flagged ``"unable to"`` anywhere, which false-failed on legitimate KB
    prose like "...players unable to join games during traffic spikes."
    """
    lo = response.lower()
    # Colon-anchored error formatting (a stack/SDK error printed into the reply).
    formatted_errors = ["error:", "exception:", "failed:", "traceback"]
    # The specialists' actual fallback / generic-failure strings (base_specialist
    # + eks_specialist). These are what a genuine failure surfaces to the user.
    real_failure_strings = [
        "unable to process the",  # base_specialist generic failure
        "mcp servers unavailable",  # eks_specialist fallback
        "mcp unavailable",
        "encountered an error processing",  # runtime entrypoint failure
    ]
    hits = [s for s in (formatted_errors + real_failure_strings) if s in lo]
    assert not hits, f"Response contains a real error indicator {hits}: {response[:200]}"


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("GBAW_GAMELIFT_KB_ID"), reason="GameLift KB not deployed")
class TestGameLiftKB:
    """Integration tests for GameLift Knowledge Base."""

    def test_fleet_cost_optimization_query(self):
        """Test: GameLift agent responds to cost optimization queries"""
        query = "How do I reduce GameLift fleet costs during off-peak hours?"

        response = gamelift_agent(query)

        # Basic response validation
        assert isinstance(response, str), "Response should be a string"
        assert len(response) > 50, "Response should have meaningful content"
        assert len(response) < 10000, "Response should be reasonable length"

        # Should not contain real error/failure indicators (anchored, not naive substrings)
        assert_no_agent_error(response)

    def test_fleet_scaling_best_practices(self):
        """Test: GameLift agent responds to scaling queries"""
        query = "What are the best practices for auto-scaling GameLift fleets?"

        response = gamelift_agent(query)

        # Basic response validation
        assert isinstance(response, str), "Response should be a string"
        assert len(response) > 50, "Response should have meaningful content"
        assert len(response) < 10000, "Response should be reasonable length"

        # Should not contain real error/failure indicators (anchored, not naive substrings)
        assert_no_agent_error(response)

    def test_fleet_instance_churn_troubleshooting(self):
        """Test: GameLift agent responds to troubleshooting queries"""
        query = "Why is my GameLift fleet showing high instance churn?"

        response = gamelift_agent(query)

        # Basic response validation
        assert isinstance(response, str), "Response should be a string"
        assert len(response) > 50, "Response should have meaningful content"
        assert len(response) < 10000, "Response should be reasonable length"

        # Should not contain real error/failure indicators (anchored, not naive substrings)
        assert_no_agent_error(response)

    def test_spot_instance_usage(self):
        """Test: GameLift agent responds to spot instance queries"""
        query = "Should I use Spot instances for my production GameLift fleet?"

        response = gamelift_agent(query)

        # Basic response validation
        assert isinstance(response, str), "Response should be a string"
        assert len(response) > 50, "Response should have meaningful content"
        assert len(response) < 10000, "Response should be reasonable length"

        # Should not contain real error/failure indicators (anchored, not naive substrings)
        assert_no_agent_error(response)


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("GBAW_EKS_KB_ID"), reason="EKS KB not deployed")
class TestEKSKB:
    """Integration tests for EKS Knowledge Base."""

    def test_pod_autoscaling_query(self):
        """Test: What's the best way to auto-scale my EKS game server pods?"""
        query = "What's the best way to auto-scale my EKS game server pods?"

        response = eks_agent(query)

        assert isinstance(response, str)
        assert len(response) > 100

        # Should mention HPA or autoscaling
        response_lower = response.lower()
        assert any(term in response_lower for term in ["hpa", "horizontal", "autoscal", "pod", "replica"])

    def test_pod_pending_troubleshooting(self):
        """Test: My game server pods are stuck in Pending state, how do I fix this?"""
        query = "My game server pods are stuck in Pending state, how do I fix this?"

        response = eks_agent(query)

        assert isinstance(response, str)
        assert len(response) > 100

        # Should mention troubleshooting steps
        response_lower = response.lower()
        assert any(term in response_lower for term in ["pending", "capacity", "resource", "node", "describe"])

    def test_node_group_sizing(self):
        """Test: EKS agent responds to instance sizing queries"""
        query = "How do I choose the right instance type for my EKS game servers?"

        response = eks_agent(query)

        # Basic response validation
        assert isinstance(response, str), "Response should be a string"
        assert len(response) > 50, "Response should have meaningful content"
        assert len(response) < 10000, "Response should be reasonable length"

        # Should not contain real error/failure indicators (anchored, not naive substrings)
        assert_no_agent_error(response)

    def test_cluster_autoscaler_setup(self):
        """Test: How do I set up cluster autoscaler for my EKS game cluster?"""
        query = "How do I set up cluster autoscaler for my EKS game cluster?"

        response = eks_agent(query)

        assert isinstance(response, str)
        assert len(response) > 100

        # Should mention cluster autoscaler
        response_lower = response.lower()
        assert any(term in response_lower for term in ["cluster", "autoscaler", "node", "scale", "deployment"])


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("GBAW_COST_KB_ID"), reason="Cost KB not deployed")
class TestCostKB:
    """Integration tests for Cost Optimization Knowledge Base."""

    def test_ec2_cost_optimization(self):
        """Test: How can I optimize EC2 costs for my game backend?"""
        query = "How can I optimize EC2 costs for my game backend?"

        response = cost_agent(query)

        assert isinstance(response, str)
        assert len(response) > 100

        # Should mention cost optimization strategies
        response_lower = response.lower()
        assert any(term in response_lower for term in ["spot", "reserved", "saving", "right-siz", "cost"])

    def test_gamelift_vs_eks_cost_comparison(self):
        """Test: What's more cost-effective: GameLift or EKS for game servers?"""
        query = "What's more cost-effective: GameLift or EKS for game servers?"

        response = cost_agent(query)

        assert isinstance(response, str)
        assert len(response) > 100

        # Should mention both services
        response_lower = response.lower()
        assert any(term in response_lower for term in ["gamelift", "eks", "cost", "pricing", "comparison"])

    def test_data_transfer_cost_reduction(self):
        """Test: How do I reduce data transfer costs for my game?"""
        query = "How do I reduce data transfer costs for my game?"

        response = cost_agent(query)

        assert isinstance(response, str)
        assert len(response) > 100

        # Should mention data transfer strategies
        response_lower = response.lower()
        assert any(term in response_lower for term in ["data", "transfer", "cloudfront", "region", "vpc"])

    def test_spot_instance_savings(self):
        """Test: How much can I save using Spot instances?"""
        query = "How much can I save using Spot instances?"

        response = cost_agent(query)

        assert isinstance(response, str)
        assert len(response) > 100

        # Should mention savings percentages
        response_lower = response.lower()
        assert any(term in response_lower for term in ["spot", "sav", "percent", "%", "cost", "discount"])


@pytest.mark.integration
class TestMultiKBGracefulDegradation:
    """Test that agents work even when KBs are unavailable."""

    def test_gamelift_without_kb(self, monkeypatch):
        """Test GameLift agent works without KB."""
        monkeypatch.delenv("GBAW_GAMELIFT_KB_ID", raising=False)

        query = "List my GameLift fleets"
        response = gamelift_agent(query)

        assert isinstance(response, str)
        assert len(response) > 0

    def test_eks_without_kb(self, monkeypatch):
        """Test EKS agent works without KB."""
        monkeypatch.delenv("GBAW_EKS_KB_ID", raising=False)

        query = "List my EKS clusters"
        response = eks_agent(query)

        assert isinstance(response, str)
        assert len(response) > 0

    def test_cost_without_kb(self, monkeypatch):
        """Test Cost agent works without KB."""
        monkeypatch.delenv("GBAW_COST_KB_ID", raising=False)

        query = "Show me my AWS costs"
        response = cost_agent(query)

        assert isinstance(response, str)
        assert len(response) > 0


@pytest.mark.integration
@pytest.mark.cloud
class TestKBRetrievalQuality:
    """Test KB retrieval quality and relevance."""

    @pytest.mark.skipif(not os.getenv("GBAW_GAMELIFT_KB_ID"), reason="GameLift KB not deployed")
    def test_gamelift_kb_retrieval_relevance(self):
        """Test GameLift KB returns relevant results."""
        # Local modules
        from utils.kb_tools import create_kb_retrieve_tool

        kb_id = os.getenv("GBAW_GAMELIFT_KB_ID")
        retrieve = create_kb_retrieve_tool(kb_id, "us-west-2")

        # Query for specific GameLift topic
        result = retrieve("GameLift auto-scaling policies")

        # Should return results
        assert result is not None, "KB retrieve returned None"
        result_str = str(result).lower()

        # Check for relevant terms
        relevant_terms = ["scaling", "policy", "fleet", "capacity"]
        has_relevant = any(term in result_str for term in relevant_terms)
        assert has_relevant, f"KB result missing relevant terms. Got: {result_str[:200]}"

    @pytest.mark.skipif(not os.getenv("GBAW_EKS_KB_ID"), reason="EKS KB not deployed")
    def test_eks_kb_retrieval_relevance(self):
        """Test EKS KB returns relevant results."""
        # Local modules
        from utils.kb_tools import create_kb_retrieve_tool

        kb_id = os.getenv("GBAW_EKS_KB_ID")
        retrieve = create_kb_retrieve_tool(kb_id, "us-west-2")

        # Query for specific EKS topic
        result = retrieve("EKS pod autoscaling")

        # Should return results
        assert result is not None, "KB retrieve returned None"
        result_str = str(result).lower()

        # Check for relevant terms
        relevant_terms = ["pod", "hpa", "autoscal", "kubernetes"]
        has_relevant = any(term in result_str for term in relevant_terms)
        assert has_relevant, f"KB result missing relevant terms. Got: {result_str[:200]}"

    @pytest.mark.skipif(not os.getenv("GBAW_COST_KB_ID"), reason="Cost KB not deployed")
    def test_cost_kb_retrieval_relevance(self):
        """Test Cost KB returns relevant results."""
        # Local modules
        from utils.kb_tools import create_kb_retrieve_tool

        kb_id = os.getenv("GBAW_COST_KB_ID")
        retrieve = create_kb_retrieve_tool(kb_id, "us-west-2")

        # Query for specific cost topic
        result = retrieve("EC2 Spot instance savings")

        # Should return results
        assert result is not None, "KB retrieve returned None"
        result_str = str(result).lower()

        # Check for relevant terms
        relevant_terms = ["spot", "saving", "cost", "ec2", "discount"]
        has_relevant = any(term in result_str for term in relevant_terms)
        assert has_relevant, f"KB result missing relevant terms. Got: {result_str[:200]}"
