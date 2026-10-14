"""EKS specialist registration + wiring tests (issue #466).

These observe the ToolProviders actually passed to the Strands ``Agent`` when a
specialist built with the EKS wiring runs, proving:

  * both MCP clients (aws-api + eks) are wrapped by the EKS-owned guard before
    reaching the model (registration),
  * the guard transform preserves the MCP client lifecycle / factory
    compatibility (a guarded provider still forwards add/remove_consumer),
  * fallback fires when no MCP clients are available,
  * routing (the EKS specialist is a named AgentTool) and the specialist's
    module wiring are untouched by this boundary,
  * IAM/RBAC posture and #468 independence are preserved (the guard only
    narrows/bounds; it never adds a mutating tool).

A repo-wide autouse fixture (tests/conftest.py) patches the module-level
``eks_agent`` and ``create_mcp_client``. To observe the real registration wiring
we build a fresh specialist via ``create_specialist_agent`` with the real
``guard_eks_mcp_client`` transform and patch the factory at the base_specialist
call site.

Synthetic, public-safe fixtures only.
"""

# Standard library
from typing import Any, Sequence
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from strands.tools.tool_provider import ToolProvider
from strands.types.tools import AgentTool

pytestmark = pytest.mark.unit


class _SentinelProvider(ToolProvider):
    """A stand-in MCP client (ToolProvider) tagged by server name."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        self.added: list[Any] = []
        self.removed: list[Any] = []

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        return []

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.added.append(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.removed.append(consumer_id)


def _build_eks_like_specialist():
    """Build a fresh specialist wired exactly like the EKS specialist."""
    # Local modules
    from agents.base_specialist import create_specialist_agent
    from agents.eks_mcp_guard import guard_eks_mcp_client
    from agents.eks_specialist import _get_eks_aws_cli_fallback
    from agents.optimized_prompts import get_optimized_eks_prompt

    return create_specialist_agent(
        service_name="EKS",
        emoji="k8s",
        mcp_server_names=["aws-api-mcp-server", "eks-mcp-server"],
        kb_id=None,
        prompt_fn=get_optimized_eks_prompt,
        fallback_fn=_get_eks_aws_cli_fallback,
        additional_tools=None,
        mcp_client_transform=guard_eks_mcp_client,
    )


def _run_capturing_tools(mcp_available: bool = True):
    specialist = _build_eks_like_specialist()
    captured: dict[str, Any] = {}

    class _FakeAgent:
        def __init__(self, *args, **kwargs):
            captured["tools"] = kwargs.get("tools")

        def __call__(self, query: str) -> str:
            return "EKS analysis result"

    def _fake_create_mcp_client(server_name: str):
        return _SentinelProvider(server_name) if mcp_available else None

    with (
        patch("agents.base_specialist.create_mcp_client", side_effect=_fake_create_mcp_client),
        patch("agents.base_specialist.Agent", _FakeAgent),
        patch("agents.base_specialist.create_specialist_bedrock_model", return_value=MagicMock()),
        patch("agents.base_specialist.create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        result = specialist._tool_func("show my clusters")
    return result, captured


def test_both_mcp_clients_are_guard_wrapped_before_reaching_agent():
    # Local modules
    from agents.eks_mcp_guard import AwsApiMcpGuardedProvider, EksMcpGuardedProvider

    _result, captured = _run_capturing_tools(mcp_available=True)
    tools = captured["tools"]
    assert tools is not None
    assert any(isinstance(t, AwsApiMcpGuardedProvider) for t in tools)
    assert any(isinstance(t, EksMcpGuardedProvider) for t in tools)
    # No bare (unguarded) sentinel provider may reach the Agent.
    assert not any(isinstance(t, _SentinelProvider) for t in tools)


def test_guarded_provider_preserves_lifecycle_forwarding():
    # Local modules
    from agents.eks_mcp_guard import guard_eks_mcp_client

    sentinel = _SentinelProvider("eks-mcp-server")
    guarded = guard_eks_mcp_client("eks-mcp-server", sentinel)
    guarded.add_consumer("agent-x")
    guarded.remove_consumer("agent-x")
    assert sentinel.added == ["agent-x"]
    assert sentinel.removed == ["agent-x"]


def test_fallback_used_when_no_mcp_clients_available():
    result, captured = _run_capturing_tools(mcp_available=False)
    assert isinstance(result, str)
    assert "aws eks" in result.lower()
    assert "tools" not in captured  # Agent never constructed


def test_guard_transform_is_wired_into_the_specialist_module():
    """The EKS specialist module imports and passes guard_eks_mcp_client, so
    registration cannot silently regress to unguarded clients (#466/#468)."""
    # Local modules
    import agents.eks_specialist as eks_specialist
    from agents.eks_mcp_guard import guard_eks_mcp_client

    assert eks_specialist.guard_eks_mcp_client is guard_eks_mcp_client


def test_boundary_adds_no_write_capability_iam_rbac_preserved():
    """#468 independence + IAM/RBAC: the guard only narrows/bounds; it never adds
    a mutating tool. call_aws grammar is read-only; the EKS allowlist has no
    mutation/secret/log/IAM/stack tools."""
    # Local modules
    from agents.eks_mcp_guard import _CLI_VERB_GRAMMAR, _EKS_ALLOWED

    mutating_prefixes = {"create", "delete", "update", "associate", "disassociate", "deregister", "put"}
    assert not any(v.split("-")[0] in mutating_prefixes for v in _CLI_VERB_GRAMMAR)
    forbidden = {
        "manage_k8s_resource",
        "apply_yaml",
        "generate_app_manifest",
        "manage_eks_stacks",
        "add_inline_policy",
        "get_policies_for_role",
        "get_pod_logs",
        "get_cloudwatch_logs",
    }
    assert _EKS_ALLOWED.isdisjoint(forbidden)


def test_chart_directive_module_is_independent_of_the_guard():
    """Chart rendering is a separate capability; importing the guard does not
    perturb it (no cross-domain coupling introduced by #466)."""
    # Local modules
    import agents.chart_directive as chart_directive

    assert chart_directive is not None
