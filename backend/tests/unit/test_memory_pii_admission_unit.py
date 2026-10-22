"""Recognizable sensitive values never enter long-term memory."""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from utils import semantic_memory

pytestmark = pytest.mark.unit

_SENSITIVE_VALUES = [
    "000000000000",
    "player@example.invalid",
    "192.0.2.17",
    "2001:db8::17",
    "game-server.internal",
    "+1 (202) 555-0142",
    "2025550142",
]


@pytest.mark.parametrize("value", _SENSITIVE_VALUES)
def test_sensitive_fact_is_rejected_before_any_memory_client_call(value):
    client = MagicMock()
    with (
        patch.object(semantic_memory, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-synthetic"),
        patch.object(semantic_memory, "_get_memory_client", return_value=client) as get_client,
    ):
        saved = semantic_memory.save_semantic_memory("actor-synthetic", f"Game runs on {value}")

    assert saved is False
    get_client.assert_not_called()
    client.batch_create_memory_records.assert_not_called()


@pytest.mark.parametrize("value", _SENSITIVE_VALUES)
def test_sensitive_input_is_not_truncated_into_an_apparently_safe_fact(value):
    # The extraction patterns can stop before dots or punctuation. Check the
    # original message before extraction so an address is not saved as "192".
    with patch.object(semantic_memory, "save_semantic_memory") as save:
        semantic_memory.extract_and_save_user_info(
            "actor-synthetic", f"My name is Zephyr. Our game uses cluster-{value}.", "Noted."
        )

    save.assert_not_called()


def test_ordinary_profile_and_game_facts_still_save():
    with patch.object(semantic_memory, "save_semantic_memory", return_value=True) as save:
        semantic_memory.extract_and_save_user_info(
            "actor-synthetic",
            "My name is Zephyr. I run a battle royale game. Our sessions last 40 minutes. "
            "Our game uses cluster-gameagent-test.",
            "Noted.",
        )

    facts = [call.args[1] for call in save.call_args_list]
    assert "User's name is Zephyr" in facts
    assert "Game type: BATTLE ROYALE" in facts
    assert "Session duration: 40 minutes" in facts
    assert "Game 'Game' runs on cluster: gameagent-test" in facts


def test_game_resource_identifiers_and_instance_sizes_are_not_contact_details():
    with patch.object(semantic_memory, "save_semantic_memory", return_value=True) as save:
        semantic_memory.extract_and_save_user_info(
            "actor-synthetic",
            "Our game runs on fleet-a1b2c3d4-e5f6-4789-abcd-ef0123456789. We use c5.2xlarge.",
            "Noted.",
        )

    assert any("fleet:" in call.args[1] for call in save.call_args_list)
    assert any("Instance type: c5.2xlarge" == call.args[1] for call in save.call_args_list)
