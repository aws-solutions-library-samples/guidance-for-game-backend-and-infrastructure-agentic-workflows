"""
EKS specialist agent.

Handles EKS cluster management, Kubernetes operations, troubleshooting,
and optimization using the AWS API and EKS MCP servers.
"""

# Local modules
from agents.base_specialist import create_specialist_agent
from agents.optimized_prompts import get_optimized_eks_prompt
from config.settings import AWS_REGION, EKS_KB_ID
from utils.mcp_client_factory import create_mcp_client


def _get_eks_aws_cli_fallback(region: str) -> str:
    """Provide AWS CLI guidance when EKS MCP servers are unavailable."""
    return f"""EKS MCP servers unavailable. Use AWS CLI and kubectl:

**List Clusters:**
```bash
aws eks list-clusters --region {region}
```

**Cluster Details:**
```bash
aws eks describe-cluster --name <cluster-name> --region {region}
```

**Node Groups:**
```bash
aws eks list-nodegroups --cluster-name <cluster-name> --region {region}
aws eks describe-nodegroup --cluster-name <cluster-name> --nodegroup-name <nodegroup-name> --region {region}
```

**Update kubeconfig:**
```bash
aws eks update-kubeconfig --region {region} --name <cluster-name>
```

**Kubernetes Operations:**
```bash
kubectl get nodes
kubectl get pods --all-namespaces
kubectl describe pod <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace>
```"""


# ============================================================================
# EKS Agent (using factory pattern with dual MCP)
# ============================================================================

# EKS agent needs BOTH MCP servers:
# - AWS API MCP: Discovers EKS clusters via `aws eks list-clusters` (resource discovery)
# - EKS MCP: Gets cluster details and runs Kubernetes operations

eks_agent = create_specialist_agent(
    service_name="EKS",
    emoji="☸️",
    mcp_server_names=["aws-api-mcp-server", "eks-mcp-server"],
    kb_id=EKS_KB_ID,
    prompt_fn=get_optimized_eks_prompt,
    fallback_fn=_get_eks_aws_cli_fallback,
    additional_tools=None,
)
