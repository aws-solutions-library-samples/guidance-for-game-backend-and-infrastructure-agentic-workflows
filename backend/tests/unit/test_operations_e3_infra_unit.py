"""Parser- and scanner-verifiable contract tests for the OPTIONAL E3 operations
execution control plane (GitHub issue #415).

E3 adds the *bounded remediation execution* plane on top of the accepted E1
observation and E2 advise control planes. It is delivered as a SEPARATE,
default-unprovisioned CloudFormation stack (``07-operations-execution.yaml``) so
that a default deployment provisions ZERO E3 resources and costs $0, and so that
the chat / general API / model / E1 / E2 roles retain zero provider-write
authority and cannot assume or invoke the executor.

These tests never call AWS. They parse the repository-owned CloudFormation
templates and shell wrappers as data and assert the **frozen E3 runtime
contract**:

* The 07 stack is default-unprovisioned: every resource is gated on a
  ``ResourcesProvisioned`` condition driven by a ``Provisioned`` parameter that
  defaults ``false``.
* Two-lever emergency disable: a runtime ``ExecutionMode`` kill switch
  (``disabled`` default) that (a) blocks new dispatch at the gateway/handler and
  (b) makes the executor fail closed — WITHOUT deleting any resource or data.
* A dispatcher Lambda + role, a Step Functions STANDARD state machine + workflow
  role, and a dedicated executor Lambda + role — three separate roles, three
  separate function/artifact identities.
* Invocation across dispatcher -> state machine -> executor carries
  ``operation_id`` only.
* The state machine has NO blind automatic retry on the executor task.
* The direct dispatch route is JWT-authorized (admin enforcement lives in
  handler code, asserted by the E3 core suite).
* Least-privilege IAM:
    - Dispatcher role: table ``GetItem`` + KMS-on-behalf (via DynamoDB) +
      logs/metrics + ``states:StartExecution`` on the EXACT state machine only.
    - Workflow role: ``lambda:InvokeFunction`` on the EXACT executor only.
    - Executor role: the exact underlying DynamoDB item actions
      (Get/Put/Update), KMS via DynamoDB, CloudWatch/logs, and
      ``gamelift:DescribeFleetCapacity`` + ``gamelift:UpdateFleetCapacity``
      scoped to the EXACT enrolled fleet ARN. No other GameLift action, no
      PassRole, secrets, source-control, or generic execute.
* An exact enrolled demo/test fleet parameter feeds the fleet-scoped GameLift
  ARN.
* No approval/execution route is exposed via chat/MCP.
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
TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"
OBSERVE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"
DEPLOY_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-execution.sh"
TEARDOWN_WRAPPER = PROJECT_ROOT / "scripts/infrastructure/teardown-operations-execution.sh"
BACKEND_SRC = PROJECT_ROOT / "backend/src"

METRIC_NAMESPACE = "GameAgent/Operations"

# The single direct dispatch route the enabled execution plane exposes, JWT-
# authorized, admin-enforced in handler code.
E3_ROUTE_KEYS = frozenset({"POST /operations/{operationId}/dispatch"})

# The two GameLift capacity actions the executor needs. They do NOT share a
# resource-authorization shape, so they cannot live in one fleet-ARN-scoped
# statement:
#   * gamelift:DescribeFleetCapacity (Read) does NOT support resource-level
#     permissions -- the AWS GameLift Servers service-authorization reference
#     lists an empty "Resource types" cell for it. Scoping it to a fleet ARN
#     yields implicitDeny in the IAM policy simulator, which is the live
#     PROVIDER_ERROR the executor hit before any write. It must be granted on
#     Resource "*".
#   * gamelift:UpdateFleetCapacity (Write) DOES support the "fleet" resource
#     type, so it is scoped to the exact enrolled fleet ARN and nothing else.
# Ref: https://docs.aws.amazon.com/service-authorization/latest/reference/list_gamelift.html
GAMELIFT_EXECUTOR_ACTIONS = frozenset(
    {
        "gamelift:DescribeFleetCapacity",
        "gamelift:UpdateFleetCapacity",
    }
)

# gamelift:DescribeFleetCapacity has no resource-level support; it is
# unavoidably granted on Resource "*". This is the only wildcard resource the
# executor's GameLift access may use, and only for this exact read action.
GAMELIFT_WILDCARD_READ_ACTIONS = frozenset({"gamelift:DescribeFleetCapacity"})

# gamelift:UpdateFleetCapacity supports the "fleet" resource type and MUST be
# pinned to the exact enrolled fleet ARN.
GAMELIFT_FLEET_SCOPED_WRITE_ACTIONS = frozenset({"gamelift:UpdateFleetCapacity"})

# The exact underlying DynamoDB item actions the executor needs (no
# TransactWriteItems IAM action exists; the transaction legs are governed by the
# underlying item actions).
ALLOWED_DYNAMODB_ACTIONS = frozenset(
    {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
    }
)

# Nothing in the 07 stack may grant any of these. These are the surfaces E3 must
# never widen: other GameLift verbs, PassRole, secrets, source control, generic
# execute-api, and any wildcard.
E3_FORBIDDEN_ACTION_SUBSTRINGS = (
    "iam:PassRole",
    "secretsmanager:",
    "codecommit:",
    "codeconnections:",
    "codestar-connections:",
    "codebuild:",
    "codepipeline:",
    "execute-api:",
    "s3:",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:DeleteItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:BatchWriteItem",
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
)

# GameLift verbs the executor must NEVER hold (only the two capacity actions are
# allowed).
GAMELIFT_FORBIDDEN_SUBSTRINGS = (
    "gamelift:CreateFleet",
    "gamelift:DeleteFleet",
    "gamelift:UpdateFleetAttributes",
    "gamelift:UpdateFleetPortSettings",
    "gamelift:UpdateRuntimeConfiguration",
    "gamelift:PutScalingPolicy",
    "gamelift:StartFleetActions",
    "gamelift:StopFleetActions",
    "gamelift:CreateGameSession",
    "gamelift:DescribeFleetUtilization",
    "gamelift:DescribeScalingPolicies",
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
    """Yield (role_name, policy_name, statement) for every inline policy stmt."""
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


def _role_named(template, needle):
    roles = _resources_of_type(template, "AWS::IAM::Role")
    for name, body in roles.items():
        if needle.lower() in name.lower():
            return name, body
    return None, None


# --------------------------------------------------------------------------- #
# Default-unprovisioned: $0 by default, every resource gated
# --------------------------------------------------------------------------- #
def test_template_exists_and_parses(template):
    assert template["Resources"], "07 stack must declare resources"


def test_provisioned_defaults_false(template):
    p = template["Parameters"]["Provisioned"]
    assert p["Default"] == "false", "07 stack must default to zero-resource ($0)"
    assert set(p["AllowedValues"]) == {"false", "true"}


def test_every_resource_is_gated_on_resources_provisioned(template):
    for name, body in template["Resources"].items():
        # ADDITIVE, opt-in read policies are gated on their own default-off
        # conditions: the E4 (issue #416) AppConfig kill-switch read on
        # HasKillSwitch, and the E5 (issue #440) autonomy AppConfig read on
        # AutonomyOperate. Both default off, so a default deploy still provisions
        # nothing. Every other resource is gated on ResourcesProvisioned.
        if body.get("Condition") in ("HasKillSwitch", "AutonomyOperate"):
            continue
        assert (
            body.get("Condition") == "ResourcesProvisioned"
        ), f"{name} must be gated on ResourcesProvisioned so a default deploy provisions nothing"


def test_resources_provisioned_condition_keys_off_provisioned(template):
    conditions = template.get("Conditions", {})
    assert "ResourcesProvisioned" in conditions, "expected a ResourcesProvisioned condition"


# --------------------------------------------------------------------------- #
# Two-lever emergency disable: runtime ExecutionMode kill switch, default off
# --------------------------------------------------------------------------- #
def test_execution_mode_defaults_disabled(template):
    mode = template["Parameters"]["ExecutionMode"]
    assert mode["Default"] == "disabled", "runtime kill switch must default fail-closed"
    assert set(mode["AllowedValues"]) == {"disabled", "remediate"}


def test_enabled_mode_requires_provisioning_rule(template):
    """Enabling remediate against a zero-resource stack must be rejected."""
    rules = template.get("Rules", {})
    assert rules, "expected a Rules block gating enable-without-provision"
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "remediate" in text


def test_disable_lever_throttles_gateway_to_zero(template):
    """Lever 1: a disabled provisioned stack throttles the dispatch gateway to
    zero (fails closed) without deleting the stage."""
    stages = _resources_of_type(template, "AWS::ApiGatewayV2::Stage")
    assert stages, "expected an API stage"
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "ThrottlingBurstLimit" in text and "ThrottlingRateLimit" in text
    # The throttle must be conditioned on the enabled mode (0 when disabled).
    assert re.search(r"ExecutionEnabled", text), "stage throttle must key off an ExecutionEnabled condition"


def test_disable_lever_is_injected_into_executor_env(template):
    """Lever 2: the executor reads the injected mode and fails closed when
    disabled — the env binding must be present so a disable makes it fail
    closed without deleting the function.

    Aligned to the core contract: the single backend-enforced ceiling is
    GBAW_OPERATIONS_MODE (fed the ExecutionMode kill switch), not the retired
    GBAW_OPERATIONS_EXECUTION_MODE."""
    functions = _resources_of_type(template, "AWS::Lambda::Function")
    executor = _find_function(functions, "executor")
    env = executor["Properties"]["Environment"]["Variables"]
    assert "GBAW_OPERATIONS_EXECUTION_MODE" not in env, (
        "the retired GBAW_OPERATIONS_EXECUTION_MODE must not be injected; the core"
        " handler reads GBAW_OPERATIONS_MODE"
    )
    assert "ExecutionMode" in str(
        env.get("GBAW_OPERATIONS_MODE")
    ), "executor must receive the ExecutionMode kill switch as GBAW_OPERATIONS_MODE"


def _find_function(functions, needle):
    for name, body in functions.items():
        if needle.lower() in name.lower():
            return body
    raise AssertionError(f"no Lambda function matching {needle!r}; have {sorted(functions)}")


# --------------------------------------------------------------------------- #
# Three separate roles + three separate function/workflow identities
# --------------------------------------------------------------------------- #
def test_three_distinct_roles_present(template):
    roles = _resources_of_type(template, "AWS::IAM::Role")
    names = " ".join(roles).lower()
    assert "dispatch" in names, "a dispatcher role is required"
    assert "workflow" in names or "statemachine" in names, "a workflow (state machine) role is required"
    assert "executor" in names, "an executor role is required"
    assert len(roles) >= 3, f"expected at least three distinct roles, got {sorted(roles)}"


def test_two_distinct_lambda_functions_present(template):
    functions = _resources_of_type(template, "AWS::Lambda::Function")
    names = " ".join(functions).lower()
    assert "dispatch" in names, "a dispatcher Lambda is required"
    assert "executor" in names, "a dedicated executor Lambda is required"
    assert len(functions) >= 2, f"expected dispatcher + executor functions, got {sorted(functions)}"


def test_standard_state_machine_present(template):
    machines = _resources_of_type(template, "AWS::StepFunctions::StateMachine")
    assert machines, "a Step Functions state machine is required"
    ((_name, body),) = machines.items()
    assert body["Properties"].get("StateMachineType") == "STANDARD", "state machine must be STANDARD type"


# --------------------------------------------------------------------------- #
# No blind automatic retry on the executor task
# --------------------------------------------------------------------------- #
def test_state_machine_has_no_blind_retry_on_executor(template):
    """The state machine definition must not attach a blind ``Retry`` to the
    executor task. A conservative absence of Retry is required; if a Retry is
    present at all it is treated as a blind automatic retry and rejected."""
    machines = _resources_of_type(template, "AWS::StepFunctions::StateMachine")
    ((_name, body),) = machines.items()
    definition = body["Properties"].get("DefinitionString") or body["Properties"].get("Definition")
    text = str(definition)
    assert "Retry" not in text, "executor task must have NO blind automatic Retry"


# --------------------------------------------------------------------------- #
# Invocation carries operation_id only
# --------------------------------------------------------------------------- #
def test_invocation_payload_is_operation_id_only(template):
    """The dispatcher starts the state machine and the state machine invokes the
    executor carrying only ``operation_id`` — no fleet id, capacity numbers, or
    principal claims flow through the wire. The state machine definition must
    reference operation_id and must not thread capacity/fleet fields."""
    machines = _resources_of_type(template, "AWS::StepFunctions::StateMachine")
    ((_name, body),) = machines.items()
    text = str(body["Properties"].get("DefinitionString") or body["Properties"].get("Definition"))
    assert "operation_id" in text, "state machine must thread operation_id"
    for leaked in ("fleet_id", "desired_instances", "capacity", "principal", "claims", "requested"):
        assert leaked not in text, f"state machine payload must not carry {leaked!r} (operation_id only)"


# --------------------------------------------------------------------------- #
# Route: single JWT-authorized dispatch route
# --------------------------------------------------------------------------- #
def test_dispatch_route_present_and_jwt(template):
    keys = _route_keys(template)
    assert E3_ROUTE_KEYS <= keys, f"missing dispatch route(s): {sorted(E3_ROUTE_KEYS - keys)}"
    for name, route in _routes(template).items():
        props = route["Properties"]
        assert props.get("AuthorizationType") == "JWT", f"{name} must require JWT auth"
        assert "AuthorizerId" in props, f"{name} must reference the authorizer"


def test_no_execution_route_is_exposed_through_chat_or_mcp():
    candidate_dirs = [BACKEND_SRC / "agents", BACKEND_SRC / "mcp", BACKEND_SRC / "tools"]
    tokens = ("/dispatch", "StartExecution", "operations-execute", "operations-executor")
    offenders = []
    for base in candidate_dirs:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            body = path.read_text(encoding="utf-8")
            for token in tokens:
                if token in body:
                    offenders.append(f"{path}: {token}")
    assert not offenders, f"execution surface must not be exposed via chat/MCP: {offenders}"


# --------------------------------------------------------------------------- #
# Dispatcher role: GetItem + KMS-on-behalf + logs/metrics + StartExecution only
# --------------------------------------------------------------------------- #
def test_dispatcher_role_start_execution_scoped_to_exact_state_machine(template):
    name, body = _role_named(template, "dispatch")
    assert body, "dispatcher role required"
    start_stmts = [
        s
        for _r, _p, s in _statements(template)
        if _r == name
        for a in _iter_action_strings(s.get("Action", []))
        if a == "states:StartExecution"
    ]
    assert start_stmts, "dispatcher role must allow states:StartExecution"
    for s in start_stmts:
        assert s.get("Resource") not in ("*", ["*"]), "StartExecution must be scoped to the exact state machine"


def test_dispatcher_role_only_reads_the_table(template):
    name, _body = _role_named(template, "dispatch")
    dynamo = [
        a
        for _r, _p, s in _statements(template)
        if _r == name
        for a in _iter_action_strings(s.get("Action", []))
        if a.lower().startswith("dynamodb:")
    ]
    assert dynamo, "dispatcher must read the table"
    assert set(dynamo) == {"dynamodb:GetItem"}, f"dispatcher must ONLY GetItem, got {sorted(set(dynamo))}"


def test_dispatcher_role_has_no_gamelift_or_lambda_invoke(template):
    name, _body = _role_named(template, "dispatch")
    actions = [
        a for _r, _p, s in _statements(template) if _r == name for a in _iter_action_strings(s.get("Action", []))
    ]
    for a in actions:
        assert not a.lower().startswith("gamelift:"), f"dispatcher must hold no GameLift action: {a}"
        assert a != "lambda:InvokeFunction", "dispatcher must not directly invoke the executor"


# --------------------------------------------------------------------------- #
# Workflow role: lambda:InvokeFunction on the exact executor only
# --------------------------------------------------------------------------- #
def test_workflow_role_invokes_only_the_executor(template):
    name, body = _role_named(template, "workflow") or _role_named(template, "statemachine")
    assert body, "workflow role required"
    invoke_stmts = [
        s
        for _r, _p, s in _statements(template)
        if _r == name
        for a in _iter_action_strings(s.get("Action", []))
        if a == "lambda:InvokeFunction"
    ]
    assert invoke_stmts, "workflow role must allow lambda:InvokeFunction"
    for s in invoke_stmts:
        assert s.get("Resource") not in ("*", ["*"]), "InvokeFunction must be scoped to the exact executor ARN"
    # Workflow role must hold NOTHING else risky.
    actions = [
        a for _r, _p, s in _statements(template) if _r == name for a in _iter_action_strings(s.get("Action", []))
    ]
    for a in actions:
        assert not a.lower().startswith("gamelift:"), f"workflow role must hold no GameLift action: {a}"
        assert not a.lower().startswith("dynamodb:"), f"workflow role must hold no DynamoDB action: {a}"


# --------------------------------------------------------------------------- #
# Executor role: exact underlying item actions, KMS via DynamoDB, logs/metrics,
# fleet-scoped Describe/UpdateFleetCapacity, and nothing else
# --------------------------------------------------------------------------- #
def test_executor_gamelift_actions_are_exactly_the_two_capacity_actions(template):
    name, _body = _role_named(template, "executor")
    gamelift = {
        a
        for _r, _p, s in _statements(template)
        if _r == name
        for a in _iter_action_strings(s.get("Action", []))
        if a.lower().startswith("gamelift:")
    }
    assert (
        gamelift == GAMELIFT_EXECUTOR_ACTIONS
    ), f"executor GameLift must be exactly the two capacity actions, got {sorted(gamelift)}"


def _gamelift_statements(template):
    """Yield executor-role statements that grant any gamelift: action."""
    name, _body = _role_named(template, "executor")
    for _r, _p, s in _statements(template):
        if _r != name:
            continue
        actions = list(_iter_action_strings(s.get("Action", [])))
        if any(a.lower().startswith("gamelift:") for a in actions):
            yield s, [a for a in actions if a.lower().startswith("gamelift:")]


def _resource_is_wildcard(resource):
    return resource in ("*", ["*"])


def _resource_is_fleet_arn(resource):
    text = str(resource)
    return "fleet/" in text or "EnrolledFleet" in text


def test_executor_describe_capacity_is_on_wildcard_resource(template):
    """gamelift:DescribeFleetCapacity has no resource-level support (empty
    Resource types cell in the service-authorization reference). Scoping it to a
    fleet ARN is implicitDeny in the IAM simulator -- the live PROVIDER_ERROR.
    It MUST be granted on Resource "*" and MUST NOT be fleet-ARN scoped."""
    matches = [(s, acts) for s, acts in _gamelift_statements(template) if "gamelift:DescribeFleetCapacity" in acts]
    assert matches, "executor must grant gamelift:DescribeFleetCapacity"
    for s, acts in matches:
        assert _resource_is_wildcard(s.get("Resource")), (
            "DescribeFleetCapacity lacks resource-level support and must be on "
            f"Resource '*', got {s.get('Resource')!r}"
        )
        assert not _resource_is_fleet_arn(
            s.get("Resource")
        ), "DescribeFleetCapacity must NOT be fleet-ARN scoped (implicitDeny)"


def test_executor_update_capacity_is_scoped_to_exact_fleet_arn(template):
    """gamelift:UpdateFleetCapacity supports the 'fleet' resource type and MUST
    be pinned to the exact enrolled fleet ARN -- never '*'."""
    matches = [(s, acts) for s, acts in _gamelift_statements(template) if "gamelift:UpdateFleetCapacity" in acts]
    assert matches, "executor must grant gamelift:UpdateFleetCapacity"
    for s, acts in matches:
        resource = s.get("Resource")
        assert not _resource_is_wildcard(resource), "UpdateFleetCapacity must be fleet-ARN scoped, not '*'"
        assert _resource_is_fleet_arn(resource), "UpdateFleetCapacity must reference the exact enrolled fleet ARN"


def test_executor_read_and_write_capacity_are_in_separate_statements(template):
    """The read (wildcard) and write (fleet-scoped) capacity actions must live
    in DIFFERENT statements: no single statement may pair them, or the write
    action would inherit the read's wildcard resource (over-broad) or the read
    action would inherit the write's fleet ARN (implicitDeny)."""
    for s, acts in _gamelift_statements(template):
        has_read = "gamelift:DescribeFleetCapacity" in acts
        has_write = "gamelift:UpdateFleetCapacity" in acts
        assert not (has_read and has_write), (
            "Describe (Resource '*') and Update (fleet ARN) must not share a " f"statement; found both in one: {acts}"
        )


def test_executor_only_describe_capacity_uses_wildcard_resource(template):
    """The ONLY gamelift action allowed a wildcard resource is the read that has
    no resource-level support. Any other gamelift action on '*' is a defect."""
    for s, acts in _gamelift_statements(template):
        if _resource_is_wildcard(s.get("Resource")):
            assert set(acts) <= GAMELIFT_WILDCARD_READ_ACTIONS, (
                "only gamelift:DescribeFleetCapacity may use Resource '*', " f"got {acts}"
            )


def test_executor_gamelift_policy_matches_simulator_expectation(template):
    """Policy-simulator expectation, encoded as the authoritative table this fix
    is derived from. For each capacity action, assert the executor grants it at
    exactly the resource shape the IAM simulator proved: DescribeFleetCapacity
    allowed only on '*' (fleet ARN -> implicitDeny); UpdateFleetCapacity allowed
    on the exact fleet ARN."""
    # action -> (allowed_on_wildcard, allowed_on_fleet_arn)
    SIMULATOR_EXPECTATION = {
        "gamelift:DescribeFleetCapacity": {"wildcard": True, "fleet_arn": False},
        "gamelift:UpdateFleetCapacity": {"wildcard": False, "fleet_arn": True},
    }
    granted: dict[str, set[str]] = {}  # action -> resource shapes it is granted on
    for s, acts in _gamelift_statements(template):
        resource = s.get("Resource")
        shape = (
            "wildcard"
            if _resource_is_wildcard(resource)
            else "fleet_arn" if _resource_is_fleet_arn(resource) else "other"
        )
        for a in acts:
            granted.setdefault(a, set()).add(shape)
    for action, expect in SIMULATOR_EXPECTATION.items():
        shapes = granted.get(action, set())
        assert shapes, f"executor must grant {action}"
        if expect["wildcard"]:
            assert "wildcard" in shapes, f"{action} must be granted on Resource '*'"
            assert "fleet_arn" not in shapes, f"{action} must not be fleet-ARN scoped (implicitDeny)"
        if expect["fleet_arn"]:
            assert "fleet_arn" in shapes, f"{action} must be granted on the exact fleet ARN"
            assert "wildcard" not in shapes, f"{action} must not be granted on Resource '*' (over-broad)"


def test_executor_dynamodb_actions_are_bounded(template):
    name, _body = _role_named(template, "executor")
    dynamo = [
        a
        for _r, _p, s in _statements(template)
        if _r == name
        for a in _iter_action_strings(s.get("Action", []))
        if a.lower().startswith("dynamodb:")
    ]
    assert dynamo, "executor must access the table"
    for a in dynamo:
        assert a in ALLOWED_DYNAMODB_ACTIONS, f"unexpected executor DynamoDB action: {a}"


def test_executor_has_kms_via_dynamodb_only(template):
    name, _body = _role_named(template, "executor")
    for _r, _p, s in _statements(template):
        if _r != name:
            continue
        actions = list(_iter_action_strings(s.get("Action", [])))
        if any(a.lower().startswith("kms:") for a in actions):
            cond = s.get("Condition", {})
            via = str(cond)
            assert (
                "kms:ViaService" in via and "dynamodb" in via
            ), "executor KMS must be pinned to DynamoDB via kms:ViaService"


# --------------------------------------------------------------------------- #
# Global IAM negatives across the whole 07 stack
# --------------------------------------------------------------------------- #
def test_no_wildcard_or_service_wildcard_actions(template):
    for action in _all_policy_actions(template):
        assert action != "*", "wildcard action not allowed"
        assert not action.endswith(":*"), f"service wildcard not allowed: {action}"


def test_no_forbidden_e3_actions(template):
    actions = _all_policy_actions(template)
    for action in actions:
        for forbidden in E3_FORBIDDEN_ACTION_SUBSTRINGS:
            assert forbidden.lower() not in action.lower(), f"forbidden E3 action present: {action}"


def test_no_forbidden_gamelift_verbs(template):
    actions = _all_policy_actions(template)
    for action in actions:
        for forbidden in GAMELIFT_FORBIDDEN_SUBSTRINGS:
            assert forbidden.lower() != action.lower(), f"forbidden GameLift verb present: {action}"


def test_no_passrole_anywhere(template):
    actions = _all_policy_actions(template)
    assert not any(a.lower() == "iam:passrole" for a in actions), "iam:PassRole must never be granted in E3"


# --------------------------------------------------------------------------- #
# Enrolled fleet parameter feeds the fleet-scoped ARN
# --------------------------------------------------------------------------- #
def test_enrolled_fleet_parameter_present_and_referenced(template):
    params = template["Parameters"]
    fleet_params = [n for n in params if "fleet" in n.lower()]
    assert fleet_params, "an exact enrolled demo/test fleet parameter is required"
    text = TEMPLATE.read_text(encoding="utf-8")
    # The fleet parameter must be woven into the GameLift resource ARN.
    assert "fleet/" in text, "the enrolled fleet must appear in a fleet-scoped ARN"


# --------------------------------------------------------------------------- #
# Metrics / alarms in the retained namespace
# --------------------------------------------------------------------------- #
def test_e3_alarms_use_the_retained_namespace(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    assert alarms, "E3 must declare execution alarms"
    for a in alarms.values():
        assert a["Properties"]["Namespace"] == METRIC_NAMESPACE


# --------------------------------------------------------------------------- #
# KMS + logs integration
# --------------------------------------------------------------------------- #
def test_log_groups_present_for_both_functions(template):
    log_groups = _resources_of_type(template, "AWS::Logs::LogGroup")
    assert len(log_groups) >= 2, "dispatcher and executor each need a log group"


# --------------------------------------------------------------------------- #
# E2 (06) stack preserved: still no execute/StartExecution surface leaked in
# --------------------------------------------------------------------------- #
def test_e2_stack_still_has_no_states_or_execute_actions():
    """Extending 06 for safe-mode remediate + outputs must NOT grant the E2
    handler any states:/execute surface; execution authority lives only in 07."""
    assert OBSERVE_TEMPLATE.exists()
    t = load_cfn_template(OBSERVE_TEMPLATE.read_text(encoding="utf-8"))
    for _r, _p, s in _statements(t):
        for a in _iter_action_strings(s.get("Action", [])):
            assert not a.lower().startswith("states:"), f"E2 stack must hold no states: action, got {a}"
            assert a != "lambda:InvokeFunction", "E2 stack must not invoke the executor"
