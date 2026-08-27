"""Unit tests for the Billing MCP historical-cost guard."""

# Standard library
import asyncio
from typing import Any, Sequence

# Third-party packages
import pytest
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

# Local modules
from agents.cost_mcp_guard import CostReportGuardedProvider, guard_cost_mcp_client

pytestmark = pytest.mark.unit


class _RecordingTool(AgentTool):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.calls: list[ToolUse] = []

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {
            "name": self._name,
            "description": "Original MCP description",
            "inputSchema": {"json": {"type": "object"}},
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        self.calls.append(tool_use)
        yield ToolResultEvent(
            {
                "toolUseId": tool_use["toolUseId"],
                "status": "success",
                "content": [{"text": "delegated"}],
            }
        )


class _RecordingProvider(ToolProvider):
    def __init__(self, tools: Sequence[AgentTool]) -> None:
        self.tools = tools
        self.added: list[Any] = []
        self.removed: list[Any] = []

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        return self.tools

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.added.append(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.removed.append(consumer_id)


async def _invoke(tool: AgentTool, operation: str) -> list[ToolResultEvent]:
    tool_use: ToolUse = {
        "toolUseId": "test-tool-use",
        "name": tool.tool_name,
        "input": {"operation": operation},
    }
    return [event async for event in tool.stream(tool_use, {})]


def test_historical_cost_operations_are_rejected_without_calling_mcp():
    delegate_tool = _RecordingTool("cost-explorer")
    provider = CostReportGuardedProvider(_RecordingProvider([delegate_tool]))
    guarded_tool = asyncio.run(provider.load_tools())[0]

    events = asyncio.run(_invoke(guarded_tool, "getCostAndUsage"))

    assert delegate_tool.calls == []
    assert events[0].tool_result["status"] == "error"
    assert "Use get_cost_report" in events[0].tool_result["content"][0]["text"]
    assert "historical Cost Explorer operation is disabled" in guarded_tool.tool_spec["description"]


def test_forecast_operations_and_non_cost_tools_are_unchanged():
    cost_explorer = _RecordingTool("cost-explorer")
    optimization = _RecordingTool("cost-optimization")
    provider = CostReportGuardedProvider(_RecordingProvider([cost_explorer, optimization]))
    loaded_tools = asyncio.run(provider.load_tools())

    events = asyncio.run(_invoke(loaded_tools[0], "getCostForecast"))

    assert events[0].tool_result["status"] == "success"
    assert cost_explorer.calls[0]["input"]["operation"] == "getCostForecast"
    assert loaded_tools[1] is optimization


def test_provider_lifecycle_and_non_billing_clients_are_preserved():
    delegate = _RecordingProvider([])
    guarded = guard_cost_mcp_client("billing-cost-management-mcp-server", delegate)

    assert isinstance(guarded, CostReportGuardedProvider)
    guarded.add_consumer("agent-1")
    guarded.remove_consumer("agent-1")
    assert delegate.added == ["agent-1"]
    assert delegate.removed == ["agent-1"]
    assert guard_cost_mcp_client("eks-mcp-server", delegate) is delegate
