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
  ``GetItem``/``Query``/``TransactWriteItems``; no S3 runtime access, no
  ``kms:Encrypt``, no GameLift write, ``iam:PassRole``, Step Functions,
  ``UpdateItem``/``DeleteItem``/``PutItem``/``Scan``, or wildcard action);
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
# DynamoDB runtime actions are limited to what core actually uses: bounded reads
# and the single transactional write.
ALLOWED_DYNAMODB_ACTIONS = frozenset(
    {
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:TransactWriteItems",
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
    "dynamodb:PutItem",
    "dynamodb:Scan",
    "dynamodb:BatchWriteItem",
    # The unused content bucket and its runtime access are removed entirely.
    "s3:",
    # Encrypt is unnecessary; the runtime uses envelope encryption via
    # GenerateDataKey and reads via Decrypt.
    "kms:Encrypt",
)

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
    mode = template["Parameters"]["OperationsMode"]
    assert mode["Default"] == "disabled"
    assert set(mode["AllowedValues"]) == {"disabled", "observe"}


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


def test_kms_actions_are_minimal(template):
    actions = [a for a in _all_policy_actions(template) if a.lower().startswith("kms:")]
    # Runtime policy uses only decrypt + envelope generation; no Encrypt.
    for action in actions:
        assert action in {"kms:Decrypt", "kms:GenerateDataKey"}, f"unexpected KMS action: {action}"


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
# Placeholder package removal (core's real handler is the only owner)
# --------------------------------------------------------------------------- #
def test_infra_owned_placeholder_observe_package_is_removed():
    """The infra worktree must not ship its own operations/observe placeholder;
    on a combined tree core's real handler must be the only owner of the path."""
    observe_dir = BACKEND_SRC / "operations" / "observe"
    assert not observe_dir.exists(), (
        "the infra-owned placeholder operations/observe package must be deleted "
        "so it cannot add/add-conflict with core's real handler"
    )


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
