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

# Third-party packages
from strands import Agent

# Local modules
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

        # Per-agent inference parameters (WA GenAI Lens: Performance Efficiency 2)
        inf = INFERENCE_CONFIG.get("orchestrator", {})
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
                )
                logger.debug(f"✅ Config created with LTM retrieval enabled")
                logger.debug(f"   Namespace pattern: {{actorId}} (resolves to: {actor_id})")
                logger.debug(f"   Retrieval: top_k=10, relevance_score=0.25, strategy=user_facts")

                # Create session manager and agent with memory
                session_manager = AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=AWS_REGION)
                agent = Agent(
                    system_prompt=get_optimized_orchestrator_prompt(),
                    tools=[gamelift_agent, eks_agent, cost_agent],
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
                tools=[gamelift_agent, eks_agent, cost_agent],
                model=orch_model,
                hooks=[
                    MaxTurnsHook(AGENT_MAX_TURNS_ORCHESTRATOR),
                    WallClockTimeoutHook(AGENT_TIMEOUT_ORCHESTRATOR_SECONDS),
                ],
            )

        # Run the agent
        logger.debug("🚀 Running agent...")
        response = agent(query)

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
