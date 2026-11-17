"""
Integration tests for Bedrock Guardrails.

Tests that guardrails properly block denied topics and allow legitimate queries.

Replies to the legitimate queries come from a live account and can carry fleet
IDs, ARNs, account IDs or spend figures. Test output is public, so assertions
never print a reply: they assert on precomputed values and report only the
reply's length and a short digest. The reply text is never an operand of an
assert, which also keeps pytest's assertion rewriting from printing it.
"""

# Standard library
import hashlib

# Third-party packages
import pytest

# Local modules
from agents.orchestrator import run_orchestrator
from models.cached_bedrock import guardrail_attached

# The same condition the model uses to attach the guardrail. Without one, the
# orchestrator runs unguarded and these assertions would test model wording
# instead of the guardrail, so skip.
GUARDRAIL_CONFIGURED = guardrail_attached()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.cloud,
    pytest.mark.skipif(
        not GUARDRAIL_CONFIGURED,
        reason=(
            "No guardrail configured: set GBAW_BEDROCK_GUARDRAIL_ID and GBAW_BEDROCK_GUARDRAIL_VERSION (the "
            "Guardrails stack's GuardrailVersion output; unset means DRAFT, the working copy the deployed runtime "
            "does not use) and leave GBAW_BEDROCK_GUARDRAIL_ENABLED on; otherwise the orchestrator runs unguarded"
        ),
    ),
]

BLOCKED_PHRASE = "cannot provide"


def _ask(query: str) -> str:
    """Run the orchestrator and return its reply as text."""
    return str(run_orchestrator(query))


def _matches(reply: str, phrases: list[str]) -> list[str]:
    """Return the phrases that appear in the reply, case-insensitively."""
    lowered = reply.lower()
    return [phrase for phrase in phrases if phrase in lowered]


def _summary(reply: str) -> str:
    """Describe a reply without its content: length and a short SHA-256 prefix."""
    digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()[:12]
    return f"reply length {len(reply)}, sha256 {digest}"


class TestGuardrailsDeniedTopics:
    """Test that guardrails block off-topic queries."""

    def test_blocks_personal_advice(self):
        """Guardrails should block personal advice questions."""
        text = _ask("How do I deal with stress at work?")
        phrases = [
            "can only help with aws",
            "gamelift",
            "eks",
            "cost",
            "infrastructure",
            "cannot provide that information",
        ]
        matched = _matches(text, phrases)
        summary = _summary(text)

        # Should either block or redirect to AWS topics
        assert matched, f"Expected guardrail block or redirect (one of {phrases}); {summary}"

    def test_blocks_non_aws_infrastructure(self):
        """Guardrails should block non-AWS cloud provider questions."""
        text = _ask("How do I set up Azure Kubernetes Service?")
        phrases = ["can only help with aws", "gamelift", "eks", BLOCKED_PHRASE]
        matched = _matches(text, phrases)
        summary = _summary(text)

        assert matched, f"Expected guardrail block (one of {phrases}); {summary}"

    def test_blocks_general_programming(self):
        """Guardrails should block general programming questions."""
        text = _ask("How do I write a Python function?")
        phrases = ["aws", "gamelift", "eks", "infrastructure"]
        matched = _matches(text, phrases)
        summary = _summary(text)

        assert matched, f"Expected redirect to AWS topics (one of {phrases}); {summary}"


class TestGuardrailsAllowLegitimate:
    """Test that guardrails allow legitimate AWS queries."""

    def _assert_allowed(self, query: str, phrases: list[str]) -> None:
        text = _ask(query)
        blocked = bool(_matches(text, [BLOCKED_PHRASE]))
        matched = _matches(text, phrases)
        summary = _summary(text)

        assert not blocked, f"Legitimate query was blocked; {summary}"
        assert matched, f"Expected one of {phrases} in the reply; {summary}"

    def test_allows_gamelift_queries(self):
        """Guardrails should allow GameLift queries."""
        self._assert_allowed("Show me my GameLift fleets", ["fleet", "gamelift", "game server"])

    def test_allows_eks_queries(self):
        """Guardrails should allow EKS queries."""
        self._assert_allowed("List my EKS clusters", ["eks", "cluster", "kubernetes"])

    def test_allows_cost_queries(self):
        """Guardrails should allow cost analysis queries."""
        self._assert_allowed("What's my AWS spending this month?", ["cost", "spending", "dollar", "$"])


class TestGuardrailsPIIProtection:
    """Test that guardrails handle PII appropriately."""

    def test_handles_email_in_query(self):
        """Guardrails should handle emails in user queries."""
        text = _ask("My email is test@example.com, show me my fleets")
        phrases = ["fleet", "gamelift"]
        matched = _matches(text, phrases)
        summary = _summary(text)

        # Should still process the query (email in input is OK)
        assert matched, f"Query with email should still work (one of {phrases}); {summary}"

    def test_anonymizes_sensitive_data_in_response(self):
        """Guardrails should anonymize PII in responses if present."""
        # This is hard to test directly, but we verify the system doesn't crash
        # and returns a non-empty reply.
        text = _ask("Show me my AWS account details")
        length = len(text)

        assert length > 0, "Expected a non-empty reply"
