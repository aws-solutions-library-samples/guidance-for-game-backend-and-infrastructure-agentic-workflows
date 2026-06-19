#!/usr/bin/env python3
"""
Pytest configuration and fixtures for backend tests.
Handles proper MCP client cleanup to prevent logging errors during teardown.
"""

# Standard library
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Load environment variables for tests
# Load backend/.env.local (updated by deploy-kb.sh with current KB IDs)
backend_env = os.path.join(os.path.dirname(__file__), "..", ".env.local")
if os.path.exists(backend_env):
    load_dotenv(backend_env)


def get_deployment_info():
    """
    Get deployment info from environment or .bedrock_agentcore.yaml.

    Returns dict with runtime_id, runtime_arn, frontend_url, region, source
    or None if no deployment detected.
    """
    # First try environment variables (CI/CD or explicit config)
    runtime_id = os.getenv("AGENTCORE_RUNTIME_ID")
    runtime_arn = os.getenv("AGENTCORE_RUNTIME_ARN")
    frontend_url = os.getenv("FRONTEND_URL")

    if runtime_id and runtime_arn:
        return {
            "runtime_id": runtime_id,
            "runtime_arn": runtime_arn,
            "frontend_url": frontend_url,
            "region": os.getenv("AWS_REGION", "us-west-2"),
            "source": "environment",
        }

    # Fallback to .bedrock_agentcore.yaml (local testing against deployed stack)
    agentcore_config = os.path.join(os.path.dirname(__file__), "..", ".bedrock_agentcore.yaml")

    if not os.path.exists(agentcore_config):
        return None

    try:
        runtime_arn = subprocess.check_output(
            ["yq", "eval", ".agents.gameagentruntime.bedrock_agentcore.agent_arn", agentcore_config],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

        runtime_id = subprocess.check_output(
            ["yq", "eval", ".agents.gameagentruntime.bedrock_agentcore.agent_id", agentcore_config],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

        if not runtime_arn or not runtime_id or runtime_arn == "null" or runtime_id == "null":
            return None

        # Get frontend URL from CloudFormation if available
        frontend_url = None
        try:
            # Third-party packages
            import boto3

            cf_client = boto3.client("cloudformation", region_name="us-west-2")
            response = cf_client.describe_stacks(StackName="game-agent-frontend")
            for output in response["Stacks"][0]["Outputs"]:
                if output["OutputKey"] == "ServiceUrl":
                    frontend_url = f"https://{output['OutputValue']}"
                    break
        except Exception:
            pass  # Frontend URL optional

        return {
            "runtime_id": runtime_id,
            "runtime_arn": runtime_arn,
            "frontend_url": frontend_url,
            "region": "us-west-2",
            "source": "agentcore_config",
        }

    except Exception:
        return None


@pytest.fixture(scope="session")
def deployment_info():
    """Session-scoped deployment info fixture. Skips if no deployment detected."""
    info = get_deployment_info()
    if not info:
        pytest.skip("No deployment detected - deploy with ./deploy-all.sh")
    return info


def pytest_configure(config):
    """Configure pytest with timeouts and performance optimizations."""
    # Add timeout markers
    config.addinivalue_line("markers", "timeout: mark test with timeout")
    config.addinivalue_line("markers", "slow: mark test as slow running")


def pytest_collection_modifyitems(config, items):
    """Raise the per-test timeout for tests that make real, slow service calls.

    The base timeout (pytest.ini: timeout = 30) suits mocked unit tests. But
    cloud/integration/e2e/ai_eval/stress tests invoke the live AgentCore runtime
    (orchestrator -> specialist -> MCP tool calls -> Bedrock), which routinely
    takes well over 30s — the app's own per-request budget
    (GBAW_AGENT_TIMEOUT_REQUEST_SECONDS) defaults to 180s. Without this, every
    real-service test trips the 30s timeout and fails spuriously.

    Applied via the pytest-timeout plugin (already a dependency) so a timeout
    fails only the offending test — it does NOT kill the whole process.
    """
    slow_markers = {"cloud", "integration", "e2e", "ai_eval", "stress"}
    for item in items:
        if slow_markers.intersection(item.keywords):
            # Only set if the test hasn't pinned its own @pytest.mark.timeout
            if not item.get_closest_marker("timeout"):
                item.add_marker(pytest.mark.timeout(180))


@pytest.fixture(autouse=True)
def mock_all_mcp_and_agents(request):
    """
    Comprehensively mock all MCP clients and agents to prevent real calls.
    This fixture runs for unit tests only to ensure no real MCP or Bedrock calls.
    Integration tests are excluded to allow real AWS/MCP interactions.
    """
    # Skip mocking for integration tests
    if "integration" in request.keywords or "cloud" in request.keywords:
        yield None
        return

    # Create mock MCP client
    mock_client = MagicMock()
    mock_client.list_tools.return_value = [
        {"name": "list_clusters", "description": "List EKS clusters"},
        {"name": "list_resources", "description": "List AWS resources"},
        {"name": "get_cost_and_usage", "description": "Get cost data"},
    ]

    # Mock different responses for different server types
    def mock_execute_tool(tool_name, params=None):
        if "cluster" in tool_name.lower() or "eks" in tool_name.lower():
            return {"clusters": []}
        elif "fleet" in tool_name.lower() or "gamelift" in tool_name.lower():
            return {"fleets": []}
        elif "cost" in tool_name.lower():
            return {"costs": [], "total": 0}
        else:
            return {"result": "Mock response", "success": True}

    mock_client.execute_tool = mock_execute_tool

    # Mock agent functions to return simple strings
    def mock_eks_agent(query):
        return "No EKS clusters found in us-west-2."

    def mock_gamelift_agent(query):
        return "No GameLift fleets found in us-west-2."

    def mock_cost_agent(query):
        return "No cost data found for the specified period."

    # Mock the orchestrator to return simple responses
    def mock_orchestrator(query, **kwargs):
        if isinstance(query, dict):
            query = query.get("prompt", str(query))
        return f"Mock response for: {str(query)[:50]}"

    # Mock boto3 GameLift client for fallback tools
    mock_gamelift_client = MagicMock()
    mock_gamelift_client.list_fleets.return_value = {"FleetIds": []}
    mock_gamelift_client.describe_fleet_attributes.return_value = {"FleetAttributes": []}
    mock_gamelift_client.describe_fleet_utilization.return_value = {"FleetUtilization": []}
    mock_gamelift_client.describe_fleet_capacity.return_value = {"FleetCapacity": []}
    mock_gamelift_client.describe_scaling_policies.return_value = {"ScalingPolicies": []}

    def mock_boto3_client(service_name, **kwargs):
        if service_name == "gamelift":
            return mock_gamelift_client
        return MagicMock()

    # Apply all mocks (use absolute imports from src root, not src. prefix)
    with (
        patch("utils.mcp_client_factory.create_mcp_client") as mock_get_server,
        patch("agents.eks_specialist.eks_agent", side_effect=mock_eks_agent),
        patch("agents.gamelift_specialist.gamelift_agent", side_effect=mock_gamelift_agent),
        patch("agents.cost_specialist.cost_agent", side_effect=mock_cost_agent),
        patch("agents.orchestrator.run_orchestrator", side_effect=mock_orchestrator),
        patch("agents.gamelift_specialist.boto3.client", side_effect=mock_boto3_client),
    ):

        mock_get_server.return_value = mock_client
        yield mock_client


@pytest.fixture
def cleanup_mcp_clients():
    """
    Cleanup MCP clients after each test to prevent logging errors.
    """
    yield  # Run the test

    # Cleanup MCP clients after test
    try:
        # Give a brief moment for cleanup
        time.sleep(0.1)
    except Exception:
        # Ignore cleanup errors - they're not critical for test results
        pass


@pytest.fixture
def mock_mcp_client():
    """
    Provides a mock MCP client for tests that need to mock MCP functionality.
    """
    mock_client = MagicMock()
    mock_client.list_tools.return_value = [{"name": "mock_tool"}]
    mock_client.execute_tool.return_value = {"success": True, "result": {"message": "Mock response"}}

    return mock_client


@pytest.fixture
def disable_mcp_logging():
    """
    Disables MCP logging during tests to prevent file handle issues.
    """
    with patch("utils.logger.logger") as mock_logger:
        mock_logger.info = lambda *args, **kwargs: None
        mock_logger.error = lambda *args, **kwargs: None
        mock_logger.warning = lambda *args, **kwargs: None
        yield mock_logger
