"""
Ground Truth Regression Tests — WA GenAI Lens: Performance Efficiency 1

Loads test cases from tests/fixtures/ground_truth.yaml (single source of truth).
Validates routing accuracy, response quality, guardrail enforcement, and edge
case handling.

Each YAML case is parametrized into its OWN test (one live agent call per test)
rather than looped inside a single test. This isolates failures, lets each case
run within the per-test timeout, and parallelizes — previously all N cases ran
in one test function, so a single test made 12+ sequential live calls and blew
any reasonable timeout (#144).
"""

# Standard library
import pathlib
import re

# Third-party packages
import pytest
import yaml

from .test_config import check_backend_available, get_test_config, make_agent_request

pytestmark = [pytest.mark.cloud, pytest.mark.ai_eval]

FIXTURES_PATH = pathlib.Path(__file__).parent.parent / "fixtures" / "ground_truth.yaml"


def _load_cases(section: str) -> list:
    """Load a section's cases from the YAML at COLLECTION time (for parametrize)."""
    try:
        with open(FIXTURES_PATH) as f:
            data = yaml.safe_load(f)
        return data.get(section, []) or []
    except Exception:
        return []


def _case_id(case: dict) -> str:
    """Stable, readable parametrize id from a case's query/description."""
    label = case.get("description") or case.get("query", "case")
    return label[:50]


def _guardrail_cases(category: str) -> list:
    return [c for c in _load_cases("guardrails") if c.get("category") == category]


@pytest.fixture(scope="module")
def config():
    """Deployed-stack config; skips the whole module if no live stack."""
    cfg = get_test_config()
    if cfg["mode"] != "deployed":
        pytest.skip("Ground truth tests require deployed stack")
    if not check_backend_available(cfg):
        pytest.skip("Deployed AgentCore Runtime not accessible")
    return cfg


# ---------------------------------------------------------------------------
# Routing accuracy — one test per query
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", _load_cases("routing"), ids=_case_id)
def test_routing(case, config):
    response = make_agent_request(case["query"], config)
    response_lower = response.lower()

    found = any(kw in response_lower for kw in case["expected_keywords"])
    assert found, (
        f"Routing failed for '{case['query']}' "
        f"(expected agent: {case['expected_agent']}). Response: {response[:200]}"
    )
    for bad in case.get("not_expected", []):
        assert bad.lower() not in response_lower, f"Hallucination '{bad}' found in response to '{case['query']}'"


# ---------------------------------------------------------------------------
# Response quality — one test per query
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", _load_cases("quality"), ids=_case_id)
def test_quality(case, config):
    response = make_agent_request(case["query"], config)
    response_lower = response.lower()

    if "min_length" in case:
        assert len(response) >= case["min_length"], f"Response too short for '{case['query']}': {len(response)} chars"
    if "max_length" in case:
        assert len(response) <= case["max_length"], f"Response too long for '{case['query']}': {len(response)} chars"
    for kw in case.get("expected_keywords", []):
        assert kw in response_lower, f"Missing keyword '{kw}' in response to '{case['query']}'"
    for bad in case.get("not_expected", []):
        assert bad.lower() not in response_lower, f"Error indicator '{bad}' found in response to '{case['query']}'"


# ---------------------------------------------------------------------------
# Guardrails — one test per case, split by category
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", _guardrail_cases("off_topic"), ids=_case_id)
def test_off_topic_rejection(case, config):
    """Off-topic queries should be redirected, not answered."""
    response = make_agent_request(case["query"], config)
    response_lower = response.lower()

    found = any(kw in response_lower for kw in case.get("expected_keywords", []))
    is_blocked = "i can only help" in response_lower or "cannot provide" in response_lower
    assert found or is_blocked, f"Off-topic query '{case['query']}' was not rejected. Response: {response[:200]}"
    for bad in case.get("not_expected", []):
        assert bad.lower() not in response_lower, f"Off-topic content '{bad}' found in response to '{case['query']}'"


@pytest.mark.parametrize("case", _guardrail_cases("prompt_injection"), ids=_case_id)
def test_prompt_injection_resistance(case, config):
    """Prompt injection attempts should not leak system internals."""
    response = make_agent_request(case["query"], config)
    response_lower = response.lower()

    for bad in case.get("not_expected", []):
        assert bad.lower() not in response_lower, f"Prompt injection succeeded: '{bad}' found for '{case['query']}'"
    if "max_length" in case:
        assert (
            len(response) <= case["max_length"]
        ), f"Injection response too long ({len(response)} chars) for '{case['query']}' — possible prompt leak"


@pytest.mark.parametrize("case", _guardrail_cases("pii_protection"), ids=_case_id)
def test_pii_protection(case, config):
    """Responses should not leak PII or sensitive identifiers."""
    response = make_agent_request(case["query"], config)
    if "not_expected_pattern" in case:
        matches = re.findall(case["not_expected_pattern"], response)
        assert not matches, f"PII pattern match found in response to '{case['query']}': {matches}"


# ---------------------------------------------------------------------------
# Edge cases — one test per case
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", _load_cases("edge_cases"), ids=_case_id)
def test_edge_cases(case, config):
    response = make_agent_request(case["query"], config)
    label = case.get("description", case["query"])

    assert response is not None, f"Null response for edge case: {label}"
    assert isinstance(response, str), f"Non-string response for edge case: {label}"
    if "min_length" in case:
        assert len(response) >= case["min_length"], f"Response too short for edge case '{label}': {len(response)} chars"
    for bad in case.get("not_expected", []):
        assert bad.lower() not in response.lower(), f"Error indicator '{bad}' in edge case response: {label}"
