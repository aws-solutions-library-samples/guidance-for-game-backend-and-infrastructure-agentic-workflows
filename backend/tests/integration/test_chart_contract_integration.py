"""Integration coverage for inline charts on the trusted backend path (#255).

Two things must hold end-to-end for the inline-chart capability to be real:

1. The versioned chart contract is part of the DEPLOYED system prompts that the
   orchestrator and every specialist send to Amazon Bedrock AgentCore — not only
   the client-side CopilotChat ``instructions`` prop. These assertions exercise
   the same accessors the agents call for ``system_prompt=``.
2. The deterministic Cost Explorer report emits a contract-valid ```chart fence
   built from the SAME validated amounts shown in its markdown table, and that
   deterministic rendering is what the orchestrator returns (bypassing model
   prose). We drive it through ``run_orchestrator`` with a stubbed Cost Explorer
   client so the whole tool→capture→response path runs.
"""

# Standard library
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from agents import orchestrator
from agents.chart_directive import CHART_CONTRACT_VERSION, build_chart_spec
from agents.cost_report import CostReportCache, CostReportService, create_cost_report_tool_bundle
from agents.optimized_prompts import (
    get_optimized_cost_prompt,
    get_optimized_eks_prompt,
    get_optimized_gamelift_prompt,
    get_optimized_orchestrator_prompt,
)

pytestmark = pytest.mark.integration


def _extract_first_chart_fence(text: str) -> dict:
    marker = "```chart\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    return json.loads(text[start:end])


def test_versioned_chart_contract_is_in_the_deployed_prompt_path():
    prompts = {
        "orchestrator": get_optimized_orchestrator_prompt(),
        "cost": get_optimized_cost_prompt(),
        "gamelift": get_optimized_gamelift_prompt(),
        "eks": get_optimized_eks_prompt(),
    }
    for name, prompt in prompts.items():
        assert "`chart`" in prompt, f"{name} prompt is missing the chart directive"
        assert CHART_CONTRACT_VERSION in prompt, f"{name} prompt is missing the contract version"
        assert '"series"' in prompt, f"{name} prompt is missing the chart schema"


def test_cost_report_rendering_includes_a_contract_valid_chart_fence():
    client = MagicMock()
    client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Estimated": False,
                "Groups": [
                    {"Keys": ["Amazon EKS"], "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}}},
                    {"Keys": ["Amazon GameLift"], "Metrics": {"UnblendedCost": {"Amount": "65.00", "Unit": "USD"}}},
                    {"Keys": ["Other services"], "Metrics": {"UnblendedCost": {"Amount": "105.00", "Unit": "USD"}}},
                ],
            }
        ]
    }
    service = CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=2, ttl_seconds=60),
        now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: "cost-chart-integration",
    )
    tools, finalize_response = create_cost_report_tool_bundle(service)
    payload = tools[0]("2026-05-01", "2026-05-15")
    rendered = payload["validatedFinancialSection"]

    # The deterministic financial section carries a chart fence.
    assert "```chart" in rendered
    spec = _extract_first_chart_fence(rendered)

    # It is accepted by the shared contract (producer == consumer).
    assert build_chart_spec(spec) is not None
    assert spec["type"] == "bar"
    assert spec["version"] == CHART_CONTRACT_VERSION

    # The charted values are EXACTLY the validated, displayed amounts — not a
    # model reconstruction.
    report = payload["report"]
    displayed = {s["service"]: float(s["amount"]) for s in report["topServices"]}
    charted = dict(zip(spec["x"]["values"], spec["series"][0]["values"]))
    for service_name, amount in displayed.items():
        assert charted[service_name] == amount
    # A non-zero remainder beyond the top services is charted as an aggregate
    # "Other services" bucket; otherwise the chart is exactly the top services.
    other_total = float(report["otherServicesTotal"])
    if other_total != 0:
        assert charted["Other services"] == other_total
    else:
        assert set(charted) == set(displayed)

    # finalize_response returns the same deterministic section (chart included).
    assert finalize_response("model prose that must be discarded") == rendered


def test_orchestrator_returns_deterministic_rendering_with_chart():
    client = MagicMock()
    client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Estimated": False,
                "Groups": [
                    {"Keys": ["Amazon EKS"], "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}}},
                    {"Keys": ["Other services"], "Metrics": {"UnblendedCost": {"Amount": "170.00", "Unit": "USD"}}},
                ],
            }
        ]
    }
    service = CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=2, ttl_seconds=60),
        now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: "cost-orch-chart",
    )

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-05-01", "2026-05-15")
            return "The routing model tried to change the total to USD 999.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("Show my costs as a chart")

    assert "USD 999.00" not in response
    assert "```chart" in response
    spec = _extract_first_chart_fence(response)
    assert build_chart_spec(spec) is not None


def test_cost_report_chart_folds_remainder_into_an_other_services_bucket():
    # More than five services forces a real "Other services" remainder, which
    # the chart must fold into a single aggregate bar built from the validated
    # other-services total.
    groups = [
        {
            "Keys": [f"Service {chr(ord('A') + i)}"],
            "Metrics": {"UnblendedCost": {"Amount": f"{100 - i * 10}.00", "Unit": "USD"}},
        }
        for i in range(7)
    ]
    client = MagicMock()
    client.get_cost_and_usage.return_value = {"ResultsByTime": [{"Estimated": False, "Groups": groups}]}
    service = CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=2, ttl_seconds=60),
        now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: "cost-bucket",
    )
    tools, _ = create_cost_report_tool_bundle(service)
    payload = tools[0]("2026-05-01", "2026-05-15")
    spec = _extract_first_chart_fence(payload["validatedFinancialSection"])

    assert build_chart_spec(spec) is not None
    report = payload["report"]
    other_total = float(report["otherServicesTotal"])
    assert other_total > 0  # there is a genuine remainder
    charted = dict(zip(spec["x"]["values"], spec["series"][0]["values"]))
    assert charted["Other services"] == other_total
    # Top-5 services plus the single aggregate bucket.
    assert len(spec["x"]["values"]) == len(report["topServices"]) + 1
