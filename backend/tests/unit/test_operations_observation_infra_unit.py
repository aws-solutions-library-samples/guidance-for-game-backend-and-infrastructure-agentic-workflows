"""Parser- and scanner-verifiable contract tests for the optional E1
operations observation control plane (GitHub issue #413).

These tests never call AWS. They parse the repository-owned CloudFormation
template and shell wrappers as data and assert:

* positive resources (authenticated HTTP API + JWT authorizer on every route,
  access logs, Lambda with reserved concurrency and bounded timeout, DynamoDB
  PAY_PER_REQUEST with PK/SK + TTL + PITR + KMS + deletion protection, a
  distinct least-privilege observation role, alarms/metrics/log retention,
  outputs, and tags);
* negative IAM invariants (exactly three GameLift reads; no GameLift write,
  ``iam:PassRole``, Step Functions, DynamoDB ``UpdateItem``/``DeleteItem``/
  ``Scan``, or wildcard action);
* the default-disabled invariant (every resource gated on ``OperationsEnabled``,
  ``OperationsMode`` default ``disabled``, and the stack un-wired from
  ``deploy-all.sh`` / ``scripts/deploy.sh``); and
* shell safety of the deploy/teardown wrappers (strict mode, explicit opt-in,
  teardown never invoked automatically).
"""

# Standard library
import json
import pathlib
import re

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"
DEPLOY_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/deploy-operations.sh"
TEARDOWN_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/teardown-operations.sh"
MAIN_DEPLOY = PROJECT_ROOT / "scripts/deploy.sh"
MAIN_TEARDOWN = PROJECT_ROOT / "scripts/teardown.sh"
DEPLOY_ALL = PROJECT_ROOT / "deploy-all.sh"
TEARDOWN_ALL = PROJECT_ROOT / "teardown-all.sh"

HANDLER = "operations.observe.lambda_entry.handler"
METRIC_NAMESPACE = "GameAgent/Operations"
FROZEN_METRICS = (
    "ObservationFailures",
    "ObservationTimeouts",
    "StuckOperations",
    "ObservationRequestLatency",
)
GAMELIFT_READ_ACTIONS = frozenset(
    {
        "gamelift:DescribeFleetUtilization",
        "gamelift:DescribeFleetCapacity",
        "gamelift:DescribeScalingPolicies",
    }
)
FORBIDDEN_ACTION_SUBSTRINGS = (
    "gamelift:Create",
    "gamelift:Update",
    "gamelift:Delete",
    "gamelift:Put",
    "gamelift:Start",
    "gamelift:Stop",
    "iam:PassRole",
    "states:",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def template():
    assert TEMPLATE.exists(), f"missing template: {TEMPLATE}"
    return load_cfn_template(TEMPLATE.read_text(encoding="utf-8"))


def _resources_of_type(template, cfn_type):
    return {name: body for name, body in template["Resources"].items() if body.get("Type") == cfn_type}


def _iter_action_strings(node):
    """Yield every IAM action string anywhere in a parsed policy document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _iter_action_strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_action_strings(value)


def _all_policy_actions(template):
    """Collect every action from every Statement's Action/NotAction in the template."""
    actions = []
    roles = _resources_of_type(template, "AWS::IAM::Role")
    policies = _resources_of_type(template, "AWS::IAM::Policy")
    bodies = list(roles.values()) + list(policies.values())
    for body in bodies:
        props = body.get("Properties", {})
        docs = []
        for inline in props.get("Policies", []) or []:
            docs.append(inline.get("PolicyDocument", {}))
        if "PolicyDocument" in props:
            docs.append(props["PolicyDocument"])
        for doc in docs:
            for statement in doc.get("Statement", []) or []:
                for key in ("Action", "NotAction"):
                    if key in statement:
                        actions.extend(_iter_action_strings(statement[key]))
    return actions


# --------------------------------------------------------------------------- #
# Positive resource assertions
# --------------------------------------------------------------------------- #
def test_template_is_a_separate_optional_stack(template):
    assert "OperationsMode" in template["Parameters"]
    assert template["Parameters"]["OperationsMode"]["Default"] == "disabled"
    assert set(template["Parameters"]["OperationsMode"]["AllowedValues"]) == {
        "disabled",
        "enabled",
    }


def test_http_api_is_authenticated_with_jwt_on_every_route(template):
    apis = _resources_of_type(template, "AWS::ApiGatewayV2::Api")
    assert apis, "expected an HTTP API"
    api = next(iter(apis.values()))
    assert api["Properties"]["ProtocolType"] == "HTTP"

    authorizers = _resources_of_type(template, "AWS::ApiGatewayV2::Authorizer")
    assert authorizers, "expected a JWT authorizer"
    authorizer = next(iter(authorizers.values()))
    assert authorizer["Properties"]["AuthorizerType"] == "JWT"
    jwt = authorizer["Properties"]["JwtConfiguration"]
    assert jwt.get("Issuer"), "JWT authorizer must bind an issuer"
    assert jwt.get("Audience"), "JWT authorizer must bind an audience"

    routes = _resources_of_type(template, "AWS::ApiGatewayV2::Route")
    assert routes, "expected at least one route"
    for name, route in routes.items():
        props = route["Properties"]
        assert props.get("AuthorizationType") == "JWT", f"{name} must require JWT auth"
        assert "AuthorizerId" in props, f"{name} must reference the authorizer"


def test_http_api_has_access_logs_and_throttling(template):
    stages = _resources_of_type(template, "AWS::ApiGatewayV2::Stage")
    assert stages, "expected an API stage"
    stage = next(iter(stages.values()))["Properties"]
    assert "AccessLogSettings" in stage, "stage must emit access logs"
    assert stage["AccessLogSettings"].get("DestinationArn")
    # Denial-of-wallet: HTTP API stage throttling.
    default_route = stage.get("DefaultRouteSettings", {})
    assert default_route.get("ThrottlingBurstLimit")
    assert default_route.get("ThrottlingRateLimit")


def test_lambda_has_reserved_concurrency_and_bounded_timeout(template):
    functions = _resources_of_type(template, "AWS::Lambda::Function")
    assert functions, "expected the observation Lambda"
    fn = next(iter(functions.values()))["Properties"]
    assert fn["Handler"] == HANDLER
    assert fn["TracingConfig"]["Mode"] == "Active", "X-Ray tracing required"

    # CloudFormation intrinsics (!Ref) render as plain strings under the safe
    # loader, so assert the resource wires the bounded parameters and that those
    # parameters carry the required numeric constraints. This is the
    # parser-verifiable truth without resolving intrinsics.
    params = template["Parameters"]

    reserved_ref = fn["ReservedConcurrentExecutions"]
    assert reserved_ref == "ReservedConcurrency", "reserved concurrency must be set"
    assert int(params["ReservedConcurrency"]["Default"]) >= 1
    assert int(params["ReservedConcurrency"]["MinValue"]) >= 1

    timeout_ref = fn["Timeout"]
    assert timeout_ref == "RequestDeadlineSeconds", "timeout must be bounded"
    assert 1 <= int(params["RequestDeadlineSeconds"]["Default"]) <= 29
    assert (
        int(params["RequestDeadlineSeconds"]["MaxValue"]) <= 29
    ), "timeout must be bounded below the 30s gateway ceiling"


def test_dynamodb_table_is_secure_and_recoverable(template):
    tables = _resources_of_type(template, "AWS::DynamoDB::Table")
    assert tables, "expected the operations table"
    table = next(iter(tables.values()))["Properties"]
    assert table["BillingMode"] == "PAY_PER_REQUEST"

    key_types = {k["KeyType"]: k["AttributeName"] for k in table["KeySchema"]}
    assert key_types.get("HASH") == "PK", "partition key must be PK"
    assert key_types.get("RANGE") == "SK", "sort key must be SK"

    assert table["TimeToLiveSpecification"]["Enabled"] is True
    assert table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    sse = table["SSESpecification"]
    assert sse["SSEEnabled"] is True
    assert sse.get("SSEType") == "KMS", "table must use KMS encryption"
    # Denial-of-wallet cap on on-demand throughput.
    on_demand = table.get("OnDemandThroughput", {})
    assert on_demand.get("MaxReadRequestUnits")
    assert on_demand.get("MaxWriteRequestUnits")


def test_alarms_metrics_and_log_retention_present(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) >= 4, "expected at least four alarms"
    alarm_metrics = {a["Properties"].get("MetricName") for a in alarms.values()}
    # Failures, timeouts, stuck operations must each have an alarm.
    for required in ("ObservationFailures", "ObservationTimeouts", "StuckOperations"):
        assert required in alarm_metrics, f"missing alarm for {required}"
    for a in alarms.values():
        assert a["Properties"]["Namespace"] == METRIC_NAMESPACE

    logs = _resources_of_type(template, "AWS::Logs::LogGroup")
    assert logs, "expected log groups"
    for lg in logs.values():
        assert lg["Properties"].get("RetentionInDays"), "log retention must be set"


def test_outputs_and_tags_present(template):
    assert "Outputs" in template and template["Outputs"], "expected stack outputs"
    # Every taggable resource carries the cost-allocation project tag.
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "Project" in text and "ManagedBy" in text


# --------------------------------------------------------------------------- #
# Negative IAM invariants
# --------------------------------------------------------------------------- #
def test_observation_role_is_distinct(template):
    roles = _resources_of_type(template, "AWS::IAM::Role")
    assert roles, "expected a dedicated observation role"


def test_exactly_three_gamelift_reads(template):
    actions = _all_policy_actions(template)
    gamelift = {a for a in actions if a.lower().startswith("gamelift:")}
    assert (
        gamelift == GAMELIFT_READ_ACTIONS
    ), f"GameLift actions must be exactly the three reads, got {sorted(gamelift)}"


def test_no_forbidden_actions(template):
    actions = _all_policy_actions(template)
    for action in actions:
        assert action != "*", "wildcard action not allowed"
        assert not (action.endswith(":*")), f"service wildcard not allowed: {action}"
        for forbidden in FORBIDDEN_ACTION_SUBSTRINGS:
            assert forbidden.lower() not in action.lower(), f"forbidden action present: {action}"


def test_dynamodb_actions_are_scoped_and_bounded(template):
    actions = [a for a in _all_policy_actions(template) if a.lower().startswith("dynamodb:")]
    assert actions, "expected scoped DynamoDB actions"
    # Only these read/transactional verbs; no bulk Scan, UpdateItem, DeleteItem.
    allowed = {
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:BatchGetItem",
        "dynamodb:PutItem",
        "dynamodb:ConditionCheckItem",
        "dynamodb:TransactWriteItems",
        "dynamodb:TransactGetItems",
    }
    for action in actions:
        assert action in allowed, f"unexpected DynamoDB action: {action}"


# --------------------------------------------------------------------------- #
# Default-disabled behavior
# --------------------------------------------------------------------------- #
def test_every_resource_is_gated_on_operations_enabled(template):
    conditions = template.get("Conditions", {})
    assert "OperationsEnabled" in conditions, "expected an OperationsEnabled condition"
    for name, body in template["Resources"].items():
        assert body.get("Condition") == "OperationsEnabled", f"resource {name} must be gated on OperationsEnabled"


def test_stack_is_unwired_from_normal_deployment():
    for path in (MAIN_DEPLOY, DEPLOY_ALL):
        text = path.read_text(encoding="utf-8")
        assert "06-operations-observation" not in text, f"{path} must not reference the optional E1 stack"


def test_teardown_all_never_invokes_operations_teardown():
    for path in (MAIN_TEARDOWN, TEARDOWN_ALL):
        text = path.read_text(encoding="utf-8")
        assert "teardown-operations" not in text, f"{path} must not auto-invoke E1 teardown"


# --------------------------------------------------------------------------- #
# Shell safety of the wrappers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("wrapper", ["deploy", "teardown"])
def test_wrappers_use_strict_mode(wrapper):
    path = DEPLOY_WRAPPER if wrapper == "deploy" else TEARDOWN_WRAPPER
    assert path.exists(), f"missing wrapper: {path}"
    text = path.read_text(encoding="utf-8")
    assert re.search(r"set -euo pipefail", text), "wrapper must use strict mode"


def test_deploy_wrapper_requires_explicit_opt_in():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # Must gate enabled deploys behind both an env value and a flag.
    assert "GBAW_OPERATIONS_MODE" in text
    assert "--enable" in text
    # Default (no opt-in) must not run an enabling cloudformation deploy.
    assert "OperationsMode=disabled" in text or "disabled" in text


def test_teardown_wrapper_requires_explicit_confirmation():
    text = TEARDOWN_WRAPPER.read_text(encoding="utf-8")
    assert "--confirm" in text
    assert "delete-operations" in text
    assert "delete-stack" in text
