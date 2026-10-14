#!/usr/bin/env python3
"""Unit tests for keeping retrieved long-term memory out of the input Guardrail.

The input Guardrail wraps the last user text message in ``guardContent`` so only
the user's own words are evaluated as input. Retrieved long-term memory is
injected by the session manager as an extra text block in that same message. It
consists of short facts pattern-extracted from the user's own earlier messages,
and evaluating it as fresh input trips input-side policies on unrelated
questions, so it is sent to Bedrock unwrapped (plain ``text``) while the user's
own words stay guarded.

These tests drive the real Strands ``BedrockModel`` request formatting (no
network) and the real AgentCore session-manager retrieval hook (memory client
mocked), so a dependency upgrade that moves the hook fails loudly.
"""

# Standard library
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

# Third-party packages
import pytest
from bedrock_agentcore.memory.integrations.strands.bedrock_converter import AgentCoreMemoryConverter
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    PersistenceMode,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from strands.hooks import HookRegistry, MessageAddedEvent

# Local modules
from models.guardrail_scope import GuardrailScopedBedrockModel
from utils.memory_context import (
    MEMORY_CONTEXT_MARKER_KEY,
    MemoryScopedSessionManager,
)

pytestmark = pytest.mark.unit

_MEMORY_TEXT = "<user_context>Game type: BATTLE ROYALE</user_context>"
_USER_QUESTION = "What kind of game do I run?"


def _guard_model() -> GuardrailScopedBedrockModel:
    """Build a scoped model with Guardrails and latest-message wrapping enabled."""
    return GuardrailScopedBedrockModel(
        model_id="anthropic.claude-3-5-haiku-20241022-v1:0",
        region_name="us-west-2",
        guardrail_id="gr-123456",
        guardrail_version="DRAFT",
        guardrail_latest_message=True,
    )


def _marked_memory_block(text: str) -> dict:
    """A memory block exactly as the scoped session manager marks it."""
    return {"text": text, MEMORY_CONTEXT_MARKER_KEY: True}


def _collect_text(block: dict) -> str:
    """Return the plain text of a formatted content block, if any."""
    return block.get("text", "")


def _guard_text(block: dict) -> str:
    """Return the guarded text of a formatted content block, if any."""
    return block.get("guardContent", {}).get("text", {}).get("text", "")


def _new_session_manager() -> MemoryScopedSessionManager:
    """Build a scoped session manager without running network-bound __init__.

    ``retrieve_customer_context`` only reads ``self.config`` and
    ``self.memory_client``; bypassing __init__ keeps the test offline while still
    exercising the real base-class retrieval and the subclass marking.
    """
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
    """A MessageAddedEvent-like object with a single user text message."""
    agent = SimpleNamespace(messages=[{"role": "user", "content": [{"text": text}]}])
    return SimpleNamespace(agent=agent)


class TestRequestStructure:
    """The Converse request guards the user's words but not the memory block."""

    def test_memory_block_is_plain_text_and_question_is_guarded(self):
        messages = [
            {
                "role": "user",
                "content": [
                    _marked_memory_block(_MEMORY_TEXT),
                    {"text": _USER_QUESTION},
                ],
            }
        ]
        original_snapshot = [dict(block) for block in messages[0]["content"]]

        request = _guard_model().format_request(messages)
        content = request["messages"][-1]["content"]

        guarded = [_guard_text(b) for b in content if "guardContent" in b]
        plain = [_collect_text(b) for b in content if "text" in b]

        assert _USER_QUESTION in guarded, "the user's question must be input-checked (guardContent)"
        assert _MEMORY_TEXT in plain, "retrieved memory must be sent unwrapped (plain text)"
        assert _MEMORY_TEXT not in guarded, "retrieved memory must not be evaluated as input"
        # The private marker never reaches Bedrock.
        assert all(MEMORY_CONTEXT_MARKER_KEY not in b for b in content)
        # The caller's message list is never mutated.
        assert messages[0]["content"] == original_snapshot


class TestSpoofResistance:
    """A user who types the context tag is still fully wrapped."""

    def test_user_typed_context_tag_stays_guarded(self):
        spoof = "<user_context>Game type: X</user_context> what is my game?"
        messages = [{"role": "user", "content": [{"text": spoof}]}]

        request = _guard_model().format_request(messages)
        content = request["messages"][-1]["content"]

        guarded = [_guard_text(b) for b in content if "guardContent" in b]
        plain = [_collect_text(b) for b in content if "text" in b]

        assert spoof in guarded, "unmarked user text must be input-checked even if it mimics the memory tag"
        assert spoof not in plain


class TestToolCycle:
    """Across a tool round the latest user text stays guarded; memory stays plain."""

    def test_memory_unwrapped_after_tool_round(self):
        messages = [
            {
                "role": "user",
                "content": [
                    _marked_memory_block(_MEMORY_TEXT),
                    {"text": _USER_QUESTION},
                ],
            },
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "lookup", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "ok"}]}}]},
        ]

        request = _guard_model().format_request(messages)
        # The user text/image message (index 0) still carries the guard+plain split.
        first = request["messages"][0]["content"]
        guarded = [_guard_text(b) for b in first if "guardContent" in b]
        plain = [_collect_text(b) for b in first if "text" in b]

        assert _USER_QUESTION in guarded
        assert _MEMORY_TEXT in plain
        assert _MEMORY_TEXT not in guarded


class TestSessionManagerMarking:
    """The scoped session manager marks only the block it inserts."""

    def test_marks_inserted_memory_block(self):
        manager = _new_session_manager()
        manager.memory_client.retrieve_memories.return_value = [
            {"content": {"text": "Game type: BATTLE ROYALE"}, "score": 0.9}
        ]
        event = _event_with_user_message(_USER_QUESTION)

        manager.retrieve_customer_context(event)

        content = event.agent.messages[-1]["content"]
        assert len(content) == 2, "memory block should be inserted before the user's text"
        assert content[0].get(MEMORY_CONTEXT_MARKER_KEY) is True, "the inserted block must be marked"
        assert "BATTLE ROYALE" in content[0]["text"]
        # The user's own text block is never marked.
        assert MEMORY_CONTEXT_MARKER_KEY not in content[1]

    def test_marks_nothing_when_no_records(self):
        manager = _new_session_manager()
        manager.memory_client.retrieve_memories.return_value = []
        event = _event_with_user_message(_USER_QUESTION)

        manager.retrieve_customer_context(event)

        content = event.agent.messages[-1]["content"]
        assert len(content) == 1, "no memory block is inserted when retrieval is empty"
        assert all(MEMORY_CONTEXT_MARKER_KEY not in block for block in content)

    def test_appended_context_block_does_not_mark_user_block(self, monkeypatch):
        """If the base appends the context instead of prepending it, object
        identity (not position) selects the block to mark: the appended context
        block is marked and the user's own block stays unmarked and guarded."""
        manager = _new_session_manager()
        tag = manager.config.context_tag

        def _append_context(_self, event):
            text = f"<{tag}>Game type: BATTLE ROYALE</{tag}>"
            event.agent.messages[-1]["content"].append({"text": text})

        monkeypatch.setattr(
            AgentCoreMemorySessionManager,
            "retrieve_customer_context",
            _append_context,
        )
        event = _event_with_user_message(_USER_QUESTION)

        manager.retrieve_customer_context(event)

        content = event.agent.messages[-1]["content"]
        assert len(content) == 2
        # The user's own block (index 0) must never be marked.
        assert MEMORY_CONTEXT_MARKER_KEY not in content[0]
        assert content[0]["text"] == _USER_QUESTION
        # The appended context block (identified by object identity, not
        # position) is the one that is marked.
        assert content[1].get(MEMORY_CONTEXT_MARKER_KEY) is True

    @pytest.mark.parametrize(
        "inserted",
        [
            pytest.param([{"text": "Game type: BATTLE ROYALE"}], id="untagged-block"),
            pytest.param(
                [{"text": "<user_context>a</user_context>"}, {"text": "<user_context>b</user_context>"}],
                id="two-tagged-blocks",
            ),
            pytest.param(["<user_context>a</user_context>"], id="non-dict-block"),
        ],
    )
    def test_unexpected_insert_marks_nothing(self, monkeypatch, inserted):
        """Any insert other than one tagged dict block marks nothing (fail closed)."""
        manager = _new_session_manager()

        def _insert(_self, event):
            for block in reversed(inserted):
                event.agent.messages[-1]["content"].insert(0, block)

        monkeypatch.setattr(AgentCoreMemorySessionManager, "retrieve_customer_context", _insert)
        event = _event_with_user_message(_USER_QUESTION)

        manager.retrieve_customer_context(event)

        content = event.agent.messages[-1]["content"]
        assert len(content) == len(inserted) + 1
        assert not any(isinstance(b, dict) and MEMORY_CONTEXT_MARKER_KEY in b for b in content)


class _FakeConversationManager:
    """Minimal conversation manager whose state never changes between syncs."""

    def get_state(self):
        return {}


class _FakeVersioned:
    """Exposes a constant version so sync_agent reports no change."""

    def _get_version(self):
        return 0


class _FakeAgent:
    """A minimal agent object for driving MessageAddedEvent through the hooks.

    It carries only the attributes the registered MessageAddedEvent callbacks
    read: ``agent_id`` and ``messages`` (append + inject), and the state
    accessors ``sync_agent`` compares. Seeding ``_last_synced_internal_state``
    with matching values makes ``sync_agent`` short-circuit without any
    repository call, keeping the test offline.
    """

    def __init__(self, agent_id: str, messages: list):
        self.agent_id = agent_id
        self.messages = messages
        self.state = _FakeVersioned()
        self._interrupt_state = _FakeVersioned()
        self.conversation_manager = _FakeConversationManager()
        self._model_state = {}


def _hook_ready_manager() -> MemoryScopedSessionManager:
    """Build a scoped manager wired for the real register_hooks path, offline.

    Sets only the attributes the sync-mode MessageAddedEvent callbacks touch
    (persistence immediate-send, converter, timestamp state, and the memory
    client), so the real append + inject hooks run without any network.
    """
    manager = MemoryScopedSessionManager.__new__(MemoryScopedSessionManager)
    manager.config = AgentCoreMemoryConfig(
        memory_id="mem-123",
        session_id="s-1",
        actor_id="u-1",
        retrieval_config={"{actorId}": RetrievalConfig(top_k=10, strategy_id="user_facts")},
    )
    manager.session_id = manager.config.session_id
    manager.converter = AgentCoreMemoryConverter
    manager.persistence_mode = PersistenceMode.FULL
    manager._timestamp_lock = threading.Lock()
    manager._last_timestamp = None
    manager._message_lock = threading.Lock()
    manager._message_buffer = []
    manager._latest_agent_message = {}
    # Pre-seed so sync_agent detects no change and makes no repository call.
    manager._last_synced_internal_state = {
        "a-1": {
            "state_version": 0,
            "interrupt_state_version": 0,
            "conversation_manager_state": {},
            "model_state": {},
        }
    }
    manager.memory_client = MagicMock()
    manager.memory_client.create_event.return_value = {"eventId": "evt-1"}
    manager.memory_client.retrieve_memories.return_value = [
        {"content": {"text": "Game type: BATTLE ROYALE"}, "score": 0.9}
    ]
    return manager


class TestHookPath:
    """The real register_hooks + MessageAddedEvent path marks and persists correctly."""

    def test_dispatch_marks_block_but_persists_neither_marker_nor_memory(self):
        manager = _hook_ready_manager()
        registry = HookRegistry()
        manager.register_hooks(registry)

        message = {"role": "user", "content": [{"text": _USER_QUESTION}]}
        agent = _FakeAgent("a-1", [message])
        event = MessageAddedEvent(agent=agent, message=message)

        registry.invoke_callbacks(event)

        # (a) The injected memory block carries the marker after dispatch.
        content = agent.messages[-1]["content"]
        marked = [block for block in content if block.get(MEMORY_CONTEXT_MARKER_KEY) is True]
        assert len(marked) == 1, "the injected memory block must be marked"
        assert "BATTLE ROYALE" in marked[0]["text"]

        # (b) Persistence runs before injection, so the create_event payload
        # contains neither the marker key nor the memory text. The payload is a
        # list of (serialized-json, role) tuples.
        manager.memory_client.create_event.assert_called_once()
        persisted_messages = manager.memory_client.create_event.call_args.kwargs["messages"]
        serialized = "".join(text for text, _role in persisted_messages)
        assert MEMORY_CONTEXT_MARKER_KEY not in serialized, "marker key must not reach persisted events"
        assert "BATTLE ROYALE" not in serialized, "retrieved memory must not reach persisted events"


class TestWiring:
    """The orchestrator and model factory use the scoped classes."""

    def test_model_factory_returns_scoped_model(self):
        # Local modules
        from models.cached_bedrock import create_specialist_bedrock_model, reset_model_cache

        reset_model_cache()
        model = create_specialist_bedrock_model()
        assert isinstance(model, GuardrailScopedBedrockModel)
        reset_model_cache()

    def test_orchestrator_builds_scoped_session_manager(self):
        # Local modules
        import agents.orchestrator as orch

        assert orch.AgentCoreMemorySessionManager is MemoryScopedSessionManager
