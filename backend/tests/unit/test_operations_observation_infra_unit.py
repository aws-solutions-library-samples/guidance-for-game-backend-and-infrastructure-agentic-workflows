"""Parser- and scanner-verifiable contract tests for the optional E1
operations observation control plane (GitHub issue #413).

These tests never call AWS. They parse the repository-owned CloudFormation
template and shell wrappers as data and assert the **frozen E1 runtime
contract**:

* positive resources (authenticated HTTP API + JWT authorizer on every route,
  access logs, Lambda with reserved concurrency and bounded timeout, DynamoDB
  PAY_PER_REQUEST with PK/SK + ``ttl`` TTL + PITR + KMS + deletion protection, a
  distinct least-privilege observation role, alarms/metrics/log retention,
  outputs, and tags);
* the frozen handler, ``OperationsMode`` vocabulary (``disabled``/``observe``),
  the exact Lambda environment bindings (the four frozen ``_S`` budget/TTL names
  the core settings module reads), the ``ttl`` TTL attribute, the frozen metric
  names, the ExtendedStatistic p99 latency alarm, the multi-tenant and
  code-artifact parameters with enabled-mode ``Rules`` validation, and the
  CloudWatch Logs KMS key-policy grant;
* negative IAM invariants (exactly three GameLift reads; DynamoDB limited to
  exactly ``PutItem``/``UpdateItem``/``GetItem`` -- the underlying actions the
  deployed E1 ``operations.observation_store.DynamoDbObservationStore`` requires
  for its conditional Put legs, fenced Update legs, and consistent GetItem reads,
  per the AWS "Using IAM with DynamoDB transactions" guide; no ineffective/
  invalid ``TransactWriteItems`` action, no unused ``Query``/``Scan``/
  ``DeleteItem``/``Batch*``/``ConditionCheckItem``; no S3 runtime access, no
  GameLift write, ``iam:PassRole``, Step Functions, or wildcard action);
* the runtime-KMS grant for the customer-managed key: exactly the documented
  DynamoDB data-plane action set (``Encrypt``/``Decrypt``/``ReEncrypt*``/
  ``GenerateDataKey*``/``DescribeKey``) on the specific operations CMK, every
  statement pinned to DynamoDB via ``kms:ViaService`` (``dynamodb.*.amazonaws.com``),
  with ``kms:CreateGrant`` isolated in its own statement guarded by
  ``kms:GrantIsForAWSResource=true`` -- never a wildcard resource, never generic
  direct KMS use, and never a missing action (the live begin_observation
  ``AccessDeniedException`` from a grant that held only Decrypt+GenerateDataKey);
* the removal of the unused S3 content bucket, its env binding, and its output;
* the removal of the unused request-deadline env var (it survives only as the
  Lambda ``Timeout`` parameter, never as a runtime env binding);
* the removal of the infra-owned placeholder ``operations/observe`` package so
  core's real handler is the only owner;
* the packaging contract (deterministic cross-platform Linux/x86_64 install of
  every transitive runtime dependency pinned from the repository lock, never
  host-architecture native wheels, plus a clean import probe);
* the artifact-bucket contract (an explicit, pre-existing bucket is required and
  verified; no discovery, no bucket creation);
* the default-disabled invariant; and
* shell safety and explicit-profile propagation of the deploy/teardown wrappers.
"""

# Standard library
import json
import pathlib
import re

# Third-party packages
import pytest
from _combined_tree import (
    HANDLER_DOTTED,
    HANDLER_IMPORT,
    HANDLER_REL,
    STORE_MODULE_REL,
    materialize_combined_operations_tree,
    module_defines_top_level_handler,
    observe_placeholder_seam_hits,
    resolve_deployed_store_src,
)

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
BACKEND_SRC = PROJECT_ROOT / "backend/src"

HANDLER = "operations.observe.lambda_entry.handler"
METRIC_NAMESPACE = "GameAgent/Operations"
FROZEN_METRICS = (
    "ObservationFailures",
    "ObservationTimeouts",
    "StuckOperations",
    "ObservationRequestLatency",
)
# Exact Lambda environment bindings frozen for E1. These budget/TTL names are
# the *frozen* ``_S`` names the core settings module (resolve_operations_settings)
# actually reads; the infra must inject exactly these so operator-set knobs take
# runtime effect. The content bucket binding is intentionally NOT here (removed
# with the unused S3 content bucket), and neither is a request-deadline env var
# (core derives the deadline from the sub-budgets + margin).
REQUIRED_ENV_KEYS = frozenset(
    {
        "GBAW_OPERATIONS_MODE",
        "GBAW_OPERATIONS_TABLE_NAME",
        "GBAW_OPERATIONS_METRIC_NAMESPACE",
        "GBAW_OPERATIONS_TENANT_ID",
        "GBAW_OPERATIONS_WORKSPACE_ID",
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE",
        # The four frozen budget/TTL variables the core contract reads.
        "GBAW_OPERATIONS_PER_READ_BUDGET_S",
        "GBAW_OPERATIONS_PERSISTENCE_BUDGET_S",
        "GBAW_OPERATIONS_CANCELLATION_MARGIN_S",
        "GBAW_OPERATIONS_OBSERVATION_TTL_S",
    }
)
# The unused content bucket binding and the unused request-deadline env var must
# never appear in the runtime environment. The old ``_SECONDS`` budget names are
# also forbidden: they diverged from the core contract and were silently ignored.
FORBIDDEN_ENV_KEYS = frozenset(
    {
        "GBAW_OPERATIONS_CONTENT_BUCKET",
        "GBAW_OPERATIONS_REQUEST_DEADLINE_SECONDS",
        "GBAW_OPERATIONS_PROVIDER_READ_BUDGET_SECONDS",
        "GBAW_OPERATIONS_PERSISTENCE_BUDGET_SECONDS",
        "GBAW_OPERATIONS_RECORD_TTL_SECONDS",
    }
)
GAMELIFT_READ_ACTIONS = frozenset(
    {
        "gamelift:DescribeFleetUtilization",
        "gamelift:DescribeFleetCapacity",
        "gamelift:DescribeScalingPolicies",
    }
)
# DynamoDB runtime actions are limited to exactly what the store actually issues.
# The deployed E1 store is operations.observation_store.DynamoDbObservationStore
# (NOT the E0 operations.validation.e0_persistence sink -- that persistence sink
# is a separate E0 concern and is deliberately never imported for this runtime
# IAM invariant). The store issues, across its lifecycle, TransactWriteItems
# calls whose legs are conditional *Put* legs AND fenced *Update* legs, plus
# strongly-consistent GetItem reads (idempotency/replay resolution, result
# loads, and status). Per the AWS "Using IAM with DynamoDB transactions" guide,
# permissions for the Put/Update/Delete/Get legs of a TransactWriteItems call are
# governed by the underlying PutItem/UpdateItem/DeleteItem/GetItem permissions --
# there is no "dynamodb:TransactWriteItems" IAM action (cfn-lint flags it as
# W3037). So the exact underlying action set the role needs is PutItem (Put
# legs), UpdateItem (fenced snapshot Update legs), and GetItem (the consistent
# reads), each scoped to the exact table ARN. No Query, Scan, DeleteItem,
# Batch*, or ConditionCheckItem is ever issued.
#   https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html
ALLOWED_DYNAMODB_ACTIONS = frozenset(
    {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
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
    # The store issues conditional Put legs, fenced Update legs, and consistent
    # GetItem reads -- so PutItem/UpdateItem/GetItem are the ALLOWED set (above)
    # and are deliberately absent here. Every other DynamoDB action is unused by
    # any real call and MUST NOT be granted -- including the ineffective/invalid
    # TransactWriteItems (no such IAM action; cfn-lint W3037) and the always-
    # denied Query/Scan/DeleteItem/Batch*/ConditionCheckItem.
    "dynamodb:Query",
    "dynamodb:TransactWriteItems",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
    "dynamodb:BatchWriteItem",
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
    # The unused content bucket and its runtime access are removed entirely.
    "s3:",
    # NOTE: kms:Encrypt is intentionally NOT forbidden on the runtime role. A
    # DynamoDB table encrypted with a customer managed key requires the *caller*
    # to hold the full data-plane KMS action set (Encrypt/Decrypt/ReEncrypt*/
    # GenerateDataKey*/DescribeKey) plus a resource-scoped CreateGrant; a role
    # missing kms:Encrypt is exactly what produced the live begin_observation
    # DynamoDB AccessDeniedException (GitHub issue #413). See the runtime-KMS
    # contract tests below for the tightly bounded shape this grant must take.
)

# The exact identity-based KMS actions a principal needs on a customer-managed
# key to read from and write to a DynamoDB table encrypted with that key, per
# the AWS "DynamoDB encryption at rest usage notes" guide
# (https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html).
# kms:Encrypt was the action missing from the live runtime role, which is what
# failed begin_observation with a DynamoDB AccessDeniedException even though the
# IAM policy simulator allowed TransactWriteItems/GetItem.
REQUIRED_RUNTIME_KMS_DATA_ACTIONS = frozenset(
    {
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:DescribeKey",
    }
)
# CreateGrant is required but MUST live in its own statement, constrained so the
# runtime can only create grants on behalf of the AWS resource (DynamoDB), never
# arbitrary grants. kms:GrantIsForAWSResource=true is the documented guard, per
# the AWS KMS condition-keys guide
# (https://docs.aws.amazon.com/kms/latest/developerguide/conditions-kms.html).
RUNTIME_KMS_CREATE_GRANT_ACTION = "kms:CreateGrant"
# Every runtime-role KMS statement must be pinned to DynamoDB via kms:ViaService
# so the key can never be used for direct, generic KMS calls by the runtime.
DYNAMODB_VIA_SERVICE_PATTERN = "dynamodb."

# The transitive runtime-dependency closure the core handler imports at load
# time (rfc8785 for canonical JSON; jsonschema + referencing for contract
# validation) and their own dependencies. Every one must be pinned in the
# package at the version frozen in the repository lock (backend/uv.lock).
PINNED_RUNTIME_DEPS = {
    "rfc8785": "0.1.4",
    "jsonschema": "4.26.0",
    "jsonschema-specifications": "2025.9.1",
    "referencing": "0.36.2",
    "attrs": "25.4.0",
    "rpds-py": "2026.5.1",
}
# The one native (non-pure-python) dependency: its wheels are platform-specific,
# so the package MUST carry a Linux/x86_64 manylinux wheel, never the host wheel.
NATIVE_DEP = "rpds-py"


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


def _observation_function(template):
    functions = _resources_of_type(template, "AWS::Lambda::Function")
    assert functions, "expected the observation Lambda"
    return next(iter(functions.values()))["Properties"]


# --------------------------------------------------------------------------- #
# Frozen mode vocabulary and parameters
# --------------------------------------------------------------------------- #
def test_operations_mode_vocabulary_is_disabled_or_observe(template):
    """E1 froze disabled|observe; E2 (issue #414) ADDITIVELY widens the
    vocabulary to disabled|observe|advise. The default stays the fail-closed
    ``disabled`` and ``observe`` is retained unchanged."""
    mode = template["Parameters"]["OperationsMode"]
    assert mode["Default"] == "disabled"
    allowed = set(mode["AllowedValues"])
    assert {"disabled", "observe"} <= allowed, "disabled and observe must be retained"
    assert allowed <= {"disabled", "observe", "advise"}, "E2 widens the vocabulary only to advise"


def test_multi_tenant_and_code_artifact_parameters_present(template):
    params = template["Parameters"]
    for required in ("TenantId", "WorkspaceId", "CodeS3Bucket", "CodeS3Key"):
        assert required in params, f"missing parameter {required}"


def test_budget_and_ttl_parameters_are_validated_and_present(template):
    """Each frozen budget/TTL env var must be backed by a validated CFN
    parameter so operators tune real, bounded runtime knobs."""
    params = template["Parameters"]
    for name in ("PerReadBudgetSeconds", "PersistenceBudgetSeconds", "CancellationMarginSeconds"):
        assert name in params, f"missing budget parameter {name}"
        p = params[name]
        assert p["Type"] == "Number"
        assert int(p["MinValue"]) >= 1
        assert int(p["MaxValue"]) <= 29, f"{name} must be bounded below the 30s gateway ceiling"
    ttl = params["ObservationTtlSeconds"]
    assert ttl["Type"] == "Number"
    assert int(ttl["MinValue"]) >= 1


def test_unused_request_deadline_env_parameter_absent_from_env(template):
    """RequestDeadlineSeconds survives only as the Lambda Timeout; core derives
    the deadline from the sub-budgets + margin, so no deadline env is injected."""
    fn = _observation_function(template)
    env = fn["Environment"]["Variables"]
    assert "GBAW_OPERATIONS_REQUEST_DEADLINE_SECONDS" not in env


def test_enabled_mode_validation_rules_present(template):
    """When OperationsMode=observe, the enabling inputs must be validated so an
    enabled deploy cannot proceed with empty issuer/audience/tenant/code."""
    rules = template.get("Rules", {})
    assert rules, "expected a Rules block enforcing enabled-mode inputs"
    text = TEMPLATE.read_text(encoding="utf-8")
    # The rule must key off observe mode and assert the critical inputs.
    assert "observe" in text
    for referenced in ("CognitoIssuer", "CognitoClientId", "TenantId", "WorkspaceId", "CodeS3Bucket", "CodeS3Key"):
        assert referenced in text, f"enabled-mode validation must reference {referenced}"


# --------------------------------------------------------------------------- #
# Positive resource assertions
# --------------------------------------------------------------------------- #
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


def test_status_route_is_the_frozen_operation_id_path(template):
    """Status is read at the frozen path parameter route.

    The status route key must be exactly ``GET /operations/{operationId}``.
    """
    routes = _resources_of_type(template, "AWS::ApiGatewayV2::Route")
    route_keys = {r["Properties"].get("RouteKey") for r in routes.values()}
    assert "GET /operations/{operationId}" in route_keys, f"status route must be the frozen path, got {route_keys}"


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


# --------------------------------------------------------------------------- #
# Access-log DestinationArn drift contract (issue 413).
#
# ``!GetAtt <LogGroup>.Arn`` returns a CloudWatch Logs log-group ARN with a
# trailing ``:*`` (the log-stream wildcard). API Gateway normalizes the value
# it stores on the stage's AccessLogSettings.DestinationArn to the bare
# log-group ARN *without* ``:*``. CloudFormation then compares the template's
# GetAtt form (``...:*``) against the API's stored form (no ``:*``) on every
# drift-detection run and reports the stage perpetually MODIFIED — pure noise
# that masks real drift. The fix supplies the exact ARN API Gateway keeps, so
# the rendered template value is byte-identical to the live value.
#
# The parsed template represents ``!GetAtt AccessLogGroup.Arn`` as the scalar
# string "AccessLogGroup.Arn" and ``!Sub 'arn:...'`` as its literal scalar
# (see backend/tests/_cfn_yaml.py), which is exactly what these tests inspect.
# --------------------------------------------------------------------------- #

# The bare log-group ARN API Gateway stores for the access log destination:
# partition/region/account are CloudFormation pseudo-parameters and the name is
# the same one AccessLogGroup declares. Crucially there is NO trailing ":*".
_EXPECTED_ACCESS_LOG_DESTINATION_ARN = (
    "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}"
    ":log-group:/aws/apigateway/${ProjectName}-operations-access"
)


def _access_log_group_name(template):
    """The literal LogGroupName the AccessLogGroup resource declares."""
    groups = _resources_of_type(template, "AWS::Logs::LogGroup")
    for body in groups.values():
        name = body.get("Properties", {}).get("LogGroupName", "")
        if "apigateway" in name and "operations-access" in name:
            return name
    raise AssertionError("AccessLogGroup with an apigateway operations-access name not found")


def test_access_log_destination_arn_matches_api_gateway_stored_form(template):
    """RED-GREEN drift guard: the stage's DestinationArn must be the exact bare
    log-group ARN API Gateway stores (no ``:*``), so live drift detection sees
    the rendered value and the API-normalized value as identical."""
    stage = next(iter(_resources_of_type(template, "AWS::ApiGatewayV2::Stage").values()))["Properties"]
    destination = stage["AccessLogSettings"]["DestinationArn"]
    assert destination == _EXPECTED_ACCESS_LOG_DESTINATION_ARN, (
        "AccessLogSettings.DestinationArn must be the bare log-group ARN API " f"Gateway stores, got {destination!r}"
    )
    # The constructed ARN must end in the exact log-group name the AccessLogGroup
    # resource declares, tying the two together so a rename cannot silently drift.
    assert destination.endswith(
        _access_log_group_name(template)
    ), "DestinationArn must be built from the AccessLogGroup's own LogGroupName"


def test_access_log_destination_arn_is_not_the_getatt_wildcard_form(template):
    """The GetAtt Arn form (which resolves to a ``:*``-suffixed ARN) is exactly
    what causes the perpetual-drift report and must never come back."""
    stage = next(iter(_resources_of_type(template, "AWS::ApiGatewayV2::Stage").values()))["Properties"]
    destination = stage["AccessLogSettings"]["DestinationArn"]
    # A short-form ``!GetAtt AccessLogGroup.Arn`` parses to this bare scalar.
    assert destination != "AccessLogGroup.Arn", (
        "DestinationArn must not use !GetAtt AccessLogGroup.Arn — it renders a "
        "log-group ARN ending in ':*' that API Gateway strips, causing drift"
    )
    # Guard both the parsed short form and any literal that still ends in ':*'.
    assert not destination.endswith(":*"), (
        "DestinationArn must not end in ':*'; API Gateway stores it without the "
        "log-stream wildcard, so a ':*' form drifts on every detection run"
    )
    assert "Fn::GetAtt" not in destination, "DestinationArn must not be a GetAtt intrinsic"


def test_api_stage_retains_access_log_group_dependency_and_policy(template):
    """Switching DestinationArn off ``!GetAtt`` drops the implicit dependency on
    the log group, so the stage must keep an explicit one, and the vended-log
    delivery resource policy must still be present."""
    stages = _resources_of_type(template, "AWS::ApiGatewayV2::Stage")
    stage_name, stage_body = next(iter(stages.items()))
    depends_on = stage_body.get("DependsOn", [])
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    assert "AccessLogGroup" in depends_on, (
        f"{stage_name} must DependsOn AccessLogGroup so the log group exists "
        "before the stage references it by constructed ARN"
    )
    # The access-log delivery resource policy must survive the change.
    policies = _resources_of_type(template, "AWS::Logs::ResourcePolicy")
    assert policies, "the CloudWatch Logs access-log delivery resource policy must remain"
    policy_text = "".join(str(b.get("Properties", {}).get("PolicyDocument", "")) for b in policies.values())
    assert (
        "/aws/apigateway/" in policy_text and "operations-access" in policy_text
    ), "delivery resource policy must still scope the access log group"


def test_lambda_has_reserved_concurrency_and_bounded_timeout(template):
    fn = _observation_function(template)
    assert fn["Handler"] == HANDLER
    assert fn["TracingConfig"]["Mode"] == "Active", "X-Ray tracing required"

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


def test_lambda_runtime_is_python313_x86_64(template):
    fn = _observation_function(template)
    assert fn["Runtime"] == "python3.13", "runtime must be the frozen Python 3.13"
    assert fn.get("Architectures") == ["x86_64"], "architecture must be x86_64"


def test_lambda_code_points_to_s3_artifact_not_inline_placeholder(template):
    """No enabled route may point at placeholder code: the function loads a real
    packaged artifact from S3 (Code.S3Bucket / Code.S3Key), never an inline
    ZipFile shim."""
    fn = _observation_function(template)
    code = fn.get("Code", {})
    assert "ZipFile" not in code, "inline placeholder code is forbidden for an enabled route"
    assert code.get("S3Bucket") == "CodeS3Bucket", "Code.S3Bucket must come from the CodeS3Bucket parameter"
    assert code.get("S3Key") == "CodeS3Key", "Code.S3Key must come from the CodeS3Key parameter"
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "placeholder" not in text.lower(), "no placeholder code path may remain"


def test_lambda_environment_bindings_are_exact(template):
    fn = _observation_function(template)
    env = fn["Environment"]["Variables"]
    present = set(env.keys())
    missing = REQUIRED_ENV_KEYS - present
    assert not missing, f"missing frozen env bindings: {sorted(missing)}"
    forbidden = FORBIDDEN_ENV_KEYS & present
    assert not forbidden, f"forbidden env bindings present: {sorted(forbidden)}"
    assert env["GBAW_OPERATIONS_METRIC_NAMESPACE"] == METRIC_NAMESPACE


def test_dynamodb_table_is_secure_and_recoverable(template):
    tables = _resources_of_type(template, "AWS::DynamoDB::Table")
    assert tables, "expected the operations table"
    table = next(iter(tables.values()))["Properties"]
    assert table["BillingMode"] == "PAY_PER_REQUEST"

    key_types = {k["KeyType"]: k["AttributeName"] for k in table["KeySchema"]}
    assert key_types.get("HASH") == "PK", "partition key must be PK"
    assert key_types.get("RANGE") == "SK", "sort key must be SK"

    ttl = table["TimeToLiveSpecification"]
    assert ttl["Enabled"] is True
    assert ttl["AttributeName"] == "ttl", "frozen TTL attribute must be 'ttl'"
    assert table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    sse = table["SSESpecification"]
    assert sse["SSEEnabled"] is True
    assert sse.get("SSEType") == "KMS", "table must use KMS encryption"
    on_demand = table.get("OnDemandThroughput", {})
    assert on_demand.get("MaxReadRequestUnits")
    assert on_demand.get("MaxWriteRequestUnits")


def test_no_s3_content_bucket_resource(template):
    buckets = _resources_of_type(template, "AWS::S3::Bucket")
    assert not buckets, "the unused E1 content bucket must be removed"


def test_alarms_metrics_and_log_retention_present(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) >= 4, "expected at least four alarms"
    alarm_metrics = {a["Properties"].get("MetricName") for a in alarms.values()}
    for required in ("ObservationFailures", "ObservationTimeouts", "StuckOperations"):
        assert required in alarm_metrics, f"missing alarm for {required}"
    for a in alarms.values():
        assert a["Properties"]["Namespace"] == METRIC_NAMESPACE

    logs = _resources_of_type(template, "AWS::Logs::LogGroup")
    assert logs, "expected log groups"
    for lg in logs.values():
        assert lg["Properties"].get("RetentionInDays"), "log retention must be set"


def test_latency_alarm_uses_extended_statistic_p99(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    latency = [
        a["Properties"] for a in alarms.values() if a["Properties"].get("MetricName") == "ObservationRequestLatency"
    ]
    assert latency, "expected a latency alarm"
    props = latency[0]
    # p99 is a percentile: it must be expressed as ExtendedStatistic, not the
    # invalid Statistic: p99.
    assert "Statistic" not in props, "percentile must not use the Statistic field"
    assert props.get("ExtendedStatistic") == "p99", "latency alarm must use ExtendedStatistic p99"


def test_cloudwatch_logs_kms_key_policy_present(template):
    """The CMK encrypts the Lambda and API access log groups, so its key policy
    must grant the CloudWatch Logs service principal encrypt/decrypt on the key,
    scoped to this account's log groups."""
    keys = _resources_of_type(template, "AWS::KMS::Key")
    assert keys, "expected a CMK"
    key = next(iter(keys.values()))["Properties"]
    statements = key["KeyPolicy"]["Statement"]

    def _principal_services(stmt):
        principal = stmt.get("Principal", {})
        service = principal.get("Service")
        if isinstance(service, str):
            return [service]
        if isinstance(service, list):
            return service
        return []

    logs_statements = [s for s in statements if any("logs." in svc for svc in _principal_services(s))]
    assert logs_statements, "KMS key policy must grant the CloudWatch Logs service principal"
    granted = set(_iter_action_strings([s.get("Action") for s in logs_statements]))
    assert any(a.startswith("kms:Decrypt") for a in granted)
    assert any(a.startswith("kms:GenerateDataKey") for a in granted)


def test_outputs_and_tags_present(template):
    assert "Outputs" in template and template["Outputs"], "expected stack outputs"
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "Project" in text and "ManagedBy" in text


def test_no_content_bucket_output(template):
    outputs = template.get("Outputs", {})
    for name in outputs:
        assert "ContentBucket" not in name, "content bucket output must be removed"


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
    for action in actions:
        assert action in ALLOWED_DYNAMODB_ACTIONS, f"unexpected DynamoDB action: {action}"


# The store leg type -> the underlying DynamoDB IAM action that governs it, per
# the AWS "Using IAM with DynamoDB transactions" guide: transactional Put/Update/
# Delete/Get are authorized by PutItem/UpdateItem/DeleteItem/GetItem, and a
# ConditionCheck leg by dynamodb:ConditionCheckItem. There is deliberately NO
# mapping for a "TransactWriteItems" action because none exists in IAM.
#   https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html
_TRANSACT_LEG_TO_UNDERLYING_ACTION = {
    "Put": "dynamodb:PutItem",
    "Update": "dynamodb:UpdateItem",
    "Delete": "dynamodb:DeleteItem",
    "Get": "dynamodb:GetItem",
    "ConditionCheck": "dynamodb:ConditionCheckItem",
}

# A GetItem read is not a transaction leg, so it maps directly to the item action
# that governs it (dynamodb:GetItem). The store's consistent reads
# (idempotency/replay resolution, result loads, status) go through this call.
_DIRECT_CALL_TO_UNDERLYING_ACTION = {
    "get_item": "dynamodb:GetItem",
}


# The store-drive derivation runs in a subprocess with the deployed store's
# ``backend/src`` FIRST on PYTHONPATH. The store src is resolved for the current
# checkout (see ``resolve_deployed_store_src``): this branch's own ``backend/src``
# ships the E1 store (operations.observation_store.DynamoDbObservationStore) and
# its whole dependency subtree (operations.observation, operations.contracts.*,
# operations.settings, operations.identity, operations.validation.*), so a normal
# single combined checkout / CI resolves it with no sibling directories; a sibling
# core worktree is only a fallback. Other tests in this suite import the
# ``operations`` package in-process first -- binding it in ``sys.modules`` -- so a
# subprocess with a clean interpreter is the faithful, order-independent way to
# import and drive the real deployed module tree with no module mixing. It drives
# the store through begin, a conditional replay/get, a stale-lease reclaim
# (Update+Put), complete (Put+Update), fail (Update+Put), and status (Get) with a
# leg-capturing fake that raises real botocore ``ClientError`` shapes, then prints
# the derived {leg types, direct calls} as JSON.
_STORE_DRIVE_SCRIPT = r"""
import datetime as dt
import json

from botocore.exceptions import ClientError

from operations.observation_store import (
    _IDEM_SK,
    _RESULT_SK,
    _STATE_SNAPSHOT_SK,
    DynamoDbObservationStore,
)


def make_transaction_canceled_error():
    # A real botocore ClientError with the exact wire shape a genuine, pure
    # ConditionalCheckFailed transaction cancel carries: Error.Code is
    # TransactionCanceledException and CancellationReasons is the positional list
    # the store's classifier reads from exc.response (never a fabricated
    # cancellation_reasons attribute). This routes the store to its idempotency/
    # state-resolution (replay/reclaim) branches so those legs and reads are
    # observable.
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "conditional check failed"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
                {"Code": "None"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )


class LegCapturingObservationClient:
    # Records every underlying action the real store issues -- TransactWriteItems
    # legs (by type) and direct get_item reads -- with no I/O. transact_write_items
    # optionally raises a real conditional ClientError (so the store follows its
    # replay/reclaim branches); get_item returns scripted marshalled items so the
    # resolve/status read paths are reached. An unscripted call (query/scan/delete/
    # batch) raises AttributeError and fails the drift guard loudly.
    def __init__(self):
        self.transact_leg_types = set()
        self.direct_calls = set()
        self._transact_raises = []
        self._get_item_responses = []

    def script_transact(self, raises):
        self._transact_raises.append(raises)

    def script_get_item(self, item):
        self._get_item_responses.append(item)

    def transact_write_items(self, **kwargs):
        for leg in kwargs["TransactItems"]:
            leg_types = list(leg.keys())
            assert len(leg_types) == 1, "a transaction leg must have exactly one type: %r" % (leg_types,)
            self.transact_leg_types.add(leg_types[0])
        raises = self._transact_raises.pop(0) if self._transact_raises else False
        if raises:
            raise make_transaction_canceled_error()
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_item(self, **kwargs):
        self.direct_calls.add("get_item")
        item = self._get_item_responses.pop(0) if self._get_item_responses else None
        return {"Item": item} if item is not None else {}


def marshal(item):
    out = {}
    for k, v in item.items():
        if isinstance(v, bool):
            out[k] = {"BOOL": v}
        elif isinstance(v, str):
            out[k] = {"S": v}
        elif isinstance(v, int):
            out[k] = {"N": str(v)}
        elif v is None:
            out[k] = {"NULL": True}
    return out


client = LegCapturingObservationClient()
frozen_now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
store = DynamoDbObservationStore(
    client=client,
    table_name="drift-probe-disposable",
    clock=lambda: frozen_now,
)

future = frozen_now + dt.timedelta(seconds=60)
ttl_epoch = int((frozen_now + dt.timedelta(hours=1)).timestamp())
op_id = "op-drift-probe"
ws = "ws-drift"

# 1) begin: fresh create -> one TransactWriteItems of conditional Put legs.
store.begin_observation(
    operation_id=op_id,
    idempotency_fingerprint="fp-1",
    workspace_id=ws,
    idempotency_token="tok-1",
    lease_holder="holder-1",
    commit_not_after=future,
    lease_not_after=future,
    ttl_epoch_s=ttl_epoch,
    intent={"kind": "observe"},
)

# 2) begin again: transact fails ConditionalCheck, so _resolve_existing reads
#    (GetItem) the mapping + snapshot + result and replays a SUCCEEDED operation.
idem_mapping = marshal(
    {"PK": "WS#%s#IDEM#tok-1" % ws, "SK": _IDEM_SK, "operation_id": op_id, "idempotency_fingerprint": "fp-1"}
)
succeeded_snapshot = marshal(
    {
        "PK": "OP#%s" % op_id,
        "SK": _STATE_SNAPSHOT_SK,
        "operation_id": op_id,
        "workspace_id": ws,
        "state": "succeeded",
        "sequence": 1,
        "generation": 1,
    }
)
result_item = marshal(
    {
        "PK": "OP#%s" % op_id,
        "SK": _RESULT_SK,
        "operation_id": op_id,
        "observation_json": "{}",
        "observation_hash": "deadbeef",
    }
)
client.script_transact(True)
client.script_get_item(idem_mapping)
client.script_get_item(succeeded_snapshot)
client.script_get_item(result_item)
store.begin_observation(
    operation_id=op_id,
    idempotency_fingerprint="fp-1",
    workspace_id=ws,
    idempotency_token="tok-1",
    lease_holder="holder-1",
    commit_not_after=future,
    lease_not_after=future,
    ttl_epoch_s=ttl_epoch,
    intent={"kind": "observe"},
)

# 3) begin again on a stale-lease OBSERVING op -> fenced reclaim: a second
#    TransactWriteItems of an Update (fencing) + a Put (recovery ledger).
expired_snapshot = marshal(
    {
        "PK": "OP#%s" % op_id,
        "SK": _STATE_SNAPSHOT_SK,
        "operation_id": op_id,
        "workspace_id": ws,
        "state": "observing",
        "sequence": 0,
        "generation": 1,
        "lease_holder": "holder-1",
        "lease_not_after": int(frozen_now.timestamp()) - 1,
        "ttl": ttl_epoch,
    }
)
client.script_transact(True)  # the create attempt collides
client.script_get_item(idem_mapping)  # _resolve_existing: mapping
client.script_get_item(expired_snapshot)  # _resolve_existing: snapshot (stale)
client.script_transact(False)  # the reclaim transaction succeeds
store.begin_observation(
    operation_id=op_id,
    idempotency_fingerprint="fp-1",
    workspace_id=ws,
    idempotency_token="tok-1",
    lease_holder="holder-2",
    commit_not_after=future,
    lease_not_after=future,
    ttl_epoch_s=ttl_epoch,
    intent={"kind": "observe"},
)

# 4) complete: Put(result) + Update(snapshot) + Put(transition) + Put(ledger).
store.complete_observation(
    operation_id=op_id,
    workspace_id=ws,
    lease_holder="holder-1",
    commit_not_after=future,
    ttl_epoch_s=ttl_epoch,
    observation={"ok": 1},
)

# 5) fail: Update(snapshot) + Put(transition) + Put(ledger).
store.fail_observation(
    operation_id=op_id,
    workspace_id=ws,
    lease_holder="holder-1",
    reason_code="drift_probe",
    ttl_epoch_s=ttl_epoch,
)

# 6) load_status: a GetItem read of the snapshot (+ result on succeeded).
client.script_get_item(succeeded_snapshot)
client.script_get_item(result_item)
store.load_status(operation_id=op_id, workspace_id=ws)

print(json.dumps({"legs": sorted(client.transact_leg_types), "calls": sorted(client.direct_calls)}))
"""


def _underlying_actions_the_store_actually_requires():
    """Drive the *real deployed* E1 store through its full lifecycle and derive
    the exact underlying DynamoDB IAM action set it requires.

    Resolves the deployed store's ``backend/src`` for the *current* checkout
    (:func:`resolve_deployed_store_src` -- this branch's own ``backend/src``
    first, a sibling core worktree only as fallback) and runs
    :data:`_STORE_DRIVE_SCRIPT` in a subprocess with that src first on
    ``PYTHONPATH`` so ``operations.observation_store.DynamoDbObservationStore``
    -- the module the CloudFormation Lambda ``Handler`` actually loads -- and its
    whole dependency subtree import cleanly regardless of suite import order and
    without depending on any sibling worktree. The script
    drives begin (fresh create), a conditional-replay begin resolving a stored
    *succeeded* operation, a stale-lease *reclaim* begin, complete, fail, and
    load_status, capturing every ``TransactWriteItems`` leg type and every direct
    ``get_item`` read. Each transaction leg is mapped to its underlying item
    action and each read to ``dynamodb:GetItem``, so the required set is derived
    from observed store behavior. The E0 persistence sink is never imported."""
    return _derive_required_actions_from_store_src(resolve_deployed_store_src(PROJECT_ROOT))


def _derive_required_actions_from_store_src(store_src):
    """Drive the store found under ``store_src`` (a ``backend/src`` directory) and
    return the underlying DynamoDB IAM action set its behavior requires.

    ``store_src`` is put FIRST on the subprocess ``PYTHONPATH`` so the clean
    interpreter imports the deployed ``operations`` package tree from exactly
    that checkout, with no dependence on suite import order or on any sibling
    worktree. A missing store is a hard failure, so the guard can never pass
    vacuously by silently importing nothing."""
    # Standard library
    import os
    import subprocess
    import sys

    assert store_src is not None, (
        "deployed E1 store not found: no backend/src carrying "
        f"{STORE_MODULE_REL} in this checkout or a sibling core worktree"
    )
    assert (store_src / STORE_MODULE_REL).is_file(), f"deployed E1 store not found at {store_src}/{STORE_MODULE_REL}"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(store_src), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    proc = subprocess.run(
        [sys.executable, "-c", _STORE_DRIVE_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"store-drive subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    observed = json.loads(proc.stdout.strip().splitlines()[-1])

    assert observed["legs"], "store issued no transaction legs to inspect"
    assert observed["calls"], "store issued no direct item calls to inspect"
    required = set()
    for leg_type in observed["legs"]:
        assert (
            leg_type in _TRANSACT_LEG_TO_UNDERLYING_ACTION
        ), f"unmapped transaction leg type {leg_type!r}; update the IAM contract"
        required.add(_TRANSACT_LEG_TO_UNDERLYING_ACTION[leg_type])
    for call in observed["calls"]:
        assert call in _DIRECT_CALL_TO_UNDERLYING_ACTION, f"unmapped direct call {call!r}; update the IAM contract"
        required.add(_DIRECT_CALL_TO_UNDERLYING_ACTION[call])
    return required


def test_iam_grants_exactly_the_underlying_actions_the_store_transacts(template):
    """Red-green drift guard tying the template's DynamoDB grant to real store legs.

    The granted DynamoDB actions on the observation role must equal *exactly* the
    set of underlying item actions the deployed E1 store's actual transaction legs
    and reads require -- no unused Query/Scan/Batch, no ineffective/invalid
    TransactWriteItems, and never a *missing* action (PutItem-only was exactly the
    under-grant that would have left the store's Update legs and GetItem reads
    unauthorized). If either the store behavior or the template drift, this fails.
    """
    required = _underlying_actions_the_store_actually_requires()
    # The deployed store issues conditional Put legs, fenced Update legs, and
    # consistent GetItem reads, so the required set is exactly these three. Pin it
    # explicitly so both an accidental widening AND an accidental narrowing of the
    # derivation are caught.
    assert required == {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
    }, f"unexpected store-required actions: {sorted(required)}"

    granted = {a for a in _all_policy_actions(template) if a.lower().startswith("dynamodb:")}
    assert granted == required, (
        "DynamoDB IAM grant must cover exactly the underlying actions the store "
        f"transacts and reads. required={sorted(required)} granted={sorted(granted)}"
    )
    # The invalid action string must be gone (cfn-lint W3037).
    assert "dynamodb:TransactWriteItems" not in granted, (
        "dynamodb:TransactWriteItems is not a valid IAM action (cfn-lint W3037); "
        "transactional legs are governed by the underlying item actions"
    )


def test_store_src_resolves_to_the_current_checkout_without_any_sibling():
    """The deployed store must resolve from *this* checkout's own ``backend/src``.

    Regression guard for the removed hardcoded sibling-worktree dependency: this
    branch already ships the deployed E1 store under its own ``backend/src`` (from
    the E1 base), so :func:`resolve_deployed_store_src` must return that path.
    In a normal single combined checkout / CI there is no sibling ``issue-413-core``
    directory, and the drift guard must still find the store to drive."""
    resolved = resolve_deployed_store_src(PROJECT_ROOT)
    assert resolved is not None, "store src did not resolve from the current checkout"
    assert (
        resolved == PROJECT_ROOT / "backend" / "src"
    ), f"store src must resolve to this checkout's backend/src, got {resolved}"
    assert (resolved / STORE_MODULE_REL).is_file(), f"resolved store src {resolved} does not carry {STORE_MODULE_REL}"


def test_drift_guard_runs_non_vacuously_in_an_isolated_single_checkout(tmp_path):
    """Prove the drift guard still derives the real action set with NO siblings.

    This copies the current checkout's ``backend/src`` (the repo alone) into an
    isolated temp root that has *no* sibling ``issue-413-core`` directory next to
    it -- the shape of a normal single combined checkout / CI -- then drives the
    real deployed store from there. The derivation must run non-vacuously and
    return exactly the underlying action set the store's real transaction legs
    and reads require, proving the guard does not depend on any sibling worktree
    and does not silently pass by importing nothing."""
    # Standard library
    import shutil

    # Materialize the repo alone under an isolated root: <iso>/repo/backend/src,
    # so <iso>/repo has no sibling worktree beside it.
    isolated_repo = tmp_path / "repo"
    src_dst = isolated_repo / "backend" / "src"
    src_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROJECT_ROOT / "backend" / "src", src_dst)

    # Sanity: the isolated root has no sibling issue-413-core to fall back to.
    assert not (
        isolated_repo.parent / "issue-413-core"
    ).exists(), "isolated root must have no sibling core worktree for a faithful single-checkout probe"

    resolved = resolve_deployed_store_src(isolated_repo)
    assert resolved == src_dst, f"isolated resolution must pick the copied src, got {resolved}"

    required = _derive_required_actions_from_store_src(resolved)
    # Non-vacuous: the store's real begin/complete/fail/reclaim/status lifecycle
    # yields exactly conditional Puts, fenced Updates, and consistent GetItem reads.
    assert required == {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
    }, f"drift guard derived an unexpected action set in isolation: {sorted(required)}"


def test_drift_guard_fails_loudly_when_no_store_is_present(tmp_path):
    """The guard must never pass vacuously when the deployed store is absent.

    If neither the current checkout nor any sibling carries the store,
    :func:`resolve_deployed_store_src` returns ``None`` and the derivation must
    raise rather than silently succeed with an empty action set."""
    empty_repo = tmp_path / "empty"
    (empty_repo / "backend" / "src").mkdir(parents=True, exist_ok=True)

    assert resolve_deployed_store_src(empty_repo) is None, "an empty checkout must not resolve a store src"
    with pytest.raises(AssertionError, match="deployed E1 store not found"):
        _derive_required_actions_from_store_src(None)


def test_dynamodb_grant_is_scoped_to_the_exact_operations_table_arn(template):
    """Every DynamoDB statement must target only the operations table ARN."""
    roles = _resources_of_type(template, "AWS::IAM::Role")
    seen = False
    for body in roles.values():
        for inline in body.get("Properties", {}).get("Policies", []) or []:
            for stmt in inline.get("PolicyDocument", {}).get("Statement", []) or []:
                actions = set(_iter_action_strings([stmt.get("Action")]))
                if not actions or not all(a.lower().startswith("dynamodb:") for a in actions):
                    continue
                seen = True
                # The scanner-safe CFN loader renders short-form intrinsics as plain
                # data, so ``!GetAtt OperationsTable.Arn`` arrives as the scalar
                # string "OperationsTable.Arn" (and a long-form Fn::GetAtt as a
                # dict). Accept either shape but require the operations table ARN.
                resources = _resource_strings(stmt)
                assert resources, "DynamoDB statement must be resource-scoped, not '*'"
                for res in resources:
                    assert res != "*", "DynamoDB statement must not use a wildcard resource"
                    if isinstance(res, str):
                        assert (
                            res == "OperationsTable.Arn"
                        ), f"DynamoDB resource must be OperationsTable.Arn, got {res!r}"
                    elif isinstance(res, dict) and "Fn::GetAtt" in res:
                        target = res["Fn::GetAtt"]
                        target = target.split(".") if isinstance(target, str) else target
                        assert (
                            target[0] == "OperationsTable" and target[1] == "Arn"
                        ), f"DynamoDB resource must be OperationsTable.Arn, got {target!r}"
                    else:
                        raise AssertionError(f"DynamoDB resource must reference the operations table ARN, got {res!r}")
    assert seen, "expected at least one DynamoDB IAM statement"


def _role_kms_statements(template):
    """Every IAM *role* inline-policy statement that grants only KMS actions.

    This isolates the runtime execution role's KMS grant (the ``OperationsKmsUse``
    policy on ``ObservationRole``) from the CloudWatch Logs grant, which lives on
    the KMS *key policy* of the ``AWS::KMS::Key`` resource, not on any role. The
    two are asserted by separate tests and must never be conflated.
    """
    statements = []
    roles = _resources_of_type(template, "AWS::IAM::Role")
    for body in roles.values():
        for inline in body.get("Properties", {}).get("Policies", []) or []:
            doc = inline.get("PolicyDocument", {})
            for stmt in doc.get("Statement", []) or []:
                actions = set(_iter_action_strings([stmt.get("Action")]))
                if actions and all(a.lower().startswith("kms:") for a in actions):
                    statements.append(stmt)
    return statements


def _statement_condition_values(stmt, needle):
    """Every condition value whose condition key contains ``needle`` (case-insensitive)."""
    values = []
    for _operator, mapping in (stmt.get("Condition", {}) or {}).items():
        for cond_key, cond_val in mapping.items():
            if needle.lower() in cond_key.lower():
                if isinstance(cond_val, list):
                    values.extend(cond_val)
                else:
                    values.append(cond_val)
    return values


def _resource_strings(stmt):
    resource = stmt.get("Resource")
    return [resource] if isinstance(resource, str) else list(resource or [])


def test_runtime_kms_grant_covers_the_documented_dynamodb_cmk_action_set(template):
    """RED against the live begin_observation failure: the runtime role held only
    kms:Decrypt + kms:GenerateDataKey, so DynamoDB customer-managed-key access
    raised AccessDeniedException even though the IAM simulator allowed
    TransactWriteItems/GetItem. A DynamoDB CMK caller needs the full documented
    data-plane action set (Encrypt/Decrypt/ReEncrypt*/GenerateDataKey*/DescribeKey)
    plus CreateGrant. This asserts every required action is present across the
    runtime role's KMS statements."""
    statements = _role_kms_statements(template)
    assert statements, "expected the runtime role to carry KMS statements"
    granted = set()
    for stmt in statements:
        assert stmt.get("Effect") == "Allow", "runtime KMS statements must be Allow"
        granted |= set(_iter_action_strings([stmt.get("Action")]))
    missing = REQUIRED_RUNTIME_KMS_DATA_ACTIONS - granted
    assert not missing, f"runtime KMS grant is missing DynamoDB CMK actions: {sorted(missing)}"
    assert RUNTIME_KMS_CREATE_GRANT_ACTION in granted, "runtime KMS grant must include kms:CreateGrant"


def test_runtime_kms_grant_has_no_unexpected_actions(template):
    """The grant must be tightly bounded: only the documented DynamoDB CMK
    data-plane actions plus CreateGrant. No wildcard, no service wildcard, and no
    generic KMS action beyond the documented minimum."""
    allowed = REQUIRED_RUNTIME_KMS_DATA_ACTIONS | {RUNTIME_KMS_CREATE_GRANT_ACTION}
    for stmt in _role_kms_statements(template):
        for action in _iter_action_strings([stmt.get("Action")]):
            assert action != "*", "wildcard action not allowed on the runtime KMS grant"
            assert action != "kms:*", "generic kms:* not allowed on the runtime KMS grant"
            assert action in allowed, f"unexpected runtime KMS action: {action}"


def test_runtime_kms_data_statement_is_pinned_to_dynamodb_via_service(template):
    """Every runtime KMS data-plane statement must be constrained by
    kms:ViaService to dynamodb.*.amazonaws.com, so the runtime can only exercise
    the key through DynamoDB and never for direct, generic KMS use."""
    data_statements = [
        stmt
        for stmt in _role_kms_statements(template)
        if RUNTIME_KMS_CREATE_GRANT_ACTION not in set(_iter_action_strings([stmt.get("Action")]))
    ]
    assert data_statements, "expected a DynamoDB data-plane KMS statement on the runtime role"
    for stmt in data_statements:
        via = _statement_condition_values(stmt, "kms:ViaService")
        assert via, "runtime KMS data statement must set a kms:ViaService condition"
        for value in via:
            assert DYNAMODB_VIA_SERVICE_PATTERN in value and value.endswith(
                "amazonaws.com"
            ), f"kms:ViaService must scope to dynamodb.*.amazonaws.com, got {value!r}"
        for resource in _resource_strings(stmt):
            assert resource != "*", "runtime KMS data statement must not use a wildcard resource"
            assert not resource.endswith(":*"), f"runtime KMS resource too broad: {resource}"


def test_runtime_kms_create_grant_is_isolated_and_resource_guarded(template):
    """CreateGrant must live in its OWN statement, guarded by
    kms:GrantIsForAWSResource=true (so the runtime can only create grants on
    behalf of the AWS resource, never arbitrary grants) and additionally pinned
    to DynamoDB via kms:ViaService. It must never be a wildcard resource and must
    never be merged into the data-plane statement."""
    statements = _role_kms_statements(template)
    grant_statements = [
        stmt
        for stmt in statements
        if RUNTIME_KMS_CREATE_GRANT_ACTION in set(_iter_action_strings([stmt.get("Action")]))
    ]
    assert grant_statements, "expected a dedicated kms:CreateGrant statement"
    for stmt in grant_statements:
        stmt_actions = set(_iter_action_strings([stmt.get("Action")]))
        assert stmt_actions == {
            RUNTIME_KMS_CREATE_GRANT_ACTION
        }, f"CreateGrant must be isolated in its own statement, got {sorted(stmt_actions)}"
        guard = _statement_condition_values(stmt, "kms:GrantIsForAWSResource")
        assert guard, "CreateGrant statement must require kms:GrantIsForAWSResource"
        normalized = {str(v).lower() for v in guard}
        assert normalized == {"true"}, f"kms:GrantIsForAWSResource must be true, got {guard}"
        via = _statement_condition_values(stmt, "kms:ViaService")
        assert via, "CreateGrant statement must also set kms:ViaService for DynamoDB"
        for value in via:
            assert DYNAMODB_VIA_SERVICE_PATTERN in value and value.endswith(
                "amazonaws.com"
            ), f"CreateGrant kms:ViaService must scope to dynamodb.*.amazonaws.com, got {value!r}"
        for resource in _resource_strings(stmt):
            assert resource != "*", "CreateGrant must not use a wildcard resource"
            assert not resource.endswith(":*"), f"CreateGrant resource too broad: {resource}"


# --------------------------------------------------------------------------- #
# Default-disabled behavior
# --------------------------------------------------------------------------- #
def test_every_resource_is_gated_on_resources_provisioned(template):
    """Resource EXISTENCE is gated on ResourcesProvisioned (driven by the
    Provisioned flag), NOT on OperationsMode. This is the core of the rollback
    fix: an emergency disable flips OperationsMode to disabled but keeps
    Provisioned=true, so gating existence on ResourcesProvisioned means disable
    deletes NOTHING. Gating on the old OperationsEnabled (observe) condition
    would delete every resource on disable — the defect this guards against."""
    conditions = template.get("Conditions", {})
    assert "ResourcesProvisioned" in conditions, "expected a ResourcesProvisioned condition"
    # The scoped CFN loader renders !Equals [!Ref Provisioned, 'true'] as a plain
    # list ["Provisioned", "true"]; assert it is driven by the Provisioned flag.
    assert conditions["ResourcesProvisioned"] == [
        "Provisioned",
        "true",
    ], "ResourcesProvisioned must be driven by the Provisioned flag, not OperationsMode"
    for name, body in template["Resources"].items():
        assert body.get("Condition") == "ResourcesProvisioned", (
            f"resource {name} must be gated on ResourcesProvisioned (existence), "
            "never on OperationsEnabled (which would delete it on disable)"
        )


def test_stack_is_unwired_from_normal_deployment():
    for path in (MAIN_DEPLOY, DEPLOY_ALL):
        text = path.read_text(encoding="utf-8")
        assert "06-operations-observation" not in text, f"{path} must not reference the optional E1 stack"


def test_teardown_all_never_invokes_operations_teardown():
    for path in (MAIN_TEARDOWN, TEARDOWN_ALL):
        text = path.read_text(encoding="utf-8")
        assert "teardown-operations" not in text, f"{path} must not auto-invoke E1 teardown"


# --------------------------------------------------------------------------- #
# Ownership seam: infra contributes NO placeholder/register_observer, core owns
# the sole real handler (asserted against the tracked contribution + a
# materialized combined tree, so it holds in both the infra-only and combined
# contexts and never conflates "no placeholder" with "path must not exist").
# --------------------------------------------------------------------------- #
def test_no_placeholder_or_register_observer_seam_is_tracked():
    """No tracked ``operations/observe`` source may carry an infra-style
    placeholder seam — a ``register_observer`` shim or a fail-closed placeholder
    stub. This is asserted by *content of the tracked files*, not by the on-disk
    path, so it is correct in both contexts and never conflates "no placeholder"
    with "path must not exist":

    * infra-only — nothing is tracked under ``operations/observe`` (no hits); and
    * combined — the tracked files are core's real handler, which carries no
      placeholder seam (no hits).

    It goes red only if the deleted infra placeholder/register_observer seam is
    reintroduced, which is exactly the add/add-conflict this guards against."""
    hits = observe_placeholder_seam_hits(PROJECT_ROOT)
    assert hits == [], (
        "no operations/observe placeholder or register_observer seam may be "
        f"tracked (core owns the real handler); found: {hits}"
    )


def test_combined_tree_carries_real_core_handler_the_template_points_at(tmp_path):
    """On a materialized combined tree, ``operations/observe/lambda_entry.py``
    exists and defines a module-level ``handler`` — the exact CloudFormation
    ``Handler`` — proving the infra template references core's real, deployable
    entry point (not a placeholder). Always materialized, so never vacuous."""
    src = materialize_combined_operations_tree(tmp_path, PROJECT_ROOT)
    lambda_entry = src / HANDLER_REL
    assert lambda_entry.is_file(), "combined tree must carry core's real observe handler"
    assert module_defines_top_level_handler(
        lambda_entry
    ), "the combined-tree handler must define a module-level def handler(...)"
    # The template Handler must name exactly this module + attribute.
    handler_value = _observation_function(load_cfn_template(TEMPLATE.read_text(encoding="utf-8")))["Handler"]
    assert handler_value == HANDLER_DOTTED, (
        f"template Handler {handler_value!r} must point at the combined-tree " f"module {HANDLER_DOTTED!r}"
    )


def test_wrapper_packages_and_imports_the_combined_tree_handler():
    """The deploy wrapper must package the combined tree's handler module and
    import-probe it, so the artifact it uploads is exactly the module the
    template invokes. Encodes the wrapper's handler-present gate and probe."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # The wrapper packages by the frozen module path...
    assert (
        f'HANDLER_MODULE_PATH="{HANDLER_REL}"' in text
    ), "wrapper must package the frozen handler module path from the combined tree"
    # ...gates the enable on that module being present in the combined tree...
    assert 'if [ ! -f "$BACKEND_SRC/$HANDLER_MODULE_PATH" ]; then' in text
    assert "exit 5" in text
    # ...and import-probes the dotted module + its module-level handler attr.
    assert f'HANDLER_IMPORT="{HANDLER_IMPORT}"' in text
    assert "assert callable(m.handler)" in text


# --------------------------------------------------------------------------- #
# Packaging contract (cross-platform Linux/x86_64, pinned, import-probed)
# --------------------------------------------------------------------------- #
def test_packaging_pins_every_transitive_runtime_dependency():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    for name, version in PINNED_RUNTIME_DEPS.items():
        assert f"{name}=={version}" in text, f"packaging must pin {name}=={version} from the repository lock"


def test_packaging_targets_linux_x86_64_never_host_wheels():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # A deterministic cross-platform install: pip/uv platform targeting for the
    # Lambda manylinux x86_64 ABI, binary-only so no host wheel is ever built.
    assert "--platform" in text, "packaging must use an explicit --platform target"
    assert "manylinux" in text and "x86_64" in text, "packaging must target the Linux x86_64 manylinux ABI"
    assert "--only-binary" in text, "packaging must be binary-only so host native wheels are never built"
    assert "--python-version" in text or "3.13" in text, "packaging must target the Python 3.13 ABI"


def test_packaging_import_probe_runs_on_clean_linux_environment():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # The import probe must resolve the frozen handler from the staged package.
    assert "importlib.import_module" in text or "import operations.observe.lambda_entry" in text
    assert "lambda_entry" in text


# --------------------------------------------------------------------------- #
# Shell safety and explicit-profile propagation of the wrappers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("wrapper", ["deploy", "teardown"])
def test_wrappers_use_strict_mode(wrapper):
    path = DEPLOY_WRAPPER if wrapper == "deploy" else TEARDOWN_WRAPPER
    assert path.exists(), f"missing wrapper: {path}"
    text = path.read_text(encoding="utf-8")
    assert re.search(r"set -euo pipefail", text), "wrapper must use strict mode"


@pytest.mark.parametrize("wrapper", ["deploy", "teardown"])
def test_wrappers_pass_profile_explicitly_to_every_aws_call(wrapper):
    """Every ``aws`` invocation must pass ``--profile`` explicitly rather than
    relying on an ambient AWS_PROFILE only."""
    path = DEPLOY_WRAPPER if wrapper == "deploy" else TEARDOWN_WRAPPER
    text = path.read_text(encoding="utf-8")
    # Fold backslash-continued lines so a multi-line aws call is one logical line,
    # then look for actual command invocations of the aws CLI. A command position
    # is line start, after a pipe/`;`/`&&`, or inside a `$( ... )` / backtick
    # capture. Comment lines (first non-space char is ``#``) and prose are skipped.
    folded = text.replace("\\\n", " ")
    invocation = re.compile(r"(?:^|[;&|]|\$\(|`)\s*(?:! )?(?:[A-Z_]+=\"?\$\()?aws\s")
    for raw in folded.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if invocation.search(raw):
            # The profile is passed explicitly either as a literal --profile flag
            # or via the AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE") array, which
            # is the DRY form the wrappers use on every call.
            has_profile = "--profile" in raw or "AWS_PROFILE_ARGS" in raw
            assert has_profile, f"aws call must pass --profile explicitly: {stripped}"


def test_deploy_wrapper_requires_explicit_opt_in():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # Enabled deploys are gated behind both the observe env value and the flag.
    assert "GBAW_OPERATIONS_MODE" in text
    assert "observe" in text
    assert "--enable" in text


def test_deploy_wrapper_verifies_caller_identity_before_writes():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    assert "get-caller-identity" in text, "wrapper must verify caller identity before any write"
    assert "AWS_PROFILE" in text and "AWS_REGION" in text


def test_deploy_wrapper_builds_and_uploads_artifact():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # A real packaging path: build a deterministic zip and upload under a
    # content-hash key, then pass CodeS3Bucket / CodeS3Key.
    assert "CodeS3Bucket" in text and "CodeS3Key" in text
    assert "s3 cp" in text or "s3api put-object" in text
    assert "sha256" in text.lower(), "artifact key must be content-hash addressed"


def test_deploy_wrapper_requires_explicit_existing_artifact_bucket():
    """The artifact bucket must be an explicit, pre-existing bucket that the
    wrapper verifies; it must never discover or create a bucket."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    assert "GBAW_OPERATIONS_ARTIFACT_BUCKET" in text, "an explicit artifact bucket env var is required"
    assert "head-bucket" in text, "the supplied bucket must be verified to exist"
    # No unsafe name discovery and no bucket creation.
    assert "list-buckets" not in text, "bucket-name discovery via list-buckets must be removed"
    assert "create-bucket" not in text, "the wrapper must never create a bucket"


def test_deploy_wrapper_verifies_bucket_region_and_account_context():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # The bucket's region/account context is confirmed before upload.
    assert "get-bucket-location" in text, "bucket region context must be verified"


def test_preview_is_read_only_no_change_set():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # Preview validates/lints only; it must not create a change set.
    assert "create-change-set" not in text, "preview must not create a change set"
    assert "validate-template" in text or "cfn-lint" in text


def test_teardown_wrapper_requires_explicit_confirmation():
    text = TEARDOWN_WRAPPER.read_text(encoding="utf-8")
    assert "--confirm" in text
    assert "delete-operations" in text
    assert "delete-stack" in text


def test_teardown_wrapper_has_no_delete_data_claim():
    """Teardown retains audit data and must not advertise a silent --delete-data
    path; cleanup of retained data is a separate, explicit future step."""
    text = TEARDOWN_WRAPPER.read_text(encoding="utf-8")
    assert "--delete-data" not in text, "the silent --delete-data claim must be removed"


# --------------------------------------------------------------------------- #
# Rollback-safe stateful resources + CloudWatch Logs / API Gateway authorization
# regression suite (GitHub issue #413 beta-stack create failure).
#
# A real beta `create` failed at BOTH KMS-encrypted log groups with a CloudWatch
# Logs AccessDenied because the CMK key policy granted the logs service principal
# only kms:Decrypt + kms:GenerateDataKey, while CloudWatch Logs additionally
# requires Encrypt / ReEncrypt* / Describe* to attach a KMS key to a log group.
# The failed create then ORPHANED an empty DynamoDB table and CMK because
# DeletionPolicy: Retain also retains on create-rollback. These tests reproduce
# the exact missing-action failure structurally and enforce rollback-safe
# stateful policies, the documented least-privilege logs grant scoped by
# encryption context to exactly the two log-group ARNs, and the API Gateway
# access-log-delivery resource policy.
# --------------------------------------------------------------------------- #

# The exact least-privilege action set the CloudWatch Logs service principal
# needs to attach a CMK to a log group and read/write encrypted log data, per
# the AWS "Encrypt log data in CloudWatch Logs using AWS KMS" guide. Encrypt and
# ReEncrypt were the actions missing in the failed beta create.
REQUIRED_LOGS_KMS_ACTION_PREFIXES = (
    "kms:Encrypt",
    "kms:Decrypt",
    "kms:ReEncrypt",
    "kms:GenerateDataKey",
    "kms:Describe",
)

# The two CMK-encrypted log groups whose ARNs the key-policy encryption-context
# condition must scope to exactly. Both failed in the real create.
LAMBDA_LOG_GROUP_SUFFIX = "log-group:/aws/lambda/${ProjectName}-operations-observe"
ACCESS_LOG_GROUP_SUFFIX = "log-group:/aws/apigateway/${ProjectName}-operations-access"

# Stateful resources that must survive teardown/update-replacement but must NOT
# orphan on a failed initial create.
STATEFUL_RESOURCE_TYPES = frozenset(
    {
        "AWS::KMS::Key",
        "AWS::DynamoDB::Table",
        "AWS::Logs::LogGroup",
    }
)


def _kms_key(template):
    keys = _resources_of_type(template, "AWS::KMS::Key")
    assert keys, "expected a CMK"
    return next(iter(keys.values()))


def _logs_key_statements(template):
    """Every CMK key-policy statement whose principal is a ``logs.*`` service."""
    statements = _kms_key(template)["Properties"]["KeyPolicy"]["Statement"]
    matched = []
    for stmt in statements:
        principal = stmt.get("Principal", {})
        service = principal.get("Service")
        services = [service] if isinstance(service, str) else (service or [])
        if any(isinstance(svc, str) and "logs." in svc for svc in services):
            matched.append(stmt)
    return matched


def _statement_action_set(stmt):
    return set(_iter_action_strings([stmt.get("Action")]))


def test_kms_key_policy_grants_full_cloudwatch_logs_action_set(template):
    """RED against the failed beta create: the logs-principal grant carried only
    Decrypt + GenerateDataKey. CloudWatch Logs also requires Encrypt, ReEncrypt*,
    and Describe* to attach the CMK to a log group, so a grant missing any of
    them reproduces the AccessDenied that failed the create."""
    logs_statements = _logs_key_statements(template)
    assert logs_statements, "KMS key policy must grant the CloudWatch Logs service principal"
    granted = set()
    for stmt in logs_statements:
        assert stmt.get("Effect") == "Allow", "logs grant must be an Allow"
        granted |= _statement_action_set(stmt)
    for required in REQUIRED_LOGS_KMS_ACTION_PREFIXES:
        assert any(
            action.startswith(required) for action in granted
        ), f"CloudWatch Logs KMS grant is missing a {required}* action (got {sorted(granted)})"


def test_kms_logs_grant_scoped_by_encryption_context_to_exactly_two_log_groups(template):
    """The logs grant must be scoped by the aws:logs:arn encryption context to
    exactly the two operations log groups (Lambda + API access), not a broad
    wildcard that would over-grant, and not a scope that omits either group so
    that its create fails."""
    logs_statements = _logs_key_statements(template)
    assert logs_statements, "expected a logs-principal grant"
    context_values = []
    for stmt in logs_statements:
        condition = stmt.get("Condition", {})
        for _operator, mapping in condition.items():
            for ctx_key, ctx_val in mapping.items():
                if "kms:EncryptionContext:aws:logs:arn" in ctx_key:
                    if isinstance(ctx_val, list):
                        context_values.extend(ctx_val)
                    else:
                        context_values.append(ctx_val)
    assert context_values, "logs grant must be scoped by the aws:logs:arn encryption context"
    joined = "\n".join(context_values)
    assert LAMBDA_LOG_GROUP_SUFFIX in joined, "encryption context must include the Lambda log group ARN"
    assert ACCESS_LOG_GROUP_SUFFIX in joined, "encryption context must include the API access log group ARN"
    # No bare account/region wildcard that matches every log group in the account.
    for value in context_values:
        assert not value.rstrip().endswith(":*"), f"encryption-context scope is too broad: {value}"
        assert not value.rstrip().endswith("log-group:*"), f"encryption-context scope is too broad: {value}"


def test_kms_logs_grant_uses_regional_service_principal(template):
    """CloudWatch Logs must be granted via its Region-qualified service principal
    (logs.<region>.amazonaws.com), matching the account/region the key lives in."""
    logs_statements = _logs_key_statements(template)
    assert logs_statements, "expected a logs-principal grant"
    for stmt in logs_statements:
        principal = stmt["Principal"]["Service"]
        services = [principal] if isinstance(principal, str) else principal
        assert any(
            "${AWS::Region}" in svc or re.match(r"logs\.[a-z0-9-]+\.amazonaws\.com", svc) for svc in services
        ), f"logs principal must be Region-qualified, got {services}"


def test_execution_role_kms_grant_stays_bounded_to_the_dynamodb_cmk_minimum(template):
    """E1 boundary preservation: the runtime role's KMS grant must be exactly the
    documented DynamoDB CMK data-plane set plus the isolated CreateGrant -- no
    more. The role now legitimately holds kms:Encrypt (a DynamoDB CMK caller
    needs it), but the CloudWatch Logs key-policy grant must NOT leak any extra
    generic KMS action onto the execution *role*, and the role must never carry a
    wildcard KMS action."""
    role_actions = {a for a in _all_policy_actions(template) if a.lower().startswith("kms:")}
    assert role_actions, "expected scoped KMS actions on the execution role"
    allowed = REQUIRED_RUNTIME_KMS_DATA_ACTIONS | {RUNTIME_KMS_CREATE_GRANT_ACTION}
    unexpected = role_actions - allowed
    assert not unexpected, f"execution role carries KMS actions beyond the documented minimum: {sorted(unexpected)}"


def test_stateful_resources_are_rollback_safe_not_plain_retain(template):
    """RED against the orphaning defect: KMS key, DynamoDB table, and log groups
    used DeletionPolicy: Retain, which retains even on a failed initial create,
    orphaning empty resources. They must use RetainExceptOnCreate so a rolled-back
    create cleans up, while still retaining in-use data on teardown."""
    found = 0
    for name, body in template["Resources"].items():
        if body.get("Type") in STATEFUL_RESOURCE_TYPES:
            found += 1
            assert (
                body.get("DeletionPolicy") == "RetainExceptOnCreate"
            ), f"{name} must use DeletionPolicy: RetainExceptOnCreate, got {body.get('DeletionPolicy')!r}"
    assert found >= 3, "expected the KMS key, DynamoDB table, and at least one log group"


def test_stateful_resources_retain_data_on_update_replacement(template):
    """Retained data must survive an update that replaces the physical resource:
    UpdateReplacePolicy stays Retain (RetainExceptOnCreate is not a valid
    UpdateReplacePolicy value and would not protect a replacement)."""
    for name, body in template["Resources"].items():
        if body.get("Type") in STATEFUL_RESOURCE_TYPES:
            assert (
                body.get("UpdateReplacePolicy") == "Retain"
            ), f"{name} must keep UpdateReplacePolicy: Retain to preserve data on replacement"


def test_api_gateway_access_log_delivery_resource_policy_present(template):
    """API Gateway access-log delivery to CloudWatch Logs requires a log-group
    resource policy granting the log-delivery service principal
    CreateLogStream/PutLogEvents. Without it, enabling access logging fails at
    delivery time. Scope it to this account and this API."""
    policies = _resources_of_type(template, "AWS::Logs::ResourcePolicy")
    assert policies, "expected an AWS::Logs::ResourcePolicy for access-log delivery"
    body = next(iter(policies.values()))
    props = body.get("Properties", {})
    assert props.get("PolicyName"), "resource policy must be named"
    doc_text = props.get("PolicyDocument", "")
    if isinstance(doc_text, dict):
        # Some loaders may surface the embedded JSON as a mapping; normalize.
        doc_text = json.dumps(doc_text)
    assert isinstance(doc_text, str), "PolicyDocument is expected to be an embedded JSON string"
    assert "delivery.logs.amazonaws.com" in doc_text, "must grant the log-delivery service principal"
    assert "logs:CreateLogStream" in doc_text, "must allow CreateLogStream for delivery"
    assert "logs:PutLogEvents" in doc_text, "must allow PutLogEvents for delivery"
    # Scoped to this account and this API where CloudFormation permits.
    assert "aws:SourceAccount" in doc_text, "delivery grant must be scoped by SourceAccount"
    assert "${AWS::AccountId}" in doc_text, "delivery grant must reference this account"
    assert "HttpApi" in doc_text, "delivery grant must be scoped to this API (SourceArn)"


def test_access_log_resource_policy_is_gated_and_scoped_to_access_group(template):
    """The delivery resource policy is part of the optional stack (gated on
    OperationsEnabled) and targets the API access log group, not the whole
    account's log groups."""
    policies = _resources_of_type(template, "AWS::Logs::ResourcePolicy")
    assert policies, "expected an AWS::Logs::ResourcePolicy"
    for name, body in policies.items():
        assert body.get("Condition") == "ResourcesProvisioned", f"{name} must be gated on ResourcesProvisioned"
    doc_text = next(iter(policies.values()))["Properties"]["PolicyDocument"]
    if isinstance(doc_text, dict):
        doc_text = json.dumps(doc_text)
    assert "operations-access" in doc_text, "delivery grant must target the API access log group"


# --------------------------------------------------------------------------- #
# Rollback design: separate initial PROVISIONING from runtime AUTHORITY.
#
# Defect being guarded against (live re-review): every resource was gated on a
# single OperationsEnabled=(OperationsMode==observe) condition, so --disable
# (OperationsMode=disabled) removed EVERY resource. Stateful resources retained
# on that delete, so a later --enable failed on existing physical names and
# CloudFormation lost ownership. The fix: a default-false Provisioned flag gates
# resource existence, while OperationsMode is a runtime kill switch that leaves
# resources in place. These tests are red against the old single-condition
# design and green only against the split model.
# --------------------------------------------------------------------------- #
def _stage(template):
    stages = _resources_of_type(template, "AWS::ApiGatewayV2::Stage")
    assert stages, "expected an HTTP API stage"
    return next(iter(stages.values()))


def test_provisioned_parameter_defaults_to_false_and_is_boolean(template):
    """A default new stack must be UNPROVISIONED: Provisioned defaults to
    'false' so a defaults deploy creates zero resources and costs $0."""
    params = template["Parameters"]
    assert "Provisioned" in params, "expected a Provisioned parameter"
    p = params["Provisioned"]
    assert p["Default"] == "false", "Provisioned must default to 'false' (zero resources, $0)"
    assert set(p["AllowedValues"]) == {"false", "true"}


def test_resource_existence_is_decoupled_from_operations_mode(template):
    """The resource-existence condition must be driven by Provisioned, and the
    OperationsEnabled condition must NOT gate any resource — otherwise a
    disable (mode=disabled) would delete resources. E2 (issue #414) widens
    OperationsEnabled to an Fn::Or over the enabled modes (observe OR advise),
    so the check accepts both the E1 equals-shape and the E2 Or-shape."""
    conditions = template["Conditions"]
    assert conditions["ResourcesProvisioned"] == ["Provisioned", "true"]
    enabled = conditions["OperationsEnabled"]
    if enabled == ["OperationsMode", "observe"]:
        pass  # E1 shape.
    else:
        # E2 shape: an Fn::Or list whose legs equal OperationsMode to each
        # enabled mode; observe MUST be one leg and advise the other.
        legs = enabled if isinstance(enabled, list) else [enabled]
        modes = set()
        for leg in legs:
            if isinstance(leg, list) and leg[:1] == ["OperationsMode"]:
                modes.add(leg[1])
        assert {"observe", "advise"} <= modes, f"OperationsEnabled must admit observe and advise: {enabled!r}"
    for name, body in template["Resources"].items():
        assert body.get("Condition") != "OperationsEnabled", (
            f"{name} is gated on OperationsEnabled; disabling would delete it. "
            "Resource existence must be gated on ResourcesProvisioned."
        )


def test_default_deploy_provisions_zero_resources(template):
    """With Provisioned='false' (the default), the ResourcesProvisioned condition
    is false, so no resource is created. Assert every resource is behind it and
    the default evaluates false."""
    assert template["Parameters"]["Provisioned"]["Default"] == "false"
    for name, body in template["Resources"].items():
        assert body.get("Condition") == "ResourcesProvisioned", name


def test_rule_rejects_observe_without_provisioning(template):
    """Unsafe combination: an ENABLED OperationsMode with Provisioned=false. A
    Rule must reject it (an enabled mode requires the resources it drives to
    exist). E1 keyed the rule off equals-observe; E2 (issue #414) widens it to
    Fn::Not[equals disabled] so it fires for observe AND advise. Accept either
    shape; in both cases the rule must assert Provisioned=true."""
    rules = template["Rules"]
    rule = rules.get("EnabledModeRequiresProvisioning")
    assert rule, "expected an EnabledModeRequiresProvisioning rule"
    cond = rule["RuleCondition"]
    if cond == ["OperationsMode", "observe"]:
        pass  # E1 shape: equals-observe.
    else:
        # E2 shape: Fn::Not[Fn::Equals[OperationsMode, disabled]].
        joined_cond = json.dumps(cond)
        assert (
            "disabled" in joined_cond and "OperationsMode" in joined_cond
        ), f"enabled-mode rule must fire for any non-disabled mode: {cond!r}"
    asserts = rule["Assertions"]
    # The single assertion requires Provisioned == 'true'.
    joined = json.dumps(asserts)
    assert "Provisioned" in joined and "true" in joined, "enabled-mode rule must assert Provisioned=true"


def test_rule_requires_bindings_whenever_provisioned(template):
    """Whenever resources exist (Provisioned=true) the issuer, audience, tenant,
    workspace, and code artifact bindings the resources reference must be
    non-empty — in observe AND in disabled-after-provision. This is what makes an
    emergency disable REUSE existing values rather than blank them."""
    rules = template["Rules"]
    rule = rules.get("ProvisionedRequiresInputs")
    assert rule, "expected a ProvisionedRequiresInputs rule"
    assert rule["RuleCondition"] == ["Provisioned", "true"]
    joined = json.dumps(rule["Assertions"])
    for referenced in ("CognitoIssuer", "CognitoClientId", "TenantId", "WorkspaceId", "CodeS3Bucket", "CodeS3Key"):
        assert referenced in joined, f"provisioned rule must require {referenced}"


def test_handler_kill_switch_injects_operations_mode(template):
    """The Lambda's GBAW_OPERATIONS_MODE must be wired straight from the
    OperationsMode parameter, so an emergency disable (mode=disabled) makes the
    handler fail closed WITHOUT changing the function's identity or code."""
    fn = _observation_function(template)
    env = fn["Environment"]["Variables"]
    assert (
        env["GBAW_OPERATIONS_MODE"] == "OperationsMode"
    ), "GBAW_OPERATIONS_MODE must be !Ref OperationsMode so disable flips it to disabled"


def test_api_stage_kill_switch_throttles_to_zero_when_not_observe(template):
    """API-safe kill switch: when OperationsMode is not an ENABLED mode
    (observe or, for E2, advise), the HTTP API stage throttles to zero so the
    gateway itself fails closed — without deleting or renaming the stage. Any
    enabled mode uses the operator-tuned limits; the gate is the same
    OperationsEnabled condition (an Or over the enabled modes)."""
    stage = _stage(template)
    settings = stage["Properties"]["DefaultRouteSettings"]
    burst = settings["ThrottlingBurstLimit"]
    rate = settings["ThrottlingRateLimit"]
    # !If [OperationsEnabled, <limit>, 0] renders as ["OperationsEnabled", <limit>, 0].
    for value in (burst, rate):
        assert (
            isinstance(value, list) and value[0] == "OperationsEnabled"
        ), "stage throttle must be gated on OperationsEnabled for the kill switch"
        assert value[-1] in (0, "0"), "disabled (non-observe) must throttle to zero"


def test_physical_names_are_stable_and_not_tied_to_mode(template):
    """Physical resource names must be deterministic (derived from ProjectName)
    and independent of OperationsMode/Provisioned, so a disable→enable cycle
    reuses the SAME names and CloudFormation never loses ownership or collides on
    re-create."""
    resources = template["Resources"]
    fn = _observation_function(template)
    assert fn["FunctionName"] == "${ProjectName}-operations-observe"
    table = next(iter(_resources_of_type(template, "AWS::DynamoDB::Table").values()))
    assert table["Properties"]["TableName"] == "${ProjectName}-operations"
    log_groups = _resources_of_type(template, "AWS::Logs::LogGroup")
    names = {json.dumps(b["Properties"]["LogGroupName"]) for b in log_groups.values()}
    assert any("operations-observe" in n for n in names)
    assert any("operations-access" in n for n in names)
    # None of these names interpolate OperationsMode or Provisioned.
    text = TEMPLATE.read_text(encoding="utf-8")
    for tainted in ("${OperationsMode}", "${Provisioned}"):
        assert tainted not in text, f"physical identifiers must not embed {tainted}"


def test_reenable_reuses_same_names_no_deletion_on_disable(template):
    """Combined invariant proving the re-enable path is reversible:

    * resource existence is gated on ResourcesProvisioned (disable keeps
      Provisioned=true, so nothing is deleted);
    * stateful resources use RetainExceptOnCreate + UpdateReplacePolicy Retain
      (data survives); and
    * physical names are fixed (re-enable reuses them, no collision)."""
    for name, body in template["Resources"].items():
        assert body.get("Condition") == "ResourcesProvisioned", name
    stateful = 0
    for body in template["Resources"].values():
        if body.get("Type") in STATEFUL_RESOURCE_TYPES:
            stateful += 1
            assert body.get("DeletionPolicy") == "RetainExceptOnCreate"
            assert body.get("UpdateReplacePolicy") == "Retain"
    assert stateful >= 3
    fn = _observation_function(template)
    assert fn["FunctionName"] == "${ProjectName}-operations-observe"


def test_operations_mode_and_provisioned_outputs_are_unconditional(template):
    """Both control-plane state outputs echo their parameters and must be
    UNCONDITIONAL, so an operator can read Provisioned/OperationsMode from a
    disabled-but-provisioned stack (and from an unprovisioned one)."""
    outputs = template["Outputs"]
    assert outputs["OperationsMode"].get("Condition") is None
    assert outputs["Provisioned"].get("Condition") is None
    assert outputs["OperationsMode"]["Value"] == "OperationsMode"
    assert outputs["Provisioned"]["Value"] == "Provisioned"
