"""Stubbed Cost Explorer tool-to-agent rendering integration."""

# Standard library
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
from agents import orchestrator
from agents.cost_report import CostReportCache, CostReportService, create_cost_report_tool_bundle

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
