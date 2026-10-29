"""Unit tests for long-term memory writes on turns the Guardrail stopped.

The input Guardrail evaluates only the user's own words. Retrieved long-term
memory reaches the model as plain text, outside the input checks, so no fact is
stored from a turn the Guardrail stopped.
"""

# Standard library
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from strands.agent.agent_result import AgentResult
from strands.telemetry.metrics import EventLoopMetrics

# Local modules
import agents.orchestrator as orch
from utils.memory_identity import memory_actor_id

# conftest's autouse `mock_all_mcp_and_agents` fixture replaces
# `agents.orchestrator.run_orchestrator` in unit tests; keep the real one.
_real_run_orchestrator = orch.run_orchestrator

pytestmark = pytest.mark.unit

_QUERY = "My name is Zephyr. What fleets do I have?"
_CONTEXT = {"user_id": "actor-synthetic-1", "session_id": "session-synthetic-1"}


def _agent_result(stop_reason: str, text: str) -> AgentResult:
    message = {"role": "assistant", "content": [{"text": text}]}
    return AgentResult(stop_reason=stop_reason, message=message, metrics=EventLoopMetrics(), state={})


def _run(result):
    """Run the real orchestrator with memory enabled and return the memory-write mock."""
    extract = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch.object(orch, "USE_BEDROCK_SESSIONS", True))
        stack.enter_context(patch.object(orch, "BEDROCK_AGENTCORE_MEMORY_ID", "mem-synthetic-1"))
        stack.enter_context(patch.object(orch, "SEMANTIC_MEMORY_AVAILABLE", True))
        stack.enter_context(patch.object(orch, "extract_and_save_user_info", extract, create=True))
        stack.enter_context(patch.object(orch, "AgentCoreMemoryConfig", MagicMock()))
        stack.enter_context(patch.object(orch, "AgentCoreMemorySessionManager", MagicMock()))
        stack.enter_context(patch.object(orch, "create_bedrock_model_with_overrides", MagicMock()))
        stack.enter_context(patch.object(orch, "create_cached_bedrock_model", MagicMock()))
        mock_agent = stack.enter_context(patch.object(orch, "Agent"))
        mock_agent.return_value.return_value = result
        response = _real_run_orchestrator(_QUERY, context=_CONTEXT)
    return extract, response


def test_turn_stopped_by_guardrail_writes_no_long_term_memory():
    """A Guardrail-stopped turn returns its reply but stores nothing."""
    blocked = "I can only help with AWS GameLift, EKS, and cost management questions."
    extract, response = _run(_agent_result("guardrail_intervened", blocked))

    extract.assert_not_called()
    assert str(response).strip() == blocked


def test_completed_turn_still_writes_long_term_memory():
    """A turn that completed normally still extracts facts from the user's message."""
    extract, response = _run(_agent_result("end_turn", "You have 2 fleets."))

    extract.assert_called_once_with(memory_actor_id(_CONTEXT["user_id"]), _QUERY, str(response))


@pytest.mark.parametrize("stop_reason", ["max_tokens", "limit_turns", "tool_use"])
def test_other_stop_reasons_keep_the_existing_memory_write(stop_reason):
    """Only a Guardrail intervention skips the write."""
    extract, _ = _run(_agent_result(stop_reason, "Partial answer."))

    extract.assert_called_once()
