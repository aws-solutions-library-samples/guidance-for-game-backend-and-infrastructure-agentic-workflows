"""Unit tests for bounded parsing and read-only validation of untrusted CloudFormation drafts."""

# Standard library
import json
import random
import time
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest
from botocore.exceptions import ClientError

# Local modules
import agents.cfn_template_validation as v
from agents.cfn_template_validation import MAX_ERRORS, NOT_VALIDATED, validate_template_text
from utils.cfn_yaml import MAX_TEMPLATE_BYTES, TemplateParseError, is_intrinsic, load_cfn_template

pytestmark = pytest.mark.unit

SYNTHETIC_ACCOUNT_ID = "123456789012"

FLEET_SCHEMA = {
    "typeName": "AWS::GameLift::ContainerFleet",
    "properties": {
        "FleetId": {"type": "string"},
        "FleetRoleArn": {"type": "string"},
        "InstanceType": {"type": "string"},
        "Description": {"type": "string"},
        "NewGameSessionProtectionPolicy": {"type": "string", "enum": ["FullProtection", "NoProtection"]},
        "LogConfiguration": {"$ref": "#/definitions/LogConfiguration"},
        "Locations": {"type": "array", "items": {"$ref": "#/definitions/LocationConfiguration"}},
        "InstanceInboundPermissions": {"type": "array", "items": {"$ref": "#/definitions/IpPermission"}},
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
        "IpPermission": {
            "type": "object",
            "properties": {"IpRange": {"type": "string"}, "Protocol": {"type": "string"}},
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


# ---------------------------------------------------------------------------
# Bounded loader
# ---------------------------------------------------------------------------


def test_loader_maps_short_form_intrinsics():
    parsed = load_cfn_template("A: !Ref B\nC: !GetAtt D.Arn\nE: !Sub '${X}'\nF: !If [c, 1, 2]\n")
    assert parsed == {
        "A": {"Ref": "B"},
        "C": {"Fn::GetAtt": ["D", "Arn"]},
        "E": {"Fn::Sub": "${X}"},
        "F": {"Fn::If": ["c", 1, 2]},
    }
    assert is_intrinsic(parsed["A"]) and not is_intrinsic({"Location": "x"})


def test_loader_accepts_json_templates():
    assert load_cfn_template('{"Resources": {"A": {"Type": "AWS::SNS::Topic"}}}') == {
        "Resources": {"A": {"Type": "AWS::SNS::Topic"}}
    }


@pytest.mark.parametrize(
    "text, code",
    [
        ("a: &x [1, 2]\nb: *x\n", "aliases_not_allowed"),
        ("a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n", "aliases_not_allowed"),
        ("a: !!python/object/apply:os.system ['true']\n", "unsupported_tag"),
        ("a: !!binary aGVsbG8=\n", "unsupported_tag"),
        ("a: !Foo bar\n", "unsupported_tag"),
        ("a: {x: 1}\nb: {<<: {y: 2}}\n", "unsupported_tag"),
        ("a: 1\na: 2\n", "duplicate_key"),
        ("? [a, b]\n: 1\n", "non_scalar_key"),
        ("a: 1\n---\nb: 2\n", "multiple_documents"),
        ("a: " + "[" * 200 + "]" * 200 + "\n", "too_deep"),
        ("a: [" + ",".join(["1"] * 3_000) + "]\n", "collection_too_large"),
        ("a: '" + "x" * 20_000 + "'\n", "scalar_too_long"),
        ("a: '" + "x" * (MAX_TEMPLATE_BYTES + 1) + "'\n", "template_too_large"),
        ("a: !GetAtt NoAttribute\n", "malformed_intrinsic"),
        ("a: !GetAtt {x: 1}\n", "malformed_intrinsic"),
        ("a: !Ref [x]\n", "malformed_intrinsic"),
        ("a: !Ref ''\n", "malformed_intrinsic"),
        ("a: !Sub {x: 1}\n", "malformed_intrinsic"),
        ("a: [unclosed\n", "invalid_yaml"),
    ],
)
def test_loader_rejects_hostile_or_ambiguous_input(text, code):
    with pytest.raises(TemplateParseError) as excinfo:
        load_cfn_template(text)
    assert excinfo.value.code == code


def test_loader_bounds_node_count():
    # 22 lists of 999 one-byte items: under the byte, collection, and depth
    # limits, but more than MAX_NODES nodes in total.
    many = "a:\n" + "".join(f"  k{i}: [{','.join(['1'] * 999)}]\n" for i in range(22))
    with pytest.raises(TemplateParseError) as excinfo:
        load_cfn_template(many)
    assert excinfo.value.code == "too_many_nodes"


def test_loader_never_raises_unexpected_errors_on_random_input():
    """Seeded boundary fuzzing: every input either parses or raises TemplateParseError, quickly."""
    alphabet = list("ab:-[]{}&*!|>'\"#\n ,.?%@`") + ["!Ref ", "!GetAtt ", "&a ", "*a", "<<: ", "!!python/"]
    rng = random.Random(1234)
    for _ in range(1_500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        start = time.monotonic()
        try:
            load_cfn_template(text)
        except TemplateParseError:
            pass
        assert time.monotonic() - start < 1.0, repr(text)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


def test_valid_template_passes_every_check_and_stays_untrusted():
    client = _client()
    result = validate_template_text(VALID, client)
    assert result["status"] == "valid"
    assert result["checks"] == {
        "syntax": "ok",
        "templateValidation": "ok",
        "registrySchema": "ok",
        "productPolicy": "ok",
    }
    assert result["untrusted"] is True and result["applied"] is False
    assert result["notValidated"] == list(NOT_VALIDATED)
    assert result["checkedResourceTypes"] == ["AWS::GameLift::ContainerFleet"]
    client.validate_template.assert_called_once()
    client.describe_type.assert_called_once_with(Type="RESOURCE", TypeName="AWS::GameLift::ContainerFleet")


def test_unknown_nested_property_is_reported_with_allowed_names():
    bad = VALID.replace("LogDestination: CLOUDWATCH", "LogDestinationType: CLOUDWATCH")
    result = validate_template_text(bad, _client())
    assert result["status"] == "invalid"
    assert result["checks"]["registrySchema"] == "invalid"
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


def test_getatt_of_a_declared_attribute_is_valid():
    template = VALID + "Outputs:\n  FleetId:\n    Value: !GetAtt Fleet.FleetId\n"
    assert validate_template_text(template, _client())["status"] == "valid"


def test_getatt_of_a_missing_attribute_is_reported():
    template = VALID + "Outputs:\n  FleetName:\n    Value: !GetAtt Fleet.Name\n"
    result = validate_template_text(template, _client())
    assert result["status"] == "invalid"
    (problem,) = result["problems"]
    assert problem["problem"] == "unknown_getatt_attribute"
    assert problem["allowed"] == ["FleetId"]


# ---------------------------------------------------------------------------
# Provider failures are distinct and never "valid"
# ---------------------------------------------------------------------------


def test_validate_template_error_is_sanitized_and_bounded():
    message = (
        f"Template format error: Unresolved resource dependencies [Role] arn:aws:iam::{SYNTHETIC_ACCOUNT_ID}:role/x "
        "https://example.invalid/p 10.0.0.1 " + "x" * 1000
    )
    result = validate_template_text(VALID, _client(validate_error=_client_error("ValidationError", message)))
    assert result["status"] == "invalid"
    assert result["checks"]["templateValidation"] == "invalid"
    text = result["templateErrors"][0]
    assert text.startswith("Template format error: Unresolved resource dependencies [Role]")
    assert SYNTHETIC_ACCOUNT_ID not in text and "arn:" not in text and "example.invalid" not in text
    assert "10.0.0.1" not in text and len(text) <= v.MAX_TEXT_CHARS


@pytest.mark.parametrize(
    "error, state",
    [
        (_client_error("AccessDenied"), "denied"),
        (_client_error("Throttling"), "throttled"),
        (_client_error("InternalFailure"), "unavailable"),
        (Exception("https://example.invalid/boom"), "unavailable"),
    ],
)
def test_template_validation_failures_are_incomplete_not_valid(error, state):
    result = validate_template_text(VALID, _client(validate_error=error))
    assert result["status"] == "incomplete"
    assert result["checks"]["templateValidation"] == state
    assert "example.invalid" not in json.dumps(result)


@pytest.mark.parametrize(
    "error, state",
    [
        (_client_error("AccessDeniedException", op="DescribeType"), "denied"),
        (_client_error("Throttling", op="DescribeType"), "throttled"),
    ],
)
def test_schema_lookup_failures_are_incomplete(error, state):
    result = validate_template_text(VALID, _client(describe_error=error))
    assert result["status"] == "incomplete"
    assert result["checks"]["registrySchema"] == state
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
        ("Resources: [", "invalid_yaml"),
        ("Parameters: {}\n", "missing_resources_section"),
        ("Resources:\n  A: {Type: X}\n" + "#" * (MAX_TEMPLATE_BYTES + 1), "template_too_large"),
        ("!!python/object/apply:os.system ['true']", "unsupported_tag"),
        ("Resources: &r {A: {Type: X}}\nOutputs: *r\n", "aliases_not_allowed"),
    ],
)
def test_malformed_input_never_calls_aws(template, code):
    client = _client()
    result = validate_template_text(template, client)
    assert result["status"] == "invalid" and result["templateErrors"] == [code]
    assert result["checks"]["syntax"] == "invalid"
    assert result["checks"]["templateValidation"] == "skipped"
    client.validate_template.assert_not_called()
    client.describe_type.assert_not_called()


# ---------------------------------------------------------------------------
# Reviewed product policy
# ---------------------------------------------------------------------------


def test_literal_account_id_is_a_policy_error():
    template = VALID.replace("InstanceType: c6i.large", f"Description: 'arn:aws:iam::{SYNTHETIC_ACCOUNT_ID}:role/x'")
    result = validate_template_text(template, _client())
    assert result["status"] == "invalid"
    assert result["checks"]["productPolicy"] == "invalid"
    assert result["policyFindings"][0]["rule"] == "literal_account_id"
    assert SYNTHETIC_ACCOUNT_ID not in json.dumps(result)


def test_unrestricted_ingress_is_flagged_for_review_without_failing():
    template = VALID + "      InstanceInboundPermissions:\n        - IpRange: 0.0.0.0/0\n          Protocol: UDP\n"
    result = validate_template_text(template, _client())
    assert result["status"] == "valid"
    assert result["policyFindings"] == [
        {
            "rule": "unrestricted_ingress",
            "severity": "review",
            "resource": "Fleet",
            "path": "Properties.InstanceInboundPermissions[0].IpRange",
        }
    ]


def test_deprecated_container_fleet_shape_is_a_policy_error():
    template = (
        "Resources:\n  Old:\n    Type: AWS::GameLift::Fleet\n    Properties:\n"
        "      ContainerGroupsConfiguration: {ContainerGroupDefinitionNames: [x]}\n"
    )
    schema = {"properties": {"ContainerGroupsConfiguration": {"type": "object"}}}
    result = validate_template_text(template, _client(schema=schema))
    assert "deprecated_container_fleet_shape" in {f["rule"] for f in result["policyFindings"]}
    assert result["status"] == "invalid"


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


def test_tool_client_failure_is_sanitized_and_incomplete():
    # Local modules
    from agents.gamelift_specialist import validate_cloudformation_template

    with patch("agents.gamelift_specialist.boto3.client", side_effect=Exception("https://example.invalid")):
        result = validate_cloudformation_template(VALID)
    assert result["status"] == "incomplete"
    assert result["untrusted"] is True and result["applied"] is False
    assert "example.invalid" not in json.dumps(result)


def test_guardrail_placeholder_is_a_policy_error_but_sub_variables_are_not():
    masked = VALID + "      InstanceInboundPermissions:\n        - IpRange: '{IP_ADDRESS}'\n          Protocol: UDP\n"
    result = validate_template_text(masked, _client())
    assert result["status"] == "invalid"
    assert [f["rule"] for f in result["policyFindings"]] == ["masked_placeholder"]

    with_sub = VALID.replace("InstanceType: c6i.large", "Description: !Sub '${AWS::StackName}-{fleet}'")
    assert validate_template_text(with_sub, _client())["policyFindings"] == []


def test_numeric_and_length_bounds_are_checked():
    schema = {
        "properties": {
            "Target": {"type": "number", "minimum": 0, "maximum": 75},
            "Name": {"type": "string", "minLength": 1, "maxLength": 8},
        },
        "additionalProperties": False,
    }
    base = "Resources:\n  P:\n    Type: AWS::GameLift::ContainerFleet\n    Properties:\n"
    ok = validate_template_text(base + "      Target: 70\n      Name: short\n", _client(schema=schema))
    assert ok["status"] == "valid"
    v._schema_cache.clear()
    bad = validate_template_text(base + "      Target: 100\n      Name: much-too-long\n", _client(schema=schema))
    assert bad["status"] == "invalid"
    assert {(p["path"], p["problem"]) for p in bad["problems"]} == {
        ("Target", "value_out_of_range"),
        ("Name", "value_out_of_range"),
    }
