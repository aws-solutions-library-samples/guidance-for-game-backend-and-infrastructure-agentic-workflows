"""Parser- and scanner-verifiable contract tests for the OPTIONAL E5 bounded
operations AUTONOMY plane (GitHub issue #440, track A "stack").

E5 adds a durable, identifier-only bounded-autonomy evaluator on top of the
accepted E1 observation, E2 advise, E3 execution, and E4 control planes. It is
delivered as a SEPARATE, default-unprovisioned CloudFormation stack
(``09-operations-autonomy.yaml``) plus a small ADDITIVE opt-in autonomy wiring
on the existing E3 execution stack (``07-operations-execution.yaml``). The
contract this file freezes:

* **Default deployment creates no autonomy resources and costs $0.** Every 09
  resource is gated on a ``ResourcesProvisioned`` condition driven by a
  ``Provisioned`` parameter that defaults ``false``. The main deployment
  (``deploy-all.sh``) never references 09; only an optional reviewed wrapper may.
* **No autonomy component holds a provider-write permission or an executor
  credential.** The 09 evaluator role may touch ONLY: the exact operations
  DynamoDB table, the operations CMK strictly via DynamoDB (kms:ViaService), the
  exact AppConfig autonomy data plane, the exact existing E3 Standard state
  machine ``states:StartExecution``, and its own logs/metrics/X-Ray. It holds NO
  ``gamelift:*``, NO ``lambda:InvokeFunction``, NO ``iam:PassRole``, and no
  wildcard write.
* **Only the existing authenticated Standard workflow can invoke the existing
  executor.** 09 never creates a second executor Lambda and never creates a
  second provider-write role; it starts the SAME E3 state machine by exact ARN.
* **Emergency disable is real and reversible.** ``AutonomyMode=disabled`` (the
  default) keeps the EventBridge evaluation rule/event source DISABLED and
  injects ``GBAW_OPERATIONS_AUTONOMY_ENABLED=false`` so both evaluation and the
  immediate pre-write autonomy hook fail closed — WITHOUT deleting any resource
  or data.
* **The autonomy AppConfig switch is SEPARATE from the E4 kill switch**, seeded
  with a default-DISABLED, fail-closed document, and E4's frozen kill-switch
  schema is unchanged (09 never inlines or mutates it).
* **The 07 execution stack gets ADDITIVE autonomy wiring only.** New parameters
  default empty/``disabled`` so a deploy that does not supply them behaves
  EXACTLY as the accepted v1 E3 stack: no autonomy env, no extra IAM, no second
  executor, no provider-write role. When explicitly enabled (AutonomyMode=operate
  with the identifiers supplied) the EXISTING executor gets the separate
  autonomy AppConfig read plus the autonomy identifiers, and its static mode /
  capability maximum become ``operate``.
* **Template size stays inline-safe.** The 09 template stays under
  CloudFormation's 51,200-byte inline limit.

These tests never call AWS. They parse the repository-owned CloudFormation
templates as data. The ``_cfn_yaml`` loader flattens CloudFormation short-form
``!`` intrinsics: ``!Ref X`` -> ``"X"``, ``!GetAtt A.Arn`` -> ``"A.Arn"``,
``!Equals [a, b]`` -> ``["a", "b"]``, ``!If [c, a, b]`` -> ``["c", "a", "b"]``,
``!Sub 'arn:...'`` -> ``"arn:..."``. The assertions below match that flattened
form.
"""

# Standard library
import json
import pathlib
import sys

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

# Add the backend src to the path so the frozen autonomy-switch parser and the
# capability id are the single source of truth for the expected document shape.
_BACKEND_SRC = str(pathlib.Path(__file__).parents[2] / "src")
if _BACKEND_SRC not in sys.path:
    sys.path.insert(0, _BACKEND_SRC)
# Local modules
from operations.autonomy_switch import validate_autonomy_switch_document  # noqa: E402
from operations.contracts.autonomy import CAPABILITY_ID  # noqa: E402

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
AUTONOMY_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/09-operations-autonomy.yaml"
EXECUTE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"
KILL_SWITCH_SCHEMA = PROJECT_ROOT / "backend/src/operations/contracts/schemas/v1/operations-kill-switch.schema.json"
DEPLOY_ALL = PROJECT_ROOT / "deploy-all.sh"
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts/deploy.sh"

METRIC_NAMESPACE = "GameAgent/Operations"
INLINE_LIMIT = 51200

# The evaluator entry point the deployable Lambda must use (issue #439 runtime).
EVALUATOR_HANDLER = "operations.autonomy_runtime.evaluator_entry.handler"

# The exact AppConfig data-plane read actions the evaluator/executor may use for
# the autonomy switch — the SAME read pair E4 uses, no authoring/admin action.
APPCONFIG_READ_ACTIONS = frozenset({"appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"})

# The exact underlying DynamoDB single-item actions the evaluator needs to
# persist the immutable evidence bundle and reserve the rolling window state.
# (There is no dynamodb:TransactWriteItems IAM action; it authorizes on the
# underlying PutItem/UpdateItem/GetItem.) NEVER Scan/DeleteItem/Batch.
ALLOWED_DYNAMODB_ACTIONS = frozenset(
    {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
    }
)

# Surfaces the E5 autonomy plane must NEVER hold. A provider write, an executor
# invoke, PassRole, secrets, source control, generic build, or an unbounded
# DynamoDB verb would each break the "no provider-write / no second executor"
# invariant.
E5_FORBIDDEN_ACTION_SUBSTRINGS = (
    "gamelift:",
    "lambda:InvokeFunction",
    "lambda:Invoke",
    "iam:PassRole",
    "secretsmanager:",
    "ssm:GetParameter",
    "codecommit:",
    "codebuild:",
    "codepipeline:",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
    "dynamodb:BatchWriteItem",
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:TransactGetItems",
    "appconfig:CreateApplication",
    "appconfig:CreateConfigurationProfile",
    "appconfig:CreateHostedConfigurationVersion",
    "appconfig:StartDeployment",
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def template():
    assert AUTONOMY_TEMPLATE.exists(), f"missing template: {AUTONOMY_TEMPLATE}"
    return load_cfn_template(AUTONOMY_TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def execute_template():
    assert EXECUTE_TEMPLATE.exists(), f"missing template: {EXECUTE_TEMPLATE}"
    return load_cfn_template(EXECUTE_TEMPLATE.read_text(encoding="utf-8"))


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


def _lambda_functions(template):
    return _resources_of_type(template, "AWS::Lambda::Function")


def _evaluator_function(template):
    fns = _lambda_functions(template)
    for name, body in fns.items():
        if "evaluator" in name.lower():
            return name, body
    raise AssertionError(f"no evaluator Lambda found in {sorted(fns)}")


# --------------------------------------------------------------------------- #
# Structure / provisioning gate — default deployment is $0.
# --------------------------------------------------------------------------- #
def test_template_exists_and_parses(template):
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "autonomy" in template["Description"].lower()


def test_provisioned_defaults_false(template):
    param = template["Parameters"]["Provisioned"]
    assert param["Default"] == "false"
    assert set(param["AllowedValues"]) == {"false", "true"}


def test_autonomy_mode_defaults_disabled(template):
    param = template["Parameters"]["AutonomyMode"]
    assert param["Default"] == "disabled"
    assert set(param["AllowedValues"]) == {"disabled", "operate"}


def test_every_resource_is_provisioning_gated(template):
    # A default deploy (Provisioned=false) must create ZERO resources -> $0.
    for name, body in template["Resources"].items():
        assert body.get("Condition") == "ResourcesProvisioned", (
            f"{name} is not gated on ResourcesProvisioned; a default deploy would " f"provision it and cost money."
        )


def test_resources_provisioned_condition_drives_off_provisioned(template):
    # !Equals [!Ref Provisioned, 'true'] -> ["Provisioned", "true"] under loader.
    assert template["Conditions"]["ResourcesProvisioned"] == ["Provisioned", "true"]


def test_autonomy_operate_condition_drives_off_autonomy_mode(template):
    conds = template["Conditions"]
    operate = [v for v in conds.values() if v == ["AutonomyMode", "operate"]]
    assert operate, "expected a condition of the form AutonomyMode == 'operate'"


def test_provision_and_operate_only_when_true(template):
    # AutonomyMode=operate must be provisioned+enabled together; a rule rejects
    # enabling autonomy against a zero-resource stack.
    rules = template.get("Rules", {})
    assert rules, "09 must declare Rules that reject operate-without-provisioning"
    joined = json.dumps(rules)
    assert "Provisioned" in joined and "AutonomyMode" in joined


# --------------------------------------------------------------------------- #
# The evaluator Lambda uses the frozen #439 handler and holds no write client.
# --------------------------------------------------------------------------- #
def test_evaluator_uses_frozen_handler_entrypoint(template):
    _name, body = _evaluator_function(template)
    assert body["Properties"]["Handler"] == EVALUATOR_HANDLER


def test_exactly_one_lambda_function_and_it_is_the_evaluator(template):
    fns = _lambda_functions(template)
    assert len(fns) == 1, f"09 must define exactly one Lambda (the evaluator), got {sorted(fns)}"
    (name,) = fns
    assert "evaluator" in name.lower()


def test_no_second_executor_or_provider_write_function(template):
    # 09 must never create an executor Lambda; the single provider write stays
    # with the E3 executor invoked by the E3 state machine.
    for name in _lambda_functions(template):
        assert "executor" not in name.lower(), f"09 must not create an executor Lambda ({name})"


def test_evaluator_attaches_official_appconfig_extension_layer_by_parameter(template):
    _name, body = _evaluator_function(template)
    layers = body["Properties"].get("Layers")
    assert layers, "evaluator must attach the official AppConfig extension layer"
    # The layer is attached via !If[AutonomyOperate, <param>, AWS::NoValue] so it
    # is absent on a default/disabled deploy. Under the loader the !If flattens to
    # a list whose condition + branches reference the validated parameter, never a
    # hardcoded literal ARN.
    assert "AppConfigExtensionLayerArn" in template["Parameters"]
    found = False
    for layer in layers:
        blob = str(layer)
        if "AutonomyOperate" in blob and "AppConfigExtensionLayerArn" in blob:
            found = True
    assert found, f"evaluator layer must be !If[AutonomyOperate, <param>, NoValue], got {layers}"


def test_evaluator_has_a_dead_letter_queue(template):
    _name, body = _evaluator_function(template)
    dlq = body["Properties"].get("DeadLetterConfig")
    assert dlq and dlq.get("TargetArn"), "evaluator Lambda must declare a DeadLetterConfig target"
    assert _resources_of_type(template, "AWS::SQS::Queue"), "09 must provision a DLQ"


# --------------------------------------------------------------------------- #
# EventBridge evaluation rule / event source is DISABLED unless explicitly on.
# --------------------------------------------------------------------------- #
def test_evaluation_rule_disabled_unless_operate(template):
    rules = _resources_of_type(template, "AWS::Events::Rule")
    assert rules, "09 must define the EventBridge evaluation rule"
    for name, body in rules.items():
        state = body["Properties"].get("State")
        # !If [AutonomyOperate, ENABLED, DISABLED] -> ["AutonomyOperate"/cond, "ENABLED", "DISABLED"]
        assert isinstance(state, list) and state[-2:] == ["ENABLED", "DISABLED"], (
            f"{name} State must be !If[<operate>, ENABLED, DISABLED] so a default "
            f"(disabled) deploy leaves the event source DISABLED, got {state}"
        )


# --------------------------------------------------------------------------- #
# Emergency disable: default injects AUTONOMY_ENABLED=false (fail closed).
# --------------------------------------------------------------------------- #
def test_disable_lever_injected_into_evaluator_env(template):
    _name, body = _evaluator_function(template)
    env = body["Properties"]["Environment"]["Variables"]
    assert "GBAW_OPERATIONS_AUTONOMY_ENABLED" in env, "evaluator must receive the autonomy enable flag"
    # !If [AutonomyOperate, 'true', 'false'] -> [<cond>, "true", "false"].
    flag = env["GBAW_OPERATIONS_AUTONOMY_ENABLED"]
    assert isinstance(flag, list) and flag[-2:] == ["true", "false"], (
        f"AUTONOMY_ENABLED must be !If[<operate>, 'true', 'false'] so a default " f"deploy fails closed, got {flag}"
    )


def test_evaluator_static_mode_is_operate_only_when_enabled(template):
    _name, body = _evaluator_function(template)
    env = body["Properties"]["Environment"]["Variables"]
    # The evaluator only ever runs at operate authority; the disable lever is the
    # separate AUTONOMY_ENABLED flag + the disabled event source.
    assert env.get("GBAW_OPERATIONS_MODE") in ("operate", ["AutonomyOperate", "operate", "disabled"])


def test_evaluator_binds_state_machine_and_autonomy_identifiers(template):
    _name, body = _evaluator_function(template)
    env = body["Properties"]["Environment"]["Variables"]
    for key in (
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN",
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH",
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID",
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT",
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE",
    ):
        assert key in env, f"evaluator env missing {key}"


# --------------------------------------------------------------------------- #
# Least-privilege IAM: the evaluator role holds NO provider write / executor
# invoke / PassRole; only the exact table, CMK-via-DynamoDB, autonomy AppConfig
# read, exact StartExecution, and logs/metrics/X-Ray.
# --------------------------------------------------------------------------- #
def test_evaluator_role_has_no_forbidden_actions(template):
    for action in _all_policy_actions(template):
        for forbidden in E5_FORBIDDEN_ACTION_SUBSTRINGS:
            assert forbidden not in action, f"09 evaluator role leaks forbidden action {action}"


def test_evaluator_dynamodb_actions_are_bounded_single_item(template):
    ddb = {a for a in _all_policy_actions(template) if a.startswith("dynamodb:")}
    assert ddb, "evaluator must have bounded DynamoDB access to the operations table"
    assert ddb <= ALLOWED_DYNAMODB_ACTIONS, f"evaluator DynamoDB actions exceed the bounded set: {ddb}"


def test_evaluator_dynamodb_scoped_to_exact_table(template):
    for _role, _policy, stmt in _statements(template):
        actions = list(_iter_action_strings(stmt.get("Action", [])))
        if any(a.startswith("dynamodb:") for a in actions):
            resource = stmt.get("Resource")
            assert resource != "*" and resource != ["*"], "DynamoDB access must not be wildcard"
            blob = str(resource)
            assert "table/" in blob, f"DynamoDB access must be scoped to the exact table: {blob}"


def test_evaluator_kms_is_via_dynamodb_only(template):
    saw_kms = False
    for _role, _policy, stmt in _statements(template):
        actions = list(_iter_action_strings(stmt.get("Action", [])))
        if any(a.startswith("kms:") for a in actions):
            saw_kms = True
            cond = json.dumps(stmt.get("Condition", {}))
            assert (
                "kms:ViaService" in cond and "dynamodb" in cond
            ), "KMS access must be pinned to DynamoDB via kms:ViaService"
    assert saw_kms, "evaluator must hold the CMK data-plane pinned to DynamoDB"


def test_evaluator_appconfig_read_actions_exactly_the_read_pair(template):
    appconfig = {a for a in _all_policy_actions(template) if a.startswith("appconfig:")}
    assert appconfig == APPCONFIG_READ_ACTIONS, f"09 appconfig actions must be exactly the read pair, got {appconfig}"


def test_evaluator_appconfig_read_scoped_to_autonomy_profile(template):
    for _role, _policy, stmt in _statements(template):
        actions = list(_iter_action_strings(stmt.get("Action", [])))
        if any(a.startswith("appconfig:") for a in actions):
            resource = stmt.get("Resource")
            assert resource != "*", "AppConfig read must not use wildcard resource"
            blob = str(resource)
            assert (
                "application/" in blob and "configuration/" in blob
            ), f"AppConfig read must be scoped to the autonomy configuration resource: {blob}"


def test_evaluator_start_execution_scoped_to_exact_state_machine(template):
    start_stmts = []
    for _role, _policy, stmt in _statements(template):
        actions = list(_iter_action_strings(stmt.get("Action", [])))
        if "states:StartExecution" in actions:
            start_stmts.append(stmt)
    assert start_stmts, "evaluator must be able to StartExecution on the E3 state machine"
    for stmt in start_stmts:
        resource = stmt.get("Resource")
        assert resource != "*" and resource != ["*"], "StartExecution must be scoped to the exact state machine ARN"
        # !Ref ExecutionStateMachineArn -> ["ExecutionStateMachineArn"]; that
        # parameter's AllowedPattern pins the ...:stateMachine:... ARN shape.
        assert "ExecutionStateMachineArn" in str(
            resource
        ), f"StartExecution must be scoped to the exact E3 state machine ARN parameter, got {resource}"
    pattern = template["Parameters"]["ExecutionStateMachineArn"].get("AllowedPattern", "")
    assert "stateMachine" in pattern, "the state machine ARN parameter must pin the stateMachine ARN shape"


def test_evaluator_role_never_invokes_a_lambda(template):
    for action in _all_policy_actions(template):
        assert action != "lambda:InvokeFunction", (
            "the evaluator must NOT invoke any Lambda; only the E3 state machine " "invokes the single executor"
        )


# --------------------------------------------------------------------------- #
# Separate AppConfig autonomy switch, default-disabled, fail-closed document.
# --------------------------------------------------------------------------- #
def test_autonomy_appconfig_profile_present(template):
    profiles = _resources_of_type(template, "AWS::AppConfig::ConfigurationProfile")
    assert profiles, "09 must provision its own AppConfig autonomy ConfigurationProfile"
    for profile in profiles.values():
        assert profile["Properties"]["LocationUri"] == "hosted"


def test_default_autonomy_document_is_disabled_and_fail_closed(template):
    versions = _resources_of_type(template, "AWS::AppConfig::HostedConfigurationVersion")
    assert versions, "09 must seed a safe default autonomy document"
    (version,) = versions.values()
    doc = json.loads(version["Properties"]["Content"])
    # It parses against the frozen in-code autonomy-switch validator...
    validate_autonomy_switch_document(doc)
    # ...and is disabled on every axis.
    assert doc["autonomy_enabled"] is False
    caps = doc["capabilities"][CAPABILITY_ID]
    assert caps == {"autonomous_write": False}
    # Fail closed: the seeded default is already expired so it never reads fresh
    # until the reviewed control plane issues a live document.
    assert doc["not_after"] <= "1971-01-01T00:00:00Z"


def test_autonomy_switch_does_not_reuse_or_mutate_e4_schema(template):
    # The E4 kill-switch schema must be UNCHANGED and NOT embedded in 09.
    raw = AUTONOMY_TEMPLATE.read_text(encoding="utf-8")
    assert "operations-kill-switch" not in raw, "09 must not embed the E4 kill-switch schema"
    # The autonomy document uses autonomy_enabled/autonomous_write, never the E4
    # operations_enabled/prepare/dispatch/execute phase shape.
    assert "operations_enabled" not in raw
    assert "autonomy_enabled" in raw


# --------------------------------------------------------------------------- #
# Monitors and alarms exist in the retained namespace.
# --------------------------------------------------------------------------- #
def test_alarms_present_in_operations_namespace(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    assert alarms, "09 must provision autonomy monitors/alarms"
    namespaces = {body["Properties"]["Namespace"] for body in alarms.values()}
    # Autonomy signal alarms live in the retained operations namespace; the DLQ
    # depth alarm necessarily uses the AWS/SQS namespace.
    assert METRIC_NAMESPACE in namespaces, "09 must alarm on a GameAgent/Operations autonomy signal"
    assert namespaces <= {METRIC_NAMESPACE, "AWS/SQS"}, f"unexpected alarm namespace(s): {namespaces}"


def test_log_group_present(template):
    groups = _resources_of_type(template, "AWS::Logs::LogGroup")
    assert groups, "09 must provision the evaluator log group"


# --------------------------------------------------------------------------- #
# The main deployment never provisions E5; only optional wrappers may.
# --------------------------------------------------------------------------- #
def test_main_deployment_never_references_the_autonomy_template():
    for script in (DEPLOY_ALL, DEPLOY_SCRIPT):
        if script.exists():
            text = script.read_text(encoding="utf-8")
            assert "09-operations-autonomy" not in text, (
                f"{script.name} must NOT deploy the E5 autonomy stack; the main " f"deployment stays deploy-all.sh only"
            )


# --------------------------------------------------------------------------- #
# Template size stays inline-safe.
# --------------------------------------------------------------------------- #
def test_autonomy_template_under_inline_limit():
    size = AUTONOMY_TEMPLATE.stat().st_size
    assert size <= INLINE_LIMIT, (
        f"09 template is {size} bytes, over the {INLINE_LIMIT}-byte inline limit; "
        f"an over-limit template needs the S3-backed deploy path"
    )


# =========================================================================== #
# 07 ADDITIVE autonomy wiring — preserves v1 defaults, never a second executor.
# =========================================================================== #
def test_07_autonomy_parameters_added_and_default_safe(execute_template):
    params = execute_template["Parameters"]
    # AutonomyMode gates the additive wiring; default disabled preserves v1.
    assert "AutonomyMode" in params, "07 must add an AutonomyMode parameter"
    assert params["AutonomyMode"]["Default"] == "disabled"
    assert set(params["AutonomyMode"]["AllowedValues"]) == {"disabled", "operate"}
    # Every other autonomy identifier defaults empty so an un-supplied deploy is
    # behaviourally identical to the accepted v1 E3 stack.
    for name in (
        "AutonomyStateMachineArn",
        "AutonomySwitchProfileId",
        "AutonomyApplicationId",
        "AutonomyEnvironmentId",
        "AutonomyPolicyId",
        "AutonomyPolicyVersion",
        "AutonomyPolicyHash",
        "AutonomyStateId",
        "AutonomySubject",
        "AutonomyClient",
    ):
        assert name in params, f"07 missing additive autonomy parameter {name}"
        assert (
            params[name].get("Default", "MISSING") == ""
        ), f"07 autonomy parameter {name} must default empty (additive/opt-in)"


def test_07_autonomy_operate_condition(execute_template):
    conds = execute_template["Conditions"]
    assert any(
        v == ["AutonomyMode", "operate"] for v in conds.values()
    ), "07 must gate the additive autonomy wiring on AutonomyMode == 'operate'"


def test_07_does_not_create_a_second_executor_or_write_role(execute_template):
    # The additive wiring must NOT add another Lambda or a second GameLift write
    # role: exactly the v1 dispatcher + executor functions remain.
    fns = _resources_of_type(execute_template, "AWS::Lambda::Function")
    names = sorted(fns)
    assert len(fns) == 2, f"07 must keep exactly the dispatcher + executor Lambdas, got {names}"
    gamelift_write_roles = 0
    for _role, _policy, stmt in _statements(execute_template):
        actions = list(_iter_action_strings(stmt.get("Action", [])))
        if "gamelift:UpdateFleetCapacity" in actions:
            gamelift_write_roles += 1
    assert gamelift_write_roles == 1, (
        "07 must keep exactly ONE provider-write (UpdateFleetCapacity) grant; the "
        "autonomy wiring must never add a second provider-write role"
    )


def test_07_executor_autonomy_env_injected_conditionally(execute_template):
    executor = None
    for name, body in _resources_of_type(execute_template, "AWS::Lambda::Function").items():
        if "executor" in name.lower() or "Executor" in name:
            executor = body
    assert executor is not None, "07 must retain the executor Lambda"
    env = executor["Properties"]["Environment"]["Variables"]
    # The enable flag is injected as !If[<operate>, 'true', 'false'] so v1 stays
    # false and the autonomy hook is absent by default.
    assert "GBAW_OPERATIONS_AUTONOMY_ENABLED" in env
    flag = env["GBAW_OPERATIONS_AUTONOMY_ENABLED"]
    assert isinstance(flag, list) and flag[-2:] == ["true", "false"]
    # The autonomy identifiers are threaded so the existing executor's pre-write
    # autonomy hook can resolve its settings when enabled.
    for key in (
        "GBAW_OPERATIONS_AUTONOMY_STATE_MACHINE_ARN",
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID",
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID",
    ):
        assert key in env, f"07 executor autonomy env missing {key}"


def test_07_executor_mode_and_capability_operate_only_when_enabled(execute_template):
    executor = None
    for name, body in _resources_of_type(execute_template, "AWS::Lambda::Function").items():
        if "executor" in name.lower() or "Executor" in name:
            executor = body
    env = executor["Properties"]["Environment"]["Variables"]
    # GBAW_OPERATIONS_MODE must fall back to the v1 ExecutionMode kill switch when
    # autonomy is disabled, and become 'operate' only under AutonomyMode=operate.
    mode = env["GBAW_OPERATIONS_MODE"]
    assert (
        isinstance(mode, list) and "operate" in mode and "ExecutionMode" in mode
    ), f"07 executor GBAW_OPERATIONS_MODE must be !If[<operate>, 'operate', <ExecutionMode>], got {mode}"
    cap = env["GBAW_OPERATIONS_CAPABILITY_MAXIMUM"]
    assert isinstance(cap, list) and cap[-2:] == [
        "operate",
        "remediate",
    ], f"07 executor capability maximum must be !If[<operate>, 'operate', 'remediate'], got {cap}"


def test_07_autonomy_appconfig_read_scoped_and_actions_unchanged(execute_template):
    # The additive autonomy AppConfig read reuses the SAME two data-plane read
    # actions (so the frozen appconfig action set is unchanged) but adds a
    # separate resource scope for the autonomy profile.
    appconfig = {a for a in _all_policy_actions(execute_template) if a.startswith("appconfig:")}
    assert appconfig == APPCONFIG_READ_ACTIONS, f"07 appconfig actions must stay exactly the read pair, got {appconfig}"
    # There must be an autonomy-scoped AppConfig read resource present when the
    # wiring exists (a distinct configuration ARN referencing the autonomy ids).
    raw = EXECUTE_TEMPLATE.read_text(encoding="utf-8")
    assert (
        "AutonomySwitchProfileId" in raw and "AutonomyApplicationId" in raw
    ), "07 must reference the autonomy AppConfig identifiers in its read scope"


def test_07_no_forbidden_actions_added(execute_template):
    # The additive wiring must not widen authority beyond v1: still no PassRole,
    # no secrets, no source control, no extra Lambda invoke beyond the workflow.
    forbidden = ("iam:PassRole", "secretsmanager:", "codecommit:", "codebuild:", "codepipeline:")
    for action in _all_policy_actions(execute_template):
        for f in forbidden:
            assert f not in action, f"07 additive wiring leaked {action}"


def test_e4_kill_switch_schema_file_unchanged_by_e5():
    # Guard: E5 must not touch the frozen E4 schema file. Its top-level id/title
    # remain the kill-switch contract, never an autonomy variant.
    schema = json.loads(KILL_SWITCH_SCHEMA.read_text(encoding="utf-8"))
    assert schema["title"] == "Operations Kill-Switch AppConfig Document 1.0"
    assert "autonomy" not in json.dumps(schema).lower()
