#!/usr/bin/env python3
"""
# Orchestrator Agent

A specialized Strands agent that orchestrates game infrastructure management
by routing queries to appropriate specialist agents.

## What This Agent Does

Routes user queries about AWS game infrastructure to the most appropriate specialist:
- GameLift Agent: For fleet management, game server hosting
- EKS Agent: For Kubernetes cluster and pod management
- Cost Agent: For AWS spending analysis and optimization

## Memory Integration

Uses native AWS AgentCore Memory integration via AgentCoreMemorySessionManager.
Memory is automatically handled when session_manager is provided.
"""

# Standard library
import re
from typing import Any

# Third-party packages
from strands import Agent

# Local modules
from agents.cost_report import begin_cost_report_capture, finish_cost_report_capture
from agents.cost_specialist import cost_agent
from agents.eks_specialist import eks_agent
from agents.gamelift_specialist import gamelift_agent
from agents.optimized_prompts import get_optimized_orchestrator_prompt, get_prompt_versions
from config.settings import (
    AGENT_MAX_TURNS_ORCHESTRATOR,
    AGENT_TIMEOUT_ORCHESTRATOR_SECONDS,
    AWS_REGION,
    BEDROCK_AGENTCORE_MEMORY_ID,
    INFERENCE_CONFIG,
    USE_BEDROCK_SESSIONS,
)
from models.cached_bedrock import create_bedrock_model_with_overrides, create_cached_bedrock_model
from utils.logger import logger
from utils.max_turns_hook import MaxTurnsHook
from utils.wall_clock_timeout_hook import WallClockTimeoutHook

# Optional memory integration imports (may not be available in all environments)
try:
    # Third-party packages
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    MEMORY_INTEGRATION_AVAILABLE = True
except ImportError:
    MEMORY_INTEGRATION_AVAILABLE = False

# Optional semantic memory imports
try:
    # Local modules
    from utils.semantic_memory import extract_and_save_user_info

    SEMANTIC_MEMORY_AVAILABLE = True
except ImportError:
    SEMANTIC_MEMORY_AVAILABLE = False


_COST_REPORT_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])cost-[0-9a-f]{32}(?![A-Za-z0-9_-])", re.IGNORECASE)
_COST_REPORT_FOLLOWUP_FAILURE = (
    "## Cost Report Unavailable\n\n"
    "The report follow-up did not complete through the validated snapshot path. "
    "Retry with the report ID and exact service names.\n\n"
    "No unverified financial report was produced."
)


def _is_cost_report_followup(query: str) -> bool:
    """Return whether a query references a production-format cost report ID."""
    return _COST_REPORT_ID_PATTERN.search(query) is not None


def run_orchestrator(query: str, context: dict = None):
    """
    Orchestrator with native AgentCore Memory integration.

    Args:
        query: User query string
        context: Request context with user_id and session_id for memory

    Returns:
        Agent response string
    """
    try:
        logger.info(f"🎮 Orchestrator processing query ({len(query)} chars)")
        logger.info(f"📋 Prompt versions: {get_prompt_versions()}")

        cost_report_followup = _is_cost_report_followup(query)
        agent_tools = [cost_agent] if cost_report_followup else [gamelift_agent, eks_agent, cost_agent]
        if cost_report_followup:
            logger.info("Routing cost report ID follow-up exclusively through the cost specialist")

        # Per-agent inference parameters (WA GenAI Lens: Performance Efficiency 2)
        inf = INFERENCE_CONFIG.get("orchestrator")
        orch_model = create_bedrock_model_with_overrides(**inf) if inf else create_cached_bedrock_model()

        # Extract memory parameters from context
        actor_id = None
        session_id = None
        if context and isinstance(context, dict):
            actor_id = context.get("user_id") or context.get("actor_id")
            session_id = context.get("session_id")
            display_name = context.get("display_name", "user")
            logger.debug(f"👤 User: {display_name}, Actor: {actor_id}, Session: {session_id}")

        # Create agent with or without memory
        if USE_BEDROCK_SESSIONS and BEDROCK_AGENTCORE_MEMORY_ID and actor_id and session_id:
            # Use native AgentCore Memory integration
            logger.debug(f"🧠 Creating agent with AgentCore Memory: {BEDROCK_AGENTCORE_MEMORY_ID}")

            try:
                # Create memory config with retrieval config for LTM
                # Namespace pattern: {actorId} - scoped to user for cross-session memory
                config = AgentCoreMemoryConfig(
                    memory_id=BEDROCK_AGENTCORE_MEMORY_ID,
                    session_id=session_id,
                    actor_id=actor_id,
                    retrieval_config={
                        "{actorId}": RetrievalConfig(
                            top_k=10,  # Retrieve more memories for better context
                            relevance_score=0.25,  # Lower threshold to catch name/identity queries
                            strategy_id="user_facts",  # LTM semantic memory strategy
                        )
                    },
                    # Strip historical toolUse/toolResult blocks when replaying a
                    # session's stored events into model context. AgentCore events
                    # are immutable, and an interrupted turn (request timeout, a
                    # parallel-tool exception, process death) can persist a toolUse
                    # with no matching toolResult. Without this, that orphan is
                    # replayed every turn and Bedrock Converse rejects the whole
                    # session ("toolResult blocks exceed toolUse blocks of previous
                    # turn") — permanently bricking it, regardless of model. We keep
                    # the conversational text; prior tool I/O is re-fetchable. (#155)
                    filter_restored_tool_context=True,
                )
                logger.debug(f"✅ Config created with LTM retrieval enabled")
                logger.debug(f"   Namespace pattern: {{actorId}} (resolves to: {actor_id})")
                logger.debug(f"   Retrieval: top_k=10, relevance_score=0.25, strategy=user_facts")

                # Create session manager and agent with memory
                session_manager = AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=AWS_REGION)
                agent = Agent(
                    system_prompt=get_optimized_orchestrator_prompt(),
                    tools=agent_tools,
                    model=orch_model,
                    session_manager=session_manager,
                    hooks=[
                        MaxTurnsHook(AGENT_MAX_TURNS_ORCHESTRATOR),
                        WallClockTimeoutHook(AGENT_TIMEOUT_ORCHESTRATOR_SECONDS),
                    ],
                )
                logger.debug("✅ Agent created with memory")

            except Exception as e:
                logger.warning(f"⚠️ Memory setup failed, using fallback: {e}")
                agent = None
        else:
            logger.debug("ℹ️  Memory not configured, using agent without memory")
            agent = None

        # Fallback: Create agent without memory
        if agent is None:
            agent = Agent(
                system_prompt=get_optimized_orchestrator_prompt(),
                tools=agent_tools,
                model=orch_model,
                hooks=[
                    MaxTurnsHook(AGENT_MAX_TURNS_ORCHESTRATOR),
                    WallClockTimeoutHook(AGENT_TIMEOUT_ORCHESTRATOR_SECONDS),
                ],
            )

        # Run the agent
        logger.debug("🚀 Running agent...")
        cost_report_capture = begin_cost_report_capture()
        try:
            response: Any = agent(query)
        except Exception:
            authoritative_cost_response = finish_cost_report_capture(cost_report_capture)
            if authoritative_cost_response is None:
                if not cost_report_followup:
                    raise
                logger.error(
                    "Cost report ID follow-up failed before producing a deterministic rendering",
                    exc_info=True,
                )
                response = _COST_REPORT_FOLLOWUP_FAILURE
            else:
                logger.warning(
                    "Orchestrator failed after a validated cost report; returning the deterministic rendering"
                )
                response = authoritative_cost_response
        else:
            authoritative_cost_response = finish_cost_report_capture(cost_report_capture)
            if authoritative_cost_response is not None:
                # A cost-report tool has already produced the complete financial
                # section. Bypass both specialist and orchestrator prose so no
                # model can replace or independently calculate those values.
                response = authoritative_cost_response
            elif cost_report_followup:
                logger.error("Cost report ID follow-up returned without invoking the deterministic reuse path")
                response = _COST_REPORT_FOLLOWUP_FAILURE

        # Extract and save semantic memories for LTM (non-blocking)
        if USE_BEDROCK_SESSIONS and BEDROCK_AGENTCORE_MEMORY_ID and actor_id:
            if SEMANTIC_MEMORY_AVAILABLE:
                try:
                    extract_and_save_user_info(actor_id, query, str(response))
                except Exception as e:
                    logger.debug(f"⚠️ Semantic memory extraction skipped: {e}")

        logger.info(f"✅ Orchestrator complete ({len(str(response))} chars)")
        return response

    except Exception as e:
        logger.error(f"❌ Orchestrator error: {e}")
        raise
