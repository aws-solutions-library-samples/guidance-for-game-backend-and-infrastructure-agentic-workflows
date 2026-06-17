"""
Ground Truth Regression Tests — WA GenAI Lens: Performance Efficiency 1

Loads test cases from tests/fixtures/ground_truth.yaml (single source of truth).
Validates routing accuracy, response quality, guardrail enforcement,
KB retrieval relevance, and edge case handling.
"""

# Standard library
import os
import pathlib
import re

# Third-party packages
import pytest
import yaml

from .test_config import check_backend_available, get_test_config, make_agent_request

pytestmark = [pytest.mark.cloud, pytest.mark.ai_eval]

FIXTURES_PATH = pathlib.Path(__file__).parent.parent / "fixtures" / "ground_truth.yaml"


@pytest.fixture(scope="module")
def ground_truth():
    with open(FIXTURES_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture(autouse=True)
def require_deployed(request):
    config = get_test_config()
    if config["mode"] != "deployed":
        pytest.skip("Ground truth tests require deployed stack")
    if not check_backend_available(config):
        pytest.skip("Deployed AgentCore Runtime not accessible")
    request.cls.config = config


class TestRoutingAccuracy:
    """Validate orchestrator routes queries to the correct specialist."""

    def test_routing(self, ground_truth):
        for case in ground_truth["routing"]:
            response = make_agent_request(case["query"], self.config)
            response_lower = response.lower()

            # Must contain at least one expected keyword
            found = any(kw in response_lower for kw in case["expected_keywords"])
            assert found, (
                f"Routing failed for '{case['query']}' "
                f"(expected agent: {case['expected_agent']}). "
                f"Response: {response[:200]}"
            )

            # Must not contain hallucination indicators
            for bad in case.get("not_expected", []):
                assert (
                    bad.lower() not in response_lower
                ), f"Hallucination '{bad}' found in response to '{case['query']}'"


class TestResponseQuality:
    """Validate response quality and safety."""

    def test_quality(self, ground_truth):
        for case in ground_truth["quality"]:
            response = make_agent_request(case["query"], self.config)
            response_lower = response.lower()

            if "min_length" in case:
                assert (
                    len(response) >= case["min_length"]
                ), f"Response too short for '{case['query']}': {len(response)} chars"
            if "max_length" in case:
                assert (
                    len(response) <= case["max_length"]
                ), f"Response too long for '{case['query']}': {len(response)} chars"
            for kw in case.get("expected_keywords", []):
                assert kw in response_lower, f"Missing keyword '{kw}' in response to '{case['query']}'"
            for bad in case.get("not_expected", []):
                assert (
                    bad.lower() not in response_lower
                ), f"Error indicator '{bad}' found in response to '{case['query']}'"


class TestGuardrailEnforcement:
    """Validate guardrails reject off-topic, adversarial, and PII-leaking queries."""

    def test_off_topic_rejection(self, ground_truth):
        """Off-topic queries should be redirected, not answered."""
        for case in ground_truth["guardrails"]:
            if case.get("category") != "off_topic":
                continue

            response = make_agent_request(case["query"], self.config)
            response_lower = response.lower()

            # Should redirect to AWS topics OR show guardrail block message
            found = any(kw in response_lower for kw in case.get("expected_keywords", []))
            is_blocked = "i can only help" in response_lower or "cannot provide" in response_lower
            assert found or is_blocked, (
                f"Off-topic query '{case['query']}' was not rejected. " f"Response: {response[:200]}"
            )

            # Should not contain off-topic content
            for bad in case.get("not_expected", []):
                assert (
                    bad.lower() not in response_lower
                ), f"Off-topic content '{bad}' found in response to '{case['query']}'"

    def test_prompt_injection_resistance(self, ground_truth):
        """Prompt injection attempts should not leak system internals."""
        for case in ground_truth["guardrails"]:
            if case.get("category") != "prompt_injection":
                continue

            response = make_agent_request(case["query"], self.config)
            response_lower = response.lower()

            for bad in case.get("not_expected", []):
                assert bad.lower() not in response_lower, (
                    f"Prompt injection succeeded: '{bad}' found in response " f"to '{case['query']}'"
                )

            if "max_length" in case:
                assert len(response) <= case["max_length"], (
                    f"Injection response too long ({len(response)} chars) "
                    f"for '{case['query']}' — may indicate system prompt leak"
                )

    def test_pii_protection(self, ground_truth):
        """Responses should not leak PII or sensitive identifiers."""
        for case in ground_truth["guardrails"]:
            if case.get("category") != "pii_protection":
                continue

            response = make_agent_request(case["query"], self.config)

            if "not_expected_pattern" in case:
                pattern = case["not_expected_pattern"]
                matches = re.findall(pattern, response)
                assert not matches, f"PII pattern match found in response to '{case['query']}': " f"{matches}"


class TestEdgeCases:
    """Validate boundary conditions and malformed inputs don't crash the system."""

    def test_edge_cases(self, ground_truth):
        for case in ground_truth["edge_cases"]:
            response = make_agent_request(case["query"], self.config)

            # Basic: should return a non-None string response
            assert response is not None, f"Null response for edge case: {case.get('description', case['query'])}"
            assert isinstance(
                response, str
            ), f"Non-string response for edge case: {case.get('description', case['query'])}"

            if "min_length" in case:
                assert len(response) >= case["min_length"], (
                    f"Response too short for edge case '{case.get('description', '')}': " f"{len(response)} chars"
                )

            for bad in case.get("not_expected", []):
                assert bad.lower() not in response.lower(), (
                    f"Error indicator '{bad}' in edge case response: " f"{case.get('description', '')}"
                )
