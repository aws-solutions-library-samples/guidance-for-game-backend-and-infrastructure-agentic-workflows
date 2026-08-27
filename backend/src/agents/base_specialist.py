"""
Base Specialist Agent Factory.

Provides a factory function to create specialist agents with consistent patterns,
reducing code duplication and ensuring uniform behavior across all specialists.
"""

# Standard library
from typing import Any, Callable, List, Optional

# Third-party packages
from strands import Agent, tool

# Local modules
from config.settings import AGENT_MAX_TURNS_SPECIALIST, AGENT_TIMEOUT_SPECIALIST_SECONDS, AWS_REGION, INFERENCE_CONFIG
from models.cached_bedrock import create_bedrock_model_with_overrides, create_specialist_bedrock_model
from utils.kb_tools import create_kb_retrieve_tool
from utils.logger import logger
from utils.max_turns_hook import MaxTurnsHook
from utils.mcp_client_factory import create_mcp_client
from utils.timing import time_operation
from utils.wall_clock_timeout_hook import WallClockTimeoutHook


def create_specialist_agent(
    service_name: str,
    emoji: str,
    mcp_server_names: Optional[List[str]],
    kb_id: Optional[str],
    prompt_fn: Callable[[], str],
    fallback_fn: Optional[Callable[[str], str]] = None,
    additional_tools: Optional[List] = None,
    additional_tools_factory: Optional[Callable[[], tuple[List, Callable[[str], str]]]] = None,
    mcp_client_transform: Optional[Callable[[str, Any], Any]] = None,
):
    """
    Factory function to create specialist agents with consistent patterns.

    Args:
        service_name: Display name (e.g., "GameLift", "EKS", "Cost")
        emoji: Emoji for logging (e.g., "🎮", "☸️", "💰")
        mcp_server_names: List of MCP servers to use (None for boto3-only agents)
        kb_id: Knowledge Base ID (None to skip KB integration)
        prompt_fn: Function that returns system prompt
        fallback_fn: Fallback function when MCP unavailable (optional)
        additional_tools: Extra tools to add (e.g., boto3 tools)
        additional_tools_factory: Per-request tools plus a response finalizer. Use
            this when tools need request-local state or deterministic rendering.
        mcp_client_transform: Optional wrapper applied to each available MCP client.

    Returns:
        Agent tool function decorated with @tool
    """

    # Create function with unique name BEFORE applying @tool decorator
    def create_agent_function():
        def agent_func(query: str) -> str:
            with time_operation(f"{service_name.lower()}_agent_total", {"query_length": len(query)}):
                logger.debug(f"{emoji} {service_name} agent processing query ({len(query)} chars)")

                # Build tools list
                tools = list(additional_tools) if additional_tools else []
                response_finalizer = None
                if additional_tools_factory:
                    request_tools, response_finalizer = additional_tools_factory()
                    tools.extend(request_tools)

                # Add MCP clients if specified (uses caching for performance)
                mcp_clients_created = 0
                if mcp_server_names:
                    for server_name in mcp_server_names:
                        mcp_client = create_mcp_client(server_name)
                        if mcp_client:
                            if mcp_client_transform:
                                mcp_client = mcp_client_transform(server_name, mcp_client)
                            tools.append(mcp_client)
                            mcp_clients_created += 1

                    # If no MCP clients created and fallback exists, use it
                    if mcp_clients_created == 0 and fallback_fn and not tools:
                        logger.warning(f"⚠️ {service_name} MCP unavailable, using fallback")
                        return fallback_fn(AWS_REGION)

                # Add KB tool if configured
                if kb_id:
                    kb_retrieve = create_kb_retrieve_tool(kb_id, AWS_REGION)
                    tools.append(kb_retrieve)
                    logger.debug(f"✅ {service_name} KB enabled")

                # Create and run agent
                try:
                    # Per-agent inference parameters (WA GenAI Lens: Performance Efficiency 2)
                    agent_key = service_name.lower()
                    inf = INFERENCE_CONFIG.get(agent_key)
                    if inf:
                        model = create_bedrock_model_with_overrides(**inf)
                    else:
                        # No pinned entry (new or renamed service_name). Fall back to the
                        # specialist role model, never the orchestrator's fast routing model.
                        logger.warning(
                            f"No INFERENCE_CONFIG entry for '{agent_key}'; using the specialist role model. "
                            f"Add a pinned entry for {service_name} to control its inference settings."
                        )
                        model = create_specialist_bedrock_model()

                    with time_operation(f"{agent_key}_agent_execution", {"query_length": len(query)}):
                        specialist = Agent(
                            system_prompt=prompt_fn(),
                            tools=tools,
                            model=model,
                            hooks=[
                                MaxTurnsHook(AGENT_MAX_TURNS_SPECIALIST),
                                WallClockTimeoutHook(AGENT_TIMEOUT_SPECIALIST_SECONDS),
                            ],
                        )

                        result = str(specialist(query))
                        if response_finalizer:
                            result = response_finalizer(result)
                        logger.debug(f"{emoji} {service_name} complete ({len(result)} chars)")

                    return result

                except Exception:
                    # Log the full exception (with traceback) server-side, but do
                    # NOT interpolate it into the user-facing string — the raw
                    # message can leak internal details (ARNs, stack frames, SDK
                    # errors). Return a generic message instead.
                    logger.error(f"❌ {service_name} agent failed", exc_info=True)
                    if fallback_fn:
                        return fallback_fn(AWS_REGION)
                    return f"Unable to process the {service_name} request right now. Please try again."

        # Set unique name and docstring
        agent_func.__name__ = f"{service_name.lower()}_agent"
        agent_func.__doc__ = f"""{service_name} specialist agent for AWS infrastructure management.

Handles {service_name}-specific queries including monitoring, optimization, and troubleshooting.

Args:
    query: The {service_name}-related query from the user

Returns:
    str: Comprehensive {service_name} analysis and recommendations
"""
        return agent_func

    # Create the function and apply @tool decorator
    specialist_agent = tool(create_agent_function())

    return specialist_agent
