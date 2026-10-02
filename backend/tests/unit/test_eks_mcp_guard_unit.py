"""Behavioral tests for the EKS specialist MCP output safety boundary.

These tests exercise the model-facing tool-result boundary (AgentTool.stream ->
ToolResultEvent.tool_result) exactly as the EKS specialist consumes MCP tools,
using the AUTHORITATIVE installed wire shapes:

  * EKS handlers return CallToolResult(content=[TextContent(prose),
    TextContent(json.dumps(model_dump()))]) with no structuredContent; Strands
    maps each TextContent to {"text": ...}.
  * aws-api call_aws returns list[CallAWSResponse]; FastMCP wraps it into
    structuredContent={"result": [...]} (each entry flattened) plus JSON text.

They prove that mutating / secret-reading / generic-dispatch operations are
unavailable, allowed read operations are projected onto an EKS-owned field
allowlist with finite bounds and a versioned envelope, and provider-authored
error text never reaches model context. Deep evasion / real-shape coverage
lives in test_eks_mcp_guard_projection_unit.py and
test_eks_mcp_guard_hardening_unit.py.

Synthetic, public-safe fixtures only.
"""

# Standard library
import asyncio
import json
from typing import Any, Sequence

# Third-party packages
import pytest
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

# Local modules
from agents.eks_mcp_guard import ENVELOPE_VERSION, MAX_ENVELOPE_BYTES, MAX_ITEMS, guard_eks_mcp_client

pytestmark = pytest.mark.unit

_FAKE_ACCOUNT = "000000000000"
_FAKE_ARN = f"arn:aws:eks:us-west-2:{_FAKE_ACCOUNT}:cluster/synthetic-cluster"
_FAKE_ENDPOINT = "https://ABC123SYNTHETIC.gr7.us-west-2.eks.amazonaws.com"
_FAKE_CA_DATA = "LS0tLSBCRUdJTiBTWU5USEVUSUMgQ0EgLS0tLQ=="  # noqa: S105 synthetic


class _FakeMcpTool(AgentTool):
    """Reproduces the real Strands-mapped ToolResult shape for an MCP tool."""

    def __init__(
        self,
        name: str,
        content: list[dict[str, Any]],
        status: str = "success",
        structured_content: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._name = name
        self._content = content
        self._status = status
        self._structured = structured_content
        self.calls: list[ToolUse] = []

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {"name": self._name, "description": "orig", "inputSchema": {"json": {"type": "object"}}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        self.calls.append(tool_use)
        result: dict[str, Any] = {
            "toolUseId": tool_use["toolUseId"],
            "status": self._status,
            "content": list(self._content),
        }
        if self._structured is not None:
            result["structuredContent"] = self._structured
        yield ToolResultEvent(result)


class _FakeProvider(ToolProvider):
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


def _eks_success(model_dump: dict[str, Any], prose: str = "Success") -> list[dict[str, Any]]:
    return [{"text": prose}, {"text": json.dumps(model_dump)}]


def _call_aws_result(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return ([{"text": json.dumps({"result": entries})}], {"result": entries})


def _load(server_name: str, tools: Sequence[AgentTool]) -> list[AgentTool]:
    guarded = guard_eks_mcp_client(server_name, _FakeProvider(tools))
    return list(asyncio.run(guarded.load_tools()))


def _find(tools: Sequence[AgentTool], name: str) -> AgentTool | None:
    return next((t for t in tools if t.tool_name == name), None)


def _invoke(tool: AgentTool, tool_input: dict[str, Any]) -> ToolResultEvent:
    tool_use: ToolUse = {"toolUseId": "tu-1", "name": tool.tool_name, "input": tool_input}
    events = asyncio.run(_collect(tool, tool_use))
    return events[-1]


async def _collect(tool: AgentTool, tool_use: ToolUse) -> list[ToolResultEvent]:
    return [event async for event in tool.stream(tool_use, {})]


def _result_text(event: ToolResultEvent) -> str:
    parts: list[str] = []
    for item in event.tool_result["content"]:
        if "text" in item:
            parts.append(str(item["text"]))
    return "\n".join(parts)


def _envelope(event: ToolResultEvent) -> dict[str, Any]:
    """Parse the single model-visible JSON content block."""
    content = event.tool_result["content"]
    assert len(content) == 1, "exactly one model-visible content block"
    return json.loads(content[0]["text"])


# ---------------------------------------------------------------------------
# Allowlist: unsafe operations unavailable
# ---------------------------------------------------------------------------
def test_mutating_secret_iam_and_log_tools_are_dropped():
    denied_names = [
        "manage_k8s_resource",
        "apply_yaml",
        "generate_app_manifest",
        "manage_eks_stacks",
        "add_inline_policy",
        "get_policies_for_role",
        "get_pod_logs",
        "get_cloudwatch_logs",
    ]
    delegates = [_FakeMcpTool(n, [{"text": "should never reach model"}]) for n in denied_names]
    loaded = {t.tool_name for t in _load("eks-mcp-server", delegates)}
    assert loaded.isdisjoint(set(denied_names))


def test_generic_non_eks_call_aws_dispatch_is_rejected():
    content, sc = _call_aws_result([])
    delegate = _FakeMcpTool("call_aws", content, structured_content=sc)
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws iam list-users"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_suggest_aws_commands_is_dropped():
    delegates = [
        _FakeMcpTool("call_aws", [{"text": json.dumps({"result": []})}], structured_content={"result": []}),
        _FakeMcpTool("suggest_aws_commands", [{"text": json.dumps({"suggestions": []})}]),
    ]
    names = {t.tool_name for t in _load("aws-api-mcp-server", delegates)}
    assert "call_aws" in names
    assert "suggest_aws_commands" not in names


def test_get_execution_plan_is_dropped_from_aws_api_tool_set():
    delegates = [
        _FakeMcpTool("call_aws", [{"text": json.dumps({"result": []})}], structured_content={"result": []}),
        _FakeMcpTool("get_execution_plan", [{"text": "plan"}]),
    ]
    names = {t.tool_name for t in _load("aws-api-mcp-server", delegates)}
    assert "call_aws" in names
    assert "get_execution_plan" not in names


# ---------------------------------------------------------------------------
# Projection: raw-field / unbounded-output leakage
# ---------------------------------------------------------------------------
def test_call_aws_describe_cluster_strips_endpoint_ca_and_full_arn():
    raw = {
        "cluster": {
            "name": "synthetic-cluster",
            "arn": _FAKE_ARN,
            "endpoint": _FAKE_ENDPOINT,
            "status": "ACTIVE",
            "version": "1.30",
            "certificateAuthority": {"data": _FAKE_CA_DATA},
            "resourcesVpcConfig": {"endpointPublicAccess": True, "publicAccessCidrs": ["0.0.0.0/0"]},
        }
    }
    entry = {"cli_command": "aws eks describe-cluster --name synthetic-cluster", "response": {"json": json.dumps(raw)}}
    content, sc = _call_aws_result([entry])
    delegate = _FakeMcpTool("call_aws", content, structured_content=sc)
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")

    text = _result_text(_invoke(tool, {"cli_command": "aws eks describe-cluster --name synthetic-cluster"}))
    assert _FAKE_ENDPOINT not in text
    assert _FAKE_CA_DATA not in text
    assert _FAKE_ACCOUNT not in text
    assert _FAKE_ARN not in text


def test_list_k8s_resources_drops_annotations_and_caps_items():
    items = []
    for i in range(500):
        items.append(
            {
                "name": f"pod-{i}",
                "namespace": "default",
                "creation_timestamp": "2024-01-01T00:00:00Z",
                "labels": {"app": "synthetic"},
                "annotations": {"kubectl.kubernetes.io/last-applied-configuration": json.dumps({"x": _FAKE_ARN})},
            }
        )
    model = {"kind": "Pod", "api_version": "v1", "namespace": "default", "count": 500, "items": items}
    delegate = _FakeMcpTool("list_k8s_resources", _eks_success(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")

    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    body = _envelope(event)
    assert "last-applied-configuration" not in json.dumps(body)
    assert _FAKE_ARN not in json.dumps(body)
    assert len(body["items"]) <= MAX_ITEMS
    assert body["count"] == 500
    assert body["outcome"] == "truncated"
    assert body["truncated"] is True
    assert body["hasMore"] is True


def test_provider_error_body_is_replaced_with_sanitized_typed_error():
    secret_error = f"AccessDenied: user {_FAKE_ARN} cannot reach {_FAKE_ENDPOINT}"
    delegate = _FakeMcpTool("list_k8s_resources", [{"text": secret_error}], status="error")
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    text = _result_text(event)
    assert _FAKE_ARN not in text
    assert _FAKE_ENDPOINT not in text
    assert "AccessDenied:" not in text
    body = json.loads(text)
    assert body["outcome"] == "denied"  # access-denied distinguishable


def test_non_eks_client_is_returned_unchanged():
    delegate = _FakeProvider([])
    assert guard_eks_mcp_client("billing-cost-management-mcp-server", delegate) is delegate


# ---------------------------------------------------------------------------
# Output-state contract
# ---------------------------------------------------------------------------
def test_denied_tools_are_dropped_from_the_loaded_tool_set():
    delegates = [
        _FakeMcpTool("manage_k8s_resource", [{"text": "x"}]),
        _FakeMcpTool("apply_yaml", [{"text": "x"}]),
        _FakeMcpTool("get_pod_logs", [{"text": "x"}]),
        _FakeMcpTool("list_k8s_resources", _eks_success({"kind": "Pod", "api_version": "v1", "count": 0, "items": []})),
    ]
    loaded_names = {t.tool_name for t in _load("eks-mcp-server", delegates)}
    assert "list_k8s_resources" in loaded_names
    assert loaded_names.isdisjoint({"manage_k8s_resource", "apply_yaml", "get_pod_logs"})


def test_complete_empty_list_is_outcome_empty_not_truncated():
    model = {"kind": "Pod", "api_version": "v1", "namespace": "default", "count": 0, "items": []}
    delegate = _FakeMcpTool("list_k8s_resources", _eks_success(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    body = _envelope(_invoke(tool, {"cluster_name": "c", "kind": "Pod", "api_version": "v1"}))
    assert body["count"] == 0
    assert body["items"] == []
    assert body["outcome"] == "empty"
    assert "truncated" not in body


def test_malformed_payload_yields_distinct_sanitized_error():
    # Prose-only content (no JSON block) -> malformed, prose must not leak.
    delegate = _FakeMcpTool("list_k8s_resources", [{"text": "not-json-at-all"}])
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c", "kind": "Pod", "api_version": "v1"})
    assert event.tool_result["status"] == "error"
    body = json.loads(_result_text(event))
    assert body["outcome"] == "malformed"
    assert "not-json-at-all" not in _result_text(event)


def test_vpc_config_drops_network_coordinates_but_keeps_counts():
    model = {
        "cluster_name": "synthetic-cluster",
        "vpc_id": "vpc-0123456789abcdef0",
        "cidr_block": "10.0.0.0/16",
        "additional_cidr_blocks": ["10.1.0.0/16"],
        "routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0abc"}],
        "subnets": [{"SubnetId": "subnet-0abc", "CidrBlock": "10.0.1.0/24"}],
        "remote_node_cidr_blocks": [],
        "remote_pod_cidr_blocks": [],
    }
    delegate = _FakeMcpTool("get_eks_vpc_config", _eks_success(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_vpc_config")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster"})
    text = _result_text(event)
    assert "vpc-0123456789abcdef0" not in text
    assert "10.0.0.0/16" not in text
    assert "subnet-0abc" not in text
    assert "igw-0abc" not in text
    body = _envelope(event)
    assert body["subnet_count"] == 1
    assert body["route_count"] == 1
    assert body["has_additional_cidr_blocks"] is True


def test_eks_insights_pagination_token_is_collapsed_to_hasmore():
    model = {
        "cluster_name": "synthetic-cluster",
        "next_token": "OPAQUE-SYNTHETIC-TOKEN-abc123",
        "insights": [
            {
                "id": "ins-1",
                "name": "Deprecated API",
                "category": "UPGRADE_READINESS",
                "last_refresh_time": 1.0,
                "last_transition_time": 2.0,
                "description": "check",
                "insight_status": {"status": "PASSING", "reason": "ok"},
                "resources": [_FAKE_ARN],
            },
        ],
    }
    delegate = _FakeMcpTool("get_eks_insights", _eks_success(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_insights")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster"})
    text = _result_text(event)
    assert "OPAQUE-SYNTHETIC-TOKEN-abc123" not in text
    assert _FAKE_ARN not in text
    body = _envelope(event)
    assert body["hasMore"] is True


def test_envelope_size_cap_trims_or_withholds_oversized_output():
    model = {"cluster_name": "c", "api_versions": ["v" + "x" * 400 for _ in range(500)], "count": 500}
    delegate = _FakeMcpTool("list_api_versions", _eks_success(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_api_versions")
    event = _invoke(tool, {"cluster_name": "c"})
    text = _result_text(event)
    assert len(text.encode("utf-8")) <= MAX_ENVELOPE_BYTES + 512
    body = _envelope(event)
    assert body["v"] == ENVELOPE_VERSION


def test_call_aws_batch_partial_when_one_command_errors():
    entries = [
        {"cli_command": "aws eks list-clusters", "response": {"json": json.dumps({"clusters": ["synthetic-cluster"]})}},
        {"cli_command": "aws eks describe-cluster --name synthetic-cluster", "error": f"AccessDenied for {_FAKE_ARN}"},
    ]
    content, sc = _call_aws_result(entries)
    delegate = _FakeMcpTool("call_aws", content, structured_content=sc)
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(
        tool, {"cli_command": ["aws eks list-clusters", "aws eks describe-cluster --name synthetic-cluster"]}
    )
    text = _result_text(event)
    assert "AccessDenied" not in text
    assert _FAKE_ARN not in text
    body = _envelope(event)
    assert body["outcome"] == "partial"
    assert body["partial"] is True
    assert any(r["cli_command"] == "aws eks list-clusters" for r in body["results"])


def test_call_aws_mutating_eks_verb_is_rejected():
    content, sc = _call_aws_result([])
    delegate = _FakeMcpTool("call_aws", content, structured_content=sc)
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks delete-cluster --name synthetic-cluster"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_provider_lifecycle_is_preserved():
    delegate = _FakeProvider([])
    guarded = guard_eks_mcp_client("eks-mcp-server", delegate)
    guarded.add_consumer("agent-1")
    guarded.remove_consumer("agent-1")
    assert delegate.added == ["agent-1"]
    assert delegate.removed == ["agent-1"]
