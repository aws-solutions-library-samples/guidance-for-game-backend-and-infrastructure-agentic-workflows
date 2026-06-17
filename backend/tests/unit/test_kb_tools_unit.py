#!/usr/bin/env python3
"""Unit tests for KB tools - verify default parameters."""

# Standard library
import os
import sys

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))


class TestKBToolsDefaults:
    """Test KB tools have correct default parameters via tool_spec."""

    def test_retrieve_default_number_of_results_is_3(self):
        """Test retrieve function defaults to 3 results (reduced from 5 for token efficiency)."""
        # Local modules
        from utils.kb_tools import retrieve

        # Access the tool_spec which contains the JSON schema with defaults
        tool_spec = retrieve.tool_spec
        props = tool_spec["inputSchema"]["json"]["properties"]
        assert props["numberOfResults"]["default"] == 3

    def test_retrieve_default_score_is_0_5(self):
        """Test retrieve function defaults to 0.5 score threshold (increased for relevance)."""
        # Local modules
        from utils.kb_tools import retrieve

        tool_spec = retrieve.tool_spec
        props = tool_spec["inputSchema"]["json"]["properties"]
        assert props["score"]["default"] == 0.5

    def test_create_kb_retrieve_tool_defaults(self):
        """Test kb_retrieve created by factory has correct defaults."""
        # Local modules
        from utils.kb_tools import create_kb_retrieve_tool

        kb_retrieve = create_kb_retrieve_tool("test-kb-id")
        tool_spec = kb_retrieve.tool_spec
        props = tool_spec["inputSchema"]["json"]["properties"]

        # Check numberOfResults default is 3
        assert props["numberOfResults"]["default"] == 3

        # Check score default is 0.5
        assert props["score"]["default"] == 0.5
