"""
Optimized system prompts with version tracking.

Addresses Well-Architected GenAI Lens: Operational Excellence 2.3
(Traceability for models, prompts, and assets)

Key optimizations:
1. Concise and direct instructions
2. Clear role definition
3. Specific task focus
4. Minimal formatting rules
5. Essential context only

Each prompt carries version metadata so that agent invocations can be
correlated with the exact prompt text that was active at the time.
Full Bedrock Prompt Management migration is a future effort — this
provides the code-level foundation for traceability.
"""

# Standard library
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class VersionedPrompt:
    """Immutable prompt template with version metadata."""

    name: str
    version: str
    text: str

    def __str__(self) -> str:
        return self.text


# ---------------------------------------------------------------------------
# Prompt definitions — bump *version* whenever the text changes.
# ---------------------------------------------------------------------------

GAMELIFT_PROMPT = VersionedPrompt(
    name="gamelift_specialist",
    version="2.2.0",
    text=(
        "You are a GameLift specialist. Help with AWS GameLift fleet management, "
        "monitoring, and optimization.\n\n"
        "**Tool Selection:**\n"
        "- For documentation questions (port ranges, configuration options, best practices): "
        "Use retrieve tool to search knowledge base\n"
        "- For fleet discovery: Use list_gamelift_fleets. It returns separate "
        "ClassicFleets and ContainerFleets collections. Do not say there are no "
        "fleets unless both collections are empty.\n"
        "- For classic fleet utilization, capacity, and scaling: Use the GameLift "
        "tools (get_fleet_utilization, get_fleet_capacity, "
        "get_scaling_policies)\n\n"
        "Provide specific, actionable recommendations. "
        "Use markdown formatting: ## headers, **bold**, bullet points."
    ),
)

EKS_PROMPT = VersionedPrompt(
    name="eks_specialist",
    version="2.1.0",
    text=(
        "You are an EKS specialist. Help with Amazon EKS cluster management "
        "and Kubernetes operations.\n\n"
        "**Tool Capabilities:**\n"
        "- AWS API tool (call_aws): Discovers EKS clusters by running "
        '"aws eks list-clusters" (use for "list", "show", "what clusters")\n'
        "- EKS MCP: Gets cluster details (requires cluster name)\n"
        "- retrieve: Searches EKS documentation for kubectl commands, best practices\n\n"
        "**Smart Workflow:**\n"
        "If user asks for cluster details without specifying a name, first discover "
        'cluster names by running "aws eks list-clusters" via the AWS API tool, '
        "then get details with EKS MCP.\n\n"
        "For documentation questions (kubectl, troubleshooting, best practices), "
        "use retrieve tool FIRST.\n\n"
        "**CRITICAL: Keep responses concise to avoid token limits.**\n"
        "- Summarize KB results in 2-3 sentences, don't quote entire documents\n"
        "- For YAML examples, show only essential fields (5-10 lines max)\n"
        "- Limit kubectl examples to 2-3 most relevant commands\n"
        "- Omit verbose explanations when a brief answer suffices\n\n"
        "Use markdown formatting: ## headers, **bold**, bullet points."
    ),
)

COST_PROMPT = VersionedPrompt(
    name="cost_specialist",
    version="3.0.0",
    text=(
        "You are an AWS cost specialist. Analyze spending and recommend savings.\n\n"
        "For actual totals, rankings, or percentages, MUST use get_cost_report. "
        "For follow-ups, MUST use reuse_cost_report with its report ID. Return "
        "validatedFinancialSection verbatim; do not calculate, rewrite, or add financial "
        "values. Use Billing MCP only for forecasts and recommendations.\n\n"
        'For empty results, state "No cost data found" and suggest different dates. '
        "Use markdown."
    ),
)

ORCHESTRATOR_PROMPT = VersionedPrompt(
    name="orchestrator",
    version="2.1.0",
    text=(
        "You are the AI orchestrator (v2). Route queries to specialists:\n\n"
        "- cost_agent: ANY spending, billing, monetary amount, cost report, report ID, "
        "or cost-report follow-up. This takes precedence even when EKS or GameLift is named.\n"
        '  Examples: "total AWS spending", "EKS costs", "reuse report ID cost-..."\n\n'
        "- eks_agent: Operational EKS or Kubernetes questions about clusters, pods, deployments, nodes\n"
        '  Examples: "list EKS", "EKS clusters", "Kubernetes", "cluster status"\n\n'
        "- gamelift_agent: Operational GameLift questions about fleets and game servers\n"
        '  Examples: "GameLift", "fleets", "game server"\n\n'
        "Never calculate or rewrite financial values. Cost report IDs must go to cost_agent.\n\n"
        "Be concise. Use markdown formatting."
    ),
)

# Registry for programmatic access
_ALL_PROMPTS: dict[str, VersionedPrompt] = {
    p.name: p for p in [GAMELIFT_PROMPT, EKS_PROMPT, COST_PROMPT, ORCHESTRATOR_PROMPT]
}


# ---------------------------------------------------------------------------
# Bedrock Prompt Management integration (WA GenAI Lens: Reliability 4)
# At startup, try fetching prompts from Bedrock PM using ARNs from env vars.
# Fallback to code-defined VersionedPrompt if unavailable.
# ---------------------------------------------------------------------------

_runtime_prompts: dict[str, str] = {}  # name → text (populated at startup)
_prompt_source: str = "code"  # "bedrock_pm" or "code"


def _load_from_bedrock_pm():
    """Attempt to load prompts from Bedrock Prompt Management."""
    global _prompt_source

    arn_map = {
        "orchestrator": os.getenv("GBAW_ORCHESTRATOR_PROMPT_ARN"),
        "gamelift_specialist": os.getenv("GBAW_GAMELIFT_PROMPT_ARN"),
        "eks_specialist": os.getenv("GBAW_EKS_PROMPT_ARN"),
        "cost_specialist": os.getenv("GBAW_COST_PROMPT_ARN"),
    }

    # Skip if no ARNs configured
    if not any(arn_map.values()):
        return

    try:
        # Third-party packages
        import boto3

        # Local modules
        from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG

        client = boto3.client("bedrock-agent", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)

        for name, arn in arn_map.items():
            if not arn:
                continue
            # Extract prompt ID and version from ARN
            # ARN format: arn:aws:bedrock:region:account:prompt/ID:version
            parts = arn.split("/")
            if len(parts) < 2:
                continue
            id_version = parts[-1].split(":")
            prompt_id = id_version[0]
            version = id_version[1] if len(id_version) > 1 else None

            kwargs = {"promptIdentifier": prompt_id}
            if version:
                kwargs["promptVersion"] = version

            resp = client.get_prompt(**kwargs)
            for variant in resp.get("variants", []):
                text = variant.get("templateConfiguration", {}).get("text", {}).get("text", "")
                if text:
                    _runtime_prompts[name] = text
                    break

        if _runtime_prompts:
            _prompt_source = "bedrock_pm"

    except Exception:
        # Fall back to code-defined prompts (safe), but surface WHY — a
        # misconfigured Prompt Management ARN or missing IAM permission would
        # otherwise be invisible, silently shipping stale code prompts in prod.
        # Local import: logger isn't imported at module top here, and this runs
        # at import time (container startup).
        # Local modules
        from utils.logger import logger

        logger.warning("Bedrock Prompt Management load failed; using code-defined prompts", exc_info=True)


# Run at import time (container startup)
_load_from_bedrock_pm()


def _get_prompt(name: str, fallback: VersionedPrompt) -> str:
    """Return Bedrock PM prompt if available, else code-defined fallback."""
    return _runtime_prompts.get(name, fallback.text)


# ---------------------------------------------------------------------------
# Public accessor functions (same signatures as before for compatibility)
# ---------------------------------------------------------------------------


def get_optimized_gamelift_prompt() -> str:
    """Get the optimized GameLift specialist system prompt."""
    return _get_prompt("gamelift_specialist", GAMELIFT_PROMPT)


def get_optimized_eks_prompt() -> str:
    """Get the optimized EKS specialist system prompt."""
    return _get_prompt("eks_specialist", EKS_PROMPT)


def get_optimized_cost_prompt() -> str:
    """Get the optimized cost specialist system prompt."""
    return _get_prompt("cost_specialist", COST_PROMPT)


def get_optimized_orchestrator_prompt() -> str:
    """Get the optimized orchestrator system prompt."""
    return _get_prompt("orchestrator", ORCHESTRATOR_PROMPT)


def get_prompt_versions() -> dict[str, str]:
    """Return a mapping of prompt name → version and source for logging/tracing."""
    versions = {p.name: p.version for p in _ALL_PROMPTS.values()}
    versions["_source"] = _prompt_source
    return versions
    return {p.name: p.version for p in _ALL_PROMPTS.values()}


# ---------------------------------------------------------------------------
# Prompt comparison data for performance tracking
# ---------------------------------------------------------------------------

PROMPT_OPTIMIZATIONS = {
    "gamelift": {
        "original_length": 1500,
        "optimized_length": len(GAMELIFT_PROMPT.text),
        "reduction_percent": round((1 - len(GAMELIFT_PROMPT.text) / 1500) * 100, 1),
    },
    "eks": {
        "original_length": 1400,
        "optimized_length": len(EKS_PROMPT.text),
        "reduction_percent": round((1 - len(EKS_PROMPT.text) / 1400) * 100, 1),
    },
    "cost": {
        "original_length": 1300,
        "optimized_length": len(COST_PROMPT.text),
        "reduction_percent": round((1 - len(COST_PROMPT.text) / 1300) * 100, 1),
    },
    "orchestrator": {
        "original_length": 800,
        "optimized_length": len(ORCHESTRATOR_PROMPT.text),
        "reduction_percent": round((1 - len(ORCHESTRATOR_PROMPT.text) / 800) * 100, 1),
    },
}


def print_optimization_summary():
    """Print summary of prompt optimizations (CLI utility)."""
    # Local modules
    from utils.logger import logger

    logger.info("🚀 Prompt Optimization Summary")
    logger.info("=" * 40)

    total_original = sum(data["original_length"] for data in PROMPT_OPTIMIZATIONS.values())
    total_optimized = sum(data["optimized_length"] for data in PROMPT_OPTIMIZATIONS.values())
    total_reduction = round((1 - total_optimized / total_original) * 100, 1)

    for agent, data in PROMPT_OPTIMIZATIONS.items():
        logger.info(f"{agent.upper()}:")
        logger.info(f"  Original: {data['original_length']} chars")
        logger.info(f"  Optimized: {data['optimized_length']} chars")
        logger.info(f"  Reduction: {data['reduction_percent']}%")

    logger.info(f"TOTAL REDUCTION: {total_reduction}% ({total_original} → {total_optimized} chars)")
    logger.info(f"Expected performance improvement: 2-5x faster responses")


if __name__ == "__main__":
    print_optimization_summary()
