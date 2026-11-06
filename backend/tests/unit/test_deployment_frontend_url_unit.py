"""Deployed-test discovery derives one https:// frontend URL from ServiceUrl (issue #641).

The ECS Express ``ServiceUrl`` output has been returned both as a bare host and
with its ``https://`` scheme. ``get_deployment_info()`` builds the frontend URL
the integration tests request, so either form must yield the scheme exactly once.
All identifiers are synthetic.
"""

# Standard library
import os
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# Local modules
import conftest

pytestmark = pytest.mark.unit

HOST = "ga-example.ecs.us-west-2.on.aws"
EXPECTED_URL = f"https://{HOST}"
SERVICE_URL_FORMS = [HOST, f"https://{HOST}", f"https://{HOST}/", f"http://{HOST}"]
RUNTIME_ID = "gameagentruntime-AbCdEf1234"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{RUNTIME_ID}"


@pytest.mark.parametrize("service_url", SERVICE_URL_FORMS)
def test_frontend_url_from_service_url_uses_one_scheme(service_url):
    assert conftest.frontend_url_from_service_url(service_url) == EXPECTED_URL


@pytest.mark.parametrize("service_url", SERVICE_URL_FORMS)
def test_get_deployment_info_derives_one_scheme_from_the_frontend_stack(monkeypatch, service_url):
    for name in ("AGENTCORE_RUNTIME_ID", "AGENTCORE_RUNTIME_ARN", "FRONTEND_URL"):
        monkeypatch.delenv(name, raising=False)

    # Pretend the generated AgentCore config exists and answer its two yq reads.
    real_exists = os.path.exists
    monkeypatch.setattr(
        conftest.os.path,
        "exists",
        lambda path: str(path).endswith(".bedrock_agentcore.yaml") or real_exists(path),
    )
    yq_answers = {
        ".agents.gameagentruntime.bedrock_agentcore.agent_arn": RUNTIME_ARN,
        ".agents.gameagentruntime.bedrock_agentcore.agent_id": RUNTIME_ID,
    }
    monkeypatch.setattr(conftest.subprocess, "check_output", lambda args, **_: f"{yq_answers[args[2]]}\n")

    cloudformation = MagicMock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [{"Outputs": [{"OutputKey": "ServiceUrl", "OutputValue": service_url}]}]
    }
    with patch("boto3.client", return_value=cloudformation):
        info = conftest.get_deployment_info()

    assert info is not None
    assert info["source"] == "agentcore_config"
    assert info["frontend_url"] == EXPECTED_URL
    cloudformation.describe_stacks.assert_called_once_with(StackName="game-agent-frontend")
