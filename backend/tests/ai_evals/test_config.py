"""
AI Evaluation Test Configuration - Deployment Aware

Uses shared get_deployment_info() from conftest.py for consistent detection.
"""

# Standard library
import json
import os
import sys

# Third-party packages
import pytest
import requests

# Add tests directory to path for conftest import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Local modules
from conftest import get_deployment_info

pytestmark = pytest.mark.ai_eval


def get_test_config():
    """Get test configuration based on deployment status."""
    info = get_deployment_info()

    if info:
        # Deployed stack mode
        return {
            "mode": "deployed",
            "runtime_id": info["runtime_id"],
            "runtime_arn": info["runtime_arn"],
            "region": info["region"],
        }
    else:
        # Local mode
        return {"mode": "local", "base_url": "http://localhost:8080"}


def make_agent_request(query: str, config: dict = None):
    """Make agent request using appropriate method."""

    if config is None:
        config = get_test_config()

    if config["mode"] == "deployed":
        # Use AWS SDK to invoke deployed AgentCore Runtime
        try:
            # Third-party packages
            import boto3

            client = boto3.client("bedrock-agentcore", region_name=config["region"])

            payload = json.dumps({"prompt": query})
            payload_bytes = payload.encode("utf-8")

            response = client.invoke_agent_runtime(
                agentRuntimeArn=config["runtime_arn"], contentType="application/json", payload=payload_bytes
            )

            # Parse response - use 'response' key not 'payload'
            response_text = response["response"].read().decode("utf-8")

            # Handle JSON-serialized string format from AgentCore
            try:
                parsed = json.loads(response_text)
                if isinstance(parsed, str):
                    return parsed
                return str(parsed)
            except Exception:
                return response_text

        except Exception as e:
            raise Exception(f"Failed to invoke deployed AgentCore Runtime: {e}")

    else:
        # Use HTTP request to local server
        response = requests.post(f"{config['base_url']}/invocations", json={"prompt": query}, timeout=30)
        response.raise_for_status()

        data = response.json()
        # Handle both dict with 'response' key and direct string response
        if isinstance(data, dict):
            return data.get("response", str(data))
        else:
            return str(data)


def check_backend_available(config: dict = None):
    """Check if backend is available for testing."""

    if config is None:
        config = get_test_config()

    if config["mode"] == "deployed":
        # For deployed mode, just check if we have valid config
        # Don't actually invoke the runtime (too slow, can throttle)
        return bool(config.get("runtime_arn") and config.get("runtime_id"))
    else:
        # Check if local server is running by trying to connect
        try:
            # Standard library
            import socket

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(("localhost", 8080))
            sock.close()
            return result == 0
        except Exception:
            return False
