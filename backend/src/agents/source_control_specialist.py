"""
Source Control Connector specialist agent.

The single mutation path on the platform: instead of changing live AWS resources,
this specialist reads Infrastructure-as-Code (IaC) from the operator-configured
repository and proposes changes as pull requests for human review (GitOps).

Built with the shared ``create_specialist_agent`` factory and registered on the
Orchestrator only when the Connector is enabled and validly configured. Its system
prompt encodes the GitOps safety rules (read first, validate, never claim to mutate
live AWS, exactly one PR per change, unmerged proposals require human review), keeping
write intent isolated from the read-only GameLift/EKS/Cost specialists (design.md →
Agent shape).
"""

# Local modules
from agents.base_specialist import create_specialist_agent
from config.settings import SCM_IAC_KB_ID
from connector.tools import get_iac_file, propose_infrastructure_change

# ============================================================================
# System prompt (GitOps rules)
# ============================================================================


def get_source_control_prompt() -> str:
    """Get the Source Control Connector specialist system prompt.

    Encodes the non-negotiable GitOps rules the write path must follow.
    """
    return (
        "You are the Source Control specialist. You help operators change "
        "Infrastructure-as-Code (IaC) safely through a GitOps workflow.\n\n"
        "**Absolute rules — never violate these:**\n"
        "- You do NOT and CANNOT mutate live AWS resources. Never claim to have "
        "created, modified, scaled, or deleted any AWS resource. Your only action "
        "is proposing IaC changes as a pull request for human review.\n"
        "- Read the current IaC first with get_iac_file before proposing any change, "
        "so your proposal is consistent with the current source of truth.\n"
        "- Validate the change against what you read (correct file paths, coherent "
        "edits, matching IaC format) before calling propose_infrastructure_change.\n"
        "- Open EXACTLY ONE pull request per change. Do not open multiple PRs for a "
        "single requested change, and do not batch unrelated changes into one PR.\n"
        "- A proposal is created UNMERGED and requires human review, approval, and "
        "merge. You cannot merge, approve, or close a proposal. Make this clear to "
        "the user.\n\n"
        "**Tools:**\n"
        "- get_iac_file(paths): read existing IaC files from the configured "
        "repository/branch. Use this first.\n"
        "- propose_infrastructure_change(intent, files, iac_format, title, "
        "description): open one pull request with the complete set of modified files. "
        "iac_format is one of {\"cloudformation\", \"terraform\"}.\n"
        "- retrieve (when available): search IaC/GitOps documentation for patterns "
        "and best practices.\n\n"
        "Both tools return structured results. If a tool returns an error, missing "
        "files, or a declined/rejected status, relay the message plainly and do not "
        "retry blindly. After proposing, report the pull request URL and remind the "
        "user that a human must review and merge it.\n\n"
        "Use markdown formatting: ## headers, **bold**, bullet points."
    )


# ============================================================================
# Source Control Agent (using factory pattern)
# ============================================================================

# boto3/MCP not used: all operations go through the provider-agnostic connector
# tools. The optional IaC KB retrieve tool is wired by the factory only when
# SCM_IAC_KB_ID is configured (Req 3.5).
source_control_agent = create_specialist_agent(
    service_name="SourceControl",
    emoji="🔀",
    mcp_server_names=None,
    kb_id=SCM_IAC_KB_ID,
    prompt_fn=get_source_control_prompt,
    fallback_fn=None,
    additional_tools=[get_iac_file, propose_infrastructure_change],
)
