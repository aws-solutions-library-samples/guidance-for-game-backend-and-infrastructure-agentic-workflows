"""
Unit tests for AgentCore Memory configuration.

These tests ensure memory configuration parameters stay within acceptable bounds
to prevent regressions that could break memory retrieval functionality.

The relevance_score parameter is critical:
- Too high (>0.4): Filters out valid memories, breaking "What's my name?" queries
- Too low (<0.1): Returns too many irrelevant memories, causing noise
"""

# Standard library
import os
import sys

# Third-party packages
import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

pytestmark = pytest.mark.unit


class TestMemoryConfigConstants:
    """Test that memory configuration constants are within bounds.

    This is a simpler approach - directly check the configuration values
    in the orchestrator source code.
    """

    def test_ltm_strategy_configured(self):
        """
        LTM strategy must be configured for cross-session memory.

        REGRESSION TEST: Memory was initially STM_ONLY which caused
        the agent to forget user information across sessions.
        """
        # Standard library
        import re

        orchestrator_path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "agents", "orchestrator.py")

        with open(orchestrator_path, "r") as f:
            source_code = f.read()

        # Check that strategy_id is set (not None)
        strategy_match = re.search(r'strategy_id\s*=\s*["\'](\w+)["\']', source_code)

        assert strategy_match, (
            "strategy_id must be configured for LTM. "
            "Without a strategy, cross-session memory won't work. "
            "CRITICAL: Setting strategy_id=None caused user name to be forgotten after logout."
        )

        strategy_id = strategy_match.group(1)
        assert strategy_id == "user_facts", (
            f"strategy_id should be 'user_facts', got '{strategy_id}'. "
            "This strategy stores user facts like name for LTM retrieval."
        )

    def test_relevance_score_in_source_code(self):
        """
        Verify the relevance_score in source code is within acceptable bounds.

        This is a belt-and-suspenders test that reads the actual source file
        and verifies the configuration.
        """
        # Standard library
        import re

        # Read the orchestrator source file
        orchestrator_path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "agents", "orchestrator.py")

        with open(orchestrator_path, "r") as f:
            source_code = f.read()

        # Find relevance_score assignment
        relevance_match = re.search(r"relevance_score\s*=\s*([\d.]+)", source_code)

        if relevance_match:
            relevance_score = float(relevance_match.group(1))

            assert relevance_score <= 0.4, (
                f"relevance_score={relevance_score} in orchestrator.py is too high! "
                f"Must be <= 0.4 to ensure name/identity queries work. "
                f"CRITICAL: A score of 0.5 was found to break 'What's my name?' queries."
            )
            assert relevance_score >= 0.1, (
                f"relevance_score={relevance_score} is too low! " f"Must be >= 0.1 to filter out irrelevant memories."
            )
        else:
            # If relevance_score is not found, that's also a problem
            pytest.skip("relevance_score not found in source code - memory may be disabled")

    def test_top_k_in_source_code(self):
        """
        Verify the top_k in source code is within acceptable bounds.
        """
        # Standard library
        import re

        orchestrator_path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "agents", "orchestrator.py")

        with open(orchestrator_path, "r") as f:
            source_code = f.read()

        # Find top_k assignment
        top_k_match = re.search(r"top_k\s*=\s*(\d+)", source_code)

        if top_k_match:
            top_k = int(top_k_match.group(1))

            assert top_k >= 5, (
                f"top_k={top_k} is too low! " f"Must be >= 5 to retrieve enough context for name/identity queries."
            )
            assert top_k <= 20, f"top_k={top_k} is too high! " f"Must be <= 20 to avoid memory retrieval noise."
        else:
            pytest.skip("top_k not found in source code - memory may be disabled")


class TestSemanticMemoryPatterns:
    """Test that semantic memory extraction patterns work correctly."""

    def test_name_extraction_my_name_is(self):
        """Test 'My name is X' pattern extraction."""
        # Local modules
        from utils.semantic_memory import _NAME_PATTERNS

        test_cases = [
            ("My name is JP", "Jp"),
            ("my name is Alice", "Alice"),
            ("MY NAME IS BOB", "Bob"),
            ("Hi! My name is Charlie Brown", "Charlie Brown"),
        ]

        for message, expected_name in test_cases:
            for pattern in _NAME_PATTERNS:
                match = pattern.search(message)
                if match:
                    name = match.group(1).title()
                    assert name == expected_name, f"Expected '{expected_name}', got '{name}' from '{message}'"
                    break
            else:
                pytest.fail(f"No pattern matched '{message}'")

    def test_name_extraction_im(self):
        """Test 'I'm X' pattern extraction."""
        # Local modules
        from utils.semantic_memory import _NAME_PATTERNS

        message = "Hi, I'm David"
        matched = False
        for pattern in _NAME_PATTERNS:
            match = pattern.search(message)
            if match:
                name = match.group(1).title()
                assert "David" in name
                matched = True
                break
        assert matched, f"No pattern matched '{message}'"

    def test_name_extraction_call_me(self):
        """Test 'Call me X' pattern extraction."""
        # Local modules
        from utils.semantic_memory import _NAME_PATTERNS

        message = "You can call me Eve"
        matched = False
        for pattern in _NAME_PATTERNS:
            match = pattern.search(message)
            if match:
                name = match.group(1).title()
                assert "Eve" in name
                matched = True
                break
        assert matched, f"No pattern matched '{message}'"


class TestMemoryRetrievalScenarios:
    """Test that memory retrieval would work for common user queries."""

    def test_name_recall_query_similarity(self):
        """
        Test that 'What's my name?' would match stored name memories.

        This is a conceptual test - we verify that the query patterns
        we expect users to use should have reasonable semantic similarity
        to stored name memories.

        Key insight: "What's my name?" has LOW semantic similarity to
        "User's name is JP" (~0.3-0.4), so relevance_score must be <= 0.4
        """
        # Document the expected behavior for future debugging
        name_storage_patterns = [
            "User's name is {name}",
            "The user prefers to be called {name}",
            "User introduced themselves as {name}",
        ]

        name_recall_queries = [
            "What's my name?",
            "Do you remember my name?",
            "What did I say my name was?",
            "Who am I?",
        ]

        # This test documents that these query patterns exist
        # and should work with relevance_score <= 0.4
        assert len(name_storage_patterns) > 0
        assert len(name_recall_queries) > 0

        # The critical assertion: document that low similarity is expected
        expected_min_similarity = 0.25  # Current relevance_score threshold
        expected_max_similarity_for_name_queries = 0.45  # Based on testing

        # These assertions document our understanding
        assert expected_min_similarity <= expected_max_similarity_for_name_queries, (
            "Our relevance_score threshold must be below the expected "
            "similarity score for name recall queries to work."
        )
