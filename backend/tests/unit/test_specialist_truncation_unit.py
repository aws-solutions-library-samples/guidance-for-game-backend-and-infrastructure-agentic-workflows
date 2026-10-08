"""A specialist answer cut off at its response budget is reported, never relayed partially."""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from strands.types.exceptions import MaxTokensReachedException

# Local modules
import agents.base_specialist as base
from agents.specialist_capture import begin_specialist_capture, finish_specialist_capture_with_history

pytestmark = pytest.mark.unit


def _specialist():
    return base.create_specialist_agent(
        service_name="GameLift",
        emoji="🎮",
        mcp_server_names=None,
        kb_id=None,
        prompt_fn=lambda: "prompt",
    )


def test_max_tokens_returns_a_clear_message_and_records_it_in_call_order():
    agent = MagicMock(side_effect=MaxTokensReachedException("partial ```yaml\nResources:\n  A:"))
    with (
        patch.object(base, "Agent", return_value=agent),
        patch.object(base, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        tool = _specialist()
        capture = begin_specialist_capture()
        try:
            result = tool("Migrate my fleet")
        finally:
            _, history = finish_specialist_capture_with_history(capture)

    expected = base.TRUNCATED_ANSWER_MESSAGE.format(service="GameLift")
    assert result == expected
    assert history == [("GameLift", expected)]
    assert "```" not in result and "Resources:" not in result
