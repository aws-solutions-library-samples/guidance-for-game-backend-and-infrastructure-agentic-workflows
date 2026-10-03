"""
Source Control Connector specialist agent (read-only).

A read-only IaC-context capability: this specialist reads Infrastructure-as-Code (IaC)
from the operator-configured repository so it can answer questions about the current
infrastructure source of truth. It cannot and does not mutate any provider — the
provider-write path has moved to a separate operations control plane / isolated executor
(#314).

Built with the shared ``create_specialist_agent`` factory and registered on the
Orchestrator only when the Connector is enabled and validly configured. Its system prompt
encodes the read-only rules (review IaC, never claim to mutate live AWS or open pull
requests), consistent with the read-only GameLift/EKS/Cost specialists.
"""

# Local modules
from agents.base_specialist import create_specialist_agent
from agents.optimized_prompts import get_optimized_source_control_prompt
from connector.tools import get_iac_file

# ============================================================================
# System prompt (GitOps rules)
# ============================================================================
#
# The GitOps system prompt now flows through the platform's versioned/managed
# prompt path: get_optimized_source_control_prompt() returns the Bedrock Prompt
# Management text when GBAW_SOURCE_CONTROL_PROMPT_ARN resolves, else the
# code-defined SOURCE_CONTROL_PROMPT fallback in agents/optimized_prompts.py —
# mirroring the gamelift/eks/cost specialists.


# ============================================================================
# Source Control Agent (using factory pattern)
# ============================================================================

# boto3/MCP not used: all operations go through the provider-agnostic connector
# tools. The specialist is built with NO IaC Knowledge Base retrieve tool: the dead
# IaC-KB configuration was removed, so kb_id is None until a real IaC KB is provisioned
# (Req 9.1, 9.2).
source_control_agent = create_specialist_agent(
    service_name="SourceControl",
    emoji="🔀",
    mcp_server_names=None,
    kb_id=None,
    prompt_fn=get_optimized_source_control_prompt,
    fallback_fn=None,
    additional_tools=[get_iac_file],
)
