"""Bounded, read-only availability checks for deployed frontend and AgentCore."""

# Standard library
import os
from concurrent.futures import ThreadPoolExecutor

# Third-party packages
import boto3
import pytest
import requests

pytestmark = [pytest.mark.cloud, pytest.mark.stress]

HEALTH_MAX_REQUESTS = 20
RUNTIME_MAX_REQUESTS = 5
MAX_WORKERS = 5
REQUEST_TIMEOUT_SECONDS = 5


def _health_request(url: str) -> tuple[int, str]:
    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    status = ""
    if response.headers.get("content-type", "").startswith("application/json"):
        status = str(response.json().get("status", ""))
    return response.status_code, status


def _runtime_status(runtime_id: str, region: str) -> str:
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    response = client.get_agent_runtime(agentRuntimeId=runtime_id)
    return str(response.get("status", ""))


def test_deployed_frontend_health_survives_bounded_concurrency():
    frontend_url = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
    assert frontend_url, "FRONTEND_URL is required for deployed stress validation"
    health_url = f"{frontend_url}/api/health"

    # Warm DNS/TLS and fail early before the bounded concurrent sample.
    assert _health_request(health_url) == (200, "healthy")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(lambda _: _health_request(health_url), range(HEALTH_MAX_REQUESTS)))

    assert results == [(200, "healthy")] * HEALTH_MAX_REQUESTS


def test_agentcore_runtime_stays_ready_under_bounded_control_plane_reads():
    runtime_id = os.getenv("AGENTCORE_RUNTIME_ID", "").strip()
    region = os.getenv("AWS_REGION", "us-west-2").strip()
    assert runtime_id, "AGENTCORE_RUNTIME_ID is required for deployed stress validation"

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        statuses = list(
            executor.map(
                lambda _: _runtime_status(runtime_id, region),
                range(RUNTIME_MAX_REQUESTS),
            )
        )

    assert statuses == ["READY"] * RUNTIME_MAX_REQUESTS
