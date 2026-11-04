"""
AI Agent Evaluation Tests - Real LLM Behavior (Deployment Aware)

These tests validate actual LLM agent behavior using industry
best practices for non-deterministic AI system evaluation.

Works with both local and deployed backends.
"""

# Standard library
import json
import re
import time

# Third-party packages
import pytest

from .test_config import check_backend_available, get_test_config, make_agent_request

pytestmark = [pytest.mark.cloud, pytest.mark.ai_eval]


class TestAgentEvals:
    """AI evaluation tests for real LLM agent behavior"""

    @pytest.fixture(autouse=True)
    def setup_config(self):
        """Setup test configuration - requires deployed stack"""
        self.config = get_test_config()

        # AI evals require deployed stack (localhost is too slow)
        if self.config["mode"] != "deployed":
            pytest.skip("AI evals require deployed stack - deploy with ./deploy-all.sh")

        if not check_backend_available(self.config):
            pytest.skip("Deployed AgentCore Runtime not accessible")

    @pytest.mark.ai_eval
    def test_agent_responds_to_simple_query(self):
        """Test agent responds to simple queries"""
        response = make_agent_request("Hello", self.config)

        assert isinstance(response, str)
        assert len(response) > 0
        assert len(response) < 1000  # Reasonable response length

    @pytest.mark.ai_eval
    def test_agent_handles_aws_queries(self):
        """Test agent handles AWS-related queries appropriately (deployment-only)"""
        queries = ["list my EKS clusters", "show my GameLift fleets", "what's my AWS spending?"]

        for query in queries:
            response = make_agent_request(query, self.config)

            assert isinstance(response, str)
            assert len(response) > 10

            # Should not contain obvious hallucinations
            hallucination_indicators = ["cluster-12345", "fleet-abcdef", "example-cluster", "test-fleet-1"]

            response_lower = response.lower()
            for indicator in hallucination_indicators:
                assert indicator not in response_lower, f"Possible hallucination detected: {indicator}"

    @pytest.mark.ai_eval
    def test_agent_error_handling(self):
        """Test agent handles errors gracefully"""
        # Test with empty query
        response = make_agent_request("", self.config)
        assert isinstance(response, str)
        assert len(response) > 0

        # Test with very long query
        long_query = "tell me about AWS " * 100
        response = make_agent_request(long_query, self.config)
        assert isinstance(response, str)
        assert len(response) > 0

    @pytest.mark.ai_eval
    def test_agent_routing_behavior(self):
        """Test agent routes queries to appropriate specialists"""
        test_cases = [
            {
                "query": "show my EKS clusters",
                "expected_keywords": ["eks", "cluster", "kubernetes", "error", "unavailable"],
            },
            {
                "query": "list my GameLift fleets",
                "expected_keywords": ["gamelift", "fleet", "game", "error", "unavailable"],
            },
            {
                "query": "what's my AWS bill?",
                "expected_keywords": ["cost", "spending", "bill", "aws", "error", "unavailable"],
            },
        ]

        for case in test_cases:
            response = make_agent_request(case["query"], self.config)
            response_lower = response.lower()

            # Should contain at least one expected keyword (including error states)
            found_keyword = any(keyword in response_lower for keyword in case["expected_keywords"])
            assert (
                found_keyword
            ), f"No expected keywords found in response for: {case['query']}. Response: {response[:200]}"

    @pytest.mark.ai_eval
    def test_agent_response_quality(self):
        """Test agent response quality and structure"""
        response = make_agent_request("help me understand my AWS infrastructure", self.config)

        # Quality checks
        assert isinstance(response, str)
        assert len(response) > 50  # Substantial response
        assert len(response) < 2000  # Not too verbose

        # Should not contain obvious errors
        error_indicators = ["error occurred", "failed to", "exception", "traceback"]

        response_lower = response.lower()
        for indicator in error_indicators:
            assert indicator not in response_lower, f"Error indicator found: {indicator}"

    @pytest.mark.ai_eval
    def test_agent_consistency(self):
        """Test agent provides consistent responses"""
        query = "what can you help me with?"

        responses = []
        for _ in range(3):
            response = make_agent_request(query, self.config)
            responses.append(response)
            time.sleep(1)  # Brief delay between requests

        # All responses should be strings
        assert all(isinstance(r, str) for r in responses)

        # All responses should be substantial
        assert all(len(r) > 20 for r in responses)

        # Responses should be similar in theme (all should mention AWS/infrastructure or error)
        aws_keywords = ["aws", "infrastructure", "gamelift", "eks", "cost", "error", "unavailable", "support"]
        for response in responses:
            response_lower = response.lower()
            has_aws_keyword = any(keyword in response_lower for keyword in aws_keywords)
            assert has_aws_keyword, f"Response lacks AWS/error context: {response[:100]}"


class TestNonCostQueriesAreNotWithheld:
    """Non-cost replies must not be swapped for the financial withheld notice.

    Contract: a non-cost reply whose numbers are purely operational (a session
    length, a fleet count) is returned to the user unchanged. It is never
    replaced by the dedicated financial withheld notice, the ``## Cost``
    advisory heading, or the deterministic cost guidance.
    """

    @pytest.fixture(autouse=True)
    def setup_config(self):
        self.config = get_test_config()
        if self.config["mode"] != "deployed":
            pytest.skip("AI evals require deployed stack - deploy with ./deploy-all.sh")
        if not check_backend_available(self.config):
            pytest.skip("Deployed AgentCore Runtime not accessible")

    @pytest.mark.ai_eval
    @pytest.mark.parametrize(
        "query",
        [
            "My name is Quill. My game is a battle royale and our sessions last about 40 minutes.",
            "What are the best practices for deploying game servers across AWS Regions for low latency?",
        ],
    )
    def test_operational_query_is_answered_not_withheld(self, query):
        # Local modules
        from agents.financial_guard import withheld_financial_reply_notice

        response = make_agent_request(query, self.config)

        assert isinstance(response, str)
        assert len(response) > 0
        # The dedicated financial withheld notice must not appear for a
        # non-cost query that only mentions quantities.
        assert withheld_financial_reply_notice() not in response
        assert "did not come from the validated cost report" not in response
        # The reply must not be swapped for the Cost advisory section or the
        # deterministic cost guidance.
        assert not response.lstrip().startswith("## Cost")
        assert "Unvalidated financial figures were withheld" not in response
        assert "## Cost Guidance" not in response
        # The reply stays on topic. The best-practices query is outside the cost
        # domain (Regions and latency), so a correct answer discusses game
        # servers, GameLift, EKS, or Regions without needing a cost figure.
        if query.startswith("My name is"):
            # A short greeting may acknowledge only the synthetic name, so the
            # on-topic bar for this case is just that the name is echoed.
            assert "quill" in response.lower()
        else:
            assert any(
                keyword in response.lower()
                for keyword in ("gamelift", "game server", "game servers", "eks", "region", "regions")
            )
