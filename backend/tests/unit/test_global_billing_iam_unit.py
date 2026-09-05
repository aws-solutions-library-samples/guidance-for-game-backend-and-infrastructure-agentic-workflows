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


def test_agentcore_pricing_access_supports_service_discovery():
    template = BASE_INFRASTRUCTURE_TEMPLATE.read_text(encoding="utf-8")
    execution_role = _cloudformation_resource(template, "AgentCoreExecutionRole")
    statement = _iam_statement(execution_role, "MiscReadAccess")

    assert "pricing:DescribeServices" in statement
    assert "pricing:GetAttributeValues" in statement
    assert "pricing:GetProducts" in statement


def test_ecs_task_cost_access_remains_region_scoped():
    template = BASE_INFRASTRUCTURE_TEMPLATE.read_text(encoding="utf-8")
    task_role = _cloudformation_resource(template, "ECSTaskRole")
    statement = _iam_statement(task_role, "CostExplorerAccess")

    assert "aws:RequestedRegion" in statement
