"""Stubbed Cost Explorer tool-to-agent rendering integration."""

# Standard library
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from agents import orchestrator
from agents.cost_report import CostReportCache, CostReportService, create_cost_report_tool_bundle
from agents.optimized_prompts import ORCHESTRATOR_PROMPT

pytestmark = pytest.mark.integration


def test_stubbed_cost_explorer_result_bypasses_generated_financial_prose():
    client = MagicMock()
    client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Estimated": True,
                "Groups": [
                    {
                        "Keys": ["Amazon EKS"],
                        "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["Amazon Bedrock AgentCore"],
                        "Metrics": {"UnblendedCost": {"Amount": "65.00", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["Other services"],
                        "Metrics": {"UnblendedCost": {"Amount": "105.00", "Unit": "USD"}},
                    },
                ],
            }
        ]
    }
    service = CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=2, ttl_seconds=60),
        now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: "cost-integration",
    )
    tools, finalize_response = create_cost_report_tool_bundle(service)

    tool_payload = tools[0]("2026-05-01", "2026-05-15")
    model_response = "Generated prose incorrectly claims the total is USD 999.00."
    agent_response = finalize_response(model_response)

    assert tool_payload["report"]["total"] == "250.00"
    assert agent_response == tool_payload["validatedFinancialSection"]
    assert "**Total:** USD 250.00" in agent_response
    assert "USD 999.00" not in agent_response
    assert "estimated" in agent_response.lower()


def test_orchestrator_returns_the_captured_rendering_instead_of_its_model_response():
    client = MagicMock()
    client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Estimated": False,
                "Groups": [
                    {
                        "Keys": ["Amazon EKS"],
                        "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["Other services"],
                        "Metrics": {"UnblendedCost": {"Amount": "170.00", "Unit": "USD"}},
                    },
                ],
            }
        ]
    }
    service = CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=2, ttl_seconds=60),
        now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: "cost-orchestrator-integration",
    )

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-05-01", "2026-05-15")
            return "The routing model changed the total to USD 999.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("Show my costs")

    assert "**Report ID:** `cost-orchestrator-integration`" in response
    assert "**Total:** USD 250.00" in response
    assert "USD 999.00" not in response


def test_orchestrator_routes_report_id_followup_through_cost_agent():
    report_id = "cost-0123456789abcdef0123456789abcdef"
    client = MagicMock()
    client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "Estimated": False,
                "Groups": [
                    {
                        "Keys": ["Amazon EKS"],
                        "Metrics": {"UnblendedCost": {"Amount": "80.00", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["Amazon GameLift"],
                        "Metrics": {"UnblendedCost": {"Amount": "65.00", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["Other services"],
                        "Metrics": {"UnblendedCost": {"Amount": "105.00", "Unit": "USD"}},
                    },
                ],
            }
        ]
    }
    service = CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=2, ttl_seconds=60),
        now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: report_id,
    )
    create_tools, _ = create_cost_report_tool_bundle(service)
    create_tools[0]("2026-05-01", "2026-05-15")

    def stub_cost_agent(query: str) -> str:
        assert report_id in query
        reuse_tools, _ = create_cost_report_tool_bundle(service)
        reuse_tools[1](report_id, ["Amazon EKS", "Amazon GameLift"])
        return "The model independently changed the combined amount to USD 999.00."

    configured_tools = []

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            return configured_tools[0](query)

    def create_stub_orchestrator(**kwargs):
        configured_tools.extend(kwargs["tools"])
        return StubOrchestratorAgent()

    with (
        patch.object(orchestrator, "Agent", side_effect=create_stub_orchestrator),
        patch.object(orchestrator, "cost_agent", side_effect=stub_cost_agent) as cost_agent,
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator(f"Reuse report ID {report_id} for Amazon EKS and Amazon GameLift.")

    assert configured_tools == [cost_agent]
    assert client.get_cost_and_usage.call_count == 1
    assert "## Validated Cost Report Snapshot Calculation" in response
    assert f"**Report ID:** `{report_id}`" in response
    assert "**Selected services:** Amazon EKS, Amazon GameLift" in response
    assert "**Snapshot reused:** Yes; no new Cost Explorer query was made." in response
    assert "USD 999.00" not in response


def test_orchestrator_fails_closed_when_report_id_followup_skips_cost_tool():
    report_id = "cost-fedcba9876543210fedcba9876543210"
    configured_tools = []

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            return "The combined amount is USD 999.00."

    def create_stub_orchestrator(**kwargs):
        configured_tools.extend(kwargs["tools"])
        return StubOrchestratorAgent()

    with (
        patch.object(orchestrator, "Agent", side_effect=create_stub_orchestrator),
        patch.object(orchestrator, "cost_agent") as cost_agent,
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator(
            f"Reuse report ID {report_id} and calculate the combined service share."
        )

    assert configured_tools == [cost_agent]
    assert "## Cost Report Unavailable" in response
    assert "No unverified financial report was produced." in response
    assert "USD 999.00" not in response


def test_orchestrator_keeps_all_specialists_for_non_report_queries():
    configured_tools = []

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            return "Operational EKS response"

    def create_stub_orchestrator(**kwargs):
        configured_tools.extend(kwargs["tools"])
        return StubOrchestratorAgent()

    with (
        patch.object(orchestrator, "Agent", side_effect=create_stub_orchestrator),
        patch.object(orchestrator, "gamelift_agent") as gamelift_agent,
        patch.object(orchestrator, "eks_agent") as eks_agent,
        patch.object(orchestrator, "cost_agent") as cost_agent,
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("List my EKS clusters")

    assert configured_tools == [gamelift_agent, eks_agent, cost_agent]
    assert response == "Operational EKS response"


def test_orchestrator_prompt_prioritizes_cost_report_followups():
    assert ORCHESTRATOR_PROMPT.version == "2.1.0"
    assert "cost-report follow-up" in ORCHESTRATOR_PROMPT.text
    assert "takes precedence even when EKS or GameLift is named" in ORCHESTRATOR_PROMPT.text
    assert "Cost report IDs must go to cost_agent" in ORCHESTRATOR_PROMPT.text
