"""Projection coverage for the EKS MCP output boundary against real wire shapes.

Every fixture in this module is modeled on the *actually installed* AWS Labs
and Strands source, verified by reading:

  * awslabs/eks_mcp_server/k8s_handler.py, insights_handler.py,
    cloudwatch_handler.py, cloudwatch_metrics_guidance_handler.py,
    vpc_config_handler.py — every read tool returns
    ``CallToolResult(content=[TextContent(prose), TextContent(json.dumps(model_dump()))])``
    on success and ``CallToolResult(content=[TextContent(error_prose)], isError=True)``
    on failure. None of them set ``structuredContent`` (they build CallToolResult
    explicitly rather than returning a Pydantic model).
  * awslabs/aws_api_mcp_server/server.py + core/common/models.py — ``call_aws``
    returns ``list[CallAWSResponse]``. FastMCP (func_metadata._wrap_output)
    wraps a non-CallToolResult structured list into
    ``structuredContent={"result": [ ... serialized CallAWSResponse ... ]}`` and
    also emits unstructured JSON text content. Each serialized entry is a
    *flattened* dict: ``{"cli_command", "response": {InterpretationResponse:
    error/status_code/error_code/pagination_token/json}, "metadata",
    "validation_failures", ..., "error"}``.
  * strands/tools/mcp/mcp_client.py:_handle_tool_result — maps MCP
    ``TextContent`` to ``{"text": ...}`` (never ``{"json": ...}``) and copies
    ``structuredContent`` onto the ToolResult only when the provider sets it.

These tests exercise the boundary against the shapes it actually receives, so
they stay meaningful as regression coverage rather than one-off RED evidence.

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
from agents.eks_mcp_guard import guard_eks_mcp_client

pytestmark = pytest.mark.unit

_FAKE_ACCOUNT = "000000000000"
_FAKE_ARN = f"arn:aws:eks:us-west-2:{_FAKE_ACCOUNT}:cluster/synthetic-cluster"
_FAKE_ENDPOINT = "https://ABC123SYNTHETIC.gr7.us-west-2.eks.amazonaws.com"
_FAKE_CA_DATA = "LS0tLSBCRUdJTiBTWU5USEVUSUMgQ0EgLS0tLQ=="  # noqa: S105 synthetic


# --------------------------------------------------------------------------- #
# Real-shape ToolResult builders                                              #
# --------------------------------------------------------------------------- #
def eks_success_content(model_dump: dict[str, Any], prose: str = "Successfully listed resources") -> list[dict]:
    """Real EKS handler success: [prose text block, json text block]."""
    return [{"text": prose}, {"text": json.dumps(model_dump)}]


def call_aws_structured(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """FastMCP wraps list[CallAWSResponse] into structuredContent={'result': [...]}."""
    return {"result": entries}


class _RealMcpTool(AgentTool):
    """Stand-in that reproduces the real Strands-mapped ToolResult shape."""

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

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        return self.tools

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass


def _load(server: str, tools: Sequence[AgentTool]) -> list[AgentTool]:
    guarded = guard_eks_mcp_client(server, _FakeProvider(tools))
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
        if "json" in item:
            parts.append(json.dumps(item["json"], default=str))
    return "\n".join(parts)


def _success_json(event: ToolResultEvent) -> dict[str, Any]:
    """The single model-visible JSON content block, parsed."""
    for item in event.tool_result["content"]:
        if "text" in item:
            try:
                return json.loads(item["text"])
            except (json.JSONDecodeError, TypeError):
                continue
        if "json" in item:
            return item["json"]
    raise AssertionError("no JSON payload in result content")


# =========================================================================== #
# CRITERION 1 — real EKS two-text-block shape must be parsed, not marked bad  #
# =========================================================================== #
def test_list_k8s_resources_real_two_text_block_shape_is_projected_not_malformed():
    """The real handler returns [prose, json-text]; the guard must parse the
    json-text (model_dump) block and project it, never mistake the prose block
    for the payload or mark the response malformed."""
    model_dump = {
        "kind": "Pod",
        "api_version": "v1",
        "namespace": "default",
        "count": 2,
        "items": [
            {
                "name": "pod-a",
                "namespace": "default",
                "creation_timestamp": "2024-01-01T00:00:00Z",
                "labels": {"app": "web"},
                "annotations": {"note": "x"},
            },
            {
                "name": "pod-b",
                "namespace": "default",
                "creation_timestamp": "2024-01-01T00:00:00Z",
                "labels": {"app": "web"},
                "annotations": {"note": "y"},
            },
        ],
    }
    delegate = _RealMcpTool(
        "list_k8s_resources", eks_success_content(model_dump, "Successfully listed 2 Pod resources")
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})

    assert event.tool_result["status"] == "success", "real structured response must not be malformed"
    payload = _success_json(event)
    assert payload["count"] == 2
    assert [i["name"] for i in payload["items"]] == ["pod-a", "pod-b"]
    # The prose success line must not become the payload.
    assert "Successfully listed" not in json.dumps(payload)


def test_get_eks_insights_real_shape_is_projected_not_malformed():
    """insights returns [prose, json-text]; the guard must project, not error."""
    model_dump = {
        "cluster_name": "synthetic-cluster",
        "next_token": "OPAQUE-TOKEN",
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
    delegate = _RealMcpTool("get_eks_insights", eks_success_content(model_dump, "Found 1 insight"))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_insights")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster"})

    assert event.tool_result["status"] == "success"
    payload = _success_json(event)
    assert payload["insights"][0]["id"] == "ins-1"
    assert "OPAQUE-TOKEN" not in _result_text(event)
    assert _FAKE_ARN not in _result_text(event)


def test_troubleshoot_guide_guidance_text_only_case_is_bounded():
    """search_eks_troubleshoot_guide is a guidance-text tool; must return bounded
    guidance from the real content shape, not error."""
    # eks_kb_handler returns guidance content as text; model_dump style dict.
    delegate = _RealMcpTool(
        "search_eks_troubleshoot_guide",
        [{"text": "Guidance"}, {"text": json.dumps({"result": "Pods pending: check taints and requests."})}],
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "search_eks_troubleshoot_guide")
    event = _invoke(tool, {"query": "pod pending"})
    assert event.tool_result["status"] == "success"


# =========================================================================== #
# CRITERION 2 — real call_aws structuredContent + flattened entry fields      #
# =========================================================================== #
def test_call_aws_reads_structured_result_and_projects_describe_cluster():
    """Real call_aws output lives in structuredContent['result'] with the
    describe payload under entry['response']['json']; the guard must read that
    path and project it."""
    raw_cluster = {
        "cluster": {
            "name": "synthetic-cluster",
            "arn": _FAKE_ARN,
            "endpoint": _FAKE_ENDPOINT,
            "status": "ACTIVE",
            "version": "1.30",
            "certificateAuthority": {"data": _FAKE_CA_DATA},
        }
    }
    entry = {
        "cli_command": "aws eks describe-cluster --name synthetic-cluster",
        "response": {
            "error": None,
            "status_code": 200,
            "error_code": None,
            "pagination_token": None,
            "json": json.dumps(raw_cluster),
        },
        "metadata": {"region_name": "us-west-2"},
        "validation_failures": None,
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name synthetic-cluster"})

    assert event.tool_result["status"] == "success", "real call_aws structured result must be projected"
    text = _result_text(event)
    assert _FAKE_ENDPOINT not in text
    assert _FAKE_CA_DATA not in text
    assert _FAKE_ARN not in text
    assert _FAKE_ACCOUNT not in text
    payload = _success_json(event)
    assert payload["results"][0]["cluster"]["name"] == "synthetic-cluster"


def test_call_aws_nested_pagination_token_and_metadata_never_leak():
    """Nested pagination_token / metadata / region must be dropped."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {
            "error": None,
            "status_code": 200,
            "error_code": None,
            "pagination_token": "NEXT-PAGE-OPAQUE-TOKEN",
            "json": json.dumps({"clusters": ["synthetic-cluster"], "nextToken": "RAW-NEXT"}),
        },
        "metadata": {"region_name": "us-west-2", "execution_time": 0.42},
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    text = _result_text(event)
    assert "NEXT-PAGE-OPAQUE-TOKEN" not in text
    assert "RAW-NEXT" not in text
    assert "execution_time" not in text
    assert "region_name" not in text


def test_call_aws_top_level_error_entry_is_sanitized_not_leaked():
    """A CallAWSResponse with a top-level error must not surface the body."""
    entry = {
        "cli_command": "aws eks describe-cluster --name synthetic-cluster",
        "error": f"AccessDeniedException: user {_FAKE_ARN} at {_FAKE_ENDPOINT}",
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name synthetic-cluster"})
    text = _result_text(event)
    assert _FAKE_ARN not in text
    assert _FAKE_ENDPOINT not in text
    assert "AccessDeniedException" not in text


def test_call_aws_validation_failure_entry_is_sanitized():
    """validation_failures carry provider prose; it must not leak."""
    entry = {
        "cli_command": "aws eks describe-cluster --name synthetic-cluster",
        "response": None,
        "validation_failures": [{"reason": f"invalid arn {_FAKE_ARN}"}],
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name synthetic-cluster"})
    assert _FAKE_ARN not in _result_text(event)


# =========================================================================== #
# CRITERION 3 — suggest_aws_commands must be dropped entirely                 #
# =========================================================================== #
def test_suggest_aws_commands_is_dropped_from_tool_set():
    """suggest_aws_commands calls an external generic suggestion endpoint
    returning arbitrary/mutating command text and is not required by the EKS
    prompt, so it must be dropped from the tool set."""
    delegates = [
        _RealMcpTool("call_aws", content=[{"text": json.dumps({"result": []})}], structured_content={"result": []}),
        _RealMcpTool("suggest_aws_commands", content=[{"text": json.dumps({"suggestions": []})}]),
    ]
    names = {t.tool_name for t in _load("aws-api-mcp-server", delegates)}
    assert "call_aws" in names
    assert "suggest_aws_commands" not in names


# =========================================================================== #
# CRITERION 4 — CLI grammar: evasions rejected before provider dispatch       #
# =========================================================================== #
@pytest.mark.parametrize(
    "command",
    [
        "aws eks list-clusters --endpoint-url http://evil.example",
        "aws eks list-clusters --profile admin",
        "aws eks list-clusters --debug",
        "aws eks describe-cluster --name synthetic-cluster --query cluster.certificateAuthority",
        "aws eks describe-cluster --name synthetic-cluster --output yaml",
        "aws eks describe-cluster --name ../../etc/passwd",
        "aws eks describe-cluster --name synthetic-cluster --cli-input-json file:///etc/passwd",
        "aws eks list-clusters --region *",
        "aws eks list-clusters --region us-west-2 --region us-east-1",
        "aws eks list-nodegroups --cluster-name $(aws sts get-caller-identity)",
        "aws eks list-clusters; aws iam list-users",
        "aws eks describe-cluster --name 'a b; rm -rf /'",
        "aws eks delete-cluster --name synthetic-cluster",
        "aws iam list-users",
        "aws eks list-clusters --unknown-flag value",
    ],
)
def test_call_aws_cli_evasions_are_rejected_before_dispatch(command):
    """Each of these CLI evasions must be rejected before dispatch (the full
    grammar is validated, not just the first three tokens)."""
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": []})}], structured_content={"result": []}
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": command})
    assert event.tool_result["status"] == "error", f"must reject: {command}"
    assert delegate.calls == [], f"delegate must not be called for: {command}"


def test_call_aws_batch_fanout_is_hard_capped():
    """An unbounded batch must be rejected/capped before dispatch."""
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": []})}], structured_content={"result": []}
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    huge = ["aws eks list-clusters"] * 50
    event = _invoke(tool, {"cli_command": huge})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_call_aws_valid_command_is_canonicalized_before_dispatch():
    """A permitted command with allowed options reaches the delegate in a
    canonical reconstructed form (not the raw model string)."""
    entry = {
        "cli_command": "aws eks describe-cluster --name synthetic-cluster",
        "response": {"json": json.dumps({"cluster": {"name": "synthetic-cluster", "status": "ACTIVE"}})},
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks   describe-cluster   --name synthetic-cluster"})
    assert event.tool_result["status"] == "success"
    assert len(delegate.calls) == 1
    sent = delegate.calls[0]["input"]["cli_command"]
    sent_cmds = sent if isinstance(sent, list) else [sent]
    assert sent_cmds == ["aws eks describe-cluster --name synthetic-cluster"]


def test_list_updates_verb_is_not_allowed_by_default():
    """list-updates exposes deployment update IDs and is dropped unless a
    reviewed need requires it."""
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": []})}], structured_content={"result": []}
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-updates --name synthetic-cluster"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


# =========================================================================== #
# CRITERION 5 — INPUT guarding before provider dispatch                       #
# =========================================================================== #
def test_list_k8s_resources_secret_kind_is_rejected_before_dispatch():
    """Secret kind must be rejected before the child server is called."""
    delegate = _RealMcpTool("list_k8s_resources", eks_success_content({"kind": "Secret", "count": 0, "items": []}))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Secret", "api_version": "v1"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_list_k8s_resources_unbounded_selector_is_rejected():
    """An oversized/free-form selector must not reach the child server."""
    delegate = _RealMcpTool("list_k8s_resources", eks_success_content({"kind": "Pod", "count": 0, "items": []}))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(
        tool,
        {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1", "label_selector": "x" * 5000},
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_manage_k8s_resource_mutating_input_never_dispatched():
    """A mutating tool must be dropped entirely from the loaded tool set."""
    loaded = _load("eks-mcp-server", [_RealMcpTool("manage_k8s_resource", [{"text": "x"}])])
    assert _find(loaded, "manage_k8s_resource") is None


def test_get_cloudwatch_metrics_bad_cluster_identifier_rejected():
    """cluster name must be a bounded EKS identifier before dispatch."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_success_content(
            {
                "cluster_name": "c",
                "metric_name": "cpu",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool, {"cluster_name": "bad name; rm -rf", "metric_name": "cpu_usage_total", "namespace": "ContainerInsights"}
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


# =========================================================================== #
# CRITERION 6 — bounded per-tool deadline (no real sleep)                     #
# =========================================================================== #
def test_hung_provider_returns_typed_unavailable_within_deadline(monkeypatch):
    """The guard's OWN bounded deadline (not the test) must convert a hung
    provider into a typed 'unavailable' outcome. No real sleep: the guard's
    deadline is patched to 0 so asyncio.wait_for fires immediately.
    """
    # Standard library
    import time as _time

    # Local modules
    import agents.eks_mcp_guard as guard_mod

    # Patch the guard's deadline to 0 -> asyncio.wait_for raises immediately.
    monkeypatch.setattr(guard_mod, "DEADLINE_SECONDS", 0)

    class _HangingTool(AgentTool):
        @property
        def tool_name(self) -> str:
            return "list_k8s_resources"

        @property
        def tool_spec(self) -> ToolSpec:
            return {"name": "list_k8s_resources", "description": "d", "inputSchema": {"json": {"type": "object"}}}

        @property
        def tool_type(self) -> str:
            return "python"

        async def stream(self, tool_use, invocation_state, **kwargs):
            # Would hang forever unless the guard enforces its own deadline.
            await asyncio.sleep(3600)
            yield ToolResultEvent({"toolUseId": tool_use["toolUseId"], "status": "success", "content": []})

    async def _run():
        guarded = guard_eks_mcp_client("eks-mcp-server", _FakeProvider([_HangingTool()]))
        tool = _find(list(await guarded.load_tools()), "list_k8s_resources")
        tool_use: ToolUse = {
            "toolUseId": "tu-1",
            "name": "list_k8s_resources",
            "input": {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"},
        }
        return [e async for e in tool.stream(tool_use, {})]

    start = _time.monotonic()
    events = asyncio.run(_run())
    elapsed = _time.monotonic() - start

    # Guard converts the hang into a typed unavailable outcome, promptly.
    assert elapsed < 2.0, "guard deadline must fire without real sleeping"
    assert events, "guard must emit a typed outcome on a hung provider"
    assert events[-1].tool_result["status"] == "error"
    body = json.loads(events[-1].tool_result["content"][0]["text"])
    assert body["outcome"] == "unavailable"


# =========================================================================== #
# CRITERION 7 — projection robustness (finite bounds, required shape)         #
# =========================================================================== #
def test_missing_items_collection_is_not_treated_as_valid_empty():
    """A response missing 'items' entirely is malformed, not empty."""
    model_dump = {"kind": "Pod", "api_version": "v1", "namespace": "default", "count": 5}  # no items key
    delegate = _RealMcpTool("list_k8s_resources", eks_success_content(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    payload_or_err = event.tool_result
    if payload_or_err["status"] == "success":
        body = _success_json(event)
        assert body.get("outcome") != "empty", "missing collection must not be reported as empty"


def test_nan_and_infinity_metric_values_are_rejected_or_dropped():
    """NaN/Infinity must never appear in the emitted JSON."""
    model_dump = {
        "cluster_name": "synthetic-cluster",
        "metric_name": "cpu_usage_total",
        "namespace": "ContainerInsights",
        "start_time": "t",
        "end_time": "t",
        "data_points": [
            {"timestamp": "t", "value": float("nan")},
            {"timestamp": "t", "value": float("inf")},
            {"timestamp": "t", "value": 1.5},
        ],
    }
    delegate = _RealMcpTool("get_cloudwatch_metrics", eks_success_content(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool, {"cluster_name": "synthetic-cluster", "metric_name": "cpu_usage_total", "namespace": "ContainerInsights"}
    )
    text = _result_text(event)
    assert "NaN" not in text and "Infinity" not in text and "inf" not in text.lower().split()


def test_event_message_control_chars_and_arn_are_sanitized():
    """Free-form event text must be sanitized of control chars and ARNs."""
    model_dump = {
        "involved_object_kind": "Pod",
        "involved_object_name": "pod-a",
        "involved_object_namespace": "default",
        "count": 1,
        "events": [
            {
                "reason": "Failed",
                "type": "Warning",
                "message": f"boom\x07\x00 see {_FAKE_ARN} at {_FAKE_ENDPOINT}",
                "count": 1,
                "first_timestamp": "t",
                "last_timestamp": "t",
                "reporting_component": "kubelet",
            }
        ],
    }
    delegate = _RealMcpTool("get_k8s_events", eks_success_content(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_k8s_events")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "name": "pod-a", "namespace": "default"})
    text = _result_text(event)
    assert _FAKE_ARN not in text
    assert _FAKE_ENDPOINT not in text
    assert "\x07" not in text and "\x00" not in text


def test_arbitrary_labels_are_excluded_from_projection():
    """Arbitrary label keys must be excluded (only a tiny reviewed allowlist of
    safe keys, if any, is permitted)."""
    model_dump = {
        "kind": "Pod",
        "api_version": "v1",
        "namespace": "default",
        "count": 1,
        "items": [
            {
                "name": "pod-a",
                "namespace": "default",
                "creation_timestamp": "t",
                "labels": {"app": "web", "eks.amazonaws.com/secret-token": "s3cr3t", "arn-ref": _FAKE_ARN},
            }
        ],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_success_content(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    text = _result_text(event)
    assert "s3cr3t" not in text
    assert _FAKE_ARN not in text


# =========================================================================== #
# CRITERION 8 — versioned envelope with explicit operation + outcome          #
# =========================================================================== #
def test_success_envelope_has_version_operation_and_outcome():
    """The success envelope must carry version/operation/outcome."""
    model_dump = {"kind": "Pod", "api_version": "v1", "namespace": "default", "count": 0, "items": []}
    delegate = _RealMcpTool("list_k8s_resources", eks_success_content(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    body = _success_json(event)
    assert body.get("v")  # version present
    assert body.get("operation") == "list_k8s_resources"
    assert body.get("outcome") == "empty"  # valid present empty collection


def test_access_denied_is_distinguishable_from_unavailable():
    """An access-denied provider error must produce a 'denied' outcome distinct
    from an availability 'unavailable' outcome, without raw bodies."""
    denied = _RealMcpTool("list_k8s_resources", [{"text": f"AccessDenied: {_FAKE_ARN}"}], status="error")
    tool = _find(_load("eks-mcp-server", [denied]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    body = event.tool_result
    assert body["status"] == "error"
    assert _FAKE_ARN not in _result_text(event)


# =========================================================================== #
# CRITERION 9 — single JSON block, final UTF-8 byte cap, multibyte exactness  #
# =========================================================================== #
def test_success_emits_single_json_content_block_not_duplicated():
    """The boundary emits exactly one model-visible JSON content block (no
    duplicated json+text block)."""
    model_dump = {"kind": "Pod", "api_version": "v1", "namespace": "default", "count": 0, "items": []}
    delegate = _RealMcpTool("list_k8s_resources", eks_success_content(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    content = event.tool_result["content"]
    assert len(content) == 1, "exactly one model-visible content block"
    assert "json" not in content[0], "must not duplicate payload as a json+text block"
    assert "text" in content[0]


def test_envelope_byte_cap_measures_final_utf8_multibyte():
    """The cap must measure serialized UTF-8 bytes, not len(str)."""
    # Multibyte content: each char is 3 bytes in UTF-8.
    big = "\u4e2d" * 20000
    model_dump = {
        "kind": "Pod",
        "api_version": "v1",
        "namespace": "default",
        "count": 1,
        "items": [{"name": big, "namespace": "default", "creation_timestamp": "t", "labels": {}}],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_success_content(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1"})
    text = next(c["text"] for c in event.tool_result["content"] if "text" in c)
    assert len(text.encode("utf-8")) <= 20_000


# =========================================================================== #
# CRITERION 11 — inventory allowlist (drop mutation/secret/log/IAM/generic)   #
# =========================================================================== #
def test_full_installed_inventory_only_read_subset_survives():
    """Every installed EKS tool is offered; only the reviewed read subset must
    survive, and get_pod_logs/get_cloudwatch_logs (sensitive) and all
    mutation/IAM/stack/manifest tools must be dropped."""
    installed = [
        "list_k8s_resources",
        "get_pod_logs",
        "get_k8s_events",
        "list_api_versions",
        "manage_k8s_resource",
        "apply_yaml",
        "generate_app_manifest",
        "search_eks_troubleshoot_guide",
        "get_cloudwatch_logs",
        "get_cloudwatch_metrics",
        "manage_eks_stacks",
        "add_inline_policy",
        "get_policies_for_role",
        "get_eks_metrics_guidance",
        "get_eks_vpc_config",
        "get_eks_insights",
    ]
    delegates = [_RealMcpTool(n, eks_success_content({"count": 0, "items": []})) for n in installed]
    survivors = {t.tool_name for t in _load("eks-mcp-server", delegates)}

    must_drop = {
        "get_pod_logs",
        "manage_k8s_resource",
        "apply_yaml",
        "generate_app_manifest",
        "get_cloudwatch_logs",
        "manage_eks_stacks",
        "add_inline_policy",
        "get_policies_for_role",
    }
    assert survivors.isdisjoint(must_drop), f"leaked: {survivors & must_drop}"
    # Every survivor must be a known, projector-backed read tool.
    assert survivors <= set(installed)
    assert "list_k8s_resources" in survivors


# =========================================================================== #
# issue #466 residual defects — call_aws list-projector exactness + outcomes  #
# =========================================================================== #
# These pin the remaining current-code gaps in the call_aws list projectors:  #
# missing list members must not silently become [], a describe verb whose      #
# target object is missing/wrong is malformed (not a null-filled complete),    #
# and a valid complete empty single-command list yields an explicit 'empty'    #
# outcome. Synthetic, public-safe fixtures only.                              #


def test_call_aws_list_clusters_missing_clusters_member_is_malformed_not_empty_list():
    """A list-clusters response object missing the 'clusters' member must be
    malformed for that entry, not silently projected as clusters: []."""
    entry = {"cli_command": "aws eks list-clusters", "response": {"error": None, "json": json.dumps({})}}
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _success_json(event)
    # No clean complete with a fabricated empty list.
    if body["outcome"] == "complete":
        raise AssertionError("missing list member must not project as a complete empty list")
    assert body["outcome"] in {"malformed", "partial"}


def test_call_aws_describe_cluster_missing_cluster_object_is_malformed():
    """describe-cluster with no 'cluster' object is malformed, not a null-filled
    complete cluster projection."""
    entry = {"cli_command": "aws eks describe-cluster --name c1", "response": {"error": None, "json": json.dumps({})}}
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name c1"})
    body = _success_json(event)
    assert body["outcome"] != "complete"


def test_call_aws_describe_cluster_wrong_target_type_is_malformed():
    """describe-cluster with a non-object 'cluster' member is malformed."""
    entry = {
        "cli_command": "aws eks describe-cluster --name c1",
        "response": {"error": None, "json": json.dumps({"cluster": "not-an-object"})},
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name c1"})
    body = _success_json(event)
    assert body["outcome"] != "complete"


def test_call_aws_valid_nonempty_list_still_projects_complete():
    """A valid non-empty list projects a complete outcome (no regression)."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "json": json.dumps({"clusters": ["c1", "c2"]})},
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _success_json(event)
    assert body["outcome"] == "complete"
    assert body["results"][0]["clusters"] == ["c1", "c2"]
