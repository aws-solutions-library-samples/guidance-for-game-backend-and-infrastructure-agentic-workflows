"""Regression tests for global billing-service IAM permissions."""

# Standard library
import pathlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
BASE_INFRASTRUCTURE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml"


def _cloudformation_resource(template: str, logical_id: str) -> str:
    start = template.index(f"  {logical_id}:\n")
    lines = template[start:].splitlines(keepends=True)
    resource = [lines[0]]
    for line in lines[1:]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        resource.append(line)
    return "".join(resource)


def _iam_statement(resource: str, sid: str) -> str:
    marker = f"              - Sid: {sid}\n"
    start = resource.index(marker)
    lines = resource[start:].splitlines(keepends=True)
    statement = [lines[0]]
    for line in lines[1:]:
        if line.startswith("              - Sid: "):
            break
        statement.append(line)
    return "".join(statement)


def test_agentcore_cost_explorer_access_is_not_region_scoped():
    template = BASE_INFRASTRUCTURE_TEMPLATE.read_text(encoding="utf-8")
    execution_role = _cloudformation_resource(template, "AgentCoreExecutionRole")
    statement = _iam_statement(execution_role, "CostExplorerReadAccess")

    assert "ce:GetCostAndUsage" in statement
    assert "ce:GetCostForecast" in statement
    assert "aws:RequestedRegion" not in statement


def test_agentcore_has_no_pricing_access():
    """Least privilege (#482): the Pricing API (``pricing:DescribeServices`` /
    ``GetAttributeValues`` / ``GetProducts``) was enabled by #360 for service
    discovery, but it is OUTSIDE the reviewed Billing MCP surface, which allows
    only forecast and recommendation operations (the reviewed Billing MCP
    forecast and recommendation surface; see SECURITY.md). The Pricing
    tool has no reviewed caller on the deployed Cost path, so the whole
    ``MiscReadAccess`` statement (pricing + free-tier + S3 Storage Lens) was
    removed from ``AgentCoreExecutionRole``."""
    template = BASE_INFRASTRUCTURE_TEMPLATE.read_text(encoding="utf-8")
    execution_role = _cloudformation_resource(template, "AgentCoreExecutionRole")

    assert "pricing:DescribeServices" not in execution_role
    assert "pricing:GetAttributeValues" not in execution_role
    assert "pricing:GetProducts" not in execution_role
    assert "MiscReadAccess" not in execution_role
    # The removed statement also carried free-tier and S3 Storage Lens reads.
    assert "freetier:GetFreeTierUsage" not in execution_role
    assert "s3:GetStorageLensConfiguration" not in execution_role


def test_ecs_task_role_has_no_cost_explorer_access():
    """Frontend least privilege (#458): the ECS task role no longer carries any
    Cost Explorer grant. Cost queries are a backend/AgentCore specialist concern;
    the frontend proxy has no Cost Explorer caller, so ``CostExplorerAccess`` was
    removed from ``ECSTaskRole`` entirely (it survives only on
    ``AgentCoreExecutionRole``)."""
    template = BASE_INFRASTRUCTURE_TEMPLATE.read_text(encoding="utf-8")
    task_role = _cloudformation_resource(template, "ECSTaskRole")

    assert "CostExplorerAccess" not in task_role
    assert "ce:GetCostAndUsage" not in task_role

    # The AgentCore execution role keeps its (globally-scoped) Cost Explorer grant.
    execution_role = _cloudformation_resource(template, "AgentCoreExecutionRole")
    assert "ce:GetCostAndUsage" in execution_role
