"""The platform-version helper matches the locked botocore AgentCore control-plane model.

``scripts/infrastructure/agentcore_platform_version.py`` sends back every
runtime configuration field from ``GetAgentRuntime`` in a full-replacement
``UpdateAgentRuntime``. These checks run against the botocore version in
``backend/uv.lock`` (no network, no credentials), so a dependency upgrade that
adds an updatable field, or changes a shape, fails here for review instead of
silently resetting the field on the next deploy. Behavior tests with a fake
control plane live in ``scripts/test/test_agentcore_platform_version.py``.
"""

# Standard library
import importlib.util
import pathlib
from unittest.mock import MagicMock

# Third-party packages
import botocore.session
import pytest
from botocore.validate import ParamValidator

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
HELPER_PATH = PROJECT_ROOT / "scripts" / "infrastructure" / "agentcore_platform_version.py"


def _helper():
    spec = importlib.util.spec_from_file_location("agentcore_platform_version", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _model():
    return botocore.session.get_session().get_service_model("bedrock-agentcore-control")


def _synthetic_runtime():
    """A GetAgentRuntime-shaped response with every configurable field set (synthetic values)."""
    return {
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/gameagentruntime-EXAMPLE123",
        "agentRuntimeId": "gameagentruntime-EXAMPLE123",
        "agentRuntimeName": "gameagentruntime",
        "agentRuntimeVersion": "7",
        "status": "READY",
        "platformVersion": "V1",
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": {"bucket": "example-bucket", "prefix": "gameagentruntime/deployment.zip"}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["opentelemetry-instrument", "agentcore_main.py"],
            }
        },
        "roleArn": "arn:aws:iam::123456789012:role/example-agentcore-execution-role",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example/.well-known/openid-configuration",
                "allowedClients": ["example-client"],
            }
        },
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "environmentVariables": {"GBAW_HOSTED_RUNTIME": "true"},
        "metadataConfiguration": {"requireMMDSV2": True},
        "description": "Game Agent runtime",
        "filesystemConfigurations": [],
        "capacityProviderConfiguration": {
            "capacityProviderArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:capacity-provider/example"
        },
    }


def test_preserved_fields_are_exactly_the_updatable_configuration():
    helper = _helper()
    update_members = set(_model().operation_model("UpdateAgentRuntime").input_shape.members)

    assert update_members - helper.NON_CONFIGURATION_MEMBERS == set(helper.PRESERVED_FIELDS)
    assert helper.NON_CONFIGURATION_MEMBERS <= update_members


def test_every_preserved_field_is_read_back_with_the_same_shape():
    helper = _helper()
    model = _model()
    update_shape = model.operation_model("UpdateAgentRuntime").input_shape.members
    read_shape = model.operation_model("GetAgentRuntime").output_shape.members

    for name in (*helper.PRESERVED_FIELDS, "platformVersion"):
        assert name in read_shape, name
        assert read_shape[name].name == update_shape[name].name, name


def test_the_helper_accepts_the_locked_client_model():
    helper = _helper()
    # Creating a client makes no network call and needs no credentials. The
    # client comes from a botocore session because the shared test fixtures
    # replace ``boto3.client`` with a mock.
    client = botocore.session.get_session().create_client("bedrock-agentcore-control", region_name="us-west-2")

    helper.check_service_model(client)


def test_the_helper_fails_closed_on_a_client_without_a_usable_model():
    helper = _helper()

    # A mock client's shape members iterate as empty, and a client with no
    # service model at all cannot be checked: both must refuse, not assume.
    with pytest.raises(helper.PlatformVersionError):
        helper.check_service_model(MagicMock())
    with pytest.raises(helper.PlatformVersionError):
        helper.check_service_model(object())


def test_a_built_update_request_validates_against_the_locked_model():
    helper = _helper()
    request = helper.build_update_request(_synthetic_runtime(), "gameagentruntime-EXAMPLE123", "V2")
    input_shape = _model().operation_model("UpdateAgentRuntime").input_shape

    report = ParamValidator().validate(request, input_shape)

    assert not report.has_errors(), report.generate_report()
    assert request["platformVersion"] == "V2"
    assert set(request) == {"agentRuntimeId", "platformVersion", *helper.PRESERVED_FIELDS}
