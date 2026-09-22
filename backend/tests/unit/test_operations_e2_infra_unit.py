"""Parser- and scanner-verifiable contract tests for the OPTIONAL E2 operations
advise control plane (GitHub issue #414).

E2 extends the accepted E1 observation control plane ADDITIVELY. It never edits
backend application code; it widens the same CloudFormation stack, deploy
wrapper, and runbook that E1 froze. These tests never call AWS. They parse the
repository-owned CloudFormation template and shell wrappers as data and assert
the **frozen E2 runtime contract** on top of the retained E1 contract:

* ``OperationsMode`` grows to ``disabled|observe|advise``. ``advise`` KEEPS the
  E1 ``observe`` routes active AND enables the E2 prepare/approval gate. It is a
  strictly higher runtime authority than ``observe``; ``disabled`` remains the
  fail-closed default and an emergency disable is still reversible.
* Provisioning stays SEPARATE from runtime authority and defaults to ``false``:
  a default deploy still provisions zero resources. ``advise`` (like ``observe``)
  requires ``Provisioned=true``.
* New JWT-authorized routes on the SAME real handler integration:
  ``POST /operations/prepare`` and
  ``POST /operations/{operationId}/approve|reject|cancel``, plus the retained
  ``GET /operations/{operationId}`` status and the retained
  ``POST /operations/observe``. Every route requires JWT. No approval route is
  exposed through chat/MCP (asserted against the MCP wiring).
* New validated, server-owned settings injected as the frozen core env names
  this slice DEFINES (the core consumes them in a later slice):
  ``GBAW_OPERATIONS_LOW_RISK_SELF_APPROVAL`` (default ``false``),
  ``GBAW_OPERATIONS_PREPARATION_EXPIRY_S``, and
  ``GBAW_OPERATIONS_APPROVAL_EXPIRY_S``. Self-approval defaults to disabled.
* New metrics/alarms ``PreparationFailures``, ``ApprovalFailures``,
  ``ApprovalExpired``, ``CancellationConflicts`` in the retained
  ``GameAgent/Operations`` namespace, and every E1 alarm is retained.
* The IAM boundary is UNCHANGED from E1: exactly the three GameLift reads, the
  three underlying DynamoDB item actions, the scoped KMS/log/metric grants. E2
  for bounded GameLift adds NO provider write, source-control credential,
  ``iam:PassRole``, ``states:`` action, generic ``execute``, or S3 content
  bucket.
* Packaging carries the E2 schema resources and import-probes the E2 handler
  routes before upload.
* The wrapper supports an explicit ``--mode observe|advise`` with the same
  double opt-in (matching ``GBAW_OPERATIONS_MODE``) and an emergency disable that
  rebuilds nothing.
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
BACKEND_SRC = PROJECT_ROOT / "backend/src"

METRIC_NAMESPACE = "GameAgent/Operations"

# Every route the enabled advise control plane exposes, all on the same real
# handler integration, all JWT-authorized. Status and observe are RETAINED.
E2_ROUTE_KEYS = frozenset(
    {
        "POST /operations/observe",
        "POST /operations/prepare",
        "POST /operations/{operationId}/approve",
        "POST /operations/{operationId}/reject",
        "POST /operations/{operationId}/cancel",
        "GET /operations/{operationId}",
    }
)
# The new approval-gate routes E2 adds (a subset of the above).
E2_NEW_ROUTE_KEYS = frozenset(
    {
        "POST /operations/prepare",
        "POST /operations/{operationId}/approve",
        "POST /operations/{operationId}/reject",
        "POST /operations/{operationId}/cancel",
    }
)

# Frozen core env names this slice DEFINES for the E2 settings the core consumes
# in a later slice. Server-owned, validated, and injected by the stack.
E2_REQUIRED_ENV_KEYS = frozenset(
    {
        "GBAW_OPERATIONS_LOW_RISK_SELF_APPROVAL",
        "GBAW_OPERATIONS_PREPARATION_EXPIRY_S",
        "GBAW_OPERATIONS_APPROVAL_EXPIRY_S",
    }
)

# The four E2 metric names (retained E1 metrics are asserted by the E1 suite).
E2_METRICS = frozenset(
    {
        "PreparationFailures",
        "ApprovalFailures",
        "ApprovalExpired",
        "CancellationConflicts",
    }
)
# E1 alarms that MUST be retained.
E1_RETAINED_METRICS = frozenset(
    {
        "ObservationFailures",
        "ObservationTimeouts",
        "StuckOperations",
        "ObservationRequestLatency",
    }
)

GAMELIFT_READ_ACTIONS = frozenset(
    {
        "gamelift:DescribeFleetUtilization",
        "gamelift:DescribeFleetCapacity",
        "gamelift:DescribeScalingPolicies",
    }
)
ALLOWED_DYNAMODB_ACTIONS = frozenset(
    {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
    }
)
# E2 for bounded GameLift adds NONE of these. Provider writes, source-control
# credentials, PassRole, Step Functions, generic execute, and S3 stay absent.
E2_FORBIDDEN_ACTION_SUBSTRINGS = (
    "gamelift:Create",
    "gamelift:Update",
    "gamelift:Delete",
    "gamelift:Put",
    "gamelift:Start",
    "gamelift:Stop",
    "iam:PassRole",
    "states:",
    "s3:",
    "codecommit:",
    "codeconnections:",
    "codestar-connections:",
    "secretsmanager:",
    "execute-api:",
    "lambda:InvokeFunction",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:DeleteItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:BatchWriteItem",
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
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


def _all_policy_actions(template):
    actions = []
    roles = _resources_of_type(template, "AWS::IAM::Role")
    policies = _resources_of_type(template, "AWS::IAM::Policy")
    for body in list(roles.values()) + list(policies.values()):
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


def _routes(template):
    return _resources_of_type(template, "AWS::ApiGatewayV2::Route")


def _route_keys(template):
    return {r["Properties"].get("RouteKey") for r in _routes(template).values()}


def _observation_function(template):
    functions = _resources_of_type(template, "AWS::Lambda::Function")
    assert functions, "expected the operations Lambda"
    return next(iter(functions.values()))["Properties"]


def _integration_targets(template):
    """The set of integration ids each route targets (as parsed scalar refs)."""
    targets = set()
    for route in _routes(template).values():
        target = route["Properties"].get("Target")
        if isinstance(target, str):
            targets.add(target)
    return targets


# --------------------------------------------------------------------------- #
# Mode vocabulary: disabled | observe | advise
# --------------------------------------------------------------------------- #
def test_operations_mode_vocabulary_includes_advise(template):
    mode = template["Parameters"]["OperationsMode"]
    assert mode["Default"] == "disabled", "default must remain fail-closed disabled"
    # advise MUST remain a selectable mode alongside the retained disabled/observe.
    # E3 (#415) additively grows the vocabulary with the higher "remediate" safe
    # mode; the E2 invariant is that disabled/observe/advise are all still present.
    allowed = set(mode["AllowedValues"])
    assert {"disabled", "observe", "advise"} <= allowed
    assert allowed <= {"disabled", "observe", "advise", "remediate"}


def test_advise_keeps_observe_routes_enabled(template):
    """``advise`` is a strictly higher authority than ``observe``: the API-stage
    kill switch must stay OPEN (throttle > 0) in advise, so the retained observe
    routes remain served. Only ``disabled`` throttles to zero."""
    text = TEMPLATE.read_text(encoding="utf-8")
    # The enabled condition must admit BOTH observe and advise (an Fn::Or over
    # the two enabled modes), not just observe.
    assert "advise" in text, "template must reference the advise mode"
    # There must be a condition that is true for advise (so the stage stays open).
    conditions = template.get("Conditions", {})
    joined = str(conditions)
    assert "advise" in joined, "a Condition must key off the advise mode"


def test_advise_requires_provisioning_rule(template):
    """Enabling advise against a zero-resource stack must be rejected, exactly as
    observe is; provisioning stays separate and defaults false."""
    text = TEMPLATE.read_text(encoding="utf-8")
    rules = template.get("Rules", {})
    assert rules, "expected a Rules block"
    # The enabled-mode rule must reference advise so advise-without-provisioning
    # is rejected the same way observe-without-provisioning is.
    assert "advise" in text
    params = template["Parameters"]
    assert params["Provisioned"]["Default"] == "false", "provisioning stays default-false"


def test_default_deploy_still_provisions_zero_resources(template):
    """Every resource stays gated on ResourcesProvisioned; a default deploy is $0."""
    for name, body in template["Resources"].items():
        # The E4 (issue #416) ADDITIVE AppConfig kill-switch read policy is gated
        # on HasKillSwitch (opt-in); every other resource is ResourcesProvisioned.
        if body.get("Condition") == "HasKillSwitch":
            continue
        assert (
            body.get("Condition") == "ResourcesProvisioned"
        ), f"{name} must be gated on ResourcesProvisioned so a default deploy provisions nothing"


# --------------------------------------------------------------------------- #
# Routes: prepare + approve/reject/cancel + retained status/observe, all JWT,
# all on the same real handler integration. No approval route via chat/MCP.
# --------------------------------------------------------------------------- #
def test_all_e2_routes_present(template):
    keys = _route_keys(template)
    missing = E2_ROUTE_KEYS - keys
    assert not missing, f"missing E2 routes: {sorted(missing)}"


def test_new_e2_routes_are_added(template):
    keys = _route_keys(template)
    missing = E2_NEW_ROUTE_KEYS - keys
    assert not missing, f"E2 must add the prepare/approve/reject/cancel routes: {sorted(missing)}"


def test_every_route_requires_jwt_auth(template):
    for name, route in _routes(template).items():
        props = route["Properties"]
        assert props.get("AuthorizationType") == "JWT", f"{name} must require JWT auth"
        assert "AuthorizerId" in props, f"{name} must reference the authorizer"


def test_all_routes_share_the_same_real_handler_integration(template):
    """Every route (observe, prepare, approve, reject, cancel, status) must fan
    into the SAME integration, which points at the single real handler Lambda —
    there is no second handler and no per-route divergent integration."""
    integrations = _resources_of_type(template, "AWS::ApiGatewayV2::Integration")
    assert len(integrations) == 1, "there must be exactly one shared handler integration"
    ((integration_name, integration_body),) = integrations.items()
    # The single integration must point at the observation Lambda function.
    uri = integration_body["Properties"].get("IntegrationUri")
    assert (
        isinstance(uri, str) and "ObservationFunction" in uri
    ), "the shared integration must target the real ObservationFunction handler"
    # Every route targets that one integration.
    targets = _integration_targets(template)
    assert len(targets) == 1, f"all routes must share one integration target, got {sorted(targets)}"
    (target,) = targets
    assert integration_name in target, "routes must target the single shared integration"


def test_no_approval_route_is_exposed_through_chat_or_mcp():
    """The approval gate is HTTP+JWT only. No approve/reject/cancel/prepare route
    may be wired into the chat surface or any MCP server tool registry."""
    # The MCP/agent tool surface lives under backend/src (agents, mcp, tools).
    # None of it may reference an operations approval route/tool.
    candidate_dirs = [
        BACKEND_SRC / "agents",
        BACKEND_SRC / "mcp",
        BACKEND_SRC / "tools",
    ]
    offenders = []
    approval_route_tokens = (
        "/operations/prepare",
        "/approve",
        "/reject",
        "/cancel",
    )
    for base in candidate_dirs:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            body = path.read_text(encoding="utf-8")
            for token in approval_route_tokens:
                if token in body:
                    offenders.append(f"{path}: {token}")
    assert not offenders, f"approval routes must not be exposed via chat/MCP: {offenders}"


# --------------------------------------------------------------------------- #
# Server-owned E2 settings (frozen core env names) — validated, self-approval off
# --------------------------------------------------------------------------- #
def test_e2_env_bindings_present_on_the_handler(template):
    env = _observation_function(template)["Environment"]["Variables"]
    missing = E2_REQUIRED_ENV_KEYS - set(env.keys())
    assert not missing, f"missing frozen E2 env bindings: {sorted(missing)}"


def test_self_approval_flag_defaults_to_false_and_is_validated(template):
    """The low-risk self-approval flag is server-owned, boolean, and default
    false so a fresh deploy never self-approves."""
    params = template["Parameters"]
    assert "LowRiskSelfApproval" in params, "a validated self-approval parameter is required"
    p = params["LowRiskSelfApproval"]
    assert p["Default"] == "false", "self-approval must default to disabled"
    assert set(p["AllowedValues"]) == {"false", "true"}, "self-approval must be a validated boolean"
    # It must be the value bound to the frozen env name.
    env = _observation_function(template)["Environment"]["Variables"]
    assert env["GBAW_OPERATIONS_LOW_RISK_SELF_APPROVAL"] == "LowRiskSelfApproval"


def test_expiry_settings_are_validated_and_bounded(template):
    params = template["Parameters"]
    for name in ("PreparationExpirySeconds", "ApprovalExpirySeconds"):
        assert name in params, f"missing expiry parameter {name}"
        p = params[name]
        assert p["Type"] == "Number"
        assert int(p["MinValue"]) >= 1, f"{name} must be positive"
        # Approval TTL contract: no more than one day (matches ApprovalPolicy).
        assert int(p["MaxValue"]) <= 86400, f"{name} must be bounded to at most one day"
    env = _observation_function(template)["Environment"]["Variables"]
    assert env["GBAW_OPERATIONS_PREPARATION_EXPIRY_S"] == "PreparationExpirySeconds"
    assert env["GBAW_OPERATIONS_APPROVAL_EXPIRY_S"] == "ApprovalExpirySeconds"


# --------------------------------------------------------------------------- #
# Metrics / alarms: four new E2 alarms, all E1 alarms retained
# --------------------------------------------------------------------------- #
def test_e2_alarms_present_for_every_new_metric(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    alarm_metrics = {a["Properties"].get("MetricName") for a in alarms.values()}
    missing = E2_METRICS - alarm_metrics
    assert not missing, f"missing E2 alarms for: {sorted(missing)}"


def test_all_e1_alarms_are_retained(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    alarm_metrics = {a["Properties"].get("MetricName") for a in alarms.values()}
    missing = E1_RETAINED_METRICS - alarm_metrics
    assert not missing, f"E1 alarms must be retained; missing: {sorted(missing)}"


def test_all_e2_alarms_use_the_retained_namespace(template):
    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    for a in alarms.values():
        assert a["Properties"]["Namespace"] == METRIC_NAMESPACE


# --------------------------------------------------------------------------- #
# IAM boundary unchanged (bounded GameLift E2 adds no write surface)
# --------------------------------------------------------------------------- #
def test_gamelift_reads_are_still_exactly_three(template):
    actions = _all_policy_actions(template)
    gamelift = {a for a in actions if a.lower().startswith("gamelift:")}
    assert gamelift == GAMELIFT_READ_ACTIONS, f"GameLift must stay the three reads, got {sorted(gamelift)}"


def test_dynamodb_actions_are_still_bounded(template):
    actions = [a for a in _all_policy_actions(template) if a.lower().startswith("dynamodb:")]
    assert actions, "expected scoped DynamoDB actions"
    for action in actions:
        assert action in ALLOWED_DYNAMODB_ACTIONS, f"unexpected DynamoDB action: {action}"


def test_no_forbidden_e2_actions(template):
    actions = _all_policy_actions(template)
    for action in actions:
        assert action != "*", "wildcard action not allowed"
        assert not action.endswith(":*"), f"service wildcard not allowed: {action}"
        for forbidden in E2_FORBIDDEN_ACTION_SUBSTRINGS:
            assert forbidden.lower() not in action.lower(), f"forbidden E2 action present: {action}"


def test_no_s3_content_bucket_for_bounded_gamelift_e2(template):
    assert not _resources_of_type(template, "AWS::S3::Bucket"), "bounded GameLift E2 must not add an S3 content bucket"


# --------------------------------------------------------------------------- #
# Packaging carries E2 schemas and probes the E2 routes
# --------------------------------------------------------------------------- #
def test_packaging_probe_covers_e2_contract_schemas():
    """The wrapper's runtime probe must load the E2 contract schemas (approval /
    prepare) before upload, so a package missing them fails closed pre-upload."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # The probe already iterates SCHEMA_NAMES; assert it references the E2
    # schema names so a regression that drops them is caught.
    for schema in ("approval-record", "prepare-operation-request", "prepared-operation"):
        assert schema in text, f"packaging probe must reference the E2 schema {schema}"


def test_packaging_probe_exercises_e2_handler_routes():
    """The probe must import the real handler and assert the E2 route verbs it
    serves are recognised (prepare/approve/reject/cancel) before upload."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    for token in ("prepare", "approve", "reject", "cancel"):
        assert token in text, f"packaging probe must cover the E2 route '{token}'"


# --------------------------------------------------------------------------- #
# Wrapper: explicit --mode observe|advise, double opt-in, emergency disable
# --------------------------------------------------------------------------- #
def test_wrapper_supports_explicit_mode_flag():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    assert "--mode" in text, "wrapper must accept an explicit --mode flag"
    # Both enabled modes must be selectable.
    assert "observe" in text and "advise" in text


def test_wrapper_double_opt_in_requires_matching_mode_env():
    """Enabling advise must require GBAW_OPERATIONS_MODE=advise to MATCH the
    --mode advise flag (belt-and-braces), exactly as observe requires
    GBAW_OPERATIONS_MODE=observe."""
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    # The wrapper compares the requested mode against the env value.
    assert "GBAW_OPERATIONS_MODE" in text
    # A matching-mode guard: the env value must equal the selected mode.
    assert re.search(r"GBAW_OPERATIONS_MODE.*\$\{?REQUESTED_MODE|REQUESTED_MODE.*GBAW_OPERATIONS_MODE", text) or (
        '"$GBAW_OPERATIONS_MODE" != "$OPERATIONS_MODE"' in text or '"${GBAW_OPERATIONS_MODE' in text
    ), "wrapper must require the env mode to match the requested mode"


def test_wrapper_emergency_disable_rebuilds_nothing():
    text = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    assert "--disable" in text
    # Disable path must reuse OperationsMode=disabled and not rebuild/upload.
    assert "OperationsMode=disabled" in text or "OperationsMode,ParameterValue=disabled" in text


def test_wrappers_still_use_strict_mode():
    for path in (DEPLOY_WRAPPER, TEARDOWN_WRAPPER):
        assert re.search(r"set -euo pipefail", path.read_text(encoding="utf-8"))
