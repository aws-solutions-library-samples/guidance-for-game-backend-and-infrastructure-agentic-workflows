"""
AI Evaluation Test Configuration - Deployment Aware

Uses shared get_deployment_info() from conftest.py for consistent detection.
"""

# Standard library
import json
import os
import sys
import uuid
from urllib.parse import quote

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
        # Hosted production runtimes use Cognito JWT bearer authorization. The
        # test token must be short-lived and supplied by the caller; tests never
        # persist credentials or fall back to a spoofable body identity.
        token = os.getenv("GBAW_TEST_ACCESS_TOKEN", "").strip()
        if not token:
            raise RuntimeError("GBAW_TEST_ACCESS_TOKEN is required for deployed AI evaluations")

        unique = uuid.uuid4().hex
        actor_id = os.getenv("GBAW_TEST_ACTOR_ID", "").strip()
        payload = {
            "prompt": query,
            "user_context": {
                **({"user_id": actor_id} if actor_id else {}),
                "session_id": f"aieval-session-{unique}",
            },
        }
        runtime_url = (
            f"https://bedrock-agentcore.{config['region']}.amazonaws.com/runtimes/"
            f"{quote(config['runtime_arn'], safe='')}/invocations?qualifier=DEFAULT"
        )
        response = requests.post(
            runtime_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"aieval-session-{unique}",
            },
            data=json.dumps(payload),
            timeout=180,
        )
        response.raise_for_status()
        response_text = response.text

        try:
            parsed = json.loads(response_text)
            return parsed if isinstance(parsed, str) else str(parsed)
        except Exception:
            return response_text

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
        return bool(
            config.get("runtime_arn") and config.get("runtime_id") and os.getenv("GBAW_TEST_ACCESS_TOKEN", "").strip()
        )
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
