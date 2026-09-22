"""Parser- and scanner-verifiable contract tests for the OPTIONAL E4 operations
CONTROL PLANE (GitHub issue #416).

E4 adds the deployment-wide AWS AppConfig kill-switch and the admin control API
on top of the accepted E1 observation, E2 advise, and E3 execution planes. It is
delivered as a SEPARATE, default-unprovisioned CloudFormation stack
(``08-operations-control-plane.yaml``) so that a default deployment provisions
ZERO E4 resources and costs $0, and so that no role gains provider-write,
PassRole, or secrets authority.

These tests never call AWS. They parse the repository-owned CloudFormation
template and shell wrappers as data and assert the **frozen E4 runtime
contract**. The ``_cfn_yaml`` loader converts CloudFormation short-form ``!``
intrinsics into plain data (dropping the tag): ``!Ref X`` -> ``"X"``,
``!GetAtt A.Arn`` -> ``"A.Arn"``, ``!Equals [a, b]`` -> ``["a", "b"]``, and
``!If [c, a, b]`` -> ``["c", "a", "b"]``. The assertions below match that
flattened form.

Asserted contract:

* The 08 stack is default-unprovisioned: every resource is gated on a
  ``ResourcesProvisioned`` condition driven by a ``Provisioned`` parameter that
  defaults ``false``.
* Two-lever emergency disable: a runtime ``ControlMode`` kill switch
  (``disabled`` default) that (a) throttles the control API stage to zero and
  (b) injects ``GBAW_OPERATIONS_CONTROL_MODE=disabled`` so the control Lambda
  fails closed — WITHOUT deleting any resource or data.
* The AppConfig application/environment/hosted profile, a safe all-disabled
  default hosted version, a normal gradual deployment strategy with a CloudWatch
  monitor + automatic rollback, and an explicit immediate hard-down strategy.
* The hosted profile's JSON_SCHEMA validator is **byte-equivalent** to the
  frozen contract schema
  ``backend/src/operations/contracts/schemas/v1/operations-kill-switch.schema.json``
  and carries no external/urn ``$ref``.
* The frozen E4 API routes (``control_plane.ROUTE_KEYS``) are all present and
  JWT-authorized.
* The official AppConfig Lambda extension layer is attached via a validated
  parameter.
* Least-privilege IAM: the control role may create hosted versions / start
  deployments ONLY for the exact AppConfig resources, and touch DynamoDB only
  through the bounded single-item actions its TransactWriteItems-based control
  write and read projections need (PutItem/UpdateItem/GetItem/Query). No provider
  write, no PassRole, no secrets, no Delete/Scan/Batch, and never the invalid
  dynamodb:TransactWriteItems action.
* An EventBridge periodic freshness-expiry sweeper targets the control Lambda.
* Every #416 alarm exists with a truthful metric/statistic and the AppConfig
  monitor is wired to the failed + unverified alarms.
"""

# Standard library
import json
import pathlib
import sys

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

# Add the backend src to the path so the frozen control-plane contract is the
# single source of truth for the expected route set (no hardcoded subset).
_BACKEND_SRC = str(pathlib.Path(__file__).parents[2] / "src")
if _BACKEND_SRC not in sys.path:
    sys.path.insert(0, _BACKEND_SRC)
# Local modules
from operations.contracts.control_plane import ROUTE_KEYS  # noqa: E402

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/08-operations-control-plane.yaml"
OBSERVE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"
EXECUTE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"
DEPLOY_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-control.sh"
TEARDOWN_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/teardown-operations-control.sh"
DISABLE_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/disable-operations-control.sh"
KILL_SWITCH_SCHEMA = PROJECT_ROOT / "backend/src/operations/contracts/schemas/v1/operations-kill-switch.schema.json"

METRIC_NAMESPACE = "GameAgent/Operations"

# The frozen E4 control-plane route set, derived DIRECTLY from the backend
# contract (control_plane.ROUTE_KEYS) rather than a hardcoded subset. The
# ControlPlaneRouter dispatches every one of these, including the kill-switch
# STATUS read 'GET /operations/control/kill-switch', which the router matches
# ahead of the '/operations/{operationId}' detail catch-all. The template must
# provision exactly this set — no more, no fewer — so a contract route can never
# silently lose its API Gateway route.
E4_ROUTE_KEYS = frozenset(ROUTE_KEYS.values())

# The exact bounded DynamoDB item actions the control/read/sweeper role needs.
# The backend performs its atomic control-record write with the DynamoDB
# TransactWriteItems *API call*, but that API authorizes on the UNDERLYING
# single-item actions of each transaction item — here PutItem and UpdateItem —
# NOT on a 'dynamodb:TransactWriteItems' action, which is not a valid IAM action
# and must never appear in a policy. The role also needs point reads (GetItem)
# for compare-and-set and the workspace-scoped catalog Query the read
# projections and the expiry sweeper enumerate with. NEVER Scan, DeleteItem, or
# any Batch action.
ALLOWED_DYNAMODB_ACTIONS = frozenset(
    {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
    }
)

# The exact AppConfig authoring actions the control role needs.
ALLOWED_APPCONFIG_ACTIONS = frozenset(
    {
        "appconfig:CreateHostedConfigurationVersion",
        "appconfig:GetHostedConfigurationVersion",
        "appconfig:GetConfigurationProfile",
        "appconfig:StartDeployment",
        "appconfig:GetDeployment",
        "appconfig:StopDeployment",
        "appconfig:GetDeploymentStrategy",
    }
)

# Surfaces E4 must NEVER widen. No provider write, no PassRole, no secrets, no
# source control, no unbounded DynamoDB, no generic AppConfig admin.
E4_FORBIDDEN_ACTION_SUBSTRINGS = (
    "iam:PassRole",
    "ssm:GetParameter",
    "codecommit:",
    "codeconnections:",
    "codestar-connections:",
    "codebuild:",
    "codepipeline:",
    "gamelift:",
    "states:",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
    "dynamodb:BatchWriteItem",
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:TransactGetItems",
    "appconfig:CreateApplication",
    "appconfig:DeleteApplication",
    "appconfig:CreateEnvironment",
    "appconfig:DeleteEnvironment",
    "appconfig:CreateConfigurationProfile",
    "appconfig:DeleteConfigurationProfile",
    "appconfig:CreateDeploymentStrategy",
    "appconfig:DeleteDeploymentStrategy",
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
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _iter_action_strings(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_action_strings(value)


def _statements(template):
    roles = _resources_of_type(template, "AWS::IAM::Role")
    policies = _resources_of_type(template, "AWS::IAM::Policy")
    for name, body in list(roles.items()) + list(policies.items()):
        props = body.get("Properties", {})
        for inline in props.get("Policies", []) or []:
            doc = inline.get("PolicyDocument", {})
            for statement in doc.get("Statement", []) or []:
                yield name, inline.get("PolicyName"), statement
        if "PolicyDocument" in props:
            for statement in props["PolicyDocument"].get("Statement", []) or []:
                yield name, props.get("PolicyName"), statement


def _all_policy_actions(template):
    actions = []
    for _role, _policy, statement in _statements(template):
        for key in ("Action", "NotAction"):
            if key in statement:
                actions.extend(_iter_action_strings(statement[key]))
    return actions


def _routes(template):
    return _resources_of_type(template, "AWS::ApiGatewayV2::Route")


def _route_keys(template):
    return {r["Properties"].get("RouteKey") for r in _routes(template).values()}


def _alarms(template):
    return _resources_of_type(template, "AWS::CloudWatch::Alarm")


def _control_function_env(template):
    fn = template["Resources"]["ControlFunction"]["Properties"]
    return fn["Environment"]["Variables"]


# --------------------------------------------------------------------------- #
# Structure / provisioning gate
# --------------------------------------------------------------------------- #
def test_template_exists_and_parses(template):
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "AppConfig" in template["Description"]


def test_provisioned_defaults_false(template):
    param = template["Parameters"]["Provisioned"]
    assert param["Default"] == "false"
    assert set(param["AllowedValues"]) == {"false", "true"}


def test_control_mode_defaults_disabled(template):
    param = template["Parameters"]["ControlMode"]
    assert param["Default"] == "disabled"
    assert set(param["AllowedValues"]) == {"disabled", "enabled"}


def test_every_resource_is_provisioning_gated(template):
    # A default deploy (Provisioned=false) must create ZERO resources.
    for name, body in template["Resources"].items():
        assert body.get("Condition") == "ResourcesProvisioned", (
            f"{name} is not gated on ResourcesProvisioned; a default deploy would " f"provision it and cost money."
        )


def test_resources_provisioned_condition_drives_off_provisioned(template):
    # !Equals [!Ref Provisioned, 'true'] -> ["Provisioned", "true"] under loader.
    assert template["Conditions"]["ResourcesProvisioned"] == ["Provisioned", "true"]


def test_enabled_mode_requires_provisioning_rule(template):
    # !Not [!Equals [!Ref ControlMode, disabled]] -> [["ControlMode", "disabled"]].
    rule = template["Rules"]["EnabledModeRequiresProvisioning"]
    assert rule["RuleCondition"] == [["ControlMode", "disabled"]]


# --------------------------------------------------------------------------- #
# AppConfig resource set
# --------------------------------------------------------------------------- #
def test_appconfig_application_environment_profile_present(template):
    assert _resources_of_type(template, "AWS::AppConfig::Application")
    assert _resources_of_type(template, "AWS::AppConfig::Environment")
    profiles = _resources_of_type(template, "AWS::AppConfig::ConfigurationProfile")
    assert profiles, "missing hosted ConfigurationProfile"
    (profile,) = profiles.values()
    assert profile["Properties"]["LocationUri"] == "hosted"


def test_two_deployment_strategies_gradual_and_immediate(template):
    strategies = _resources_of_type(template, "AWS::AppConfig::DeploymentStrategy")
    by_growth_bake = {
        (s["Properties"]["GrowthFactor"], s["Properties"]["FinalBakeTimeInMinutes"]) for s in strategies.values()
    }
    # Gradual: <100% growth with a non-zero bake window; immediate: 100% no bake.
    assert any(gf < 100 and bake > 0 for gf, bake in by_growth_bake), "missing gradual strategy"
    assert (100, 0) in by_growth_bake, "missing immediate (hard-down) strategy"


def test_default_hosted_version_is_all_disabled(template):
    versions = _resources_of_type(template, "AWS::AppConfig::HostedConfigurationVersion")
    assert versions, "missing safe default HostedConfigurationVersion"
    (version,) = versions.values()
    doc = json.loads(version["Properties"]["Content"])
    assert doc["operations_enabled"] is False
    caps = doc["capabilities"]["gamelift.capacity-adjustment"]
    assert caps == {"prepare": False, "dispatch": False, "execute": False}
    assert doc["contract_version"] == "1.0"


def test_validator_schema_is_byte_equivalent_to_contract(template):
    """The AppConfig JSON_SCHEMA validator must be byte-equivalent (as parsed
    JSON) to the frozen kill-switch contract schema so AppConfig enforces the
    exact same document shape the backend validates."""
    profiles = _resources_of_type(template, "AWS::AppConfig::ConfigurationProfile")
    (profile,) = profiles.values()
    validators = profile["Properties"]["Validators"]
    json_schema_validators = [v for v in validators if v["Type"] == "JSON_SCHEMA"]
    assert len(json_schema_validators) == 1, "expected exactly one JSON_SCHEMA validator"
    embedded = json.loads(json_schema_validators[0]["Content"])
    contract = json.loads(KILL_SWITCH_SCHEMA.read_text(encoding="utf-8"))
    assert embedded == contract, "validator schema is not byte-equivalent to the contract schema"


def test_validator_schema_has_no_external_refs(template):
    profiles = _resources_of_type(template, "AWS::AppConfig::ConfigurationProfile")
    (profile,) = profiles.values()
    content = [v for v in profile["Properties"]["Validators"] if v["Type"] == "JSON_SCHEMA"][0]["Content"]
    schema = json.loads(content)

    def _refs(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref":
                    yield value
                else:
                    yield from _refs(value)
        elif isinstance(node, list):
            for item in node:
                yield from _refs(item)

    refs = list(_refs(schema))
    assert refs, "schema has no $ref at all -- unexpected"
    for ref in refs:
        assert ref.startswith("#/"), f"non-local $ref in AppConfig validator: {ref}"


# --------------------------------------------------------------------------- #
# Monitor + automatic rollback
# --------------------------------------------------------------------------- #
def test_environment_monitors_wire_real_rollback_alarms(template):
    env = _resources_of_type(template, "AWS::AppConfig::Environment")
    (env_body,) = env.values()
    monitors = env_body["Properties"]["Monitors"]
    assert monitors, "gradual deployment has no CloudWatch monitor -> no auto-rollback"
    # !GetAtt Alarm.Arn flattens to the string "Alarm.Arn". The monitor watches
    # alarms driven by metrics the backend ACTUALLY emits: a kill switch that
    # cannot be read back fresh (KillSwitchUnavailable) triggers auto-rollback.
    monitored = {m["AlarmArn"] for m in monitors}
    assert "KillSwitchUnavailableAlarm.Arn" in monitored
    for m in monitors:
        assert "AlarmRoleArn" in m


def test_monitor_role_only_describes_alarms(template):
    role = template["Resources"]["AppConfigMonitorRole"]["Properties"]
    actions = []
    for inline in role["Policies"]:
        for stmt in inline["PolicyDocument"]["Statement"]:
            actions.extend(_iter_action_strings(stmt["Action"]))
    assert actions == ["cloudwatch:DescribeAlarms"]
    assumed = role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["Service"]
    assert assumed == "appconfig.amazonaws.com"


# --------------------------------------------------------------------------- #
# Frozen routes + JWT
# --------------------------------------------------------------------------- #
def test_frozen_routes_present_and_jwt(template):
    # The provisioned route set must equal the frozen contract route set exactly,
    # derived from control_plane.ROUTE_KEYS — including the kill-switch route.
    assert _route_keys(template) == E4_ROUTE_KEYS
    assert "GET /operations/control/kill-switch" in E4_ROUTE_KEYS
    for route in _routes(template).values():
        assert route["Properties"]["AuthorizationType"] == "JWT"


# --------------------------------------------------------------------------- #
# AppConfig Lambda extension layer via validated parameter
# --------------------------------------------------------------------------- #
def test_control_function_attaches_appconfig_extension_layer(template):
    # !Ref AppConfigExtensionLayerArn flattens to the string parameter name.
    layers = template["Resources"]["ControlFunction"]["Properties"]["Layers"]
    assert "AppConfigExtensionLayerArn" in layers


def test_extension_layer_parameter_is_pattern_constrained_to_official_layer(template):
    param = template["Parameters"]["AppConfigExtensionLayerArn"]
    pattern = param["AllowedPattern"]
    # Only the official AppConfig extension layer or the public SSM resolve ref.
    assert "AWS-AppConfig-Extension" in pattern
    assert "aws-appconfig/lambda-extension" in pattern
    assert param["Default"].startswith("{{resolve:ssm:/aws/service/aws-appconfig/lambda-extension/")


def test_control_function_injects_appconfig_identifiers(template):
    env = _control_function_env(template)
    # Backend contract (operations.settings.resolve_control_plane_deployment_settings)
    # reads the AppConfig identifiers under names WITHOUT the "_ID"/"_PROFILE_ID"
    # suffix. The template must emit exactly these names or the handler fails
    # closed at load.
    assert env["GBAW_OPERATIONS_APPCONFIG_APPLICATION"] == "ControlApplication"
    assert env["GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT"] == "ControlEnvironmentResource"
    assert env["GBAW_OPERATIONS_APPCONFIG_PROFILE"] == "KillSwitchConfigurationProfile"
    assert env["GBAW_OPERATIONS_APPCONFIG_GRADUAL_STRATEGY_ID"] == "GradualDeploymentStrategy"
    assert env["GBAW_OPERATIONS_APPCONFIG_IMMEDIATE_STRATEGY_ID"] == "ImmediateDeploymentStrategy"
    # The AppConfig Lambda extension listens on a localhost port the handler GETs.
    assert env["GBAW_OPERATIONS_APPCONFIG_EXTENSION_PORT"] == "AppConfigExtensionPort"
    # The signing key is delivered as a Secrets Manager ARN (the agreed fix), not
    # a plaintext value in the template, and never appears in outputs/logs.
    assert env["GBAW_OPERATIONS_CURSOR_SIGNING_KEY_SECRET_ARN"] == "CursorSigningSecret"
    assert "GBAW_OPERATIONS_CURSOR_SIGNING_KEY" not in env, "signing key must not be a plaintext env value"
    # Whether the capability is provisioned (bool).
    assert env["GBAW_OPERATIONS_CONTROL_PROVISIONED"] == "Provisioned"
    # Kill switch lever 2 (control-plane on/off), distinct from the ADR-0001
    # deployment ceiling GBAW_OPERATIONS_MODE.
    assert env["GBAW_OPERATIONS_CONTROL_MODE"] == "ControlMode"
    # The single backend-enforced ceiling MUST be present and MUST be the
    # deployment ceiling parameter, never repurposed to the control on/off lever.
    assert env["GBAW_OPERATIONS_MODE"] == "OperationsMode"
    assert env["GBAW_OPERATIONS_MODE"] != env["GBAW_OPERATIONS_CONTROL_MODE"]
    # The freshness horizon is a code default (control_service), not an env var.
    assert "GBAW_OPERATIONS_KILL_SWITCH_FRESHNESS_SECONDS" not in env


def test_control_and_sweeper_share_the_control_mode_lever(template):
    """Both the control and the sweeper Lambda run with CONTROL_MODE=enabled/the
    ControlMode lever so an emergency disable fails BOTH closed without deleting
    resources; neither repurposes the global operations ceiling."""
    for fn_name in ("ControlFunction", "SweeperFunction"):
        env = template["Resources"][fn_name]["Properties"]["Environment"]["Variables"]
        assert env["GBAW_OPERATIONS_CONTROL_MODE"] == "ControlMode"
        assert env["GBAW_OPERATIONS_MODE"] == "OperationsMode"


# --------------------------------------------------------------------------- #
# Least-privilege IAM
# --------------------------------------------------------------------------- #
def test_no_forbidden_actions_anywhere(template):
    actions = _all_policy_actions(template)
    for forbidden in E4_FORBIDDEN_ACTION_SUBSTRINGS:
        for action in actions:
            assert forbidden not in action, f"forbidden action surface present: {action}"


def test_control_role_dynamodb_is_bounded_to_transaction_underlying_actions(template):
    ddb_actions = {a for a in _all_policy_actions(template) if a.startswith("dynamodb:")}
    assert ddb_actions == ALLOWED_DYNAMODB_ACTIONS
    # Explicitly assert the invalid/over-broad actions are absent.
    for banned in (
        "dynamodb:TransactWriteItems",
        "dynamodb:Scan",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
    ):
        assert banned not in ddb_actions


def test_control_role_secret_access_is_exactly_getsecretvalue_scoped(template):
    """The role may read ONLY the cursor-signing secret's value, nothing else in
    Secrets Manager, and no other secret ARN."""
    secret_statements = []
    for _role, _policy, statement in _statements(template):
        actions = list(_iter_action_strings(statement.get("Action", [])))
        if any(a.startswith("secretsmanager:") for a in actions):
            secret_statements.append((actions, statement.get("Resource")))
    assert secret_statements, "control role has no scoped secret read for the signing key"
    for actions, resource in secret_statements:
        assert set(actions) == {"secretsmanager:GetSecretValue"}, actions
        blob = json.dumps(resource)
        assert "CursorSigningSecret" in blob, blob
        assert resource != "*"


def test_control_role_appconfig_actions_are_exactly_allowed(template):
    ac_actions = {a for a in _all_policy_actions(template) if a.startswith("appconfig:")}
    assert ac_actions == ALLOWED_APPCONFIG_ACTIONS


def test_appconfig_authoring_scoped_to_exact_resources(template):
    """Every AppConfig authoring statement's Resource must reference the exact
    E4 application/environment/profile/strategy (via !Sub with the exact logical
    ids), never a wildcard application."""
    for role, policy, statement in _statements(template):
        actions = list(_iter_action_strings(statement.get("Action", [])))
        if not any(a.startswith("appconfig:") for a in actions):
            continue
        resources = statement.get("Resource")
        resource_blob = json.dumps(resources)
        assert "ControlApplication" in resource_blob or "DeploymentStrategy" in resource_blob, (
            f"AppConfig statement in {role}/{policy} is not scoped to the exact "
            f"E4 AppConfig resources: {resource_blob}"
        )
        assert resources != "*"


def test_no_star_star_admin_statement(template):
    for role, policy, statement in _statements(template):
        actions = list(_iter_action_strings(statement.get("Action", [])))
        resource = statement.get("Resource")
        if resource == "*":
            # Only scoped-namespace metric / xray / describe-alarm read-only
            # statements may use Resource "*".
            for action in actions:
                assert action.startswith("cloudwatch:") or action.startswith(
                    "xray:"
                ), f"unexpected wildcard-resource action {action} in {role}/{policy}"


# --------------------------------------------------------------------------- #
# EventBridge expiry sweeper
# --------------------------------------------------------------------------- #
def test_sweeper_is_a_distinct_function_with_the_sweeper_entrypoint(template):
    """The expiry sweeper is its OWN Lambda on operations.control.sweeper_entry.handler
    (NOT the control API handler), with its own log group, and it reuses the
    least-privilege control role."""
    sweeper = template["Resources"]["SweeperFunction"]["Properties"]
    assert sweeper["Handler"] == "operations.control.sweeper_entry.handler"
    control = template["Resources"]["ControlFunction"]["Properties"]
    assert control["Handler"] == "operations.control.control_entry.handler"
    assert sweeper["Handler"] != control["Handler"]
    # Its own dedicated log group (no orphan/unused log group left behind).
    assert "SweeperLogGroup" in template["Resources"]
    assert template["Resources"]["SweeperLogGroup"]["Type"] == "AWS::Logs::LogGroup"


def test_expiry_sweeper_targets_the_sweeper_function(template):
    rules = _resources_of_type(template, "AWS::Events::Rule")
    assert rules, "missing EventBridge freshness-expiry sweeper"
    (rule,) = rules.values()
    props = rule["Properties"]
    assert props["ScheduleExpression"].startswith("rate(")
    targets = props["Targets"]
    # The rule must target the DISTINCT sweeper function, not the control API fn.
    assert any(t["Arn"] == "SweeperFunction.Arn" for t in targets)
    assert not any(t["Arn"] == "ControlFunction.Arn" for t in targets)
    # Sweeper is disabled when the control plane is disabled (fail-closed).
    assert props["State"] == ["ControlEnabled", "ENABLED", "DISABLED"]


def test_sweeper_has_invoke_permission_on_the_sweeper_function(template):
    perms = _resources_of_type(template, "AWS::Lambda::Permission")
    events_perms = [p for p in perms.values() if p["Properties"]["Principal"] == "events.amazonaws.com"]
    assert events_perms, "missing EventBridge invoke permission for the sweeper"
    for perm in events_perms:
        assert perm["Properties"]["FunctionName"] == "SweeperFunction"


def test_no_orphan_log_group(template):
    """Every provisioned log group is referenced by a function (no unused group)."""
    log_groups = set(_resources_of_type(template, "AWS::Logs::LogGroup"))
    referenced = set()
    for fn in _resources_of_type(template, "AWS::Lambda::Function").values():
        # DependsOn / Environment references flatten to logical-id strings.
        blob = json.dumps(fn)
        for lg in log_groups:
            if lg in blob:
                referenced.add(lg)
    # Access log group is referenced by the API stage, not a function.
    for stage in _resources_of_type(template, "AWS::ApiGatewayV2::Stage").values():
        blob = json.dumps(stage)
        for lg in log_groups:
            if lg in blob or lg.replace("Group", "") in blob:
                referenced.add(lg)
    unused = log_groups - referenced - {"ControlAccessLogGroup"}
    assert not unused, f"orphan/unused log group(s): {unused}"


# --------------------------------------------------------------------------- #
# Alarms: every #416 control-plane alarm, truthful metric/statistic
# --------------------------------------------------------------------------- #
# Every E4 alarm MUST reference a metric the backend actually emits. Backend
# emitters (verified against operations/**/metrics.py at the backend contract):
#   control/metrics.py     -> ControlApplied, ControlVersionConflict,
#                             ControlDenied, KillSwitchUnavailable,
#                             OperationsExpirySweepExpired
#   execute/metrics.py     -> ExecutionFailures, ExecutionHumanReconciliationRequired
# There is NO emitted signal for "retrying", "rollback-failed", or a
# "deployments-started" budget count, so those alarms are NOT provisioned
# (fabricating a metric would create an alarm that can never fire truthfully).
BACKEND_EMITTED_OPERATIONS_METRICS = frozenset(
    {
        "ControlApplied",
        "ControlVersionConflict",
        "ControlDenied",
        "KillSwitchUnavailable",
        "OperationsExpirySweepExpired",
        "ExecutionFailures",
        "ExecutionHumanReconciliationRequired",
        "ObservationFailures",
        "ObservationTimeouts",
        "StuckOperations",
        "ObservationRequestLatency",
        "ExecutionRequestLatency",
        "ExecutionReconciled",
        "ExecutionProviderWrites",
        "PreparationFailures",
        "ApprovalFailures",
        "ApprovalExpired",
        "CancellationConflicts",
    }
)

# The E4 stack's own required alarms (all on backend-emitted metrics).
E4_REQUIRED_ALARM_METRICS = frozenset(
    {
        "KillSwitchUnavailable",
        "ControlVersionConflict",
        "ControlDenied",
        "OperationsExpirySweepExpired",
        "ExecutionHumanReconciliationRequired",
    }
)


def test_all_416_alarms_reference_only_backend_emitted_metrics(template):
    alarms = _alarms(template)
    by_metric = {a["Properties"]["MetricName"]: a["Properties"] for a in alarms.values()}
    # Every alarm's metric must be a real emitted signal -- no fabrication.
    for metric in by_metric:
        assert metric in BACKEND_EMITTED_OPERATIONS_METRICS, f"alarm on non-emitted metric: {metric}"
    # The required E4 alarms are all present.
    assert E4_REQUIRED_ALARM_METRICS.issubset(
        set(by_metric)
    ), f"missing #416 alarms: {E4_REQUIRED_ALARM_METRICS - set(by_metric)}"
    for metric, props in by_metric.items():
        assert props["Namespace"] == METRIC_NAMESPACE
        assert props.get("Statistic") in {"Sum", "Maximum"}
        assert "ExtendedStatistic" not in props, f"{metric} misuses a percentile"


def test_no_fabricated_deployment_metric_alarms(template):
    """The template must not resurrect the fabricated deployment-lifecycle
    metrics that the backend never emits."""
    fabricated = {
        "KillSwitchFailed",
        "KillSwitchStuck",
        "KillSwitchRetrying",
        "KillSwitchUnverified",
        "KillSwitchRollbackFailed",
        "KillSwitchDeploymentsStarted",
        "OperationsDisabled",
    }
    present = {a["Properties"]["MetricName"] for a in _alarms(template).values()}
    assert not (fabricated & present), f"fabricated-metric alarm(s) present: {fabricated & present}"


# --------------------------------------------------------------------------- #
# Wrappers / scripts exist and verify identity
# --------------------------------------------------------------------------- #
def test_deploy_wrapper_exists_and_resolves_extension_layer():
    assert DEPLOY_WRAPPER.exists(), f"missing deploy wrapper: {DEPLOY_WRAPPER}"
    body = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # Resolves the authoritative regional layer from the public SSM parameter.
    assert "/aws/service/aws-appconfig/lambda-extension/x86/latest" in body
    # Verifies identity/region before any write.
    assert "get-caller-identity" in body
    assert "AppConfigExtensionLayerArn" in body


def test_teardown_and_disable_wrappers_exist():
    assert TEARDOWN_WRAPPER.exists(), f"missing teardown wrapper: {TEARDOWN_WRAPPER}"
    assert DISABLE_WRAPPER.exists(), f"missing disable wrapper: {DISABLE_WRAPPER}"


def test_deploy_wrapper_probe_covers_the_kill_switch_route():
    """The packaging/runtime probe must verify the backend serves the frozen
    kill-switch route, so a package whose contract dropped it fails closed before
    upload instead of 404-ing the provisioned API Gateway route at runtime."""
    body = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # Runtime (container) probe asserts the contract route set carries kill_switch.
    assert 'ROUTE_KEYS.get("kill_switch")' in body
    assert "KILL_SWITCH_ROUTE" in body
    # Structural (no-container) probe greps the packaged contract for the route.
    assert "GET /operations/control/kill-switch" in body


def test_disable_wrapper_is_reversible_lever_flip_only():
    body = DISABLE_WRAPPER.read_text(encoding="utf-8")
    # Data-preserving: keeps Provisioned=true, flips ControlMode=disabled only.
    assert "ParameterKey=Provisioned,ParameterValue=true" in body
    assert "ParameterKey=ControlMode,ParameterValue=disabled" in body
    assert "--confirm" in body
    assert "get-caller-identity" in body


# --------------------------------------------------------------------------- #
# AppConfig validator behaviour: the embedded schema must validate the safe
# default + valid fixtures and reject a bad document using a BARE Draft 2020-12
# validator with NO registry, exactly as AWS AppConfig does.
# --------------------------------------------------------------------------- #
def _embedded_schema(template):
    profiles = _resources_of_type(template, "AWS::AppConfig::ConfigurationProfile")
    (profile,) = profiles.values()
    content = [v for v in profile["Properties"]["Validators"] if v["Type"] == "JSON_SCHEMA"][0]["Content"]
    return json.loads(content)


def test_embedded_validator_accepts_default_and_valid_fixtures(template):
    # Third-party packages
    from jsonschema import Draft202012Validator

    schema = _embedded_schema(template)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)  # no registry: self-contained.
    for name in (
        "operations-kill-switch.default-safe.json",
        "operations-kill-switch.valid.json",
    ):
        doc = json.loads((PROJECT_ROOT / "backend/tests/fixtures/operations/v1" / name).read_text(encoding="utf-8"))
        assert list(validator.iter_errors(doc)) == [], f"{name} should validate under the embedded schema"


def test_embedded_validator_rejects_bad_documents(template):
    # Third-party packages
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_embedded_schema(template))
    bad_docs = [
        # unknown capability
        {
            "contract_version": "1.0",
            "config_version": 1,
            "issued_at": "2026-01-15T00:00:00Z",
            "not_after": "2026-01-15T00:05:00Z",
            "operations_enabled": False,
            "capabilities": {
                "gamelift.capacity-adjustment": {"prepare": False, "dispatch": False, "execute": False},
                "rogue": {},
            },
        },
        # offset (non-Z) timestamp
        {
            "contract_version": "1.0",
            "config_version": 1,
            "issued_at": "2026-01-15T00:00:00+00:00",
            "not_after": "2026-01-15T00:05:00Z",
            "operations_enabled": False,
            "capabilities": {"gamelift.capacity-adjustment": {"prepare": False, "dispatch": False, "execute": False}},
        },
        # missing required capability
        {
            "contract_version": "1.0",
            "config_version": 1,
            "issued_at": "2026-01-15T00:00:00Z",
            "not_after": "2026-01-15T00:05:00Z",
            "operations_enabled": False,
            "capabilities": {},
        },
        # wrong contract_version const
        {
            "contract_version": "2.0",
            "config_version": 1,
            "issued_at": "2026-01-15T00:00:00Z",
            "not_after": "2026-01-15T00:05:00Z",
            "operations_enabled": False,
            "capabilities": {"gamelift.capacity-adjustment": {"prepare": False, "dispatch": False, "execute": False}},
        },
    ]
    for doc in bad_docs:
        assert list(validator.iter_errors(doc)), f"embedded schema must reject: {doc}"
