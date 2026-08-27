"""Restrict Billing MCP operations that bypass validated cost reports."""

from __future__ import annotations

# Standard library
from typing import Any, Sequence, cast

# Third-party packages
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

_BILLING_MCP_SERVER = "billing-cost-management-mcp-server"
_COST_EXPLORER_TOOL = "cost-explorer"
_REJECTED_OPERATIONS = frozenset({"getCostAndUsage", "getCostAndUsageWithResources"})
_REJECTION_MESSAGE = (
    "This historical Cost Explorer operation is disabled. Use get_cost_report so totals, "
    "rankings, and percentages come from one validated snapshot."
)


class _CostExplorerOperationGuard(AgentTool):
    """Reject unvalidated historical cost operations and delegate all others."""

    def __init__(self, delegate: AgentTool) -> None:
        super().__init__()
        self._delegate = delegate

    @property
    def tool_name(self) -> str:
        return self._delegate.tool_name

    @property
    def tool_spec(self) -> ToolSpec:
        spec = cast(ToolSpec, dict(self._delegate.tool_spec))
        spec["description"] = f"{_REJECTION_MESSAGE}\n\n{spec.get('description', '')}"
        return spec

    @property
    def tool_type(self) -> str:
        return self._delegate.tool_type

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        operation = tool_use.get("input", {}).get("operation")
        if operation in _REJECTED_OPERATIONS:
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [{"text": _REJECTION_MESSAGE}],
                }
            )
            return

        async for event in self._delegate.stream(tool_use, invocation_state, **kwargs):
            yield event


class CostReportGuardedProvider(ToolProvider):
    """Preserve Billing MCP lifecycle while guarding its Cost Explorer tool."""

    def __init__(self, delegate: ToolProvider) -> None:
        self._delegate = delegate

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        tools = await self._delegate.load_tools(**kwargs)
        return [_CostExplorerOperationGuard(tool) if tool.tool_name == _COST_EXPLORER_TOOL else tool for tool in tools]

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self._delegate.add_consumer(consumer_id, **kwargs)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self._delegate.remove_consumer(consumer_id, **kwargs)


def guard_cost_mcp_client(server_name: str, client: ToolProvider) -> ToolProvider:
    """Guard historical cost operations on the Billing MCP provider."""
    if server_name != _BILLING_MCP_SERVER:
        return client
    return CostReportGuardedProvider(client)
