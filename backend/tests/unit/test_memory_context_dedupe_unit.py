#!/usr/bin/env python3
"""Unit tests for de-duplicating lines inside the injected memory block.

Cross-session long-term memory can hold several normalized-equal copies of the
same fact, and retrieval returns them all. :meth:`retrieve_customer_context`
injects one ``<user_context>…</user_context>`` block; these tests drive the
real hook (memory client mocked to return duplicate records) and assert that
the marked block carries each line once, in first-seen order, while a block
that fails the tag check is neither deduped nor marked.
"""

# Standard library
from types import SimpleNamespace
from unittest.mock import MagicMock

# Third-party packages
import pytest
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

# Local modules
from utils.memory_context import MEMORY_CONTEXT_MARKER_KEY, MemoryScopedSessionManager

pytestmark = pytest.mark.unit

_USER_QUESTION = "What kind of game do I run?"


def _new_session_manager() -> MemoryScopedSessionManager:
    """Build a scoped session manager without network-bound __init__."""
    manager = MemoryScopedSessionManager.__new__(MemoryScopedSessionManager)
    manager.config = AgentCoreMemoryConfig(
        memory_id="mem-123",
        session_id="s-1",
        actor_id="u-1",
        retrieval_config={"{actorId}": RetrievalConfig(top_k=10, strategy_id="user_facts")},
    )
    manager.memory_client = MagicMock()
    return manager


def _event_with_user_message(text: str) -> SimpleNamespace:
    agent = SimpleNamespace(messages=[{"role": "user", "content": [{"text": text}]}])
    return SimpleNamespace(agent=agent)


def test_duplicate_lines_collapsed_in_marked_block():
    """Duplicate retrieved facts yield a block whose lines appear once, in order."""
    manager = _new_session_manager()
    manager.memory_client.retrieve_memories.return_value = [
        {"content": {"text": "Game type: BATTLE ROYALE"}, "score": 0.9},
        {"content": {"text": "game type: battle royale"}, "score": 0.88},
        {"content": {"text": "Platform: GameLift"}, "score": 0.8},
        {"content": {"text": "  Game type: BATTLE ROYALE  "}, "score": 0.7},
    ]
    event = _event_with_user_message(_USER_QUESTION)

    manager.retrieve_customer_context(event)

    content = event.agent.messages[-1]["content"]
    block = content[0]
    assert block.get(MEMORY_CONTEXT_MARKER_KEY) is True
    tag = manager.config.context_tag
    inner = block["text"][len(f"<{tag}>") : -len(f"</{tag}>")]
    lines = [line for line in inner.splitlines() if line.strip()]
    # First occurrence of each normalized line is kept, order preserved.
    assert lines == ["Game type: BATTLE ROYALE", "Platform: GameLift"]


def test_untagged_block_is_not_deduped_or_marked(monkeypatch):
    """A block that fails the tag check keeps its duplicate lines and no marker."""
    manager = _new_session_manager()

    def _append_untagged(_self, event):
        text = "Game type: BATTLE ROYALE\nGame type: BATTLE ROYALE"
        event.agent.messages[-1]["content"].insert(0, {"text": text})

    monkeypatch.setattr(AgentCoreMemorySessionManager, "retrieve_customer_context", _append_untagged)
    event = _event_with_user_message(_USER_QUESTION)

    manager.retrieve_customer_context(event)

    content = event.agent.messages[-1]["content"]
    block = content[0]
    assert MEMORY_CONTEXT_MARKER_KEY not in block
    # Untouched: both duplicate lines remain.
    assert block["text"] == "Game type: BATTLE ROYALE\nGame type: BATTLE ROYALE"
