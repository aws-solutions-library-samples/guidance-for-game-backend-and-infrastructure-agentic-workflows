"""
Unit tests for response parsing logic.
Tests the behavior before and after refactoring.
"""

# Standard library
import os
import sys

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Local modules
from utils.response_parser import ResponseParser


class MockResponse:
    """Mock response objects for testing different formats."""

    pass


class TestResponseParsingBehavior:
    """Test current response parsing behavior to ensure refactor maintains compatibility."""

    def test_parse_message_with_content_list(self):
        """Test parsing Strands AgentResult with message.content as list."""
        mock = MockResponse()
        mock.message = {"role": "assistant", "content": [{"text": "Hello "}, {"text": "World"}]}

        # Current behavior
        result = self._parse_current(mock)
        assert result == "Hello World"

    def test_parse_message_with_content_string(self):
        """Test parsing message with content as string."""
        mock = MockResponse()
        mock.message = {"content": "Simple string content"}

        result = self._parse_current(mock)
        assert result == "Simple string content"

    def test_parse_message_without_content(self):
        """Test parsing message without content field."""
        mock = MockResponse()
        mock.message = "Direct message string"

        result = self._parse_current(mock)
        assert result == "Direct message string"

    def test_parse_content_list(self):
        """Test parsing direct content attribute as list."""
        mock = MockResponse()
        mock.content = [{"text": "Content "}, {"text": "blocks"}]

        result = self._parse_current(mock)
        assert result == "Content blocks"

    def test_parse_content_string(self):
        """Test parsing direct content attribute as string."""
        mock = MockResponse()
        mock.content = "Direct content string"

        result = self._parse_current(mock)
        assert result == "Direct content string"

    def test_parse_text_attribute(self):
        """Test parsing text attribute."""
        mock = MockResponse()
        mock.text = "Simple text"

        result = self._parse_current(mock)
        assert result == "Simple text"

    def test_parse_fallback_str(self):
        """Test fallback to str() conversion."""
        mock = MockResponse()
        mock.value = "some value"

        result = self._parse_current(mock)
        assert "MockResponse" in result or "value" in result

    def test_parse_content_list_with_non_dict(self):
        """Test parsing content list with mixed types."""
        mock = MockResponse()
        mock.content = [{"text": "Text1 "}, "plain string", {"text": "Text2"}]

        result = self._parse_current(mock)
        assert "Text1" in result
        assert "plain string" in result
        assert "Text2" in result

    def test_parse_content_list_without_text_key(self):
        """Test parsing content list with dicts missing text key."""
        mock = MockResponse()
        mock.content = [{"other": "value"}, {"text": "Valid text"}]

        result = self._parse_current(mock)
        assert "Valid text" in result

    def _parse_current(self, response):
        """Use refactored ResponseParser."""
        return ResponseParser.parse(response)
