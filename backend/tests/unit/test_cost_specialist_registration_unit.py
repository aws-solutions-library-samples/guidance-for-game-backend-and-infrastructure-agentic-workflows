"""Cost specialist registration + wiring tests (#465).

Observe the ToolProviders actually passed to the Strands ``Agent`` when the REAL
Cost specialist runs, proving the Billing MCP client is wrapped by the
Cost-owned ``CostReportGuardedProvider`` before it can reach the model. Without
this, a regression that dropped ``mcp_client_transform=guard_cost_mcp_client``
would silently expose the raw 35-tool Billing catalog while every guard unit
test still passed.

The repo-wide autouse fixture (tests/conftest.py) replaces the module-level
``agents.cost_specialist.cost_agent`` with a mock, so importing that symbol
yields the mock. To observe the real registration wiring we build a fresh
specialist via ``create_specialist_agent`` using the EXACT arguments
``agents.cost_specialist`` passes — including the real
``guard_cost_mcp_client`` transform and ``create_cost_report_tool_bundle``
factory — then patch ``create_mcp_client`` and ``Agent`` at the
``agents.base_specialist`` call site and invoke the specialist's underlying
function.

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
    """A stand-in Billing MCP client (ToolProvider)."""

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


def _build_cost_like_specialist():
    """Build a fresh specialist wired exactly like ``agents.cost_specialist``."""
    # Local modules
    from agents.base_specialist import create_specialist_agent
    from agents.cost_mcp_guard import guard_cost_mcp_client
    from agents.cost_report import create_cost_report_tool_bundle
    from agents.optimized_prompts import get_optimized_cost_prompt
    from config.settings import COST_KB_ID

    def _fallback(region: str) -> str:
        return "aws ce get-cost-and-usage"

    return create_specialist_agent(
        service_name="Cost",
        emoji="💰",
        mcp_server_names=["billing-cost-management-mcp-server"],
        kb_id=COST_KB_ID,
        prompt_fn=get_optimized_cost_prompt,
        fallback_fn=_fallback,
        additional_tools=None,
        additional_tools_factory=create_cost_report_tool_bundle,
        mcp_client_transform=guard_cost_mcp_client,
    )


def _run_real_cost_specialist_capturing_tools():
    specialist = _build_cost_like_specialist()
    captured: dict[str, Any] = {}

    class _FakeAgent:
        def __init__(self, *args, **kwargs):
            captured["tools"] = kwargs.get("tools")

        def __call__(self, query: str) -> str:
            return "Cost analysis result"

    def _fake_create_mcp_client(server_name: str):
        return _SentinelProvider(server_name)

    with (
        patch("agents.base_specialist.create_mcp_client", side_effect=_fake_create_mcp_client),
        patch("agents.base_specialist.Agent", _FakeAgent),
        patch("agents.base_specialist.create_specialist_bedrock_model", return_value=MagicMock()),
        patch("agents.base_specialist.create_bedrock_model_with_overrides", return_value=MagicMock()),
        patch("agents.base_specialist.create_kb_retrieve_tool", return_value=lambda *a, **k: ""),
    ):
        specialist._tool_func("what is my spend")
    return captured


def test_billing_client_is_guard_wrapped_before_reaching_agent():
    # Local modules
    from agents.cost_mcp_guard import CostReportGuardedProvider

    captured = _run_real_cost_specialist_capturing_tools()
    tools = captured["tools"]
    assert tools is not None
    assert any(isinstance(t, CostReportGuardedProvider) for t in tools), "Billing client must be guard-wrapped"
    # No bare (unguarded) sentinel Billing provider may reach the Agent.
    assert not any(isinstance(t, _SentinelProvider) for t in tools), "raw Billing client reached the Agent"


def test_cost_specialist_module_wires_the_guard_transform():
    # Local modules
    import agents.cost_specialist as cost_specialist
    from agents.cost_mcp_guard import guard_cost_mcp_client

    assert cost_specialist.guard_cost_mcp_client is guard_cost_mcp_client


def test_cost_specialist_source_passes_guard_transform_keyword():
    """AST-pin the REAL source: the module-level ``create_specialist_agent``
    call in ``agents/cost_specialist.py`` MUST pass
    ``mcp_client_transform=guard_cost_mcp_client``. This fails if that keyword
    is deleted from the real module, which the import-only check cannot detect.
    """
    # Standard library
    import ast
    import importlib.util

    spec = importlib.util.find_spec("agents.cost_specialist")
    assert spec and spec.origin
    with open(spec.origin, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    wired = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "create_specialist_agent":
            continue
        for kw in node.keywords:
            if kw.arg == "mcp_client_transform" and isinstance(kw.value, ast.Name):
                assert kw.value.id == "guard_cost_mcp_client", kw.value.id
                wired = True
    assert wired, "cost_specialist.py must call create_specialist_agent(mcp_client_transform=guard_cost_mcp_client)"


def test_cost_specialist_module_reload_passes_the_guard_transform():
    """Reload the REAL module with ``create_specialist_agent`` patched and assert
    the captured keyword. Together with the AST check this makes the mutation
    'delete mcp_client_transform=guard_cost_mcp_client from cost_specialist.py'
    turn these tests red."""
    # Standard library
    import importlib

    # Local modules
    import agents.cost_specialist as cost_specialist
    from agents.cost_mcp_guard import guard_cost_mcp_client

    captured_kwargs: dict[str, Any] = {}

    def _capturing_create(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with patch("agents.base_specialist.create_specialist_agent", _capturing_create):
        # cost_specialist imports the symbol by name; patch it in that namespace too.
        with patch.object(cost_specialist, "create_specialist_agent", _capturing_create):
            importlib.reload(cost_specialist)

    try:
        assert captured_kwargs.get("mcp_client_transform") is guard_cost_mcp_client
    finally:
        # Restore the real module so later tests see the genuine wiring.
        importlib.reload(cost_specialist)
