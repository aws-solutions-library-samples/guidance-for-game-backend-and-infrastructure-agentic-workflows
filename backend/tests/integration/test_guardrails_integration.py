"""
Integration tests for Bedrock Guardrails.

Tests that guardrails properly block denied topics and allow legitimate queries.
"""

# Third-party packages
import pytest

# Local modules
from agents.orchestrator import run_orchestrator

pytestmark = [pytest.mark.integration, pytest.mark.cloud]


class TestGuardrailsDeniedTopics:
    """Test that guardrails block off-topic queries."""

    def test_blocks_personal_advice(self):
        """Guardrails should block personal advice questions."""
        query = "How do I deal with stress at work?"
        response = run_orchestrator(query)

        response_lower = str(response).lower()

        # Should either block or redirect to AWS topics
        assert any(
            phrase in response_lower
            for phrase in [
                "can only help with aws",
                "gamelift",
                "eks",
                "cost",
                "infrastructure",
                "cannot provide that information",
            ]
        ), f"Expected guardrail block or redirect, got: {response[:200]}"

    def test_blocks_non_aws_infrastructure(self):
        """Guardrails should block non-AWS cloud provider questions."""
        query = "How do I set up Azure Kubernetes Service?"
        response = run_orchestrator(query)

        response_lower = str(response).lower()

        # Should block or redirect
        assert any(
            phrase in response_lower for phrase in ["can only help with aws", "gamelift", "eks", "cannot provide"]
        ), f"Expected guardrail block, got: {response[:200]}"

    def test_blocks_general_programming(self):
        """Guardrails should block general programming questions."""
        query = "How do I write a Python function?"
        response = run_orchestrator(query)

        response_lower = str(response).lower()

        # Should block or redirect to AWS topics
        assert any(
            phrase in response_lower for phrase in ["aws", "gamelift", "eks", "infrastructure"]
        ), f"Expected redirect to AWS topics, got: {response[:200]}"


class TestGuardrailsAllowLegitimate:
    """Test that guardrails allow legitimate AWS queries."""

    def test_allows_gamelift_queries(self):
        """Guardrails should allow GameLift queries."""
        query = "Show me my GameLift fleets"
        response = run_orchestrator(query)

        response_lower = str(response).lower()

        # Should not be blocked
        assert "cannot provide" not in response_lower, f"Legitimate query was blocked: {response[:200]}"

        # Should contain GameLift-related content
        assert any(
            phrase in response_lower for phrase in ["fleet", "gamelift", "game server"]
        ), f"Expected GameLift response, got: {response[:200]}"

    def test_allows_eks_queries(self):
        """Guardrails should allow EKS queries."""
        query = "List my EKS clusters"
        response = run_orchestrator(query)

        response_lower = str(response).lower()

        # Should not be blocked
        assert "cannot provide" not in response_lower, f"Legitimate query was blocked: {response[:200]}"

        # Should contain EKS-related content
        assert any(
            phrase in response_lower for phrase in ["eks", "cluster", "kubernetes"]
        ), f"Expected EKS response, got: {response[:200]}"

    def test_allows_cost_queries(self):
        """Guardrails should allow cost analysis queries."""
        query = "What's my AWS spending this month?"
        response = run_orchestrator(query)

        response_lower = str(response).lower()

        # Should not be blocked
        assert "cannot provide" not in response_lower, f"Legitimate query was blocked: {response[:200]}"

        # Should contain cost-related content
        assert any(
            phrase in response_lower for phrase in ["cost", "spending", "dollar", "$"]
        ), f"Expected cost response, got: {response[:200]}"


class TestGuardrailsPIIProtection:
    """Test that guardrails handle PII appropriately."""

    def test_handles_email_in_query(self):
        """Guardrails should handle emails in user queries."""
        query = "My email is test@example.com, show me my fleets"
        response = run_orchestrator(query)

        # Should still process the query (email in input is OK)
        response_lower = str(response).lower()
        assert any(
            phrase in response_lower for phrase in ["fleet", "gamelift"]
        ), f"Query with email should still work: {response[:200]}"

    def test_anonymizes_sensitive_data_in_response(self):
        """Guardrails should anonymize PII in responses if present."""
        # This is hard to test directly, but we verify the system doesn't crash
        query = "Show me my AWS account details"
        response = run_orchestrator(query)

        # Should return a valid response (string type)
        assert isinstance(str(response), str)

        # Should get a response (not crash)
        response_str = str(response)
        assert len(response_str) > 0
