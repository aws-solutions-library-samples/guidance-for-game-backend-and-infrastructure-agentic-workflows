"""
Response parser for extracting plain text from Bedrock AI responses.

Handles multiple response formats from different frameworks (Strands, direct Bedrock, etc.)
"""

# Standard library
from typing import Any


class ResponseParser:
    """Parse AI agent responses to plain text strings."""

    @staticmethod
    def parse(response: Any) -> str:
        """
        Parse agent response to plain text string.

        Args:
            response: Agent response object (Strands AgentResult, Bedrock response, etc.)

        Returns:
            Plain text string extracted from response
        """
        if hasattr(response, "message"):
            return ResponseParser._parse_message(response.message)
        if hasattr(response, "content"):
            return ResponseParser._parse_content(response.content)
        if hasattr(response, "text"):
            return str(response.text)
        return str(response)

    @staticmethod
    def _parse_message(message: Any) -> str:
        """Parse message attribute (Strands format)."""
        if isinstance(message, dict) and "content" in message:
            return ResponseParser._parse_content(message["content"])
        return str(message)

    @staticmethod
    def _parse_content(content: Any) -> str:
        """Parse content attribute (list of blocks or string)."""
        if isinstance(content, list):
            return "".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in content)
        return str(content)
