"""Unit tests for read-only CloudFormation template validation."""

# Standard library
import json
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
import agents.cfn_template_validation as v
from agents.cfn_template_validation import MAX_ERRORS, MAX_TEMPLATE_BYTES, validate_template_text
from utils.cfn_yaml import is_intrinsic, load_cfn_template

pytestmark = pytest.mark.unit

SYNTHETIC_ACCOUNT_ID = "123456789012"

FLEET_SCHEMA = {
    "typeName": "AWS::GameLift::ContainerFleet",
    "properties": {
        "FleetId": {"type": "string"},
        "FleetRoleArn": {"type": "string"},
        "InstanceType": {"type": "string"},
        "NewGameSessionProtectionPolicy": {"type": "string", "enum": ["FullProtection", "NoProtection"]},
        "LogConfiguration": {"$ref": "#/definitions/LogConfiguration"},
        "Locations": {"type": "array", "items": {"$ref": "#/definitions/LocationConfiguration"}},
    },
    "definitions": {
        "LogConfiguration": {
            "type": "object",
            "properties": {"LogDestination": {"type": "string", "enum": ["NONE", "CLOUDWATCH", "S3"]}},
            "additionalProperties": False,
        },
        "LocationConfiguration": {
            "type": "object",
            "properties": {"Location": {"type": "string"}},
            "required": ["Location"],
            "additionalProperties": False,
        },
    },
    "required": ["FleetRoleArn"],
    "readOnlyProperties": ["/properties/FleetId"],
    "additionalProperties": False,
}


def _template(fleet_properties: str) -> str:
    return (
        'AWSTemplateFormatVersion: "2010-09-09"\n'
        "Resources:\n"
        "  Fleet:\n"
        "    Type: AWS::GameLift::ContainerFleet\n"
        "    Properties:\n"
        f"{fleet_properties}"
    )


VALID = _template(
    "      FleetRoleArn: !GetAtt Role.Arn\n"
    "      InstanceType: c6i.large\n"
    "      NewGameSessionProtectionPolicy: FullProtection\n"
    "      LogConfiguration:\n"
    "        LogDestination: CLOUDWATCH\n"
    "      Locations:\n"
    "        - Location: !Ref AWS::Region\n"
)


def _client(schema=FLEET_SCHEMA, validate_error=None, describe_error=None):
    client = MagicMock()
    if validate_error is not None:
        client.validate_template.side_effect = validate_error
    if describe_error is not None:
        client.describe_type.side_effect = describe_error
    else:
        client.describe_type.return_value = {"Schema": json.dumps(schema)}
    return client


def _client_error(code, message="synthetic", op="ValidateTemplate"):
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    v._schema_cache.clear()
    yield
    v._schema_cache.clear()


def test_loader_maps_short_form_intrinsics():
    parsed = load_cfn_template("A: !Ref B\nC: !GetAtt D.Arn\nE: !Sub '${X}'\n")
    assert parsed == {"A": {"Ref": "B"}, "C": {"Fn::GetAtt": ["D", "Arn"]}, "E": {"Fn::Sub": "${X}"}}
    assert is_intrinsic(parsed["A"]) and not is_intrinsic({"Location": "x"})


def test_valid_template_passes():
    client = _client()
    result = validate_template_text(VALID, client)
    assert result["status"] == "valid"
    assert result["problems"] == [] and result["templateErrors"] == []
    assert result["checkedResourceTypes"] == ["AWS::GameLift::ContainerFleet"]
    client.validate_template.assert_called_once()
    client.describe_type.assert_called_once_with(Type="RESOURCE", TypeName="AWS::GameLift::ContainerFleet")


def test_unknown_nested_property_is_reported_with_allowed_names():
    bad = VALID.replace("LogDestination: CLOUDWATCH", "LogDestinationType: CLOUDWATCH")
    result = validate_template_text(bad, _client())
    assert result["status"] == "invalid"
    assert result["problems"] == [
        {
            "resource": "Fleet",
            "path": "LogConfiguration.LogDestinationType",
            "problem": "unknown_property",
            "allowed": ["LogDestination"],
        }
    ]


@pytest.mark.parametrize(
    "replacement, problem, path",
    [
        (("      FleetRoleArn: !GetAtt Role.Arn\n", ""), "missing_required_property", "FleetRoleArn"),
        (("InstanceType: c6i.large", "FleetId: x"), "read_only_property", "FleetId"),
        (("FullProtection", "Always"), "invalid_enum_value", "NewGameSessionProtectionPolicy"),
        (("- Location: !Ref AWS::Region", "- Region: us-east-1"), "unknown_property", "Locations[0].Region"),
        (("LogDestination: CLOUDWATCH", "LogDestination: [CLOUDWATCH]"), "wrong_value_shape", None),
    ],
)
def test_schema_problems_are_typed(replacement, problem, path):
    result = validate_template_text(VALID.replace(*replacement), _client())
    assert result["status"] == "invalid"
    assert problem in {p["problem"] for p in result["problems"]}
    if path:
        assert path in {p["path"] for p in result["problems"]}


def test_intrinsics_are_not_checked_as_literals():
    template = VALID.replace("NewGameSessionProtectionPolicy: FullProtection", "NewGameSessionProtectionPolicy: !Ref P")
    assert validate_template_text(template, _client())["status"] == "valid"


def test_validate_template_error_is_sanitized_and_bounded():
    message = (
        f"Template format error: Unresolved resource dependencies [Role] arn:aws:iam::{SYNTHETIC_ACCOUNT_ID}:role/x "
        "https://example.invalid/p 10.0.0.1 " + "x" * 1000
    )
    result = validate_template_text(VALID, _client(validate_error=_client_error("ValidationError", message)))
    assert result["status"] == "invalid"
    text = result["templateErrors"][0]
    assert text.startswith("Template format error: Unresolved resource dependencies [Role]")
    assert SYNTHETIC_ACCOUNT_ID not in text and "arn:" not in text and "example.invalid" not in text
    assert "10.0.0.1" not in text and len(text) <= v.MAX_TEXT_CHARS


def test_denied_validate_template_is_incomplete_not_valid():
    result = validate_template_text(VALID, _client(validate_error=_client_error("AccessDenied")))
    assert result["status"] == "incomplete"
    assert result["error"] == {"codes": ["access_denied"]}


def test_schema_lookup_failure_is_incomplete():
    error = _client_error("Throttling", op="DescribeType")
    result = validate_template_text(VALID, _client(describe_error=error))
    assert result["status"] == "incomplete"
    assert result["uncheckedResourceTypes"] == ["AWS::GameLift::ContainerFleet"]


def test_unknown_resource_type_is_invalid():
    error = _client_error("TypeNotFoundException", op="DescribeType")
    result = validate_template_text(VALID, _client(describe_error=error))
    assert result["status"] == "invalid"
    assert result["problems"][0]["problem"] == "unknown_resource_type"


@pytest.mark.parametrize(
    "template, code",
    [
        ("", "empty_template"),
        ("Resources: [", "unparseable_yaml_or_json"),
        ("Parameters: {}\n", "missing_resources_section"),
        ("Resources:\n" + "#" * (MAX_TEMPLATE_BYTES + 1), "template_too_large"),
        ("!!python/object/apply:os.system ['true']", "unparseable_yaml_or_json"),
    ],
)
def test_malformed_input_never_calls_aws(template, code):
    client = _client()
    result = validate_template_text(template, client)
    assert result["status"] == "invalid" and result["templateErrors"] == [code]
    client.validate_template.assert_not_called()


def test_fenced_template_is_accepted():
    assert validate_template_text(f"```yaml\n{VALID}```", _client())["status"] == "valid"


def test_problem_list_is_bounded():
    many = "".join(f"      Bogus{i}: x\n" for i in range(MAX_ERRORS + 10))
    result = validate_template_text(VALID + many, _client())
    assert len(result["problems"]) == MAX_ERRORS and result["truncated"] is True


def test_schemas_are_cached_across_calls():
    client = _client()
    validate_template_text(VALID, client)
    validate_template_text(VALID, client)
    assert client.describe_type.call_count == 1


def test_tool_uses_a_cloudformation_client():
    # Local modules
    from agents.gamelift_specialist import validate_cloudformation_template

    client = _client()
    with patch("agents.gamelift_specialist.boto3.client", return_value=client) as factory:
        result = validate_cloudformation_template(VALID)
    assert factory.call_args.args[0] == "cloudformation"
    assert result["status"] == "valid"


def test_tool_client_failure_is_sanitized():
    # Local modules
    from agents.gamelift_specialist import validate_cloudformation_template

    with patch("agents.gamelift_specialist.boto3.client", side_effect=Exception("https://example.invalid")):
        result = validate_cloudformation_template(VALID)
    assert result["status"] == "incomplete"
    assert "example.invalid" not in json.dumps(result)
