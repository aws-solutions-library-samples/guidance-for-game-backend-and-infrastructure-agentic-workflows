"""Assertion-level coverage for the EKS MCP output boundary's hardening pass.

These tests pin the *residual* safety behaviors identified by a full-file
inspection of ``agents.eks_mcp_guard`` against the actually installed AWS Labs
sources (verified by reading the handler/model source in ``.venv``):

  * ``search_eks_troubleshoot_guide`` returns a SINGLE plain ``TextContent``
    carrying the raw HTTP response body (``response.text``) — NOT a JSON
    mapping (``eks_kb_handler.py``). The guard must parse that one text block
    for this allowlisted tool, sanitize + bound it, and keep provider errors
    typed.
  * EKS tool input is projected to a per-tool canonical field set; unknown keys
    are rejected and never forwarded to the delegate.
  * Each installed Pydantic model's REQUIRED fields (``models.py``) drive the
    projector's required-shape check: a response missing a required top-level
    field or list is ``malformed``/``partial``, never ``complete``/``empty``.
  * Scalar/label grammars reject 12-digit identifiers, ``host:port`` network
    coordinates, IPv6, padded dotted-numerics, and credential markers even in
    otherwise-allowed fields.
  * Pagination is truthful for both EKS Insights ``next_token`` and nested
    ``call_aws`` continuation members (``nextToken``/``pagination_token``).
  * ``call_aws`` requires exact list/object shapes, classifies nested
    access-denied from bounded structured codes, marks partial on count
    mismatch/mixed failure, and always sends a code-owned bounded
    ``max_results``.
  * The final envelope serializes deterministically with ``allow_nan=False``
    and a stable code-owned ``errorCode`` on error envelopes.
  * External task cancellation propagates rather than becoming an availability
    outcome; a deadline timeout becomes ``unavailable``.

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
import agents.eks_mcp_guard as guard_mod
from agents.eks_mcp_guard import guard_eks_mcp_client

# Bind the real factory function at collection time (before the autouse mock in
# conftest patches the module attribute) so the factory-sanitization test below
# exercises the genuine implementation rather than the global MagicMock.
from utils.mcp_client_factory import create_mcp_client as _real_create_mcp_client

pytestmark = pytest.mark.unit

_FAKE_ACCOUNT = "000000000000"
_FAKE_ARN = f"arn:aws:eks:us-west-2:{_FAKE_ACCOUNT}:cluster/synthetic-cluster"
_FAKE_ENDPOINT = "https://ABC123SYNTHETIC.gr7.us-west-2.eks.amazonaws.com"


# --------------------------------------------------------------------------- #
# Real-shape fakes                                                            #
# --------------------------------------------------------------------------- #
def eks_two_block(model_dump: dict[str, Any], prose: str = "ok") -> list[dict[str, Any]]:
    """Real EKS handler success: [prose text block, json-text block]."""
    return [{"text": prose}, {"text": json.dumps(model_dump)}]


def call_aws_structured(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"result": entries}


class _RealMcpTool(AgentTool):
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
        return {
            "name": self._name,
            "description": "orig",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "cli_command": {"type": "string"},
                        "max_results": {"type": "integer"},
                        "vpc_id": {"type": "string"},
                        "next_token": {"type": "string"},
                        "dimensions": {"type": "object"},
                    },
                }
            },
        }

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

    async def _collect() -> list[ToolResultEvent]:
        return [event async for event in tool.stream(tool_use, {})]

    return asyncio.run(_collect())[-1]


def _body(event: ToolResultEvent) -> dict[str, Any]:
    for item in event.tool_result["content"]:
        if "text" in item:
            try:
                return json.loads(item["text"])
            except (json.JSONDecodeError, TypeError):
                continue
    raise AssertionError("no JSON payload")


def _text(event: ToolResultEvent) -> str:
    return "\n".join(str(item.get("text", "")) for item in event.tool_result["content"])


# =========================================================================== #
# Finding 1 — troubleshoot guide: single plain-text (raw HTTP body) path      #
# =========================================================================== #
def test_troubleshoot_guide_single_plain_text_block_is_projected_not_malformed():
    """The real handler returns ONE TextContent with the raw HTTP body (a plain
    string, not JSON). The guard must project it as bounded guidance."""
    raw_body = "- Check node taints\n- Verify CPU/memory requests\n- Inspect recent events"
    delegate = _RealMcpTool("search_eks_troubleshoot_guide", [{"text": raw_body}])
    tool = _find(_load("eks-mcp-server", [delegate]), "search_eks_troubleshoot_guide")
    event = _invoke(tool, {"query": "pods pending"})
    assert event.tool_result["status"] == "success", "raw-text guidance must not be malformed"
    body = _body(event)
    assert body["outcome"] == "complete"
    assert "node taints" in body["guidance"]


def test_troubleshoot_guide_plain_text_is_sanitized_and_bounded():
    """Raw HTTP body may embed ARNs/endpoints/control chars; these are redacted."""
    raw_body = f"See {_FAKE_ARN} at {_FAKE_ENDPOINT}\x07 then retry"
    delegate = _RealMcpTool("search_eks_troubleshoot_guide", [{"text": raw_body}])
    tool = _find(_load("eks-mcp-server", [delegate]), "search_eks_troubleshoot_guide")
    event = _invoke(tool, {"query": "diagnose"})
    text = _text(event)
    assert _FAKE_ARN not in text
    assert _FAKE_ENDPOINT not in text
    assert "\x07" not in text


def test_troubleshoot_guide_provider_error_stays_typed():
    """An isError=True result keeps a typed outcome, no raw body."""
    delegate = _RealMcpTool(
        "search_eks_troubleshoot_guide", [{"text": f"Error: denied for {_FAKE_ARN}"}], status="error"
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "search_eks_troubleshoot_guide")
    event = _invoke(tool, {"query": "diagnose"})
    assert event.tool_result["status"] == "error"
    assert _FAKE_ARN not in _text(event)


def test_troubleshoot_query_grammar_matches_installed_300_char_limit():
    """The installed handler restricts query to <=300 chars and a limited
    grammar; the guard must reject a longer/illegal query before dispatch."""
    delegate = _RealMcpTool("search_eks_troubleshoot_guide", [{"text": "guidance"}])
    tool = _find(_load("eks-mcp-server", [delegate]), "search_eks_troubleshoot_guide")
    event = _invoke(tool, {"query": "x" * 301})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


# =========================================================================== #
# Finding 2 — per-tool canonical input projection: unknown keys rejected      #
# =========================================================================== #
def test_unknown_input_keys_are_rejected_before_dispatch():
    """Unknown keys (e.g. a raw manifest) must be rejected, not forwarded."""
    delegate = _RealMcpTool(
        "list_k8s_resources", eks_two_block({"kind": "Pod", "api_version": "v1", "count": 0, "items": []})
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(
        tool,
        {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1", "raw_manifest": "evil"},
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_only_reviewed_fields_reach_the_delegate():
    """The delegate receives ONLY the reviewed canonical fields."""
    delegate = _RealMcpTool(
        "list_k8s_resources", eks_two_block({"kind": "Pod", "api_version": "v1", "count": 0, "items": []})
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(
        tool,
        {"cluster_name": "synthetic-cluster", "kind": "Pod", "api_version": "v1", "namespace": "default"},
    )
    assert event.tool_result["status"] == "success"
    sent_keys = set(delegate.calls[0]["input"].keys())
    assert sent_keys <= {"cluster_name", "kind", "api_version", "namespace", "label_selector", "field_selector"}


def test_insights_opaque_next_token_input_is_rejected():
    """A model-supplied opaque next_token must not be accepted/forwarded."""
    delegate = _RealMcpTool("get_eks_insights", eks_two_block({"cluster_name": "synthetic-cluster", "insights": []}))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_insights")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster", "next_token": "OPAQUE"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_vpc_config_only_forwards_cluster_name():
    """get_eks_vpc_config forwards only cluster_name (no vpc_id / network keys)."""
    model = {"vpc_id": "vpc-abc", "cidr_block": "10.0.0.0/16", "routes": [], "cluster_name": "synthetic-cluster"}
    delegate = _RealMcpTool("get_eks_vpc_config", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_vpc_config")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster"})
    assert event.tool_result["status"] == "success"
    assert set(delegate.calls[0]["input"].keys()) == {"cluster_name"}


# =========================================================================== #
# Finding 5 — required-shape checks from installed Pydantic models            #
# =========================================================================== #
def test_vpc_missing_required_routes_is_malformed_not_complete():
    """EksVpcConfigData requires 'routes' (and vpc_id/cidr_block/cluster_name).
    A response missing 'routes' must be malformed, not a zero-count complete."""
    delegate = _RealMcpTool("get_eks_vpc_config", eks_two_block({"cluster_name": "synthetic-cluster"}))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_vpc_config")
    event = _invoke(tool, {"cluster_name": "synthetic-cluster"})
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_vpc_present_lists_are_zero_only_when_actually_present():
    """When required lists ARE present but empty, VPC is complete with 0 counts."""
    model = {"vpc_id": "vpc-1", "cidr_block": "10.0.0.0/16", "routes": [], "subnets": [], "cluster_name": "c1"}
    delegate = _RealMcpTool("get_eks_vpc_config", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_vpc_config")
    event = _invoke(tool, {"cluster_name": "c1"})
    body = _body(event)
    assert body["outcome"] == "complete"
    assert body["subnet_count"] == 0
    assert body["route_count"] == 0


def test_insights_missing_required_top_level_is_malformed():
    """EksInsightsData requires cluster_name + insights; a missing 'insights'
    list is malformed, not empty."""
    delegate = _RealMcpTool("get_eks_insights", eks_two_block({"cluster_name": "c1"}))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_insights")
    event = _invoke(tool, {"cluster_name": "c1"})
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_metrics_guidance_missing_required_metrics_is_malformed():
    """MetricsGuidanceData requires resource_type + metrics."""
    delegate = _RealMcpTool("get_eks_metrics_guidance", eks_two_block({"resource_type": "pod"}))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_metrics_guidance")
    event = _invoke(tool, {"resource_type": "pod"})
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_insight_row_missing_required_member_marks_partial():
    """EksInsightItem requires id/name/category/insight_status; a row missing
    'id' must set partial, not silently drop to a clean complete."""
    model = {
        "cluster_name": "c1",
        "insights": [
            {
                "name": "no-id",
                "category": "CONFIGURATION",
                "description": "x",
                "insight_status": {"status": "P", "reason": "r"},
            },
            {
                "id": "ins-2",
                "name": "ok",
                "category": "CONFIGURATION",
                "description": "x",
                "insight_status": {"status": "P", "reason": "r"},
            },
        ],
    }
    delegate = _RealMcpTool("get_eks_insights", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_insights")
    event = _invoke(tool, {"cluster_name": "c1"})
    body = _body(event)
    assert body.get("partial") is True
    assert body["outcome"] == "partial"


# =========================================================================== #
# Finding 6 — scalar/label grammars reject network coords + identifiers       #
# =========================================================================== #
def test_host_port_network_coordinate_is_not_preserved_in_scalar_field():
    """A host:port network coordinate in a scalar field must be rejected/redacted."""
    model = {
        "kind": "Pod",
        "api_version": "v1",
        "count": 1,
        "items": [
            {"name": "pod-a", "namespace": "default", "creation_timestamp": "host.example.com:8443", "labels": {}}
        ],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    assert "host.example.com:8443" not in _text(event)


def test_twelve_digit_label_value_is_rejected():
    """A reviewed label value must also pass sensitive-shape rejection: a bare
    12-digit account-like value must not survive."""
    model = {
        "kind": "Pod",
        "api_version": "v1",
        "count": 1,
        "items": [{"name": "pod-a", "labels": {"role": "123456789012"}}],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    assert "123456789012" not in _text(event)


def test_ipv6_and_padded_dotted_numeric_are_redacted_in_free_text():
    """IPv6 and zero-padded dotted-numeric coordinates in free text are redacted."""
    model = {
        "involved_object_kind": "Pod",
        "involved_object_name": "pod-a",
        "count": 1,
        "events": [
            {
                "message": "node 2001:0db8:85a3:0000:0000:8a2e:0370:7334 and 010.001.002.003 unreachable",
                "type": "Warning",
                "reason": "Failed",
            }
        ],
    }
    delegate = _RealMcpTool("get_k8s_events", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_k8s_events")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "name": "pod-a", "namespace": "default"})
    text = _text(event)
    assert "2001:0db8:85a3" not in text
    assert "010.001.002.003" not in text


def test_credential_marker_in_free_text_is_redacted():
    """A C1-style credential marker is redacted from free text."""
    model = {
        "involved_object_kind": "Pod",
        "involved_object_name": "pod-a",
        "count": 1,
        "events": [{"message": "token AKIAIOSFODNN7EXAMPLE leaked", "type": "Warning", "reason": "Failed"}],
    }
    delegate = _RealMcpTool("get_k8s_events", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_k8s_events")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "name": "pod-a", "namespace": "default"})
    assert "AKIAIOSFODNN7EXAMPLE" not in _text(event)


# =========================================================================== #
# Finding 7 — pagination truthfulness                                         #
# =========================================================================== #
def test_call_aws_nested_next_token_reports_partial_hasmore():
    """A raw list continuation member (nextToken) means pagination remains:
    the guard must report partial/hasMore, never complete, and omit the token."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "json": json.dumps({"clusters": ["c1"], "nextToken": "RAW-NEXT"})},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _body(event)
    assert body.get("hasMore") is True
    assert body["outcome"] != "complete"
    assert "RAW-NEXT" not in _text(event)


def test_call_aws_nested_pagination_token_reports_partial_hasmore():
    """The InterpretationResponse.pagination_token also means pagination remains."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {
            "error": None,
            "pagination_token": "PAGE-OPAQUE",
            "json": json.dumps({"clusters": ["c1"]}),
        },
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _body(event)
    assert body.get("hasMore") is True
    assert "PAGE-OPAQUE" not in _text(event)


# =========================================================================== #
# Finding 8 — call_aws exact shapes, nested denied, count mismatch            #
# =========================================================================== #
def test_call_aws_nested_access_denied_is_classified_denied():
    """A nested error_code of AccessDeniedException must classify as denied
    (distinct from unavailable), and never leak the body."""
    entry = {
        "cli_command": "aws eks describe-cluster --name synthetic-cluster",
        "response": {
            "error": f"AccessDeniedException: {_FAKE_ARN}",
            "error_code": "AccessDeniedException",
            "status_code": 403,
            "json": None,
        },
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name synthetic-cluster"})
    body = _body(event)
    assert body["outcome"] == "denied"
    assert _FAKE_ARN not in _text(event)
    assert "AccessDeniedException" not in _text(event)


def test_call_aws_entry_count_mismatch_marks_partial():
    """More commands than returned entries means results are missing: partial."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "json": json.dumps({"clusters": ["c1"]})},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": ["aws eks list-clusters", "aws eks list-nodegroups --cluster-name c1"]})
    body = _body(event)
    assert body.get("partial") is True
    assert body["outcome"] == "partial"


def test_call_aws_mixed_success_and_failure_is_partial():
    """One success + one failing entry => partial (not complete, not unavailable)."""
    ok = {"cli_command": "aws eks list-clusters", "response": {"error": None, "json": json.dumps({"clusters": ["c1"]})}}
    bad = {"cli_command": "aws eks list-nodegroups --cluster-name c1", "error": "boom"}
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [ok, bad]})}],
        structured_content=call_aws_structured([ok, bad]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": ["aws eks list-clusters", "aws eks list-nodegroups --cluster-name c1"]})
    body = _body(event)
    assert body["outcome"] == "partial"


def test_call_aws_entries_not_a_list_is_malformed():
    """A structuredContent result that is not a list of dicts is malformed."""
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": "not-a-list"})}],
        structured_content={"result": "not-a-list"},
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    assert _body(event)["outcome"] == "malformed"


def test_call_aws_all_success_empty_list_is_explicit_empty():
    """A single verb legitimately returning an empty list is truthfully 'empty'
    (an explicit empty outcome, not 'complete')."""
    entry = {"cli_command": "aws eks list-clusters", "response": {"error": None, "json": json.dumps({"clusters": []})}}
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _body(event)
    assert body["outcome"] == "empty"
    assert body["results"][0]["clusters"] == []


# =========================================================================== #
# Finding 9 — code-owned bounded max_results always sent                      #
# =========================================================================== #
def test_call_aws_sends_bounded_max_results_when_model_omits_it():
    """Even when the model omits max_results, a code-owned bound is sent."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "json": json.dumps({"clusters": ["c1"]})},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    assert event.tool_result["status"] == "success"
    sent = delegate.calls[0]["input"].get("max_results")
    assert isinstance(sent, int) and sent > 0, "a code-owned bounded max_results must be sent"


def test_call_aws_clamps_oversized_max_results():
    """An oversized max_results is clamped to the reviewed maximum."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "json": json.dumps({"clusters": ["c1"]})},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    _invoke(tool, {"cli_command": "aws eks list-clusters", "max_results": 100000})
    assert delegate.calls[0]["input"]["max_results"] <= guard_mod.MAX_RESULTS_CAP


def test_call_aws_rejects_bool_max_results_instead_of_silently_dropping_bound():
    """A bool/invalid max_results must be rejected, not silently unbounded."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "json": json.dumps({"clusters": ["c1"]})},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters", "max_results": True})
    if event.tool_result["status"] == "success":
        # If accepted, it must have been clamped to a code-owned integer bound.
        assert isinstance(delegate.calls[0]["input"]["max_results"], int)
        assert not isinstance(delegate.calls[0]["input"]["max_results"], bool)
    else:
        assert delegate.calls == []


# =========================================================================== #
# Finding 10 — deterministic serialization + stable errorCode                 #
# =========================================================================== #
def test_error_envelope_has_stable_error_code():
    """Error envelopes carry a stable, code-owned errorCode."""
    delegate = _RealMcpTool("list_k8s_resources", [{"text": "AccessDenied blah"}], status="error")
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    body = _body(event)
    assert body.get("errorCode"), "error envelope must expose a stable errorCode"
    assert body["outcome"] == "denied"


def test_success_serialization_is_deterministic_sorted():
    """Keys are emitted in a deterministic (sorted) order for stable output."""
    model = {"kind": "Pod", "api_version": "v1", "namespace": "default", "count": 0, "items": []}
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    raw = next(c["text"] for c in event.tool_result["content"] if "text" in c)
    reparsed = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    assert raw == reparsed, "serialization must be deterministic (sorted keys, compact separators)"


def test_envelope_byte_cap_is_exact_no_slack():
    """The final UTF-8 cap is exact — no +512 allowance."""
    big = "\u4e2d" * 20000  # 3 bytes/char in UTF-8
    model = {
        "kind": "Pod",
        "api_version": "v1",
        "count": 1,
        "items": [{"name": big, "namespace": "default", "creation_timestamp": "t", "labels": {}}],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    text = next(c["text"] for c in event.tool_result["content"] if "text" in c)
    assert len(text.encode("utf-8")) <= guard_mod.MAX_ENVELOPE_BYTES


# =========================================================================== #
# Finding 4 — cancellation propagates; deadline => unavailable                #
# =========================================================================== #
def test_external_cancellation_propagates_not_swallowed():
    """A CancelledError raised by external task cancellation must propagate,
    not be converted into an availability 'unavailable' result."""

    class _CancellingTool(AgentTool):
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
            raise asyncio.CancelledError()
            yield  # pragma: no cover

    async def _run():
        guarded = guard_eks_mcp_client("eks-mcp-server", _FakeProvider([_CancellingTool()]))
        tool = _find(list(await guarded.load_tools()), "list_k8s_resources")
        tool_use: ToolUse = {
            "toolUseId": "tu-1",
            "name": "list_k8s_resources",
            "input": {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"},
        }
        return [e async for e in tool.stream(tool_use, {})]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())


def test_deadline_timeout_becomes_unavailable(monkeypatch):
    """A genuine deadline timeout (not external cancel) becomes unavailable."""
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
            await asyncio.sleep(3600)
            yield ToolResultEvent({"toolUseId": tool_use["toolUseId"], "status": "success", "content": []})

    async def _run():
        guarded = guard_eks_mcp_client("eks-mcp-server", _FakeProvider([_HangingTool()]))
        tool = _find(list(await guarded.load_tools()), "list_k8s_resources")
        tool_use: ToolUse = {
            "toolUseId": "tu-1",
            "name": "list_k8s_resources",
            "input": {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"},
        }
        return [e async for e in tool.stream(tool_use, {})]

    events = asyncio.run(_run())
    assert events[-1].tool_result["status"] == "error"
    assert _body(events[-1])["outcome"] == "unavailable"


# =========================================================================== #
# Finding 3 — narrowed ToolSpec/inputSchema on guarded tools                  #
# =========================================================================== #
def test_call_aws_toolspec_does_not_advertise_unbounded_fields():
    """The advertised call_aws inputSchema must not advertise pagination tokens
    or free-form fields the runtime rejects; only reviewed fields are shown."""
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": []})}], structured_content={"result": []}
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    spec = tool.tool_spec
    schema = spec.get("inputSchema", {}).get("json", {})
    props = schema.get("properties", {})
    assert "next_token" not in props
    assert "vpc_id" not in props


def test_vpc_toolspec_does_not_advertise_vpc_id():
    """The guarded get_eks_vpc_config schema must not advertise vpc_id."""
    model = {"vpc_id": "vpc-1", "cidr_block": "10.0.0.0/16", "routes": [], "cluster_name": "c1"}
    delegate = _RealMcpTool("get_eks_vpc_config", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_vpc_config")
    schema = tool.tool_spec.get("inputSchema", {}).get("json", {})
    props = schema.get("properties", {})
    assert "vpc_id" not in props


# =========================================================================== #
# Finding 12 — parameterized coverage for every surviving EKS read tool       #
# =========================================================================== #
# (tool_name, valid input, success model_dump using exact installed field names)
_EKS_READ_TOOL_CASES = [
    (
        "list_k8s_resources",
        {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"},
        {
            "kind": "Pod",
            "api_version": "v1",
            "namespace": "default",
            "count": 1,
            "items": [{"name": "pod-a", "labels": {"app": "web"}}],
        },
    ),
    (
        "get_k8s_events",
        {"cluster_name": "c1", "kind": "Pod", "name": "pod-a", "namespace": "default"},
        {
            "involved_object_kind": "Pod",
            "involved_object_name": "pod-a",
            "count": 1,
            "events": [{"message": "ok", "type": "Normal", "reason": "Started"}],
        },
    ),
    (
        "list_api_versions",
        {"cluster_name": "c1"},
        {"cluster_name": "c1", "api_versions": ["v1", "apps/v1"], "count": 2},
    ),
    (
        "get_eks_metrics_guidance",
        {"resource_type": "pod"},
        {
            "resource_type": "pod",
            "metrics": [
                {
                    "name": "cpu",
                    "description": "cpu",
                    "unit": "Percent",
                    "namespace": "ContainerInsights",
                    "dimensions": ["ClusterName"],
                }
            ],
        },
    ),
    (
        "get_cloudwatch_metrics",
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"ClusterName": "c1"},
        },
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "start_time": "t",
            "end_time": "t",
            "data_points": [{"timestamp": "t", "value": 1.5}],
        },
    ),
    (
        "get_eks_insights",
        {"cluster_name": "c1"},
        {
            "cluster_name": "c1",
            "insights": [
                {
                    "id": "ins-1",
                    "name": "n",
                    "category": "CONFIGURATION",
                    "description": "d",
                    "insight_status": {"status": "PASSING", "reason": "ok"},
                }
            ],
        },
    ),
    (
        "get_eks_vpc_config",
        {"cluster_name": "c1"},
        {"vpc_id": "vpc-1", "cidr_block": "10.0.0.0/16", "routes": [], "subnets": [], "cluster_name": "c1"},
    ),
]


@pytest.mark.parametrize("tool_name,valid_input,model_dump", _EKS_READ_TOOL_CASES)
def test_every_eks_read_tool_projects_its_real_two_block_shape(tool_name, valid_input, model_dump):
    """Each surviving EKS read tool projects its real [prose, json-text] shape to
    a versioned success envelope (never malformed for a valid response)."""
    delegate = _RealMcpTool(tool_name, eks_two_block(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), tool_name)
    event = _invoke(tool, valid_input)
    assert event.tool_result["status"] == "success", f"{tool_name} valid response must project"
    body = _body(event)
    assert body["v"], "versioned envelope"
    assert body["operation"] == tool_name
    assert body["outcome"] in {"complete", "empty", "partial", "truncated"}


@pytest.mark.parametrize("tool_name,valid_input,model_dump", _EKS_READ_TOOL_CASES)
def test_every_eks_read_tool_rejects_unknown_key(tool_name, valid_input, model_dump):
    """Every surviving EKS read tool rejects an unknown input key before dispatch."""
    delegate = _RealMcpTool(tool_name, eks_two_block(model_dump))
    tool = _find(_load("eks-mcp-server", [delegate]), tool_name)
    event = _invoke(tool, {**valid_input, "unreviewed_extra": "x"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


# Every allowed call_aws verb with a minimal valid raw-JSON payload.
_CALL_AWS_VERB_CASES = [
    ("aws eks list-clusters", {"clusters": ["c1"]}),
    ("aws eks describe-cluster --name c1", {"cluster": {"name": "c1", "status": "ACTIVE", "version": "1.30"}}),
    ("aws eks list-nodegroups --cluster-name c1", {"nodegroups": ["ng-1"]}),
    (
        "aws eks describe-nodegroup --cluster-name c1 --nodegroup-name ng-1",
        {"nodegroup": {"nodegroupName": "ng-1", "status": "ACTIVE"}},
    ),
    ("aws eks list-fargate-profiles --cluster-name c1", {"fargateProfileNames": ["fp-1"]}),
    (
        "aws eks describe-fargate-profile --cluster-name c1 --fargate-profile-name fp-1",
        {"fargateProfile": {"fargateProfileName": "fp-1", "status": "ACTIVE"}},
    ),
    ("aws eks list-addons --cluster-name c1", {"addons": ["vpc-cni"]}),
    (
        "aws eks describe-addon --cluster-name c1 --addon-name vpc-cni",
        {"addon": {"addonName": "vpc-cni", "status": "ACTIVE"}},
    ),
]


@pytest.mark.parametrize("command,raw_json", _CALL_AWS_VERB_CASES)
def test_every_allowed_call_aws_verb_projects(command, raw_json):
    """Each allowed read-only `aws eks` verb projects its real flattened
    CallAWSResponse entry to a versioned success envelope."""
    entry = {"cli_command": command, "response": {"error": None, "json": json.dumps(raw_json)}}
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": command})
    assert event.tool_result["status"] == "success", f"{command} must project"
    body = _body(event)
    assert body["operation"] == "call_aws"
    assert body["results"], "a projected entry is present"
    # A code-owned bounded max_results was always sent.
    assert isinstance(delegate.calls[0]["input"]["max_results"], int)


# =========================================================================== #
# issue #466 residual defects — assertion-level coverage on current code      #
# =========================================================================== #
# The blocks below pin the residual current-code gaps found by a fresh         #
# full-file inspection: sanitizer coverage (C1 / embedded 12-digit runs /      #
# 4-digit dotted components / credential assignments / Bearer|Basic /          #
# unterminated PEM), CloudWatch + metrics-guidance input truth, the           #
# delegate-Exception drain path, call_aws failure/denied classification and    #
# canonicalization, the empty-outcome contract, bounded error classification,  #
# narrowed ToolSpec schema constraints, and factory startup/transport error    #
# sanitization for the EKS servers. Synthetic, public-safe fixtures only.      #


# --------------------------------------------------------------------------- #
# Defect 1 — free-text / scalar sanitizer coverage                            #
# --------------------------------------------------------------------------- #
def test_sanitize_strips_c1_control_characters():
    """C1 controls (0x80-0x9f) must be stripped from free text, not preserved."""
    out = guard_mod._sanitize_text("before\x85\x90after")
    assert out is not None
    assert "\x85" not in out and "\x9f" not in out and "\x90" not in out
    assert "before" in out and "after" in out


def test_sanitize_redacts_embedded_twelve_digit_run():
    """A 12-digit account-like run embedded in a longer token must be redacted,
    not just a standalone \\b-delimited one."""
    out = guard_mod._sanitize_text("id=x123456789012y and 4567890123456789")
    assert out is not None
    assert "123456789012" not in out


def test_sanitize_redacts_four_digit_dotted_components():
    """A dotted-numeric with a 4-digit component (evades the 1-3 digit IPv4/
    padded rules) must still be redacted."""
    out = guard_mod._sanitize_text("reach 1000.1.2.3 or 10.2000.3.4 now")
    assert out is not None
    assert "1000.1.2.3" not in out
    assert "10.2000.3.4" not in out


def test_sanitize_redacts_bearer_and_basic_auth_tokens():
    """Authorization Bearer/Basic credential material must be redacted."""
    out = guard_mod._sanitize_text("Authorization: Bearer abcDEF123456.tok and Basic dXNlcjpwYXNz")
    assert out is not None
    assert "abcDEF123456.tok" not in out
    assert "dXNlcjpwYXNz" not in out


def test_sanitize_redacts_credential_assignments():
    """password=/secret=/token=/apikey= assignments must be redacted."""
    out = guard_mod._sanitize_text("password=hunter2 secret: sk-9f8a7b token=abc123XYZ apikey=KEY99")
    assert out is not None
    assert "hunter2" not in out
    assert "sk-9f8a7b" not in out
    assert "abc123XYZ" not in out
    assert "KEY99" not in out


def test_sanitize_redacts_unterminated_pem_marker_and_body():
    """An unterminated PEM marker + body (no END line) must not leak."""
    marker = "-----BEGIN RSA " + "PRIVATE" + " KEY-----"
    leaky = f"{marker}\nMIIEpAIBAAKCAQEA9w0BAQEFAAO\nMORESECRETBODY"
    out = guard_mod._sanitize_text(leaky)
    assert out is not None
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "MIIEpAIBAAKCAQEA9w0BAQEFAAO" not in out


def test_sanitize_redacts_full_pem_block_still():
    """A complete PEM block remains redacted (no regression)."""
    block = "-----BEGIN CERTIFICATE-----\nQUJDREVG\n-----END CERTIFICATE-----"
    out = guard_mod._sanitize_text(block)
    assert out is not None
    assert "QUJDREVG" not in out
    assert "BEGIN CERTIFICATE" not in out


def test_scalar_field_rejects_four_digit_dotted_and_c1():
    """A scalar identifier carrying a 4-digit dotted coordinate or a C1 control
    must be refused/scrubbed, not ride through an allowed scalar field."""
    model = {
        "kind": "Pod",
        "api_version": "v1",
        "count": 1,
        "items": [{"name": "pod-a", "namespace": "default", "creation_timestamp": "1000.2.3.4", "labels": {}}],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    assert "1000.2.3.4" not in _text(event)


# --------------------------------------------------------------------------- #
# Defect 3 — free-text length/redaction changed+truncated signalling          #
# --------------------------------------------------------------------------- #
def test_event_message_clipped_marks_field_truncated_signal():
    """When a free-text field is clipped by the length cap, the projection must
    expose an explicit truncation/partial signal rather than silently clipping
    while the outcome stays complete."""
    long_msg = "A" * (guard_mod.MAX_TEXT + 500)
    model = {
        "involved_object_kind": "Pod",
        "involved_object_name": "pod-a",
        "count": 1,
        "events": [{"message": long_msg, "type": "Warning", "reason": "Failed"}],
    }
    delegate = _RealMcpTool("get_k8s_events", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_k8s_events")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "name": "pod-a", "namespace": "default"})
    body = _body(event)
    # The clipped free text must be flagged: either the event carries a
    # truncated marker or the envelope is marked partial/truncated.
    ev0 = body["events"][0]
    clipped = ev0.get("message_truncated") is True or ev0.get("truncated") is True
    assert (
        clipped or body.get("partial") is True or body.get("truncated") is True
    ), "a silently clipped free-text field must surface a truncation/partial signal"


# --------------------------------------------------------------------------- #
# Defect 3 — CloudWatch required top-level shape                              #
# --------------------------------------------------------------------------- #
def test_cloudwatch_missing_required_start_end_is_malformed_not_empty():
    """CloudWatchMetricsData requires start_time/end_time/data_points; a payload
    missing start_time/end_time must be malformed, not a zero-point empty."""
    model = {
        "cluster_name": "c1",
        "metric_name": "cpu_usage_total",
        "namespace": "ContainerInsights",
        "data_points": [],
    }
    delegate = _RealMcpTool("get_cloudwatch_metrics", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"ClusterName": "c1"},
        },
    )
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_cloudwatch_data_point_missing_timestamp_marks_partial():
    """Each data point requires timestamp+value; a point missing timestamp is
    dropped and the aggregate marked partial, not a clean complete."""
    model = {
        "cluster_name": "c1",
        "metric_name": "cpu_usage_total",
        "namespace": "ContainerInsights",
        "start_time": "t0",
        "end_time": "t1",
        "data_points": [{"value": 1.5}, {"timestamp": "t", "value": 2.0}],
    }
    delegate = _RealMcpTool("get_cloudwatch_metrics", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"ClusterName": "c1"},
        },
    )
    body = _body(event)
    assert body.get("partial") is True
    assert body["outcome"] == "partial"


def test_insight_row_missing_required_timestamps_marks_partial():
    """EksInsightItem requires last_refresh_time + last_transition_time; a row
    missing them must be dropped + partial, not a clean complete."""
    model = {
        "cluster_name": "c1",
        "insights": [
            {
                "id": "ins-1",
                "name": "n",
                "category": "CONFIGURATION",
                "description": "d",
                "insight_status": {"status": "PASSING", "reason": "ok"},
            }
        ],
    }
    delegate = _RealMcpTool("get_eks_insights", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_insights")
    event = _invoke(tool, {"cluster_name": "c1"})
    body = _body(event)
    assert body.get("partial") is True
    assert body["outcome"] == "partial"


def test_insight_status_missing_reason_is_dropped_partial():
    """EksInsightStatus requires status AND reason; a status missing reason makes
    the row drop and marks partial."""
    model = {
        "cluster_name": "c1",
        "insights": [
            {
                "id": "ins-1",
                "name": "n",
                "category": "CONFIGURATION",
                "last_refresh_time": 1.0,
                "last_transition_time": 2.0,
                "description": "d",
                "insight_status": {"status": "PASSING"},
            }
        ],
    }
    delegate = _RealMcpTool("get_eks_insights", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_insights")
    event = _invoke(tool, {"cluster_name": "c1"})
    body = _body(event)
    assert body.get("partial") is True


def test_list_k8s_declared_count_mismatch_marks_partial():
    """A declared count that exceeds the returned items (after valid projection)
    means results are missing: partial, never a clean complete."""
    model = {
        "kind": "Pod",
        "api_version": "v1",
        "count": 9,  # declares 9 but ships 1 valid item
        "items": [{"name": "pod-a", "labels": {}}],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(model))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    body = _body(event)
    assert body["outcome"] in {"partial", "truncated"}
    assert body.get("partial") is True or body.get("truncated") is True


# --------------------------------------------------------------------------- #
# Defect 4 — input truth: metrics-guidance + CloudWatch                      #
# --------------------------------------------------------------------------- #
def test_metrics_guidance_missing_resource_type_is_rejected():
    """resource_type is required by MetricsGuidanceData; a missing resource_type
    must be rejected before dispatch (not silently accepted)."""
    delegate = _RealMcpTool("get_eks_metrics_guidance", eks_two_block({"resource_type": "pod", "metrics": []}))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_metrics_guidance")
    event = _invoke(tool, {})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_missing_namespace_is_rejected():
    """namespace is required; a missing namespace must be rejected pre-dispatch."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(tool, {"cluster_name": "c1", "metric_name": "cpu_usage_total"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_namespace_must_be_reviewed_enum():
    """namespace must be one of the reviewed enum values, not arbitrary text."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {"cluster_name": "c1", "metric_name": "cpu_usage_total", "namespace": "AWS/Arbitrary/Whatever"},
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_arbitrary_dimension_key_is_rejected():
    """A dimension key outside the reviewed set must be rejected."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"NotReviewed": "x"},
        },
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_reviewed_dimension_key_is_allowed():
    """A reviewed dimension key (ClusterName) with a safe value is allowed."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [{"timestamp": "t", "value": 1.0}],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"ClusterName": "c1", "Namespace": "default"},
        },
    )
    assert event.tool_result["status"] == "success"
    assert delegate.calls, "a valid reviewed dimension request must reach the delegate"


def test_cloudwatch_unsafe_dimension_value_is_rejected():
    """A reviewed key with an unsafe (non-identifier) value must be rejected."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"ClusterName": "bad; rm -rf /"},
        },
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_accepts_bounded_minutes_limit_period_stat():
    """Optional minutes/limit/period/stat are accepted within hard bounds and a
    canonical bounded mapping is forwarded to the delegate."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [{"timestamp": "t", "value": 1.0}],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"ClusterName": "c1"},
            "minutes": 60,
            "limit": 50,
            "period": 300,
            "stat": "Average",
        },
    )
    assert event.tool_result["status"] == "success"
    sent = delegate.calls[0]["input"]
    assert sent.get("minutes") == 60
    assert sent.get("period") == 300
    assert sent.get("stat") == "Average"


def test_cloudwatch_out_of_bounds_minutes_is_rejected():
    """An out-of-range minutes value must be rejected, not passed unbounded."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "minutes": 10_000_000,
        },
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_unparseable_time_range_is_rejected():
    """start/end that cannot be parsed and bounded safely must be rejected."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "start_time": "not a real timestamp!!",
            "end_time": "also bad",
        },
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_account_like_cluster_identifier_is_rejected():
    """A sensitive/account-like cluster identifier must be rejected."""
    delegate = _RealMcpTool(
        "get_cloudwatch_metrics",
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu_usage_total",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {"cluster_name": "123456789012", "metric_name": "cpu_usage_total", "namespace": "ContainerInsights"},
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


# --------------------------------------------------------------------------- #
# Defect 5 — delegate Exception drains to typed unavailable (no logged text)  #
# --------------------------------------------------------------------------- #
def test_delegate_runtime_error_becomes_unavailable_without_logging_text():
    """A delegate raising a RuntimeError whose text embeds an ARN/endpoint must
    become a typed 'unavailable' outcome; its text must not appear in the
    guard's captured logs or the model-facing output. CancelledError still
    propagates (covered separately)."""
    # Third-party packages
    from loguru import logger as _loguru_logger

    secret = f"kaboom {_FAKE_ARN} at {_FAKE_ENDPOINT}"
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda m: captured.append(str(m)), level="DEBUG")

    class _RaisingTool(AgentTool):
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
            raise RuntimeError(secret)
            yield  # pragma: no cover

    async def _run():
        guarded = guard_eks_mcp_client("eks-mcp-server", _FakeProvider([_RaisingTool()]))
        tool = _find(list(await guarded.load_tools()), "list_k8s_resources")
        tool_use: ToolUse = {
            "toolUseId": "tu-1",
            "name": "list_k8s_resources",
            "input": {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"},
        }
        return [e async for e in tool.stream(tool_use, {})]

    try:
        events = asyncio.run(_run())
    finally:
        _loguru_logger.remove(sink_id)

    logged = "".join(captured)
    assert events, "delegate Exception must not propagate; a typed outcome is emitted"
    assert events[-1].tool_result["status"] == "error"
    assert _body(events[-1])["outcome"] == "unavailable"
    assert _FAKE_ARN not in _text(events[-1])
    assert _FAKE_ARN not in logged
    assert _FAKE_ENDPOINT not in logged
    assert "kaboom" not in logged


# --------------------------------------------------------------------------- #
# Defect 7 — call_aws failure gate / denied classification / canonical match  #
# --------------------------------------------------------------------------- #
def test_call_aws_error_code_denies_even_when_error_field_is_falsey():
    """When response.error is an empty string but error_code says AccessDenied,
    the entry must classify as denied (the falsey-error gate must not swallow it
    into malformed/complete)."""
    entry = {
        "cli_command": "aws eks describe-cluster --name c1",
        "response": {"error": "", "error_code": "AccessDeniedException", "status_code": 403, "json": None},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name c1"})
    assert _body(event)["outcome"] == "denied"


def test_call_aws_failed_constraints_denial_marker_classifies_denied():
    """A denial marker present only in failed_constraints must classify denied."""
    entry = {
        "cli_command": "aws eks describe-cluster --name c1",
        "response": {"error": None, "json": None},
        "failed_constraints": ["AccessDenied"],
        "error": "blocked",
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name c1"})
    assert _body(event)["outcome"] == "denied"


def test_call_aws_non_2xx_status_with_json_is_not_treated_as_success():
    """A non-2xx status_code with a JSON body must not be projected as success."""
    entry = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "status_code": 500, "json": json.dumps({"clusters": []})},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _body(event)
    assert body["outcome"] != "complete"


def test_call_aws_entry_cli_command_mismatch_is_malformed():
    """If the returned entry's cli_command does not canonicalize to the requested
    command, that entry must be treated as malformed, not projected as success."""
    entry = {
        "cli_command": "aws eks describe-cluster --name OTHER-CLUSTER",
        "response": {"error": None, "json": json.dumps({"cluster": {"name": "x", "status": "ACTIVE"}})},
    }
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks describe-cluster --name c1"})
    body = _body(event)
    # A mismatched command must not be projected as a clean complete success.
    assert body["outcome"] != "complete"


def test_call_aws_extra_entries_beyond_commands_marks_partial():
    """More entries than commands (len(entries) != len(commands)) must be
    partial, not silently complete on the zipped subset."""
    ok = {"cli_command": "aws eks list-clusters", "response": {"error": None, "json": json.dumps({"clusters": ["c1"]})}}
    extra = {
        "cli_command": "aws eks list-nodegroups --cluster-name c1",
        "response": {"error": None, "json": json.dumps({"nodegroups": []})},
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [ok, extra]})}],
        structured_content=call_aws_structured([ok, extra]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _body(event)
    assert body.get("partial") is True
    assert body["outcome"] == "partial"


# --------------------------------------------------------------------------- #
# Defect 8 — empty outcome contract for call_aws                              #
# --------------------------------------------------------------------------- #
def test_call_aws_valid_complete_empty_list_yields_empty_outcome():
    """A valid single-command call returning an empty collection must yield an
    explicit 'empty' outcome, not 'complete'."""
    entry = {"cli_command": "aws eks list-clusters", "response": {"error": None, "json": json.dumps({"clusters": []})}}
    delegate = _RealMcpTool(
        "call_aws", content=[{"text": json.dumps({"result": [entry]})}], structured_content=call_aws_structured([entry])
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters"})
    body = _body(event)
    assert body["outcome"] == "empty"
    assert body["results"][0]["clusters"] == []


def test_call_aws_mixed_empty_and_nonempty_is_complete_with_entries():
    """A batch mixing an empty list result and a non-empty list result is
    complete overall (no failures), with per-entry contents preserved."""
    empty = {
        "cli_command": "aws eks list-fargate-profiles --cluster-name c1",
        "response": {"error": None, "json": json.dumps({"fargateProfileNames": []})},
    }
    nonempty = {
        "cli_command": "aws eks list-clusters",
        "response": {"error": None, "json": json.dumps({"clusters": ["c1"]})},
    }
    delegate = _RealMcpTool(
        "call_aws",
        content=[{"text": json.dumps({"result": [empty, nonempty]})}],
        structured_content=call_aws_structured([empty, nonempty]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(
        tool,
        {"cli_command": ["aws eks list-fargate-profiles --cluster-name c1", "aws eks list-clusters"]},
    )
    body = _body(event)
    assert body["outcome"] == "complete"
    assert len(body["results"]) == 2


# --------------------------------------------------------------------------- #
# Defect 9 — bounded error-text classification                                #
# --------------------------------------------------------------------------- #
def test_classify_error_only_inspects_bounded_prefix():
    """_classify_error must inspect only a bounded prefix of provider error text
    for denial markers (a denial marker far past the bound is not scanned), and
    must never leak the raw body."""
    prefix = "generic failure. "
    tail_marker = "x" * 100_000 + " accessdenied"
    delegate = _RealMcpTool("list_k8s_resources", [{"text": prefix + tail_marker}], status="error")
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    body = _body(event)
    # A denial marker only present beyond the bounded prefix is not scanned:
    # the result is unavailable, and the raw body never leaks.
    assert body["outcome"] == "unavailable"
    assert "accessdenied" not in _text(event).lower()


def test_classify_error_detects_denial_within_bounded_prefix():
    """A denial marker within the bounded prefix is still classified denied."""
    delegate = _RealMcpTool("list_k8s_resources", [{"text": "AccessDenied: nope"}], status="error")
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    assert _body(event)["outcome"] == "denied"


# --------------------------------------------------------------------------- #
# Defect 10 — narrowed ToolSpec schema constraints (not just absent fields)   #
# --------------------------------------------------------------------------- #
def _real_list_k8s_schema() -> dict:
    """A realistic delegate inputSchema carrying ALL actual advertised fields."""
    return {
        "json": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "cluster_name": {"type": "string"},
                "kind": {"type": "string"},
                "api_version": {"type": "string"},
                "namespace": {"type": "string"},
                "label_selector": {"type": "string"},
                "field_selector": {"type": "string"},
                "next_token": {"type": "string"},
                "raw_manifest": {"type": "string"},
            },
            "required": ["cluster_name", "kind", "api_version"],
        }
    }


class _SchemaTool(_RealMcpTool):
    """A _RealMcpTool whose inputSchema is supplied explicitly (full fields)."""

    def __init__(self, name: str, schema: dict, content: list[dict[str, Any]]) -> None:
        super().__init__(name, content)
        self._schema = schema

    @property
    def tool_spec(self) -> ToolSpec:
        return {"name": self._name, "description": "orig", "inputSchema": self._schema}


def test_list_k8s_toolspec_narrows_properties_required_and_additional_properties():
    """The guarded list_k8s_resources schema advertises only reviewed properties,
    keeps a valid required subset, and forbids additionalProperties."""
    delegate = _SchemaTool(
        "list_k8s_resources",
        _real_list_k8s_schema(),
        eks_two_block({"kind": "Pod", "api_version": "v1", "count": 0, "items": []}),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    schema = tool.tool_spec["inputSchema"]["json"]
    props = set(schema.get("properties", {}).keys())
    assert props <= {"cluster_name", "kind", "api_version", "namespace", "label_selector", "field_selector"}
    assert "next_token" not in props and "raw_manifest" not in props
    assert set(schema.get("required", [])) <= props
    assert schema.get("additionalProperties") is False


def test_cloudwatch_toolspec_declares_reviewed_bounds_and_enums():
    """The guarded get_cloudwatch_metrics schema advertises reviewed properties
    with bounds/enums (namespace enum; minutes/limit/period numeric bounds) and
    forbids additionalProperties."""
    schema = {
        "json": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "cluster_name": {"type": "string"},
                "metric_name": {"type": "string"},
                "namespace": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
                "dimensions": {"type": "object"},
                "minutes": {"type": "integer"},
                "limit": {"type": "integer"},
                "period": {"type": "integer"},
                "stat": {"type": "string"},
                "raw": {"type": "string"},
            },
        }
    }
    delegate = _SchemaTool(
        "get_cloudwatch_metrics",
        schema,
        eks_two_block(
            {
                "cluster_name": "c1",
                "metric_name": "cpu",
                "namespace": "ContainerInsights",
                "start_time": "t",
                "end_time": "t",
                "data_points": [],
            }
        ),
    )
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    schema_out = tool.tool_spec["inputSchema"]["json"]
    props = schema_out.get("properties", {})
    assert "raw" not in props
    assert schema_out.get("additionalProperties") is False
    # namespace advertised as a bounded enum
    assert isinstance(props.get("namespace", {}).get("enum"), list) and props["namespace"]["enum"]
    # numeric bounds advertised on minutes/period
    assert "maximum" in props.get("minutes", {}) or "minimum" in props.get("minutes", {})
    assert "maximum" in props.get("period", {}) or "minimum" in props.get("period", {})
    # stat advertised as a bounded enum
    assert isinstance(props.get("stat", {}).get("enum"), list) and props["stat"]["enum"]


def test_call_aws_toolspec_declares_max_results_bounds_and_no_additional_properties():
    """The guarded call_aws schema advertises bounded max_results and forbids
    additionalProperties."""
    schema = {
        "json": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "cli_command": {"type": "string"},
                "max_results": {"type": "integer"},
                "region": {"type": "string"},
                "profile": {"type": "string"},
            },
        }
    }
    delegate = _SchemaTool("call_aws", schema, [{"text": json.dumps({"result": []})}])
    # _SchemaTool has no structured_content; call_aws path tolerates that here
    # because we only inspect the advertised spec, not a dispatch.
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    schema_out = tool.tool_spec["inputSchema"]["json"]
    props = schema_out.get("properties", {})
    assert set(props.keys()) <= {"cli_command", "max_results"}
    assert schema_out.get("additionalProperties") is False
    mr = props.get("max_results", {})
    assert mr.get("minimum") == guard_mod.MAX_RESULTS_MIN
    assert mr.get("maximum") == guard_mod.MAX_RESULTS_CAP


# --------------------------------------------------------------------------- #
# Defect 11 — factory startup/transport error sanitization (EKS servers only) #
# --------------------------------------------------------------------------- #
def test_factory_startup_error_is_type_only_for_eks_servers(monkeypatch):
    """A startup/transport exception whose text embeds child/provider content
    must be logged as a type-only fixed message for the two EKS stdio servers —
    the child text must not appear in captured logs."""
    # Third-party packages
    from loguru import logger as _loguru_logger

    # Local modules
    import utils.mcp_client_factory as factory

    secret = f"child stderr blob {_FAKE_ARN} {_FAKE_ENDPOINT}"

    def _boom(_server_name):
        raise RuntimeError(secret)

    # Force resolution to raise inside the retry loop so the except paths run.
    monkeypatch.setattr(factory, "_resolve_mcp_command", _boom)
    monkeypatch.setattr(factory, "MCP_CREATE_RETRY_DELAY", 0)

    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda m: captured.append(str(m)), level="DEBUG")
    try:
        client = _real_create_mcp_client("eks-mcp-server", use_cache=False)
    finally:
        _loguru_logger.remove(sink_id)

    logged = "".join(captured)
    assert client is None
    assert _FAKE_ARN not in logged
    assert _FAKE_ENDPOINT not in logged
    assert "child stderr blob" not in logged


# =========================================================================== #
# Final orchestrator review regressions                                       #
# =========================================================================== #
@pytest.mark.parametrize(
    "payload",
    [
        {"api_version": "v1", "count": 0, "items": []},
        {"kind": "Pod", "count": 0, "items": []},
        {"kind": "Pod", "api_version": "v1", "items": []},
    ],
)
def test_list_resources_missing_required_top_level_member_is_malformed(payload):
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(payload))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    event = _invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"})
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_list_resources_declared_count_mismatch_is_partial():
    payload = {
        "kind": "Pod",
        "api_version": "v1",
        "count": 0,
        "items": [{"name": "pod-a"}],
    }
    delegate = _RealMcpTool("list_k8s_resources", eks_two_block(payload))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_k8s_resources")
    body = _body(_invoke(tool, {"cluster_name": "c1", "kind": "Pod", "api_version": "v1"}))
    assert body["outcome"] == "partial"
    assert body["partial"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"api_versions": [], "count": 0},
        {"cluster_name": "c1", "api_versions": []},
    ],
)
def test_api_versions_missing_required_member_is_malformed(payload):
    delegate = _RealMcpTool("list_api_versions", eks_two_block(payload))
    tool = _find(_load("eks-mcp-server", [delegate]), "list_api_versions")
    event = _invoke(tool, {"cluster_name": "c1"})
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_event_redaction_is_not_reported_complete():
    payload = {
        "involved_object_kind": "Pod",
        "involved_object_name": "pod-a",
        "count": 1,
        "events": [
            {
                "message": "credential token=synthetic-secret-value",
                "reason": "Failed",
                "type": "Warning",
            }
        ],
    }
    delegate = _RealMcpTool("get_k8s_events", eks_two_block(payload))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_k8s_events")
    body = _body(_invoke(tool, {"cluster_name": "c1", "kind": "Pod", "name": "pod-a"}))
    assert "synthetic-secret-value" not in json.dumps(body)
    assert body["outcome"] != "complete"
    assert body.get("redacted") is True


def test_cloudwatch_metrics_requires_dimensions_before_dispatch():
    delegate = _RealMcpTool("get_cloudwatch_metrics", eks_two_block({}))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {"cluster_name": "c1", "metric_name": "cpu_usage_total", "namespace": "ContainerInsights"},
    )
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []


def test_cloudwatch_metrics_forwards_only_bounded_reviewed_inputs():
    payload = {
        "cluster_name": "c1",
        "metric_name": "cpu_usage_total",
        "namespace": "ContainerInsights",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T01:00:00Z",
        "data_points": [],
    }
    delegate = _RealMcpTool("get_cloudwatch_metrics", eks_two_block(payload))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_cloudwatch_metrics")
    event = _invoke(
        tool,
        {
            "cluster_name": "c1",
            "metric_name": "cpu_usage_total",
            "namespace": "ContainerInsights",
            "dimensions": {"ClusterName": "c1"},
            "minutes": 60,
            "limit": 100,
            "period": 60,
            "stat": "Average",
        },
    )
    assert event.tool_result["status"] == "success"
    sent = delegate.calls[0]["input"]
    assert sent["dimensions"] == {"ClusterName": "c1"}
    assert 1 <= sent["limit"] <= guard_mod.MAX_ITEMS


def test_vpc_present_optional_collection_with_wrong_type_is_malformed():
    payload = {
        "vpc_id": "vpc-synthetic",
        "cidr_block": "10.0.0.0/16",
        "routes": [],
        "cluster_name": "c1",
        "additional_cidr_blocks": "not-a-list",
    }
    delegate = _RealMcpTool("get_eks_vpc_config", eks_two_block(payload))
    tool = _find(_load("eks-mcp-server", [delegate]), "get_eks_vpc_config")
    event = _invoke(tool, {"cluster_name": "c1"})
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_call_aws_local_list_cap_is_explicitly_truncated():
    clusters = [f"cluster-{index}" for index in range(guard_mod.MAX_ITEMS + 5)]
    command = "aws eks list-clusters"
    entry = {"cli_command": command, "response": {"error": None, "json": json.dumps({"clusters": clusters})}}
    delegate = _RealMcpTool(
        "call_aws",
        [{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    body = _body(_invoke(tool, {"cli_command": command}))
    assert body["outcome"] == "truncated"
    assert body["truncated"] is True
    assert len(body["results"][0]["clusters"]) == guard_mod.MAX_ITEMS


def test_call_aws_returned_entry_requires_matching_command():
    command = "aws eks list-clusters"
    entry = {"response": {"error": None, "json": json.dumps({"clusters": []})}}
    delegate = _RealMcpTool(
        "call_aws",
        [{"text": json.dumps({"result": [entry]})}],
        structured_content=call_aws_structured([entry]),
    )
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": command})
    assert event.tool_result["status"] == "error"
    assert _body(event)["outcome"] == "malformed"


def test_call_aws_unknown_input_key_is_rejected_before_dispatch():
    delegate = _RealMcpTool("call_aws", [{"text": "unused"}], structured_content={"result": []})
    tool = _find(_load("aws-api-mcp-server", [delegate]), "call_aws")
    event = _invoke(tool, {"cli_command": "aws eks list-clusters", "profile": "synthetic"})
    assert event.tool_result["status"] == "error"
    assert delegate.calls == []
