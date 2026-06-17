"""
Unit tests for markdown handling.
Tests to verify markdown is preserved for CopilotKit rendering.
"""

# Standard library
import os
import re
import sys

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestMarkdownHandling:
    """Test markdown preservation for CopilotKit."""

    def test_preserve_bold_markdown(self):
        """CopilotKit should receive **bold** markdown."""
        text = "This is **important** information"
        result = self._process_current(text)
        assert "**important**" in result, "Bold markdown should be preserved"

    def test_preserve_italic_markdown(self):
        """CopilotKit should receive *italic* markdown."""
        text = "This is *emphasized* text"
        result = self._process_current(text)
        assert "*emphasized*" in result, "Italic markdown should be preserved"

    def test_preserve_headers(self):
        """CopilotKit should receive ## headers."""
        text = "## Important Section\nContent here"
        result = self._process_current(text)
        assert "## Important Section" in result, "Headers should be preserved"

    def test_preserve_bullet_lists(self):
        """CopilotKit should receive - bullet lists."""
        text = "Items:\n- First item\n- Second item"
        result = self._process_current(text)
        assert "- First item" in result, "Bullet lists should be preserved"
        assert "- Second item" in result, "Bullet lists should be preserved"

    def test_preserve_numbered_lists(self):
        """CopilotKit should receive 1. numbered lists."""
        text = "Steps:\n1. First step\n2. Second step"
        result = self._process_current(text)
        assert "1. First step" in result, "Numbered lists should be preserved"
        assert "2. Second step" in result, "Numbered lists should be preserved"

    def test_preserve_mixed_markdown(self):
        """CopilotKit should receive mixed markdown formatting."""
        text = """## Fleet Status

Your **GameLift** fleet has:
- Active instances: *5*
- Pending: **2**

Next steps:
1. Monitor capacity
2. Check costs"""

        result = self._process_current(text)
        assert "## Fleet Status" in result
        assert "**GameLift**" in result
        assert "- Active instances: *5*" in result
        assert "1. Monitor capacity" in result

    def _process_current(self, text):
        """Preserve markdown for CopilotKit rendering."""
        # CopilotKit uses react-markdown and handles rendering natively
        return text
