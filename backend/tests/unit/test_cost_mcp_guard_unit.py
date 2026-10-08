"""Unit tests for the Billing MCP output guard (#465).

The guard exposes exactly three model-visible Billing MCP tools
(``cost-explorer`` forecast-only, ``compute-optimizer``, ``cost-optimization``),
constrains each tool's ``operation`` and input, projects each allowed result
into a bounded code-owned envelope, and replaces provider-authored errors with
typed sanitized outcomes. Historical ``getCostAndUsage*`` stays blocked with a
``get_cost_report`` redirect.

These tests attack ALLOWED fields with hostile synthetic values, capture real
Loguru output, prove the exact serialized byte cap, and observe the actual tool
list produced for the specialist factory.
"""

# Standard library
import asyncio
import json
from typing import Any, Sequence

# Third-party packages
import pytest
from botocore.exceptions import (
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    TokenRetrievalError,
)
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

# Local modules
import config.settings as settings
from agents.cost_mcp_guard import MAX_ENVELOPE_BYTES, CostReportGuardedProvider, guard_cost_mcp_client
from utils.logger import logger

pytestmark = pytest.mark.unit

# Synthetic hostile values; none may ever reach model context or logs.
ACCT = "123456789012"
ARN = f"arn:aws:ce:us-east-1:{ACCT}:recommendation/synthetic"
URL = "https://internal.example.invalid/x?token=abc"
REQ_ID = "req-synthetic-0001"


class _ResultTool(AgentTool):
    """Fake Billing MCP tool emitting the FastMCP/Strands wire shape."""

    def __init__(self, name: str, result: dict[str, Any]) -> None:
        super().__init__()
        self._name = name
        self._result = result
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
        yield ToolResultEvent(self._result)


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


def _success(data: dict[str, Any]) -> dict[str, Any]:
    body = {"status": "success", "data": data}
    return {
        "toolUseId": "t",
        "status": "success",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
    }


def _error(message: str, status: str = "error", aws_error_code: str = "AccessDeniedException") -> dict[str, Any]:
    body = {"status": status, "data": {"aws_error_code": aws_error_code}, "message": message}
    return {
        "toolUseId": "t",
        "status": "error" if status == "error" else "success",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": status == "error",
    }


def _load(tools: Sequence[AgentTool]) -> list[AgentTool]:
    return list(asyncio.run(CostReportGuardedProvider(_RecordingProvider(tools)).load_tools()))


def _one(name: str, result: dict[str, Any]) -> AgentTool:
    return _load([_ResultTool(name, result)])[0]


def _invoke(tool: AgentTool, inp: dict[str, Any]) -> dict[str, Any]:
    tool_use: ToolUse = {"toolUseId": "t", "name": tool.tool_name, "input": inp}
    events = asyncio.run(_collect(tool, tool_use))
    return events[-1].tool_result


async def _collect(tool: AgentTool, tool_use: ToolUse) -> list[ToolResultEvent]:
    return [e async for e in tool.stream(tool_use, {})]


def _env(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def _assert_no_sensitive(result: dict[str, Any]) -> None:
    blob = json.dumps(result)
    assert ("arn:aws" in blob) is False, "ARN leaked"
    assert (URL in blob) is False, "URL leaked"
    assert ("example.invalid" in blob) is False, "network coordinate leaked"
    assert (ACCT in blob) is False, "account id leaked"
    assert (REQ_ID in blob) is False, "request id leaked"


# --------------------------------------------------------------------------- #
# Tool allowlist                                                              #
# --------------------------------------------------------------------------- #
def test_only_three_tools_are_model_visible_including_future_tool():
    names = [
        "cost-explorer",
        "compute-optimizer",
        "cost-optimization",
        "compute-optimizer-automation",
        "budget",
        "cost-anomaly",
        "aws-pricing",
        "free-tier-usage",
        "invoicing",
        "ri-performance",
        "sp-performance",
        "storage-lens",
        "session-sql",
        "cost-comparison",
        "rec-details",
        "bcm-pricing-calc",
        "billing-conductor",
        "a-future-tool-added-by-a-later-package-version",
    ]
    loaded = {t.tool_name for t in _load([_ResultTool(n, _success({})) for n in names])}
    assert loaded == {"cost-explorer", "compute-optimizer", "cost-optimization"}, loaded


def test_tool_descriptions_are_code_owned():
    for name in ("cost-explorer", "compute-optimizer", "cost-optimization"):
        tool = _one(name, _success({}))
        assert tool.tool_spec["description"] != "orig"
        assert "bounded" in tool.tool_spec["description"].lower()


# --------------------------------------------------------------------------- #
# Historical cost ops stay blocked                                            #
# --------------------------------------------------------------------------- #
def test_historical_cost_operations_blocked_without_calling_mcp():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "getCostAndUsage"})
    assert delegate.calls == []
    assert result["status"] == "error"
    assert "get_cost_report" in result["content"][0]["text"]


def test_resource_level_historical_also_blocked():
    tool = _one("cost-explorer", _success({}))
    result = _invoke(tool, {"operation": "getCostAndUsageWithResources"})
    assert result["status"] == "error"
    assert "get_cost_report" in result["content"][0]["text"]


def test_historical_block_routes_to_deterministic_report_path():
    """A historical cost query is pushed back to the owned deterministic tools.

    The guard refuses historical Cost Explorer MCP operations and names
    ``get_cost_report``; the deterministic bundle still registers both owned
    tools (``get_cost_report`` and ``reuse_cost_report``). Together these keep
    totals, rankings, and percentages on the validated snapshot path.
    """
    # Guard side: block + redirect, no MCP call.
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    for op in ("getCostAndUsage", "getCostAndUsageWithResources"):
        result = _invoke(tool, {"operation": op})
        assert delegate.calls == []
        assert "get_cost_report" in result["content"][0]["text"]

    # Owned deterministic path side: both tools remain registered, unchanged.
    # Local modules
    from agents.cost_report import create_cost_report_tool_bundle  # noqa: PLC0415

    tools, _finalizer = create_cost_report_tool_bundle()
    tool_names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}
    assert any("get_cost_report" in n for n in tool_names)
    assert any("reuse_cost_report" in n for n in tool_names)


# --------------------------------------------------------------------------- #
# Operation allowlist                                                         #
# --------------------------------------------------------------------------- #
def test_unknown_or_non_forecast_operation_is_denied():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "getDimensionValues"})
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_cost_optimization_only_three_operations():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "some_other_op"})
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


# --------------------------------------------------------------------------- #
# Input validation                                                            #
# --------------------------------------------------------------------------- #
def test_account_ids_input_rejected_before_dispatch():
    delegate = _ResultTool("compute-optimizer", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "get_ec2_instance_recommendations", "account_ids": json.dumps([ACCT])})
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_group_by_account_id_rejected():
    delegate = _ResultTool("cost-optimization", _success({"summaries": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "list_recommendation_summaries", "group_by": "AccountId"})
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_group_by_region_allowed():
    delegate = _ResultTool("cost-optimization", _success({"summaries": [], "group_by": "Region"}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "list_recommendation_summaries", "group_by": "Region"})
    assert delegate.calls, "delegate should have been called"
    assert _env(result)["status"] in {"empty", "ok"}


def test_unbounded_filter_key_rejected():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "list_recommendations", "filters": "{}"})
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_out_of_range_max_results_rejected():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "list_recommendations", "max_results": True})
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_max_results_is_clamped_before_dispatch():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    _invoke(tool, {"operation": "list_recommendations", "max_results": 100000})
    # The guard over-fetches by one row to detect truncation, so an out-of-range
    # request is clamped to the cap and then forwarded as cap + 1 (<= 51).
    assert delegate.calls[0]["input"]["max_results"] <= 51


def test_forecast_malformed_dates_rejected():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(
        tool,
        {
            "operation": "getCostForecast",
            "metric": "UNBLENDED_COST",
            "granularity": "MONTHLY",
            "start_date": "not-a-date",
            "end_date": "2026-02-01",
        },
    )
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_forecast_canonical_input_forwarded():
    data = {"Total": {"Amount": "500.00", "Unit": "USD"}, "ForecastResultsByTime": []}
    delegate = _ResultTool("cost-explorer", _success(data))
    tool = _load([delegate])[0]
    _invoke(
        tool,
        {
            "operation": "getCostForecast",
            "metric": "UNBLENDED_COST",
            "granularity": "MONTHLY",
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "account_ids": None,
        },
    )
    # account_ids present (even None) is a forbidden key → rejected, so delegate
    # is NOT called. Validate separately with a clean input.
    assert delegate.calls == []


def test_forecast_clean_input_projects_estimated_total():
    data = {
        "Total": {"Amount": "500.00", "Unit": "USD"},
        "ForecastResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-02-01"},
                "MeanValue": "500.0",
                "PredictionIntervalLowerBound": "450.0",
                "PredictionIntervalUpperBound": "550.0",
            }
        ],
    }
    tool = _one("cost-explorer", _success(data))
    env = _env(
        _invoke(
            tool,
            {
                "operation": "getCostForecast",
                "metric": "UNBLENDED_COST",
                "granularity": "MONTHLY",
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
            },
        )
    )
    assert env["estimated"] is True
    assert env["total_amount"] == 500.0
    assert env["unit"] == "USD"
    assert env["periods"][0]["mean"] == 500.0
    assert env["status"] == "ok"


# --------------------------------------------------------------------------- #
# Projection drops sensitive fields on allowed operations                     #
# --------------------------------------------------------------------------- #
def test_compute_optimizer_drops_arn_account_and_derives_resource_id():
    data = {
        "recommendations": [
            {
                "instance_arn": ARN,
                "account_id": ACCT,
                "current_instance": {"instance_type": "m5.large", "finding": "OVERPROVISIONED"},
                "recommendation_options": [
                    {
                        "instance_type": "m5.xlarge",
                        "performance_risk": 1,
                        "savings_opportunity": {
                            "savings_percentage": 20.0,
                            "estimated_monthly_savings": {"currency": "USD", "value": 12.5},
                        },
                    }
                ],
                "last_refresh_timestamp": "2026-01-01T00:00:00",
                "tags": {"Owner": "secret-team", "Endpoint": URL},
            }
        ],
        "next_token": None,
    }
    tool = _one("compute-optimizer", _success(data))
    result = _invoke(tool, {"operation": "get_ec2_instance_recommendations"})
    _assert_no_sensitive(result)
    env = _env(result)
    rec = env["recommendations"][0]
    assert rec["finding"] == "OVERPROVISIONED"
    assert rec["current_type"] == "m5.large"
    assert rec["recommended_options"][0]["type"] == "m5.xlarge"
    assert rec["recommended_options"][0]["savings"]["estimated_monthly_savings"] == 12.5
    assert "tags" not in rec and "account_id" not in rec and "instance_arn" not in rec
    assert env["estimated"] is True


def test_cost_optimization_recommendation_drops_arn_tags_account():
    data = {
        "recommendations": [
            {
                "recommendation_id": "rec-abc123",
                "account_id": ACCT,
                "resource_arn": ARN,
                "region": "us-east-1",
                "action_type": "Rightsize",
                "current_resource_type": "Ec2Instance",
                "recommended_resource_type": "Ec2Instance",
                "estimated_monthly_savings": 42.0,
                "estimated_savings_percentage": 30.0,
                "currency_code": "USD",
                "implementation_effort": "Low",
                "restart_needed": False,
                "rollback_possible": True,
                "last_refresh_timestamp": "2026-01-01T00:00:00",
                "lookback_period_in_days": 14,
                "tags": {"team": "secret"},
                "recommended_resource_details": {"blob": URL},
            }
        ]
    }
    tool = _one("cost-optimization", _success(data))
    result = _invoke(tool, {"operation": "list_recommendations"})
    _assert_no_sensitive(result)
    rec = _env(result)["recommendations"][0]
    assert rec["recommendation_id"] == "rec-abc123"
    assert rec["estimated_monthly_savings"] == 42.0
    assert rec["estimated_savings_percentage"] == 30.0
    assert rec["restart_needed"] is False and rec["rollback_possible"] is True
    assert "tags" not in rec and "recommended_resource_details" not in rec
    assert "account_id" not in rec and "resource_arn" not in rec


def test_hostile_values_in_allowed_fields_are_dropped():
    data = {
        "recommendations": [
            {
                "recommendation_id": "rec\x07-ctl",  # control char
                "region": ACCT,  # account-id shaped
                "action_type": "arn:aws:x",  # ARN prefix
                "current_resource_type": URL,  # URL scheme
                "estimated_monthly_savings": float("inf"),  # non-finite
                "estimated_savings_percentage": 1e308 * 10,  # overflow -> inf
                "currency_code": "10.0.0.1",  # network coordinate
            }
        ]
    }
    tool = _one("cost-optimization", _success(data))
    result = _invoke(tool, {"operation": "list_recommendations"})
    _assert_no_sensitive(result)
    env = _env(result)
    # The row had no usable field → dropped → malformed.
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "malformed_response"


# --------------------------------------------------------------------------- #
# State distinctness                                                          #
# --------------------------------------------------------------------------- #
def test_states_are_distinct():
    empty = _env(
        _invoke(_one("cost-optimization", _success({"recommendations": []})), {"operation": "list_recommendations"})
    )
    assert empty["status"] == "empty"

    malformed = _env(
        _invoke(_one("cost-optimization", _success({"recommendations": "x"})), {"operation": "list_recommendations"})
    )
    assert malformed["status"] == "incomplete"
    assert malformed["error"]["code"] == "malformed_response"

    paginated = _env(
        _invoke(
            _one(
                "cost-optimization",
                _success({"recommendations": [{"recommendation_id": "rec-1"}], "next_token": "opaque-token"}),
            ),
            {"operation": "list_recommendations"},
        )
    )
    assert paginated["status"] == "incomplete"
    assert paginated["paginated"] is True
    assert ("opaque-token" in json.dumps(paginated)) is False  # no raw token


def test_denied_vs_enrollment_errors_distinct():
    denied = _env(
        _invoke(
            _one("cost-optimization", _error("User is not authorized to perform")),
            {"operation": "list_recommendations"},
        )
    )
    assert denied["status"] == "denied"
    assert denied["error"]["code"] == "access_denied"

    enroll = _env(
        _invoke(
            _one(
                "compute-optimizer",
                _error("Compute Optimizer is not active", aws_error_code="ComputeOptimizerNotActive"),
            ),
            {"operation": "get_ec2_instance_recommendations"},
        )
    )
    assert enroll["status"] == "incomplete"
    assert enroll["error"]["code"] == "not_enrolled"


def test_provider_error_text_never_reaches_model():
    raw = f"Access denied. request_id {REQ_ID}, account {ACCT}, see {ARN}"
    result = _invoke(_one("cost-optimization", _error(raw)), {"operation": "list_recommendations"})
    _assert_no_sensitive(result)
    assert ("AccessDeniedException" in json.dumps(result)) is False
    assert _env(result)["status"] == "denied"


# --------------------------------------------------------------------------- #
# Envelope byte cap                                                           #
# --------------------------------------------------------------------------- #
def test_final_serialized_envelope_is_bounded():
    recs = [
        {
            "recommendation_id": f"rec-{i}",
            "region": "us-east-1",
            "action_type": "Rightsize",
            "estimated_monthly_savings": 1.0,
            "currency_code": "USD",
        }
        for i in range(400)
    ]
    result = _invoke(
        _one("cost-optimization", _success({"recommendations": recs})), {"operation": "list_recommendations"}
    )
    serialized = result["content"][0]["text"]
    assert len(serialized.encode("utf-8")) <= MAX_ENVELOPE_BYTES
    env = _env(result)
    # Over-fetching by one and receiving far more than the requested bound makes
    # this an incomplete, paginated result; the status is never a clean "ok".
    assert env["status"] in {"truncated", "incomplete"}


def test_item_cap_marks_truncated():
    recs = [{"recommendation_id": f"rec-{i}", "region": "us-east-1"} for i in range(200)]
    env = _env(
        _invoke(_one("cost-optimization", _success({"recommendations": recs})), {"operation": "list_recommendations"})
    )
    # 200 rows for a 25-row request: more rows than requested AND past the hard
    # item cap. Residual pagination outranks the local cap for the status, but
    # the truncation marker is still set independently.
    assert env["status"] == "incomplete"
    assert env["paginated"] is True
    assert env["truncated"] is True


# --------------------------------------------------------------------------- #
# Logging is sanitized (real Loguru sink)                                     #
# --------------------------------------------------------------------------- #
def test_log_output_is_sanitized():
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        raw = f"Access denied. request_id {REQ_ID}, account {ACCT}, see {ARN}"
        _invoke(_one("cost-optimization", _error(raw)), {"operation": "list_recommendations"})
    finally:
        logger.remove(sink_id)
    blob = "\n".join(records)
    assert REQ_ID not in blob
    assert ACCT not in blob
    assert "arn:aws" not in blob
    assert "cost_guard" in blob  # a code-owned line was emitted


# --------------------------------------------------------------------------- #
# Provider lifecycle                                                          #
# --------------------------------------------------------------------------- #
def test_provider_lifecycle_and_non_billing_clients_are_preserved():
    delegate = _RecordingProvider([])
    guarded = guard_cost_mcp_client("billing-cost-management-mcp-server", delegate)
    assert isinstance(guarded, CostReportGuardedProvider)
    guarded.add_consumer("agent-1")
    guarded.remove_consumer("agent-1")
    assert delegate.added == ["agent-1"]
    assert delegate.removed == ["agent-1"]
    assert guard_cost_mcp_client("eks-mcp-server", delegate) is delegate


def test_specialist_tool_list_only_exposes_guarded_tools():
    """Observe the actual tool list a provider yields to the specialist factory."""
    names = ["cost-explorer", "compute-optimizer", "cost-optimization", "budget", "invoicing"]
    provider = CostReportGuardedProvider(_RecordingProvider([_ResultTool(n, _success({})) for n in names]))
    tools = asyncio.run(provider.load_tools())
    # Local modules
    from agents.cost_mcp_guard import _CostGuardedTool  # noqa: PLC0415

    assert all(isinstance(t, _CostGuardedTool) for t in tools)
    assert {t.tool_name for t in tools} == {"cost-explorer", "compute-optimizer", "cost-optimization"}


# --------------------------------------------------------------------------- #
# Reviewed Compute Optimizer operation surface (EC2 + Auto Scaling only)      #
# --------------------------------------------------------------------------- #
def test_ebs_and_lambda_rightsizing_are_not_reviewed_ops():
    """EBS and Lambda rightsizing need dependent IAM grants the chat runtime
    never had, so they are not model-visible: denied, delegate never reached."""
    for op in ("get_ebs_volume_recommendations", "get_lambda_function_recommendations"):
        delegate = _ResultTool("compute-optimizer", _success({"recommendations": []}))
        tool = _load([delegate])[0]
        result = _invoke(tool, {"operation": op})
        assert delegate.calls == [], f"{op} must not reach the delegate"
        assert _env(result)["status"] == "denied", f"{op} must be denied"


def test_ec2_and_asg_rightsizing_remain_allowed():
    for op in ("get_ec2_instance_recommendations", "get_auto_scaling_group_recommendations"):
        delegate = _ResultTool("compute-optimizer", _success({"recommendations": []}))
        tool = _load([delegate])[0]
        result = _invoke(tool, {"operation": op})
        assert delegate.calls, f"{op} should reach the delegate"
        assert _env(result)["status"] in {"empty", "ok"}


# --------------------------------------------------------------------------- #
# Compute Optimizer region is bound to the deployment region                  #
# --------------------------------------------------------------------------- #
def test_compute_optimizer_region_must_equal_deployment_region():
    other = "eu-west-1" if settings.AWS_REGION != "eu-west-1" else "us-east-2"
    delegate = _ResultTool("compute-optimizer", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "get_ec2_instance_recommendations", "region": other})
    assert delegate.calls == [], "a non-deployment region must not reach the delegate"
    env = _env(result)
    assert env["status"] == "denied"
    assert env["error"]["code"] == "denied_input"


def test_compute_optimizer_absent_region_defaults_to_deployment_region():
    delegate = _ResultTool("compute-optimizer", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    _invoke(tool, {"operation": "get_ec2_instance_recommendations"})
    assert delegate.calls, "absent region should still forward (bound to deployment region)"
    assert delegate.calls[0]["input"].get("region") == settings.AWS_REGION


def test_compute_optimizer_matching_region_forwards_canonical_region():
    delegate = _ResultTool("compute-optimizer", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    _invoke(tool, {"operation": "get_ec2_instance_recommendations", "region": settings.AWS_REGION})
    assert delegate.calls, "matching region should be forwarded"
    assert delegate.calls[0]["input"].get("region") == settings.AWS_REGION


# --------------------------------------------------------------------------- #
# Aggregate byte-trim keeps prior status/flags independent                    #
# --------------------------------------------------------------------------- #
def test_residual_pagination_stays_incomplete_not_truncated_in_envelope():
    """A paginated (residual continuation) result large enough to also hit the
    item cap stays incomplete+paginated and additionally carries truncated; the
    local cap must not clobber the status to 'truncated' nor drop paginated."""
    recs = [
        {"recommendation_id": f"rec-{i}", "region": "us-east-1", "action_type": "Rightsize", "currency_code": "USD"}
        for i in range(400)
    ]
    env = _env(
        _invoke(
            _one("cost-optimization", _success({"recommendations": recs, "next_token": "more"})),
            {"operation": "list_recommendations"},
        )
    )
    assert env["status"] == "incomplete", env.get("status")
    assert env["paginated"] is True
    assert env["truncated"] is True
    assert env["partial"] is True


# --------------------------------------------------------------------------- #
# Error classification: structured code is authoritative over free text       #
# --------------------------------------------------------------------------- #
def test_denial_mentioning_enrollment_classifies_as_denied():
    """A denial whose message also mentions enrollment is classified from the
    authoritative provider code (AccessDeniedException), not incidental wording."""
    result = _invoke(
        _one(
            "compute-optimizer",
            _error(
                "Access denied: you are not enrolled and not authorized for enrollment",
                aws_error_code="AccessDeniedException",
            ),
        ),
        {"operation": "get_ec2_instance_recommendations"},
    )
    env = _env(result)
    assert env["status"] == "denied"
    assert env["error"]["code"] == "access_denied"
    _assert_no_sensitive(result)


def test_enrollment_without_denial_classifies_as_not_enrolled():
    result = _invoke(
        _one(
            "compute-optimizer",
            _error("opt-in required: ComputeOptimizerNotActive", aws_error_code="OptInRequiredException"),
        ),
        {"operation": "get_ec2_instance_recommendations"},
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "not_enrolled"


# --------------------------------------------------------------------------- #
# Log line carries a bounded per-invocation correlation token                 #
# --------------------------------------------------------------------------- #
def test_log_line_includes_correlation_token():
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        _invoke(_one("cost-optimization", _success({"recommendations": []})), {"operation": "list_recommendations"})
    finally:
        logger.remove(sink_id)
    blob = "\n".join(records)
    # Standard library
    import re  # noqa: PLC0415

    assert re.search(r"cid=[0-9a-f]{12}\b", blob), f"no correlation token in: {blob}"
    assert "cost_guard" in blob
    assert ACCT not in blob and "arn:aws" not in blob


def test_correlation_tokens_differ_per_invocation():
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        tool = _one("cost-optimization", _success({"recommendations": []}))
        _invoke(tool, {"operation": "list_recommendations"})
        _invoke(tool, {"operation": "list_recommendations"})
    finally:
        logger.remove(sink_id)
    # Standard library
    import re  # noqa: PLC0415

    cids = re.findall(r"cid=([0-9a-f]{12})\b", "\n".join(records))
    assert len(cids) >= 2
    assert len(set(cids)) == len(cids), "each invocation must get a distinct correlation token"


# --------------------------------------------------------------------------- #
# cost-forecast-only surface with code-owned scope parameters                 #
# --------------------------------------------------------------------------- #
def _forecast_input(**extra):
    base = {
        "operation": "getCostForecast",
        "metric": "UNBLENDED_COST",
        "granularity": "MONTHLY",
        "start_date": "2026-02-01",
        "end_date": "2026-03-01",
    }
    base.update(extra)
    return base


def test_usage_forecast_is_removed_from_the_surface():
    delegate = _ResultTool("cost-explorer", _success({"Total": {"Amount": "1", "Unit": "Hrs"}}))
    tool = _load([delegate])[0]
    result = _invoke(
        tool,
        {
            "operation": "getUsageForecast",
            "metric": "USAGE_QUANTITY",
            "granularity": "MONTHLY",
            "start_date": "2026-02-01",
            "end_date": "2026-03-01",
        },
    )
    assert delegate.calls == [], "getUsageForecast must not reach the delegate"
    assert _env(result)["status"] == "denied"


def test_cost_forecast_scoped_by_services_builds_canonical_filter():
    data = {"Total": {"Amount": "500.00", "Unit": "USD"}, "ForecastResultsByTime": []}
    delegate = _ResultTool("cost-explorer", _success(data))
    tool = _load([delegate])[0]
    env = _env(_invoke(tool, _forecast_input(services=["Amazon GameLift"])))
    assert delegate.calls, "scoped forecast should reach the delegate"
    forwarded = delegate.calls[0]["input"]
    assert "filter" in forwarded, "canonical filter must be forwarded as the delegate's filter"
    # The private scope key is echoed to the model but must never be forwarded.
    assert "__scope__" not in forwarded, "the private __scope__ key must be stripped before dispatch"
    parsed = json.loads(forwarded["filter"])
    assert parsed == {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon GameLift"]}}
    assert env["scope"] == {"SERVICE": ["Amazon GameLift"]}


def test_cost_forecast_scoped_by_services_and_regions_builds_and_filter():
    data = {"Total": {"Amount": "500.00", "Unit": "USD"}, "ForecastResultsByTime": []}
    delegate = _ResultTool("cost-explorer", _success(data))
    tool = _load([delegate])[0]
    _invoke(tool, _forecast_input(services=["Amazon GameLift"], regions=["us-west-2"]))
    assert "__scope__" not in delegate.calls[0]["input"], "the private __scope__ key must be stripped before dispatch"
    forwarded = json.loads(delegate.calls[0]["input"]["filter"])
    assert "And" in forwarded
    keys = {next(iter(part["Dimensions"]["Key"] for _ in [0])) for part in forwarded["And"]}
    assert keys == {"SERVICE", "REGION"}


def test_cost_forecast_scope_canonicalization_is_order_independent():
    data = {"Total": {"Amount": "500.00", "Unit": "USD"}, "ForecastResultsByTime": []}
    d1 = _ResultTool("cost-explorer", _success(data))
    d2 = _ResultTool("cost-explorer", _success(data))
    t1, t2 = _load([d1])[0], _load([d2])[0]
    _invoke(t1, _forecast_input(services=["AWS Support (Business)", "Amazon GameLift"]))
    _invoke(t2, _forecast_input(services=["Amazon GameLift", "AWS Support (Business)"]))
    assert d1.calls[0]["input"]["filter"] == d2.calls[0]["input"]["filter"], "canonical filter must be order-stable"


def test_cost_forecast_unscoped_echoes_account_scope():
    data = {"Total": {"Amount": "500.00", "Unit": "USD"}, "ForecastResultsByTime": []}
    delegate = _ResultTool("cost-explorer", _success(data))
    tool = _load([delegate])[0]
    env = _env(_invoke(tool, _forecast_input()))
    assert env["scope"] == "account"
    # Even unscoped, the delegate input must never carry the private scope key.
    assert delegate.calls, "unscoped forecast should still reach the delegate"
    assert "__scope__" not in delegate.calls[0]["input"], "the private __scope__ key must be stripped before dispatch"


def test_raw_filter_from_model_is_still_rejected():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(
        tool,
        _forecast_input(filter=json.dumps({"Dimensions": {"Key": "SERVICE", "Values": ["Amazon GameLift"]}})),
    )
    assert delegate.calls == [], "raw filter must never reach the delegate"
    assert _env(result)["status"] == "denied"


@pytest.mark.parametrize(
    "bad_services",
    [
        [ARN],
        [URL],
        [ACCT],
        ["10.0.0.1"],
        ["ctl\x07char"],
        ["x" * 300],
        ["Amazon GameLift", "Amazon GameLift"],  # duplicate
        [],  # empty list
        ["ok"] * 11,  # too many
        "not-a-list",
        [123],
        ["Amazon GameLift" + chr(10)],  # trailing newline (the $-anchor gap)
        ["Amazon GameLift" + chr(13) + chr(10)],  # trailing CRLF
    ],
)
def test_forecast_scope_rejects_hostile_services(bad_services):
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, _forecast_input(services=bad_services))
    assert delegate.calls == [], f"hostile services {bad_services!r} must not reach the delegate"
    assert _env(result)["status"] == "denied"


@pytest.mark.parametrize(
    "bad_regions",
    [
        [ARN],
        [ACCT],
        ["US_WEST_2"],
        ["x" * 300],
        ["ok"] * 11,
        "nope",
        ["us-west-2" + chr(10)],  # trailing newline (the $-anchor gap)
        ["us-west-2" + chr(13) + chr(10)],  # trailing CRLF
    ],
)
def test_forecast_scope_rejects_hostile_regions(bad_regions):
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, _forecast_input(regions=bad_regions))
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_scope_grammar_fullmatch_rejects_trailing_newline(monkeypatch):
    # The scope grammars are anchored with ^...$ (kept so they remain valid
    # ECMA-262 input-schema patterns), but Python's $ also matches just before a
    # final newline, so .match would accept a trailing-newline value.
    # _valid_scope_values uses fullmatch so the grammar itself rejects it, not
    # only the downstream control-character check. To observe the grammar in
    # isolation, the sensitive-shape check (which also rejects a newline as a
    # control character) is neutralized for this assertion.
    # Local modules
    import agents.cost_mcp_guard as guard  # noqa: PLC0415

    monkeypatch.setattr(guard, "_value_is_sensitive_safe", lambda value: True)
    newline = chr(10)
    assert guard._valid_scope_values(["Amazon GameLift" + newline], "SERVICE") is None
    assert guard._valid_scope_values(["us-west-2" + newline], "REGION") is None
    # A clean value still validates with the control-char check neutralized.
    assert guard._valid_scope_values(["Amazon GameLift"], "SERVICE") == ["Amazon GameLift"]
    assert guard._valid_scope_values(["us-west-2"], "REGION") == ["us-west-2"]


def test_scope_values_enforce_advertised_maxlength():
    # The advertised item maxLength is 128. The region grammar is otherwise
    # unbounded, so _valid_scope_values must reject a 129-character region even
    # though the grammar matches it, and accept a 128-character one. The service
    # grammar caps itself at 127 characters, so the length bound is exercised on
    # the longest grammar-valid service name.
    # Local modules
    import agents.cost_mcp_guard as guard  # noqa: PLC0415

    region_128 = "aa-" + "x" * 125  # 128 chars, grammar-valid
    region_129 = "aa-" + "x" * 126  # 129 chars, grammar-valid but over maxLength
    assert len(region_128) == 128 and len(region_129) == 129
    assert guard._RE_REGION_CODE.fullmatch(region_129) is not None, "fixture must be grammar-valid at 129"
    assert guard._valid_scope_values([region_128], "REGION") == [region_128]
    assert guard._valid_scope_values([region_129], "REGION") is None

    service_127 = "A" * 127  # longest grammar-valid SERVICE name
    assert len(service_127) == 127
    assert guard._RE_SERVICE_NAME.fullmatch(service_127) is not None
    assert guard._valid_scope_values([service_127], "SERVICE") == [service_127]


def test_forecast_scope_rejects_over_maxlength_region_through_tool():
    # The same bound must hold through the full guarded tool: a 129-character
    # grammar-valid region is a denied_input rejection that never reaches the
    # delegate (otherwise it would be forwarded in the filter and echoed).
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, _forecast_input(regions=["aa-" + "x" * 126]))
    assert delegate.calls == [], "an over-maxLength region must not reach the delegate"
    assert _env(result)["status"] == "denied"


# --------------------------------------------------------------------------- #
# unknown input keys are rejected (not silently dropped)                      #
# --------------------------------------------------------------------------- #
def test_unknown_input_key_rejected_as_denied_input():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "list_recommendations", "group_by": "Region"})
    # group_by is not valid for list_recommendations -> unknown-key rejection.
    assert delegate.calls == []
    env = _env(result)
    assert env["status"] == "denied"
    assert env["error"]["code"] == "denied_input"


def test_unknown_forecast_key_rejected():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, _forecast_input(metrics="UNBLENDED_COST"))
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


# --------------------------------------------------------------------------- #
# code-owned input schema per guarded tool                                    #
# --------------------------------------------------------------------------- #
def test_guarded_input_schema_is_code_owned_canonical_keys():
    ce = _one("cost-explorer", _success({}))
    props = set(ce.tool_spec["inputSchema"]["json"]["properties"])
    assert props == {
        "operation",
        "metric",
        "granularity",
        "start_date",
        "end_date",
        "prediction_interval_level",
        "services",
        "regions",
    }
    # Upstream-only keys the guard rejects must NOT be advertised.
    assert "filter" not in props and "billing_view_arn" not in props and "next_token" not in props
    # operation enum is the reviewed value(s) only.
    op_enum = set(ce.tool_spec["inputSchema"]["json"]["properties"]["operation"]["enum"])
    assert op_enum == {"getCostForecast"}


def test_guarded_compute_optimizer_schema_is_canonical():
    co = _one("compute-optimizer", _success({"recommendations": []}))
    props = set(co.tool_spec["inputSchema"]["json"]["properties"])
    assert props == {"operation", "region", "max_results"}
    op_enum = set(co.tool_spec["inputSchema"]["json"]["properties"]["operation"]["enum"])
    assert op_enum == {"get_ec2_instance_recommendations", "get_auto_scaling_group_recommendations"}


def test_guarded_cost_optimization_schema_is_canonical():
    coh = _one("cost-optimization", _success({"recommendations": []}))
    props = set(coh.tool_spec["inputSchema"]["json"]["properties"])
    assert props == {"operation", "group_by", "max_results", "resource_id"}
    op_enum = set(coh.tool_spec["inputSchema"]["json"]["properties"]["operation"]["enum"])
    assert op_enum == {"list_recommendation_summaries", "list_recommendations", "get_recommendation"}


# --------------------------------------------------------------------------- #
# top-level error_type classification + not_found                             #
# --------------------------------------------------------------------------- #
def _toplevel_error(error_type: str, message: str) -> dict[str, Any]:
    """Shape produced by the Billing MCP ``handle_aws_error`` (Cost Explorer):
    error_type at the TOP level, NO ``data`` key."""
    body = {
        "status": "error",
        "service": "Cost Explorer",
        "operation": "GetCostForecast",
        "error_type": error_type,
        "message": message,
        "request_id": REQ_ID,
        "full_error": message,
    }
    return {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }


def test_toplevel_throttling_error_type_maps_to_throttled():
    result = _invoke(
        _one("cost-explorer", _toplevel_error("LimitExceededException", "Rate exceeded")), _forecast_input()
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "throttled"
    _assert_no_sensitive(result)


def test_toplevel_throttlingexception_maps_to_throttled():
    result = _invoke(_one("cost-explorer", _toplevel_error("ThrottlingException", "slow down")), _forecast_input())
    assert _env(result)["error"]["code"] == "throttled"


def test_toplevel_validation_error_type_maps_to_invalid_request():
    result = _invoke(_one("cost-explorer", _toplevel_error("ValidationException", "bad input")), _forecast_input())
    assert _env(result)["error"]["code"] == "invalid_request"


def test_toplevel_access_denied_from_handle_aws_error_shape():
    result = _invoke(
        _one("cost-explorer", _toplevel_error("AccessDeniedException", f"not authorized {ARN}")),
        _forecast_input(),
    )
    env = _env(result)
    assert env["status"] == "denied"
    assert env["error"]["code"] == "access_denied"
    _assert_no_sensitive(result)


def _coh_warning_notfound() -> dict[str, Any]:
    """Shape produced by the installed CoH ``get_recommendation`` not-found:
    status=warning, data.error_code=ResourceNotFoundException."""
    body = {
        "status": "warning",
        "data": {"error_code": "ResourceNotFoundException", "resource_id": "rec-9999", "resource_type": "Ec2Instance"},
        "message": "No recommendation found.",
    }
    return {
        "toolUseId": "t",
        "status": "success",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
    }


def test_resource_not_found_maps_to_not_found_code():
    result = _invoke(
        _one("cost-optimization", _coh_warning_notfound()),
        {"operation": "get_recommendation", "resource_id": "rec-9999"},
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "not_found"
    _assert_no_sensitive(result)


def test_echoed_model_input_not_scanned_for_classification():
    """A resource_id that textually contains a denial marker must not flip a
    non-error (or differently-classed) outcome to access_denied."""
    body = {
        "status": "error",
        "data": {"error_code": "ValidationException"},
        "message": "Invalid value: not authorized access denied unauthorized forbidden",
    }
    result = {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }
    env = _env(_invoke(_one("cost-optimization", result), {"operation": "list_recommendations"}))
    # Authoritative structured code is ValidationException -> invalid_request,
    # not access_denied from the message free-text.
    assert env["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# operation echo bounded to reviewed/blocked names                            #
# --------------------------------------------------------------------------- #
def test_unreviewed_operation_echo_is_the_other_token_and_bounded():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    hostile_op = "x" * 100_000
    result = _invoke(tool, {"operation": hostile_op})
    env = _env(result)
    assert env["operation"] == "other", "unreviewed op name must be the code-owned token"
    assert len(result["content"][0]["text"].encode("utf-8")) <= MAX_ENVELOPE_BYTES


def test_blocked_historical_operation_name_may_be_echoed():
    tool = _one("cost-explorer", _success({}))
    # getCostAndUsage is a known blocked op; its redirect names get_cost_report.
    result = _invoke(tool, {"operation": "getCostAndUsage"})
    assert "get_cost_report" in result["content"][0]["text"]


# --------------------------------------------------------------------------- #
# per-granularity forecast horizon                                            #
# --------------------------------------------------------------------------- #
def test_monthly_forecast_allows_up_to_eighteen_months():
    data = {"Total": {"Amount": "1", "Unit": "USD"}, "ForecastResultsByTime": []}
    delegate = _ResultTool("cost-explorer", _success(data))
    tool = _load([delegate])[0]
    # ~15 months ahead: allowed for MONTHLY (base's 400-day cap wrongly rejected).
    result = _invoke(
        tool,
        _forecast_input(granularity="MONTHLY", start_date="2026-02-01", end_date="2027-05-01"),
    )
    assert delegate.calls, "a 15-month MONTHLY forecast must be allowed"
    assert _env(result)["status"] in {"ok", "empty"}


def test_daily_forecast_rejects_beyond_three_months():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    # ~5 months of DAILY is beyond the API's 3-month daily horizon.
    result = _invoke(
        tool,
        _forecast_input(granularity="DAILY", start_date="2026-02-01", end_date="2026-07-15"),
    )
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


# --------------------------------------------------------------------------- #
# historical redirect is case-insensitive                                     #
# --------------------------------------------------------------------------- #
def test_historical_redirect_is_case_insensitive():
    tool = _one("cost-explorer", _success({}))
    for op in ("getcostandusage", "GETCOSTANDUSAGE", "GetCostAndUsageWithResources"):
        result = _invoke(tool, {"operation": op})
        assert "get_cost_report" in result["content"][0]["text"], op


# --------------------------------------------------------------------------- #
# get_recommendation takes recommendation_id in resource_id                   #
# --------------------------------------------------------------------------- #
def test_get_recommendation_forwards_recommendation_id_only():
    delegate = _ResultTool("cost-optimization", _success({"recommendation_id": "rec-0001", "region": "us-west-2"}))
    tool = _load([delegate])[0]
    _invoke(tool, {"operation": "get_recommendation", "resource_id": "rec-0001"})
    assert delegate.calls, "valid get_recommendation should reach the delegate"
    fwd = delegate.calls[0]["input"]
    assert fwd["resource_id"] == "rec-0001"
    # resource_type is a FIXED code-owned placeholder (the upstream tool requires
    # it for a presence check only); it is never model-derived.
    assert fwd.get("resource_type") == "recommendation"


def test_get_recommendation_rejects_hostile_resource_id():
    for bad in (ARN, URL, ACCT, "ctl\x07", "x" * 300):
        delegate = _ResultTool("cost-optimization", _success({}))
        tool = _load([delegate])[0]
        result = _invoke(tool, {"operation": "get_recommendation", "resource_id": bad})
        assert delegate.calls == [], f"hostile resource_id {bad!r} must not reach the delegate"
        assert _env(result)["status"] == "denied"


# --------------------------------------------------------------------------- #
# Hub list truncation is forwarded as bounded+1 and flagged                   #
# --------------------------------------------------------------------------- #
def test_hub_list_requests_bounded_plus_one():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    _invoke(tool, {"operation": "list_recommendations", "max_results": 25})
    assert delegate.calls[0]["input"]["max_results"] == 26, "guard must over-fetch by one to detect truncation"


def test_hub_list_truncation_marks_incomplete_paginated():
    # Installed helper returns exactly max_results rows and NO token; with
    # bounded+1 forwarded, 26 rows coming back means more were available.
    rows = [{"recommendation_id": f"rec-{i:04d}", "region": "us-west-2"} for i in range(26)]
    tool = _one("cost-optimization", _success({"recommendations": rows}))
    env = _env(_invoke(tool, {"operation": "list_recommendations", "max_results": 25}))
    assert env["status"] == "incomplete"
    assert env["paginated"] is True
    assert env["partial"] is True
    assert len(env["recommendations"]) == 25


def test_hub_list_within_bound_is_ok():
    rows = [{"recommendation_id": f"rec-{i:04d}", "region": "us-west-2"} for i in range(25)]
    tool = _one("cost-optimization", _success({"recommendations": rows}))
    env = _env(_invoke(tool, {"operation": "list_recommendations", "max_results": 25}))
    assert env["status"] == "ok"
    assert "paginated" not in env


# --------------------------------------------------------------------------- #
# byte cap over 20k, withheld fallback, timeout, delegate exception,          #
# text-block-only payload, get_recommendation input path                      #
# --------------------------------------------------------------------------- #
def test_envelope_cap_trims_when_untrimmed_envelope_exceeds_cap_keeping_flags_independent(monkeypatch):
    # Build a payload whose UNTRIMMED envelope demonstrably exceeds the 20k cap,
    # so the aggregate byte trim must run. Long grammar-valid tokens (within
    # MAX_STRING) on 50 rows push the serialized size over the cap.
    # Local modules
    import agents.cost_mcp_guard as guard  # noqa: PLC0415

    calls = {"n": 0}
    real_trim = guard._trim_largest_collection

    def _counting_trim(body):
        calls["n"] += 1
        return real_trim(body)

    monkeypatch.setattr(guard, "_trim_largest_collection", _counting_trim)

    long_tok = "A" * 110  # within MAX_STRING (128), token-grammar valid
    rows = [
        {
            "recommendation_id": f"rec-{i:04d}",
            "region": "us-west-2",
            "action_type": "Rightsize",
            "current_resource_type": f"Ec2.{long_tok}",
            "recommended_resource_type": f"Ec2.{long_tok}",
            "implementation_effort": f"Low.{long_tok}",
            "source": f"ComputeOptimizer.{long_tok}",
            "currency": "USD",
        }
        for i in range(50)
    ]
    # Confirm the pre-trim envelope really exceeds the cap by projecting and
    # serializing the same payload without the aggregate trim.
    # Local modules
    from agents.cost_mcp_guard import ENVELOPE_VERSION, _dumps, _echo_op  # noqa: PLC0415
    from agents.cost_projections import project_cost_optimization_recommendations  # noqa: PLC0415

    projected = project_cost_optimization_recommendations({"recommendations": rows, "next_token": "more"}, 50)
    untrimmed = {"v": ENVELOPE_VERSION, "operation": _echo_op("list_recommendations"), **projected}
    pretrim_size = len(_dumps(untrimmed).encode("utf-8"))
    assert pretrim_size > MAX_ENVELOPE_BYTES, f"fixture must exceed the cap untrimmed, got {pretrim_size}"

    tool = _one("cost-optimization", _success({"recommendations": rows, "next_token": "more"}))
    result = _invoke(tool, {"operation": "list_recommendations", "max_results": 50})
    text = result["content"][0]["text"]
    env = json.loads(text)
    # The aggregate trim must have actually run and brought the final envelope
    # under the cap (a no-op trim would fall through to the withheld fallback,
    # whose message this test asserts is absent).
    assert calls["n"] >= 1, "the aggregate trim must have run on an over-cap envelope"
    assert len(text.encode("utf-8")) <= MAX_ENVELOPE_BYTES
    assert env["status"] == "incomplete"
    # A genuine trim keeps a non-empty, strictly-smaller retained collection and
    # emits NO withheld message. A no-op trim (withheld fallback) keeps all 50
    # rows out of the content and sets message instead, so both bounds fail it.
    assert 0 < len(env["recommendations"]) < 50, env.get("recommendations")
    assert "message" not in env, "a real trim must not emit the withheld fallback message"
    # The local byte trim set truncated; residual provider pagination survives
    # independently of that trim, so the two markers stay distinct.
    assert env.get("truncated") is True
    assert env.get("paginated") is True
    assert env.get("partial") is True


def test_withheld_fallback_when_even_an_empty_collection_cannot_fit():
    """Force the withheld fallback: a projected payload whose non-list fields
    already exceed the cap cannot be trimmed down, so the guard emits the
    code-owned 'withheld' envelope while preserving prior flags."""
    # Local modules
    from agents.cost_mcp_guard import _ok  # noqa: PLC0415

    giant = "x" * (MAX_ENVELOPE_BYTES + 500)
    projected = {"status": "incomplete", "partial": True, "note": giant, "recommendations": []}
    result = _ok("t", "list_recommendations", projected)
    text = result["content"][0]["text"]
    env = json.loads(text)
    assert env["status"] == "incomplete"
    assert env["truncated"] is True
    assert "withheld" in env["message"]
    assert env["partial"] is True


def test_text_block_only_payload_is_projected():
    """When structuredContent is absent, the guard must still read the first
    JSON text block (the FastMCP wire also carries the body there)."""
    body = {"status": "success", "data": {"recommendations": [{"recommendation_id": "rec-1", "region": "us-west-2"}]}}
    result = {"toolUseId": "t", "status": "success", "content": [{"text": json.dumps(body)}]}
    env = _env(_invoke(_one("cost-optimization", result), {"operation": "list_recommendations"}))
    assert env["status"] == "ok"
    assert env["recommendations"][0]["recommendation_id"] == "rec-1"


class _TimeoutTool(AgentTool):
    def __init__(self) -> None:
        super().__init__()
        self._name = "cost-optimization"

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> ToolSpec:
        return {"name": self._name, "description": "orig", "inputSchema": {"json": {"type": "object"}}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):
        # Standard library
        import asyncio as _a  # noqa: PLC0415

        await _a.sleep(3600)
        yield ToolResultEvent({"toolUseId": "t", "status": "success", "content": []})


class _RaisingTool(_TimeoutTool):
    async def stream(self, tool_use, invocation_state, **kwargs):
        raise RuntimeError(f"provider blew up {ARN}")
        yield  # pragma: no cover


def test_delegate_timeout_yields_typed_incomplete(monkeypatch):
    # Shrink the deadline so the test does not actually wait.
    # Local modules
    import agents.cost_mcp_guard as guard  # noqa: PLC0415

    monkeypatch.setattr(guard, "DEADLINE_SECONDS", 0.05)
    tool = _load([_TimeoutTool()])[0]
    result = _invoke(tool, {"operation": "list_recommendations"})
    env = _env(result)
    assert env["status"] == "incomplete"
    # The deadline outcome keeps the narrower-request advice, which is distinct
    # from the cause-neutral provider_error message; assert it where it is
    # emitted so routing this branch to the provider_error text is caught.
    assert env["message"] == guard._MSG_UNAVAILABLE
    assert "narrower" in env["message"].lower()


def test_delegate_exception_yields_typed_incomplete_without_leak():
    # Local modules
    import agents.cost_mcp_guard as guard  # noqa: PLC0415

    tool = _load([_RaisingTool()])[0]
    result = _invoke(tool, {"operation": "list_recommendations"})
    _assert_no_sensitive(result)
    env = _env(result)
    assert env["status"] == "incomplete"
    # The delegate-exception branch shares the deadline message; pin it here too.
    assert env["message"] == guard._MSG_UNAVAILABLE
    assert "narrower" in env["message"].lower()


# --------------------------------------------------------------------------- #
# Cost Optimization Hub list dispatcher `service_error` body carries the        #
# provider code only inside data.message; the guard must read it so throttling #
# classifies as throttled, not provider_error, without echoing the text.       #
# --------------------------------------------------------------------------- #
def _coh_service_error(provider_code: str) -> dict[str, Any]:
    """Shape produced by the installed CoH dispatcher for a re-raised ClientError
    on list_recommendations / list_recommendation_summaries: data.error_type is
    the fixed 'service_error' and the botocore string is only in data.message.
    The botocore prefix is built at runtime so a hostile ARN can be embedded to
    prove it never reaches model context."""
    botocore_text = (
        f"An error occurred ({provider_code}) when calling the ListRecommendations "
        f"operation: Rate exceeded for {ARN} req {REQ_ID}"
    )
    body = {
        "status": "error",
        "data": {
            "error_type": "service_error",
            "service": "Cost Optimization Hub",
            "operation": "list_recommendations",
            "message": botocore_text,
        },
        "message": "Error fetching recommendations from Cost Optimization Hub.",
    }
    return {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }


def test_hub_service_error_throttling_maps_to_throttled():
    result = _invoke(
        _one("cost-optimization", _coh_service_error("ThrottlingException")),
        {"operation": "list_recommendations"},
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert (
        env["error"]["code"] == "throttled"
    ), "service_error throttling must classify as throttled, not provider_error"
    _assert_no_sensitive(result)


def test_hub_service_error_internal_maps_to_provider_error():
    result = _invoke(
        _one("cost-optimization", _coh_service_error("InternalServerException")),
        {"operation": "list_recommendations"},
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "provider_error"
    _assert_no_sensitive(result)


# --------------------------------------------------------------------------- #
# typed credential / data-unavailable codes with their own messages            #
# --------------------------------------------------------------------------- #
def test_data_unavailable_maps_to_typed_code():
    result = _invoke(
        _one("cost-explorer", _toplevel_error("DataUnavailableException", "The requested data is unavailable.")),
        _forecast_input(),
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "data_unavailable"
    _assert_no_sensitive(result)


@pytest.mark.parametrize(
    "provider_code",
    ["UnrecognizedClientException", "ExpiredTokenException", "MissingAuthenticationToken"],
)
def test_credential_errors_classify_as_credentials_unavailable(provider_code):
    result = _invoke(
        _one("cost-explorer", _toplevel_error(provider_code, f"bad creds {ARN}")),
        _forecast_input(),
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "credentials_unavailable", provider_code
    _assert_no_sensitive(result)


def _aws_connection_error(boto_error_type: str) -> dict[str, Any]:
    """Shape produced by the Billing MCP ``handle_aws_error`` (in
    ``utilities/aws_service_base.py``) for a botocore ``BotoCoreError``: a
    top-level ``error_type`` of ``aws_connection_error`` with the exception's
    class name in ``boto_error_type`` and only a generic connection message. A
    hostile ARN/request id is embedded in the surrounding fields to prove none of
    it reaches model context."""
    body = {
        "status": "error",
        "service": "Cost Explorer",
        "operation": "GetCostForecast",
        "error_type": "aws_connection_error",
        "boto_error_type": boto_error_type,
        "message": f"AWS service connection error: {boto_error_type}",
        "details": f"connection detail {ARN} req {REQ_ID}",
    }
    return {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }


def _ce_client_creation_error(details_repr: str) -> dict[str, Any]:
    """Shape produced by the installed Cost Explorer tool when ``create_aws_client``
    fails (``tools/cost_explorer_tools.py``): ``format_response('error', ...)``
    wraps ``data.error_type == "client_creation_error"`` with ``repr(exc)`` in
    ``data.details`` and NO top-level ``error_type``/``boto_error_type``. A
    hostile ARN/request id surrounds the token to prove none of it leaks."""
    body = {
        "status": "error",
        "data": {
            "error_type": "client_creation_error",
            "message": f"Failed to create AWS client near {ARN} req {REQ_ID}",
            "details": details_repr,
        },
    }
    return {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }


def _hub_service_error_message(message: str) -> dict[str, Any]:
    """Shape produced by the Cost Optimization Hub list dispatcher (in
    ``tools/cost_optimization_hub_tools.py``) when its inner handler catches a
    non-ClientError: ``data.error_type`` is the fixed ``service_error`` and
    ``data.message`` is ``str(exc)`` with no "An error occurred (...)" prefix. A
    re-raised botocore ``NoCredentialsError`` yields exactly "Unable to locate
    credentials"; a ``CredentialRetrievalError`` yields a message beginning
    "Error when retrieving credentials from "."""
    body = {
        "status": "error",
        "data": {
            "error_type": "service_error",
            "service": "Cost Optimization Hub",
            "operation": "list_recommendations",
            "message": message,
        },
        "message": "Error retrieving recommendation summaries.",
    }
    return {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }


# Build the real botocore credential bodies from the exception objects so the
# tests track botocore's actual str()/repr() rather than hard-coded copies.
_PARTIAL_CREDS = PartialCredentialsError(provider="env", cred_var="aws_secret_access_key")
_RETRIEVAL_CREDS = CredentialRetrievalError(provider="container-role", error_msg="endpoint timed out")
_NO_CREDS = NoCredentialsError()
_ENDPOINT_ERR = EndpointConnectionError(endpoint_url="https://ce.us-east-1.amazonaws.com/")


@pytest.mark.parametrize(
    "boto_error_type",
    ["NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError"],
)
def test_connection_error_credential_classes_map_to_credentials_unavailable(boto_error_type):
    # A botocore credential failure surfaces as aws_connection_error with the
    # class in boto_error_type; those known credential classes must classify as
    # credentials_unavailable rather than a generic provider error.
    result = _invoke(_one("cost-explorer", _aws_connection_error(boto_error_type)), _forecast_input())
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "credentials_unavailable", boto_error_type
    _assert_no_sensitive(result)


def test_connection_error_non_credential_class_stays_provider_error():
    # A different aws_connection_error class is not a credential failure and must
    # remain a generic provider error.
    result = _invoke(_one("cost-explorer", _aws_connection_error("EndpointConnectionError")), _forecast_input())
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "provider_error"
    _assert_no_sensitive(result)


@pytest.mark.parametrize(
    "exc",
    [_NO_CREDS, _PARTIAL_CREDS, _RETRIEVAL_CREDS],
    ids=["NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError"],
)
def test_cost_explorer_client_creation_credential_classes_map_to_credentials_unavailable(exc):
    # The Cost Explorer tool creates its client in its own try and reports a
    # credential-resolution failure as client_creation_error with repr(exc) in
    # data.details. PartialCredentialsError in particular is resolved only at
    # client creation, so this is the ONLY shape it ever takes. All three known
    # credential classes must classify as credentials_unavailable here.
    result = _invoke(_one("cost-explorer", _ce_client_creation_error(repr(exc))), _forecast_input())
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "credentials_unavailable", exc.__class__.__name__
    _assert_no_sensitive(result)


@pytest.mark.parametrize(
    "details_repr",
    [repr(_ENDPOINT_ERR), repr(ValueError("bad value")), "Timeout: could not reach endpoint"],
    ids=["EndpointConnectionError", "ValueError", "no-class-token"],
)
def test_cost_explorer_client_creation_non_credential_stays_provider_error(details_repr):
    # A client_creation_error whose repr names a non-credential class (or carries
    # no class token at all) is NOT a credential failure and stays provider_error.
    result = _invoke(_one("cost-explorer", _ce_client_creation_error(details_repr)), _forecast_input())
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "provider_error", details_repr
    _assert_no_sensitive(result)


def test_hub_service_error_missing_credentials_text_maps_to_credentials_unavailable():
    # The Hub list dispatcher re-raises a botocore NoCredentialsError into a
    # service_error whose data.message is the fixed "Unable to locate credentials"
    # text (built from str(NoCredentialsError())) with no code prefix; it must
    # classify as credentials_unavailable.
    result = _invoke(
        _one("cost-optimization", _hub_service_error_message(str(_NO_CREDS))),
        {"operation": "list_recommendations"},
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "credentials_unavailable"
    _assert_no_sensitive(result)


def test_hub_service_error_credential_retrieval_prefix_maps_to_credentials_unavailable():
    # A re-raised CredentialRetrievalError has no "An error occurred (...)" code
    # token; its str() begins with the fixed "Error when retrieving credentials
    # from " prefix. That prefix must classify as credentials_unavailable.
    message = str(_RETRIEVAL_CREDS)
    assert message.lower().startswith("error when retrieving credentials from ")
    result = _invoke(
        _one("cost-optimization", _hub_service_error_message(message)),
        {"operation": "list_recommendations"},
    )
    env = _env(result)
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "credentials_unavailable"
    _assert_no_sensitive(result)


# --------------------------------------------------------------------------- #
# Credential-matching contract: hostile rows pin exact-token / exact-text /    #
# prefix matching so a loosened match (substring, prefix, case-insensitive     #
# where it must be exact, unstripped, or ungated) is caught.                   #
# --------------------------------------------------------------------------- #
def _code_of(result: dict[str, Any]) -> str:
    env = _env(result)
    return env["error"]["code"] if isinstance(env.get("error"), dict) else env.get("status", "")


@pytest.mark.parametrize(
    "boto_error_type,expected",
    [
        # Exact class names in any case and with surrounding whitespace match.
        ("NoCredentialsError", "credentials_unavailable"),
        ("  nocredentialserror  ", "credentials_unavailable"),
        ("PARTIALCREDENTIALSERROR", "credentials_unavailable"),
        # Superstrings, prefixes, suffixes, and embedded tokens must NOT match.
        ("NoCredentialsErrorX", "provider_error"),
        ("XNoCredentialsError", "provider_error"),
        ("NoCredentials", "provider_error"),
        ("NoCredentialsError\u200bX", "provider_error"),  # zero-width suffix
        ("NoCredentialsError,PartialCredentialsError", "provider_error"),
        ("credentials_error", "provider_error"),  # the synthetic token itself
    ],
)
def test_aws_connection_error_class_match_is_exact(boto_error_type, expected):
    result = _invoke(_one("cost-explorer", _aws_connection_error(boto_error_type)), _forecast_input())
    assert _code_of(result) == expected, boto_error_type
    _assert_no_sensitive(result)


def test_aws_connection_error_gate_requires_the_sentinel():
    # Without the fixed aws_connection_error sentinel, a boto_error_type naming a
    # credential class must NOT open the credential branch.
    body = {
        "status": "error",
        "error_type": "some_other_connection_error",
        "boto_error_type": "NoCredentialsError",
        "message": "connection error",
    }
    wire = {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }
    assert _code_of(_invoke(_one("cost-explorer", wire), _forecast_input())) == "provider_error"


@pytest.mark.parametrize("boto_error_type", [None, 123, {"x": 1}])
def test_aws_connection_error_non_string_class_stays_provider_error(boto_error_type):
    body = {
        "status": "error",
        "error_type": "aws_connection_error",
        "boto_error_type": boto_error_type,
        "message": "connection error",
    }
    wire = {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }
    assert _code_of(_invoke(_one("cost-explorer", wire), _forecast_input())) == "provider_error"


@pytest.mark.parametrize(
    "details_repr,expected",
    [
        # repr() always starts with the class name + "(": exact token in any case.
        ("NoCredentialsError('Unable to locate credentials')", "credentials_unavailable"),
        ("partialcredentialserror('x')", "credentials_unavailable"),
        # The token is anchored at the start, so a leading superstring must miss.
        ("XNoCredentialsError('x')", "provider_error"),
        # A class name only mentioned later in the details must not match.
        ("ValueError('see NoCredentialsError')", "provider_error"),
        # No "(" after the token -> no class token -> provider_error.
        ("NoCredentialsError", "provider_error"),
    ],
)
def test_client_creation_error_class_token_is_anchored(details_repr, expected):
    result = _invoke(_one("cost-explorer", _ce_client_creation_error(details_repr)), _forecast_input())
    assert _code_of(result) == expected, details_repr
    _assert_no_sensitive(result)


@pytest.mark.parametrize(
    "message,expected",
    [
        # Exact text, any case, surrounding whitespace -> credentials_unavailable.
        ("Unable to locate credentials", "credentials_unavailable"),
        ("  unable to locate credentials  ", "credentials_unavailable"),
        # The CredentialRetrievalError prefix (case-insensitive) matches.
        ("Error when retrieving credentials from container-role: boom", "credentials_unavailable"),
        ("error when retrieving credentials from env", "credentials_unavailable"),
        # A different prefix/superstring/other text must NOT be a credential hit.
        ("Totally unable to locate credentials now", "provider_error"),
        ("Unable to locate credential", "provider_error"),
        # The exact-text clause is exact, not a prefix: a trailing suffix misses.
        ("Unable to locate credentials for the account", "provider_error"),
        ("Could not retrieve credentials from somewhere", "provider_error"),
        ("Error while retrieving credentials from x", "provider_error"),
        # The retrieval clause is a start-anchored prefix, not a substring: the
        # same phrase embedded mid-message must NOT match.
        ("Boom: error when retrieving credentials from env", "provider_error"),
    ],
)
def test_hub_service_error_credential_text_match_contract(message, expected):
    result = _invoke(
        _one("cost-optimization", _hub_service_error_message(message)),
        {"operation": "list_recommendations"},
    )
    assert _code_of(result) == expected, message
    _assert_no_sensitive(result)


@pytest.mark.parametrize(
    "details_repr,expected",
    [
        # repr() of an exception always begins with the class name immediately
        # followed by "(", so the class token is read only from the START of the
        # details. A credential class name that appears only after leading text
        # (a human-readable "Failed: ..." note) or inside angle brackets is NOT
        # the exception's own class and must stay provider_error. Pins that the
        # token is read from the anchored start, not found anywhere in the text.
        ("Failed: NoCredentialsError('x')", "provider_error"),
        ("<NoCredentialsError('x')>", "provider_error"),
    ],
)
def test_client_creation_error_token_is_read_from_the_start_not_mid_text(details_repr, expected):
    result = _invoke(_one("cost-explorer", _ce_client_creation_error(details_repr)), _forecast_input())
    assert _code_of(result) == expected, details_repr
    _assert_no_sensitive(result)


def test_client_creation_token_parse_requires_the_client_creation_sentinel():
    # The anchored class-token parse runs ONLY under the client_creation_error
    # data.error_type. A service_error body whose data.details happens to carry a
    # credential-class repr must NOT be parsed as a client-creation credential
    # failure (data.details is not the service_error shape's code source); it
    # stays provider_error. Pins the gate and that the gate compares the
    # case-folded, trimmed sentinel.
    body = {
        "status": "error",
        "data": {
            "error_type": "service_error",
            "service": "Cost Optimization Hub",
            "operation": "list_recommendations",
            "message": "some provider failure",
            "details": "NoCredentialsError('x')",
        },
    }
    wire = {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }
    assert _code_of(_invoke(_one("cost-optimization", wire), {"operation": "list_recommendations"})) == "provider_error"


def test_client_creation_error_type_tolerates_padding_and_case():
    # The gate case-folds and trims data.error_type, so a padded, upper-case
    # sentinel still opens the branch and a credential-class repr classifies as
    # credentials_unavailable. Pins the strip().lower() on the gate.
    body = {
        "status": "error",
        "data": {
            "error_type": "  CLIENT_CREATION_ERROR  ",
            "message": "Failed to create AWS client",
            "details": "NoCredentialsError('Unable to locate credentials')",
        },
    }
    wire = {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }
    assert _code_of(_invoke(_one("cost-explorer", wire), _forecast_input())) == "credentials_unavailable"


@pytest.mark.parametrize("details_value", [None, 123, ["NoCredentialsError('x')"]], ids=["none", "int", "list"])
def test_client_creation_error_non_string_details_stays_provider_error(details_value):
    # A non-string data.details must be skipped (the regex is only applied to a
    # str), so a client_creation_error with a null, numeric, or list details is a
    # generic provider_error. Pins the isinstance(details, str) check.
    body = {
        "status": "error",
        "data": {
            "error_type": "client_creation_error",
            "message": "Failed to create AWS client",
            "details": details_value,
        },
    }
    wire = {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }
    assert _code_of(_invoke(_one("cost-explorer", wire), _forecast_input())) == "provider_error", details_value


@pytest.mark.parametrize(
    "message,expected",
    [
        # The CredentialRetrievalError prefix match tolerates leading whitespace
        # (the message is trimmed before the prefix test), so a padded retrieval
        # message still classifies as a credential failure. Pins the strip()
        # ahead of the startswith() clause.
        ("  Error when retrieving credentials from x", "credentials_unavailable"),
        # botocore's TokenRetrievalError text begins "Error when retrieving token
        # from ...", NOT "... credentials from ...". The prefix is specific to
        # credential retrieval, so a token-retrieval failure stays provider_error.
        # Pins that the prefix is not shortened to a generic "error when
        # retrieving " that would also swallow TokenRetrievalError text.
        (str(TokenRetrievalError(provider="sso", error_msg="x")), "provider_error"),
    ],
)
def test_hub_service_error_retrieval_prefix_is_trimmed_and_credential_specific(message, expected):
    assert "token" in str(TokenRetrievalError(provider="sso", error_msg="x")).lower()
    result = _invoke(
        _one("cost-optimization", _hub_service_error_message(message)),
        {"operation": "list_recommendations"},
    )
    assert _code_of(result) == expected, message
    _assert_no_sensitive(result)


def test_scope_length_bound_runs_before_the_sensitive_shape_check(monkeypatch):
    # The advertised maxLength bound must be enforced BEFORE _value_is_sensitive_
    # safe so an over-length value is rejected without paying that check's cost on
    # an unbounded string. Spy on _value_is_sensitive_safe and assert a 129-char
    # region is rejected without ever calling it.
    # Local modules
    import agents.cost_mcp_guard as guard  # noqa: PLC0415

    calls: list[str] = []
    original = guard._value_is_sensitive_safe

    def _spy(value: str) -> bool:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(guard, "_value_is_sensitive_safe", _spy)
    region_129 = "aa-" + "x" * 126  # 129 chars, grammar-valid but over maxLength
    assert len(region_129) == 129
    assert guard._RE_REGION_CODE.fullmatch(region_129) is not None, "fixture must be grammar-valid at 129"
    assert guard._valid_scope_values([region_129], "REGION") is None
    assert calls == [], "the sensitive-shape check must not run on an over-maxLength value"
    # A within-bound value still reaches the sensitive-shape check and validates.
    assert guard._valid_scope_values(["us-west-2"], "REGION") == ["us-west-2"]
    assert "us-west-2" in calls


def test_each_typed_code_has_its_own_message():
    # Local modules
    from agents.cost_mcp_guard import _message_for_code  # noqa: PLC0415
    from agents.cost_projections import (  # noqa: PLC0415
        ERROR_CREDENTIALS_UNAVAILABLE,
        ERROR_DATA_UNAVAILABLE,
        ERROR_NOT_ENROLLED,
        ERROR_NOT_FOUND,
        ERROR_THROTTLED,
    )

    messages = {
        _message_for_code(code)
        for code in (
            ERROR_THROTTLED,
            ERROR_NOT_FOUND,
            ERROR_NOT_ENROLLED,
            ERROR_DATA_UNAVAILABLE,
            ERROR_CREDENTIALS_UNAVAILABLE,
        )
    }
    # Each code gets a distinct, code-owned message (no single generic string).
    assert len(messages) == 5, messages
    for msg in messages:
        assert msg and isinstance(msg, str)


def test_provider_error_message_is_cause_neutral_and_differs_from_unavailable():
    # The unmapped provider_error outcome and the bounded-deadline / delegate-
    # exception "unavailable" outcome must carry distinct, code-owned messages.
    # provider_error covers internal/unavailable/connection failures that a
    # narrower request does not help, so its message must not advise narrowing,
    # while the deadline/delegate "unavailable" outcome keeps that advice.
    # Local modules
    from agents.cost_mcp_guard import _MSG_UNAVAILABLE, _message_for_code  # noqa: PLC0415
    from agents.cost_projections import ERROR_PROVIDER_ERROR  # noqa: PLC0415

    provider_msg = _message_for_code(ERROR_PROVIDER_ERROR)
    # Behavioral assertion: the provider_error message carries no narrower-request
    # advice and is distinct from the unavailable (deadline) message.
    assert "narrower" not in provider_msg.lower()
    assert provider_msg != _MSG_UNAVAILABLE
    # The deadline / delegate-exception message keeps the narrower-request advice.
    assert "narrower" in _MSG_UNAVAILABLE.lower()


# --------------------------------------------------------------------------- #
# free-text classifier fallback; payload-None, projector-exception,            #
# prediction_interval, invalid metric / granularity / calendar; bound-50       #
# receiving 51 rows.                                                            #
# --------------------------------------------------------------------------- #
def _toplevel_free_text(message: str) -> dict[str, Any]:
    """A handle_aws_error-shaped body whose error_type carries NO mapped code,
    forcing the bounded free-text fallback classifier."""
    body = {
        "status": "error",
        "service": "Cost Explorer",
        "operation": "GetCostForecast",
        "error_type": "UnmappedWeirdError",
        "message": message,
        "request_id": REQ_ID,
    }
    return {
        "toolUseId": "t",
        "status": "error",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
        "isError": True,
    }


def test_free_text_fallback_classifier_from_unmapped_shape():
    # A provider error with an unmapped error_type but a throttle phrase in the
    # top-level message falls back to the bounded free-text scan.
    result = _invoke(_one("cost-explorer", _toplevel_free_text("Request was throttled, slow down")), _forecast_input())
    assert _env(result)["error"]["code"] == "throttled"
    # An unmapped shape with no recognizable phrase is a generic provider_error.
    result2 = _invoke(_one("cost-explorer", _toplevel_free_text("Something unusual happened")), _forecast_input())
    assert _env(result2)["error"]["code"] == "provider_error"


def test_payload_none_is_typed_malformed():
    # A success-signalled result whose body has no usable `data` mapping yields a
    # typed malformed outcome (payload-None path), never a clean ok.
    body = {"status": "success", "data": None}
    result = {
        "toolUseId": "t",
        "status": "success",
        "content": [{"text": json.dumps(body)}],
        "structuredContent": body,
    }
    env = _env(_invoke(_one("cost-optimization", result), {"operation": "list_recommendations"}))
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "malformed_response"


def test_projector_exception_is_typed_malformed(monkeypatch):
    # Local modules
    import agents.cost_mcp_guard as guard  # noqa: PLC0415

    def _boom(*_a, **_k):
        raise RuntimeError("projector blew up")

    monkeypatch.setattr(guard, "project_forecast", _boom)
    data = {"Total": {"Amount": "1", "Unit": "USD"}, "ForecastResultsByTime": []}
    env = _env(_invoke(_one("cost-explorer", _success(data)), _forecast_input()))
    assert env["status"] == "incomplete"
    assert env["error"]["code"] == "malformed_response"


def test_prediction_interval_level_accepted_and_forwarded():
    data = {"Total": {"Amount": "1", "Unit": "USD"}, "ForecastResultsByTime": []}
    delegate = _ResultTool("cost-explorer", _success(data))
    tool = _load([delegate])[0]
    _invoke(tool, _forecast_input(prediction_interval_level=95))
    assert delegate.calls, "a valid prediction_interval_level must reach the delegate"
    assert delegate.calls[0]["input"].get("prediction_interval_level") == 95


@pytest.mark.parametrize("bad_pil", [0, 69, 100, 200, True, "95", 95.0])
def test_prediction_interval_level_rejected(bad_pil):
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, _forecast_input(prediction_interval_level=bad_pil))
    assert delegate.calls == [], f"invalid prediction_interval_level {bad_pil!r} must not reach the delegate"
    assert _env(result)["status"] == "denied"


def test_invalid_metric_rejected():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, _forecast_input(metric="MADE_UP_METRIC"))
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_invalid_granularity_rejected():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    result = _invoke(tool, _forecast_input(granularity="HOURLY"))
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_calendar_invalid_date_rejected():
    delegate = _ResultTool("cost-explorer", _success({}))
    tool = _load([delegate])[0]
    # Grammar-valid YYYY-MM-DD but not a real calendar date.
    result = _invoke(tool, _forecast_input(start_date="2026-02-30", end_date="2026-03-01"))
    assert delegate.calls == []
    assert _env(result)["status"] == "denied"


def test_bound_50_receiving_51_rows_is_paginated_not_truncated_through_guard():
    rows = [{"recommendation_id": f"rec-{i:04d}", "region": "us-west-2"} for i in range(51)]
    tool = _one("cost-optimization", _success({"recommendations": rows}))
    env = _env(_invoke(tool, {"operation": "list_recommendations", "max_results": 50}))
    assert env["status"] == "incomplete"
    assert env["paginated"] is True
    assert env["partial"] is True
    assert "truncated" not in env, "the single over-fetch probe row must not set truncated"
    assert len(env["recommendations"]) == 50


# --------------------------------------------------------------------------- #
# code-owned schemas express the enforced limits                               #
# --------------------------------------------------------------------------- #
def test_forecast_scope_schema_states_enforced_limits():
    tool = _one("cost-explorer", _success({}))
    schema = tool.tool_spec["inputSchema"]["json"]
    services = schema["properties"]["services"]
    regions = schema["properties"]["regions"]
    for arr in (services, regions):
        assert arr["minItems"] == 1
        assert arr["uniqueItems"] is True
        assert "pattern" in arr["items"]
        assert arr["items"]["maxLength"] <= 128
    # Dates carry a pattern; horizon is described.
    assert "pattern" in schema["properties"]["start_date"]
    assert "pattern" in schema["properties"]["end_date"]


def test_compute_optimizer_region_schema_pins_deployment_region():
    tool = _one("compute-optimizer", _success({}))
    schema = tool.tool_spec["inputSchema"]["json"]
    region = schema["properties"]["region"]
    assert region.get("enum") == [settings.AWS_REGION], region


def test_max_results_zero_rejected_as_denied_input():
    delegate = _ResultTool("cost-optimization", _success({"recommendations": []}))
    tool = _load([delegate])[0]
    result = _invoke(tool, {"operation": "list_recommendations", "max_results": 0})
    assert delegate.calls == [], "max_results 0 must be rejected to match the schema minimum of 1"
    env = _env(result)
    assert env["status"] == "denied"
    assert env["error"]["code"] == "denied_input"
