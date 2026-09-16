"""Behavioral safety contracts for bounded deployed availability checks."""

# Standard library
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_STRESS_PATH = Path(__file__).parents[1] / "performance" / "test_deployed_health_stress.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("deployed_stress_for_test", _STRESS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_bounds_are_small_and_explicit():
    module = _load_module()

    assert 1 <= module.HEALTH_MAX_REQUESTS <= 20
    assert 1 <= module.RUNTIME_MAX_REQUESTS <= 5
    assert 1 <= module.MAX_WORKERS <= 5
    assert 1 <= module.REQUEST_TIMEOUT_SECONDS <= 5


def test_health_probe_is_read_only_get_with_timeout():
    module = _load_module()
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.json.return_value = {"status": "healthy"}

    with patch.object(module.requests, "get", return_value=response) as get:
        result = module._health_request("https://example.invalid/api/health")

    assert result == (200, "healthy")
    get.assert_called_once_with("https://example.invalid/api/health", timeout=module.REQUEST_TIMEOUT_SECONDS)


def test_runtime_probe_uses_read_only_control_plane_get():
    module = _load_module()
    client = MagicMock()
    client.get_agent_runtime.return_value = {"status": "READY"}

    with patch.object(module.boto3, "client", return_value=client) as client_factory:
        result = module._runtime_status("runtime-example", "us-west-2")

    assert result == "READY"
    client_factory.assert_called_once_with("bedrock-agentcore-control", region_name="us-west-2")
    client.get_agent_runtime.assert_called_once_with(agentRuntimeId="runtime-example")
