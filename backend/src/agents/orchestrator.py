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
from agents.specialist_capture import begin_specialist_capture, finish_specialist_capture
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
# Deterministic failure for a *fresh* cost-bearing request (not a report-ID
# follow-up) that never produced an authoritative rendering — including a cost
# specialist that failed before invoking get_cost_report. Contains no numbers.
_COST_REPORT_UNAVAILABLE = (
    "## Cost Report Unavailable\n\n"
    "This cost request did not complete through the validated Cost Explorer report path, "
    "so no spending figures can be shown. Retry the cost question, optionally specifying an "
    "explicit start and end date.\n\n"
    "No unverified financial report was produced."
)
_COST_ADVISORY_GUIDANCE = (
    "## Cost Guidance\n\n"
    "Review utilization, right-sizing, idle resources, commitment options, and workload scheduling. "
    "Use the validated Cost Explorer report path for account totals, shares, historical spending, "
    "or numeric forecasts. No unverified financial value was shown."
)
_COST_TOPIC_PATTERN = re.compile(
    r"\b(?:cost|costs|costing|spend|spending|billing|bills?|budgets?|prices?|pricing|forecast|"
    r"savings?|discounts?|invoice|expenses?|charges?|fees?|estimates?|cheapest|expensive)\b",
    re.IGNORECASE,
)
_OPERATIONAL_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:eks|kubernetes|cluster|namespace|pod|deployment|resource|label|tag|annotation)\b"
    r"[^\n]{0,80}\b(?:billing|cost-center|cost\s+center)\b"
    r"|\b(?:billing|cost-center|cost\s+center)\b[^\n]{0,80}"
    r"\b(?:eks|kubernetes|cluster|namespace|pod|deployment|resource|label|tag|annotation)\b",
    re.IGNORECASE,
)

# Detection of requests that require an authoritative *account* cost snapshot.
# Advisory questions such as "How much does GameLift cost?", "cost optimization
# strategies", or "budget planning" intentionally do not match: those may use
# KB/MCP recommendations or public pricing without claiming actual account totals.
_FORECAST_COST_PATTERN = re.compile(
    r"\b(?:forecast|forecasting|projection|projected|next\s+(?:week|month|quarter|year)|future)\b",
    re.IGNORECASE,
)

_EXPLICIT_ACTUAL_COST_PATTERN = re.compile(
    r"\bhow\s+much\s+(?:am\s+i|are\s+we|did\s+i|did\s+we|have\s+i|have\s+we)\b"
    r"|\bcosting\s+(?:me|us)\b"
    r"|\b(?:my|our)\b[^\n]{0,60}\b(?:spend|spending|bill|charges?|expenditure)\b"
    r"|\b(?:today|yesterday|last\s+(?:week|month|quarter|year))\b[^\n]{0,80}"
    r"\b(?:costs?|bill|charges?|expenditure|savings?)\b"
    r"|\bwhat\s+(?:did|was)\b[^\n]{0,80}\b(?:cost|costs|bill|charges?|expenditure|savings?)\b",
    re.IGNORECASE,
)

_ADVISORY_COST_PATTERN = re.compile(
    r"\b(?:optim(?:ization|isation|ize|ise)|strateg(?:y|ies)|best\s+practices|cheapest|"
    r"reduce|saving|savings|architecture|pricing\s+models?)\b",
    re.IGNORECASE,
)

_COST_INTENT_PATTERN = re.compile(
    r"\b(?:cost report|cost breakdown|billing statement|invoice)\b"
    r"|\btotal\s+costs?\b"
    r"|\b(?:my|our)\b[^\n]{0,80}\b(?:costs?|spend|spending|bill|charges?|expenditure)\b"
    r"|\b(?:show|list|give|provide|summarize|break\s+down)\b[^\n]{0,100}"
    r"\b(?:costs?|spending|bill|charges?|expenditure)\b"
    r"|\b(?:costs?|spending|charges?|expenditure)\b[^\n]{0,60}\b(?:by\s+service|today|yesterday|"
    r"this\s+month|last\s+month|this\s+quarter|last\s+quarter|this\s+year|last\s+year|"
    r"for\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?)\b"
    r"|\b(?:today|yesterday|last\s+(?:week|month|quarter|year))\b[^\n]{0,80}"
    r"\b(?:costs?|bill|charges?|expenditure|savings?)\b"
    r"|\b(?:costs?|bill|charges?|expenditure|savings?)\b[^\n]{0,80}"
    r"\b(?:today|yesterday|last\s+(?:week|month|quarter|year))\b"
    r"|\bwhat\s+(?:did|was)\b[^\n]{0,80}\b(?:cost|costs|bill|charges?|expenditure|savings?)\b"
    r"|\bcosting\s+(?:me|us)\b"
    r"|\bhow\s+much\s+(?:am\s+i|are\s+we|did\s+i|did\s+we|have\s+i|have\s+we)\b",
    re.IGNORECASE,
)


def _is_cost_report_followup(query: str) -> bool:
    """Return whether a query references a production-format cost report ID."""
    return _COST_REPORT_ID_PATTERN.search(query) is not None


def _is_operational_identifier_query(query: str) -> bool:
    """Return whether financial vocabulary is used only as an AWS identifier."""
    return _OPERATIONAL_IDENTIFIER_PATTERN.search(query) is not None


def _has_cost_intent(query: str) -> bool:
    """Return whether a query requires an authoritative historical cost report."""
    if _FORECAST_COST_PATTERN.search(query):
        return False
    if _EXPLICIT_ACTUAL_COST_PATTERN.search(query):
        return True
    if _is_operational_identifier_query(query):
        return False
    if _ADVISORY_COST_PATTERN.search(query):
        return False
    return _COST_INTENT_PATTERN.search(query) is not None


def _has_cost_topic(query: str) -> bool:
    """Return whether a query enters the cost/pricing advisory domain."""
    if _is_operational_identifier_query(query):
        return False
    return _COST_TOPIC_PATTERN.search(query) is not None


# The cost specialist reports under this service name; its output is sourced from
# the authoritative cost rendering, never re-used as a composed prose section.
_COST_SERVICE_NAME = "cost"
_NON_COST_COMPLETION_SECTIONS = {
    "gamelift": (
        "## GameLift\n\n"
        "The GameLift specialist completed its operational lookup. Ask a focused GameLift follow-up "
        "to view the detailed fleet result without mixing it with financial output."
    ),
    "eks": (
        "## EKS\n\n"
        "The EKS specialist completed its operational lookup. Ask a focused EKS follow-up "
        "to view the detailed cluster result without mixing it with financial output."
    ),
}


def _cost_attempted(query: str, specialist_outputs: list[tuple[str, str]]) -> bool:
    """Whether the query requires an authoritative account-cost snapshot.

    Query intent is authoritative here. Merely invoking the Cost specialist is
    insufficient because advisory optimization and architecture questions may
    legitimately use documentation or recommendation tools without generating
    an account cost report.
    """
    del specialist_outputs  # Retained in the signature for call-site clarity.
    return _has_cost_intent(query)


def _safe_non_cost_sections(specialist_outputs: list[tuple[str, str]]) -> list[str]:
    """Return deterministic nonfinancial markers for consulted specialists.

    Captured specialist prose is model-authored and may contain arbitrary
    financial language. Mixed cost responses therefore expose only fixed
    service markers; users can request each operational result separately.
    """
    sections: list[str] = []
    for service_name, output in specialist_outputs:
        normalized = service_name.strip().lower()
        if normalized == _COST_SERVICE_NAME or not output or not output.strip():
            continue
        sections.append(
            _NON_COST_COMPLETION_SECTIONS.get(
                normalized,
                f"## {service_name.strip() or 'Specialist'}\n\nThe specialist completed its operational lookup.",
            )
        )
    return sections


def _cost_specialist_ran(specialist_outputs: list[tuple[str, str]]) -> bool:
    return any(name.strip().lower() == _COST_SERVICE_NAME for name, _ in specialist_outputs)


def _compose_advisory_response(specialist_outputs: list[tuple[str, str]]) -> str:
    """Compose safe operational context plus deterministic cost guidance.

    Model-authored Cost specialist prose is intentionally never returned unless
    it came through the authoritative report capture. Advisory/forecast requests
    receive deterministic, nonnumeric guidance instead.
    """
    return "\n\n".join([*_safe_non_cost_sections(specialist_outputs), _COST_ADVISORY_GUIDANCE])


def _compose_final_response(
    authoritative_section: str,
    specialist_outputs: list[tuple[str, str]],
    *,
    cost_only: bool,
) -> str:
    """Deterministically assemble the final answer around an authoritative cost section.

    ``authoritative_section`` is the only source of financial content — the
    validated cost tool's rendering, its typed error, or the deterministic
    "Cost Report Unavailable" failure. Captured non-cost specialist sections are
    sanitized (fail-closed) and placed ahead of it in fixed service order. The
    orchestrator model's own prose is discarded entirely, so no model-authored
    financial value can escape.

    ``cost_only`` (report-ID follow-ups) returns just the authoritative section,
    preserving the prior single-section behavior.
    """
    if cost_only:
        return authoritative_section

    non_cost_sections = _safe_non_cost_sections(specialist_outputs)
    if not non_cost_sections:
        return authoritative_section

    return "\n\n".join([*non_cost_sections, authoritative_section.strip()])


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
        specialist_capture = begin_specialist_capture()
        try:
            response: Any = agent(query)
        except Exception:
            authoritative_cost_response = finish_cost_report_capture(cost_report_capture)
            specialist_outputs = finish_specialist_capture(specialist_capture)
            if authoritative_cost_response is not None:
                logger.warning(
                    "Orchestrator failed after a validated cost report; returning the deterministic rendering"
                )
                response = _compose_final_response(
                    authoritative_cost_response,
                    specialist_outputs,
                    cost_only=cost_report_followup,
                )
            elif cost_report_followup:
                logger.error(
                    "Cost report ID follow-up failed before producing a deterministic rendering",
                    exc_info=True,
                )
                response = _COST_REPORT_FOLLOWUP_FAILURE
            elif _cost_attempted(query, specialist_outputs):
                # A fresh account-report request raised before producing an
                # authoritative rendering. Fail closed and preserve only safe
                # non-cost specialist sections.
                logger.error(
                    "Cost-bearing request raised before a validated cost report; failing closed",
                    exc_info=True,
                )
                response = _compose_final_response(
                    _COST_REPORT_UNAVAILABLE,
                    specialist_outputs,
                    cost_only=False,
                )
            elif _cost_specialist_ran(specialist_outputs) or _has_cost_topic(query):
                # Advisory/forecast request: discard model-authored financial
                # prose and return deterministic nonnumeric guidance, composed
                # with any safe non-cost specialist context.
                response = _compose_advisory_response(specialist_outputs)
            else:
                # Pure operational failure with no cost involvement: preserve the
                # existing behavior and surface the error.
                raise
        else:
            authoritative_cost_response = finish_cost_report_capture(cost_report_capture)
            specialist_outputs = finish_specialist_capture(specialist_capture)
            if authoritative_cost_response is not None:
                # A cost-report tool produced the authoritative financial section.
                # Compose it deterministically with any captured GameLift/EKS
                # specialist output and discard the orchestrator's own prose, so
                # no model can replace or independently calculate those values
                # while multi-specialist context is preserved.
                response = _compose_final_response(
                    authoritative_cost_response,
                    specialist_outputs,
                    cost_only=cost_report_followup,
                )
            elif cost_report_followup:
                logger.error("Cost report ID follow-up returned without invoking the deterministic reuse path")
                response = _COST_REPORT_FOLLOWUP_FAILURE
            elif _cost_attempted(query, specialist_outputs):
                # A fresh account-report request completed without a validated
                # snapshot. Preserve safe operational sections and fail closed
                # for the financial portion.
                logger.warning("Cost-bearing request completed without a validated cost report; failing closed")
                response = _compose_final_response(
                    _COST_REPORT_UNAVAILABLE,
                    specialist_outputs,
                    cost_only=False,
                )
            elif _cost_specialist_ran(specialist_outputs):
                # Cost advisory/forecast path without a historical report. Use
                # captured specialist output only, and strip any unvalidated
                # financial claims instead of trusting orchestrator prose.
                response = _compose_advisory_response(specialist_outputs) or _COST_REPORT_UNAVAILABLE
            else:
                # If the routing model answers a cost topic directly, discard
                # its prose and return deterministic nonnumeric guidance. Pure
                # operational responses remain unchanged; mixed cost responses
                # never reach this branch because specialist capture is active.
                response = _COST_ADVISORY_GUIDANCE if _has_cost_topic(query) else response

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
