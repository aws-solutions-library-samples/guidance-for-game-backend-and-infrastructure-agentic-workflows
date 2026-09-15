"""
Real System Integration Tests - Deployment Aware

Tests actual system components (localhost or deployed) without mocks.
Auto-detects environment and runs appropriate tests.
"""

# Standard library
import os
import sys

# Third-party packages
import boto3
import pytest
import requests
from botocore.exceptions import ClientError

# Add tests directory to path for conftest import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Local modules
from ai_evals.test_config import make_agent_request
from conftest import get_deployment_info

pytestmark = pytest.mark.integration


class TestSystemIntegration:
    """Test real system components - requires deployed stack."""

    @pytest.fixture(scope="class")
    def backend_config(self):
        """Get backend configuration - requires deployed stack."""
        deployment = get_deployment_info()
        if not deployment:
            pytest.skip("Integration tests require deployed stack - deploy with ./deploy-all.sh")

        return {
            "mode": "deployed",
            "runtime_id": deployment["runtime_id"],
            "runtime_arn": deployment["runtime_arn"],
            "region": deployment["region"],
        }

    def test_backend_health(self, backend_config):
        """Test the runtime control-plane state without bypassing JWT auth."""
        client = boto3.client("bedrock-agentcore-control", region_name=backend_config["region"])
        response = client.get_agent_runtime(agentRuntimeId=backend_config["runtime_id"])

        assert response["status"] == "READY"
        authorizer = response.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {})
        assert authorizer.get("discoveryUrl", "").startswith("https://cognito-idp.")
        assert authorizer.get("discoveryUrl", "").endswith("/.well-known/openid-configuration")
        assert authorizer.get("allowedClients")
        assert response.get("requestHeaderConfiguration", {}).get("requestHeaderAllowlist") == ["Authorization"]

    @pytest.mark.slow
    def test_agent_invocation(self, backend_config):
        """Test a JWT-authorized agent invocation and response."""
        assert os.getenv(
            "GBAW_TEST_ACCESS_TOKEN", ""
        ).strip(), "GBAW_TEST_ACCESS_TOKEN is required for hosted invocation"

        response_text = make_agent_request("Hello", backend_config)
        assert len(response_text) > 0


class TestDeployedStackIntegration:
    """Tests that ONLY run against deployed stack."""

    def test_agentcore_runtime_exists(self):
        """Test that AgentCore Runtime exists in AWS (deployment only)."""
        deployment = get_deployment_info()
        if not deployment:
            pytest.skip("Deployment-only test - requires deployed stack")

        try:
            client = boto3.client("bedrock-agentcore-control", region_name=deployment["region"])
            response = client.get_agent_runtime(agentRuntimeId=deployment["runtime_id"])
            assert response["agentRuntimeArn"] == deployment["runtime_arn"]
            assert response["status"] == "READY"
        except ClientError as e:
            pytest.fail(f"AgentCore Runtime not accessible: {e}")

    def test_frontend_accessibility(self):
        """Test that deployed frontend is accessible."""
        deployment = get_deployment_info()
        if not deployment:
            pytest.skip("Deployment-only test - requires deployed stack")

        if not deployment.get("frontend_url"):
            pytest.skip("No frontend URL available")

        try:
            response = requests.get(deployment["frontend_url"], timeout=10)
            assert response.status_code in [200, 301, 302, 307, 308]
            if response.status_code == 200:
                assert "html" in response.text.lower()
        except requests.RequestException as e:
            pytest.fail(f"Frontend not accessible: {e}")
