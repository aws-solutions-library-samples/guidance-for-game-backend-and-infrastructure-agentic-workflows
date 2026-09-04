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
from agents.specialist_capture import record_specialist_output

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


def test_orchestrator_composes_specialist_sections_with_authoritative_cost():
    """Multi-specialist query: GameLift/EKS sections survive alongside the validated cost total."""
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
        report_id_factory=lambda: "cost-multi-success",
    )

    gamelift_section = "## GameLift Fleets\n\n- fleet-alpha: ACTIVE"
    eks_section = "## EKS Clusters\n\n- game-cluster: ACTIVE"

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            # Simulate the specialists the orchestrator consulted (recorded at
            # their source in base_specialist) plus a validated cost tool call.
            record_specialist_output("GameLift", gamelift_section)
            record_specialist_output("EKS", eks_section)
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-05-01", "2026-05-15")
            return "The orchestrator prose fabricates a total of USD 999.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("Show GameLift fleets, EKS clusters, and my costs")

    # Mixed cost responses use deterministic service markers rather than
    # model-authored specialist prose.
    assert gamelift_section not in response
    assert eks_section not in response
    assert "## GameLift" in response
    assert "## EKS" in response
    # Authoritative cost rendering is present with the validated total.
    assert "**Report ID:** `cost-multi-success`" in response
    assert "**Total:** USD 250.00" in response
    # Model-authored financial prose is excluded.
    assert "USD 999.00" not in response
    # Specialist sections precede the authoritative cost section deterministically.
    cost_index = response.index("## Validated AWS Cost Explorer Report")
    assert response.index("## GameLift") < cost_index
    assert response.index("## EKS") < cost_index


def test_orchestrator_composes_specialist_sections_with_failed_cost_report():
    """Multi-specialist query with a failed cost report: sections survive, error rendered, no fake totals."""
    client = MagicMock()
    # Empty grouped results -> deterministic NO_COST_DATA error (not an exception).
    client.get_cost_and_usage.return_value = {"ResultsByTime": [{"Estimated": False, "Groups": []}]}
    service = CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=2, ttl_seconds=60),
        now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        report_id_factory=lambda: "cost-multi-failure",
    )

    gamelift_section = "## GameLift Fleets\n\n- fleet-beta: ACTIVE"
    eks_section = "## EKS Clusters\n\n- prod-cluster: ACTIVE"

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            record_specialist_output("GameLift", gamelift_section)
            record_specialist_output("EKS", eks_section)
            tools, _ = create_cost_report_tool_bundle(service)
            payload = tools[0]("2026-05-01", "2026-05-15")
            assert "error" in payload
            return "The orchestrator prose fabricates a total of USD 999.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("Show GameLift fleets, EKS clusters, and my costs")

    # Mixed cost responses use deterministic service markers rather than
    # model-authored specialist prose.
    assert gamelift_section not in response
    assert eks_section not in response
    assert "## GameLift" in response
    assert "## EKS" in response
    # The authoritative failure rendering is present; no fabricated totals leak.
    assert "## Cost Report Unavailable" in response
    assert "No unverified financial report was produced." in response
    assert "USD 999.00" not in response
    # Specialist sections precede the authoritative cost failure section.
    cost_index = response.index("## Cost Report Unavailable")
    assert response.index("## GameLift") < cost_index
    assert response.index("## EKS") < cost_index


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


def test_orchestrator_sanitizes_malicious_financial_prose_in_non_cost_specialist():
    """A non-cost specialist that emits a monetary value is fail-closed sanitized.

    The GameLift section carries a model-authored dollar amount. Even though a
    valid cost report exists, that fabricated value must not survive into the
    composed answer: the offending section is replaced with a number-free notice
    while the authoritative cost total is preserved.
    """
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
        report_id_factory=lambda: "cost-malicious-prose",
    )

    # A malicious/hallucinated GameLift section injecting a fabricated total.
    poisoned_gamelift = "## GameLift Fleets\n\nMonthly cost is 4242 for fleet-alpha."
    clean_eks = "## EKS Clusters\n\n- game-cluster: ACTIVE (3 nodes)"

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            record_specialist_output("GameLift", poisoned_gamelift)
            record_specialist_output("EKS", clean_eks)
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-05-01", "2026-05-15")
            return "Prose fabricates a total of USD 999.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("Show GameLift spend, EKS clusters, and my costs")

    # Model-authored specialist prose is never returned in a mixed cost response.
    assert "4242" not in response
    assert poisoned_gamelift not in response
    assert clean_eks not in response
    assert "## GameLift" in response
    assert "## EKS" in response
    # The authoritative cost total is present; no model-authored total leaked.
    assert "**Total:** USD 250.00" in response
    assert "USD 999.00" not in response


def test_orchestrator_fresh_cost_query_fails_closed_when_no_tool_runs():
    """A fresh cost question that never reaches the validated tool must not return model prose.

    The orchestrator model answers with a fabricated total and no cost tool runs
    (no authoritative capture). The request expresses cost intent, so the
    deterministic "Cost Report Unavailable" notice replaces the model prose.
    """

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            # No cost tool invoked; model fabricates a monetary answer.
            return "Your total AWS spending this month is USD 12,345.67."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("What is my total AWS spending this month?")

    assert "## Cost Report Unavailable" in response
    assert "No unverified financial report was produced." in response
    assert "USD 12,345.67" not in response
    assert "12,345" not in response


def test_orchestrator_cost_specialist_failure_before_tool_fails_closed():
    """A cost specialist that fails before get_cost_report fails closed, keeping non-cost sections."""
    clean_eks = "## EKS Clusters\n\n- game-cluster: ACTIVE"

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            record_specialist_output("EKS", clean_eks)
            # Cost specialist ran but failed before its validated tool: it records
            # a safe fallback message under the cost service, and the model prose
            # still fabricates a total.
            record_specialist_output("Cost", "Unable to process the Cost request right now. Please try again.")
            return "Meanwhile your spend is USD 777.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("List EKS clusters and show my spend")

    # Fail closed on the financial side.
    assert "## Cost Report Unavailable" in response
    assert "USD 777.00" not in response
    assert "777" not in response
    # Non-cost model prose is replaced by the deterministic service marker;
    # the cost specialist's own prose is never re-used as a section.
    assert clean_eks not in response
    assert "## EKS" in response
    assert "Unable to process the Cost request" not in response


def test_orchestrator_exception_after_cost_report_returns_deterministic_rendering():
    """If the orchestrator raises after a validated report, the deterministic rendering is returned."""
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
        report_id_factory=lambda: "cost-exc-after-report",
    )

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            tools, _ = create_cost_report_tool_bundle(service)
            tools[0]("2026-05-01", "2026-05-15")
            raise RuntimeError("model blew up after producing the validated report")

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("Show my costs")

    assert "**Report ID:** `cost-exc-after-report`" in response
    assert "**Total:** USD 250.00" in response


def test_orchestrator_exception_on_fresh_cost_query_fails_closed():
    """A cost-bearing request that raises before any rendering fails closed (no propagated prose)."""

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            raise RuntimeError("model blew up before any cost tool ran")

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("How much am I spending on AWS?")

    assert "## Cost Report Unavailable" in response
    assert "No unverified financial report was produced." in response


def test_orchestrator_pure_operational_exception_propagates():
    """A non-cost operational query that raises still propagates (unchanged behavior)."""

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            raise RuntimeError("operational failure")

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="operational failure"):
            orchestrator.run_orchestrator("List my EKS clusters")


def test_specialist_tool_contracts_are_unchanged():
    """Public tool contracts (names + single required 'query' string) are preserved."""
    for agent_tool, expected_name in (
        (orchestrator.gamelift_agent, "gamelift_agent"),
        (orchestrator.eks_agent, "eks_agent"),
        (orchestrator.cost_agent, "cost_agent"),
    ):
        spec = agent_tool.tool_spec
        assert spec["name"] == expected_name
        schema = spec["inputSchema"]["json"]
        assert schema["required"] == ["query"]
        assert schema["properties"]["query"]["type"] == "string"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What's my AWS spending this month?", True),
        ("Compare my GameLift vs EKS costs", True),
        ("How much is GameLift costing me?", True),
        ("Show my total cost for this month", True),
        ("Show AWS costs by service for March", True),
        ("What did EC2 cost yesterday?", True),
        ("Break down yesterday's AWS charges", True),
        ("What is our March cloud expenditure?", True),
        ("How much did we save with Savings Plans last month?", True),
        ("What was yesterday's AWS bill?", True),
        ("What are cost optimization strategies for game servers?", False),
        ("What are AWS spending optimization strategies?", False),
        ("Forecast our AWS spending next month", False),
        ("What is the cheapest hosting pattern?", False),
        ("How much does GameLift cost?", False),
        ("Compare EC2 and GameLift architecture costs", False),
        ("Help me with budget planning", False),
        ("Time was spent restarting pods", False),
        ("Explain AWS pricing models", False),
    ],
)
def test_authoritative_cost_report_intent_is_narrow(query, expected):
    assert orchestrator._has_cost_intent(query) is expected


def test_cost_advisory_without_report_preserves_nonfinancial_response():
    """Advisory cost guidance may use KB/recommendation tools without a report."""
    advisory = "Use Spot capacity and right-size game server instances."

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            record_specialist_output("Cost", advisory)
            return advisory

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("What are cost optimization strategies for game servers?")

    assert response == orchestrator._COST_ADVISORY_GUIDANCE
    assert "right-sizing" in response
    assert "Cost Report Unavailable" not in response


def test_cost_advisory_with_unvalidated_values_preserves_clean_guidance():
    """Advisory prose remains useful while unvalidated figures are withheld."""
    captured_advisory = (
        "Use Spot capacity and right-size game server instances.\n\n"
        "Projected savings are 25% and the example price is USD 999.00."
    )

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            record_specialist_output("Cost", captured_advisory)
            return "The orchestrator rewrites the estimate as USD 777.00."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("What are AWS spending optimization strategies?")

    assert "Use Spot capacity and right-size game server instances." not in response
    assert "25%" not in response
    assert "USD 999.00" not in response
    assert "USD 777.00" not in response
    assert "right-sizing" in response
    assert "No unverified financial value was shown." in response


def test_cost_setup_failure_cannot_leak_advisory_model_financial_prose():
    """A Cost setup failure is captured before tools exist and fails closed."""

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            record_specialist_output("Cost", "")
            return "GameLift costs USD 999.00 per month."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("How much does GameLift cost?")

    assert "USD 999.00" not in response
    assert "## Cost Guidance" in response


def test_direct_advisory_model_response_withholds_financial_paragraph():
    """Direct routing-model advice is sanitized even without specialist capture."""
    model_response = "Use Spot capacity first.\n\nProjected savings are 25% and GameLift costs USD 999.00."

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            return model_response

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("What are AWS spending optimization strategies?")

    assert "Use Spot capacity first." not in response
    assert "25%" not in response
    assert "USD 999.00" not in response
    assert "right-sizing" in response
    assert "No unverified financial value was shown." in response


def test_direct_operational_model_response_remains_unchanged():
    """The global financial guard preserves ordinary operational metrics."""
    operational = "EKS has 3 nodes at 42% CPU utilization and 1.50 GiB memory."

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            return operational

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("List my EKS cluster utilization")

    assert response == operational


def test_direct_cost_topic_never_returns_number_words_from_model():
    """Number-word prices cannot bypass the deterministic cost guidance path."""

    class StubOrchestratorAgent:
        def __call__(self, query: str) -> str:
            return "It will be five dollars per month."

    with (
        patch.object(orchestrator, "Agent", return_value=StubOrchestratorAgent()),
        patch.object(orchestrator, "USE_BEDROCK_SESSIONS", False),
        patch.object(orchestrator, "create_bedrock_model_with_overrides", return_value=MagicMock()),
    ):
        response = orchestrator.run_orchestrator("How much does GameLift cost?")

    assert response == orchestrator._COST_ADVISORY_GUIDANCE
    assert "five dollars" not in response
