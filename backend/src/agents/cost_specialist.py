"""
AWS Cost Explorer specialist agent.

Provides comprehensive cost analysis, optimization recommendations,
and spending insights using AWS Cost Explorer MCP server.
"""

# Local modules
from agents.base_specialist import create_specialist_agent
from agents.optimized_prompts import get_optimized_cost_prompt
from config.settings import COST_KB_ID


def _get_cost_aws_cli_fallback(region: str) -> str:
    """Provide AWS CLI guidance when Cost Explorer MCP is unavailable."""
    return f"""Cost Explorer MCP server unavailable. Use AWS CLI:

**Current Month Costs:**
```bash
aws ce get-cost-and-usage \\
  --time-period Start=$(date +%Y-%m-01),End=$(date +%Y-%m-%d) \\
  --granularity MONTHLY \\
  --metrics BlendedCost \\
  --region {region}
```

**Service Breakdown:**
```bash
aws ce get-cost-and-usage \\
  --time-period Start=$(date +%Y-%m-01),End=$(date +%Y-%m-%d) \\
  --granularity MONTHLY \\
  --metrics BlendedCost \\
  --group-by Type=DIMENSION,Key=SERVICE \\
  --region {region}
```

**Cost Optimization:**
```bash
aws ce get-rightsizing-recommendation --region {region}
aws ce get-savings-utilization --region {region}
```"""


# ============================================================================
# Cost Agent (using factory pattern)
# ============================================================================

cost_agent = create_specialist_agent(
    service_name="Cost",
    emoji="💰",
    mcp_server_names=["billing-cost-management-mcp-server"],
    kb_id=COST_KB_ID,
    prompt_fn=get_optimized_cost_prompt,
    fallback_fn=_get_cost_aws_cli_fallback,
    additional_tools=None,
)
