"""Unit tests for the orchestrator's AgentCore memory configuration (#155).

A turn interrupted between persisting an assistant `toolUse` and writing its
`toolResult` leaves an orphaned block in the (immutable) session history. Without
`filter_restored_tool_context`, that orphan is replayed every turn and Bedrock
Converse rejects the whole session, permanently bricking it. The orchestrator
must enable the strip-on-replay flag so a poisoned session self-heals.
"""

# Standard library
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
import agents.orchestrator as orch

# Capture the REAL run_orchestrator at import time. conftest's autouse
# `mock_all_mcp_and_agents` fixture patches `agents.orchestrator.run_orchestrator`
# for unit tests; we want the genuine implementation here so the config-building
# path actually executes.
_real_run_orchestrator = orch.run_orchestrator

pytestmark = pytest.mark.unit


def test_memory_config_enables_filter_restored_tool_context():
    """run_orchestrator must build AgentCoreMemoryConfig with the strip flag on."""
    captured = {}

    def fake_config(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch.object(orch, "USE_BEDROCK_SESSIONS", True),
        patch.object(orch, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-test-123"),
        patch.object(orch, "AgentCoreMemoryConfig", side_effect=fake_config),
        patch.object(orch, "AgentCoreMemorySessionManager", MagicMock()),
        patch.object(orch, "create_bedrock_model_with_overrides", MagicMock()),
        patch.object(orch, "create_cached_bedrock_model", MagicMock()),
        patch.object(orch, "Agent") as mock_agent,
    ):
        # Agent(...)(query) -> response string
        mock_agent.return_value.return_value = "ok"

        _real_run_orchestrator("hello", context={"user_id": "u-1", "session_id": "s-1"})

    assert captured.get("filter_restored_tool_context") is True, (
        "orchestrator must set filter_restored_tool_context=True so an orphaned "
        "toolUse in replayed history can't brick the session (#155)"
    )
    # Sanity: it still wires the core identity fields.
    assert captured.get("session_id") == "s-1"
    assert captured.get("actor_id") == "u-1"
