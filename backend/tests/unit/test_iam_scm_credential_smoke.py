#!/usr/bin/env python3
"""Smoke test: the Source Control Connector IAM template is read-only and scoped.

This parses ``infrastructure/cloudformation/01-base-infrastructure.yaml`` and asserts the
**read-only split** posture of the connector's contribution to ``AgentCoreExecutionRole``
(the chat-runtime read role):

* **No write surface** (task 11.3) — the template defines NO ``ScmCredentialAccess`` policy
  and NO ``ScmCredentialSecretArn`` parameter (the old write-credential wiring is gone), and
  default synthesis grants no write-credential ``GetSecretValue`` to any chat role and
  creates no source-control write resources.
* **Scoped read grant** (task 11.4) — the template defines a ``ScmReadCredentialSecretArn``
  parameter distinct from any write param, and the only ``secretsmanager:GetSecretValue``
  grant targets the read ARN (``!Ref ScmReadCredentialSecretArn``) scoped to the
  chat-runtime role.
* **Minimal audit grant** (task 11.5) — ``ScmAuditLogAccess`` permits only
  ``logs:CreateLogStream`` + ``logs:PutLogEvents``, is KB-independent, and remains the
  read-audit target (the dedicated connector audit log group).

The template uses CloudFormation intrinsic short tags (``!Ref``, ``!Sub``, ``!If``,
``!GetAtt``, ``!Not``, ``!Equals``). A dedicated ``SafeLoader`` subclass with a multi-tag
constructor (mirroring ``connector.iac_validation._CfnLoader``) tolerates them by turning
each intrinsic into an ordinary Python mapping so the document parses into plain structures.

Validates: Requirements 3.1, 3.2, 3.4, 6.2, 6.3, 9.3, 9.5, 11.2, 11.3
"""

# Standard library
from pathlib import Path

# Third-party packages
import pytest
import yaml

pytestmark = pytest.mark.unit


# --- Locate the template ------------------------------------------------------------------

# tests/unit/<this file> -> tests/unit -> tests -> backend -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_PATH = _REPO_ROOT / "infrastructure" / "cloudformation" / "01-base-infrastructure.yaml"

_ROLE_LOGICAL_ID = "AgentCoreExecutionRole"

# Read-credential (kept, scoped) wiring.
_SCM_READ_POLICY_NAME = "ScmReadCredentialAccess"
_SCM_READ_CONDITION_NAME = "ScmReadCredentialConfigured"
_SCM_READ_PARAMETER_NAME = "ScmReadCredentialSecretArn"

# Removed write-credential wiring — these must NOT appear anywhere in the template.
_REMOVED_WRITE_POLICY_NAME = "ScmCredentialAccess"
_REMOVED_WRITE_CONDITION_NAME = "ScmCredentialConfigured"
_REMOVED_WRITE_PARAMETER_NAME = "ScmCredentialSecretArn"

# The dedicated audit-sink grant (KB-independent, opt-in via ScmAuditLogGroupName).
_SCM_AUDIT_POLICY_NAME = "ScmAuditLogAccess"
_SCM_AUDIT_CONDITION_NAME = "ScmAuditLogGroupConfigured"
_SCM_AUDIT_LOG_GROUP_LOGICAL_ID = "ScmAuditLogGroup"


# --- CloudFormation-aware YAML loader -----------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    """A ``SafeLoader`` subclass that understands CloudFormation short-form tags."""


def _cfn_multi_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    """Represent any ``!Tag`` intrinsic as a plain mapping so parsing succeeds.

    ``!Ref`` / ``!Condition`` map to their bare CloudFormation keys; every other intrinsic
    maps to ``Fn::<suffix>`` (the standard JSON form). ``deep=True`` ensures nested
    structures (e.g. the policy mapping inside an ``!If``) are fully constructed.
    """
    if tag_suffix in ("Ref", "Condition"):
        key = tag_suffix
    else:
        key = f"Fn::{tag_suffix}"

    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = None
    return {key: value}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


# --- Fixtures -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template() -> dict:
    """Parse the base-infrastructure template with the CFN-aware loader."""
    assert _TEMPLATE_PATH.is_file(), f"template not found at {_TEMPLATE_PATH}"
    parsed = yaml.load(_TEMPLATE_PATH.read_text(), Loader=_CfnLoader)  # noqa: S506 - safe subclass
    assert isinstance(parsed, dict), "template root is not a mapping"
    return parsed


@pytest.fixture(scope="module")
def role_policies(template: dict) -> list:
    """Return the inline ``Policies`` list of the AgentCore execution role."""
    resources = template.get("Resources", {})
    role = resources.get(_ROLE_LOGICAL_ID)
    assert role is not None, f"{_ROLE_LOGICAL_ID} not found in Resources"
    assert role.get("Type") == "AWS::IAM::Role"
    policies = role.get("Properties", {}).get("Policies")
    assert isinstance(policies, list) and policies, "role has no inline Policies list"
    return policies


# --- Helpers ------------------------------------------------------------------------------


def _as_list(value) -> list:
    """Normalize a scalar-or-list IAM field (Action/Statement/Resource) to a list."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _find_conditional_policy(policies: list, policy_name: str):
    """Return ``(condition_name, policy_dict)`` for the named ``!If``-gated policy, or None."""
    for entry in policies:
        if not isinstance(entry, dict):
            continue
        fn_if = entry.get("Fn::If")
        if not isinstance(fn_if, list) or len(fn_if) != 3:
            continue
        condition_name, true_branch, _false_branch = fn_if
        if isinstance(true_branch, dict) and true_branch.get("PolicyName") == policy_name:
            return condition_name, true_branch
    return None


def _find_scm_read_conditional_policy(policies: list):
    """Return ``(condition_name, policy_dict)`` for the ``!If``-gated read-credential policy."""
    return _find_conditional_policy(policies, _SCM_READ_POLICY_NAME)


def _iter_statements(policies: list):
    """Yield every IAM statement across all inline policies (including the true-branch of
    any ``!If``-gated policy)."""
    for entry in policies:
        if not isinstance(entry, dict):
            continue
        candidates = []
        if "Fn::If" in entry:
            fn_if = entry["Fn::If"]
            if isinstance(fn_if, list) and len(fn_if) == 3 and isinstance(fn_if[1], dict):
                candidates.append(fn_if[1])
        else:
            candidates.append(entry)
        for policy in candidates:
            doc = policy.get("PolicyDocument", {})
            for statement in _as_list(doc.get("Statement")):
                if isinstance(statement, dict):
                    yield statement


def _all_actions(policies: list) -> list:
    """Every action string granted (Effect: Allow) across the role's inline policies."""
    actions = []
    for statement in _iter_statements(policies):
        if statement.get("Effect") != "Allow":
            continue
        actions.extend(str(a) for a in _as_list(statement.get("Action")))
    return actions


# Service prefixes that represent *live AWS infrastructure* the runtime reads. Observability
# (logs/xray/cloudwatch), the agent's own memory/runtime (bedrock-agentcore), model access
# (bedrock), and pure read/reporting services are intentionally excluded from the "must never
# mutate" set — the concern here is live infrastructure resources.
_LIVE_INFRA_SERVICE_PREFIXES = (
    "gamelift:",
    "eks:",
    "ec2:",
    "ecr:",
    "cloudcontrol:",
    "cloudformation:",
    "s3:",
)

# Verbs that mutate a resource. Read-only IAM actions use Get/List/Describe/BatchGet/etc.
_MUTATING_VERBS = (
    "Create",
    "Delete",
    "Update",
    "Put",
    "Modify",
    "Terminate",
    "Run",
    "Start",
    "Stop",
    "Attach",
    "Detach",
    "Associate",
    "Disassociate",
    "Remove",
    "Set",
    "Reboot",
    "Register",
    "Deregister",
    "Enable",
    "Disable",
    "Apply",
    "Restore",
    "Revoke",
    "Import",
    "Copy",
    "Add",
)


def _action_verb(action: str) -> str:
    """Return the verb portion of a ``service:Verb`` action (empty for wildcard)."""
    _, _, verb = action.partition(":")
    return verb


# --- Task 11.3: no write surface ----------------------------------------------------------


def test_no_write_credential_parameter_present(template: dict):
    """(Req 3.1, 3.2) The removed write-credential parameter ``ScmCredentialSecretArn`` does
    not exist anywhere in the template's Parameters."""
    params = template.get("Parameters", {})
    assert _REMOVED_WRITE_PARAMETER_NAME not in params, (
        f"{_REMOVED_WRITE_PARAMETER_NAME} write parameter must be removed"
    )


def test_no_write_credential_condition_present(template: dict):
    """(Req 3.1) The removed write-credential condition ``ScmCredentialConfigured`` is gone."""
    conditions = template.get("Conditions", {})
    assert _REMOVED_WRITE_CONDITION_NAME not in conditions, (
        f"{_REMOVED_WRITE_CONDITION_NAME} write condition must be removed"
    )


def test_no_write_credential_access_policy_present(role_policies: list):
    """(Req 3.1, 3.4, 11.2, 11.3) The role defines NO ``ScmCredentialAccess`` write policy —
    default synthesis grants no write-credential GetSecretValue to any chat role."""
    assert _find_conditional_policy(role_policies, _REMOVED_WRITE_POLICY_NAME) is None, (
        f"{_REMOVED_WRITE_POLICY_NAME} write-credential policy must be removed"
    )
    for entry in role_policies:
        if isinstance(entry, dict):
            assert entry.get("PolicyName") != _REMOVED_WRITE_POLICY_NAME, (
                f"{_REMOVED_WRITE_POLICY_NAME} write-credential policy must be removed"
            )


def test_removed_write_names_absent_from_raw_template(template: dict):
    """(Req 3.1, 11.2) None of the removed write-credential logical names appear anywhere in
    the parsed template (parameters, conditions, or resources)."""
    rendered = str(template)
    for name in (_REMOVED_WRITE_PARAMETER_NAME, _REMOVED_WRITE_CONDITION_NAME, _REMOVED_WRITE_POLICY_NAME):
        assert name not in rendered, f"removed write name {name!r} must not appear in the template"


def test_no_source_control_write_resources_created(template: dict):
    """(Req 3.4, 11.3) The template creates no source-control write resources — the only
    connector-owned resource is the read-audit log group (a CloudWatch Logs group)."""
    resources = template.get("Resources", {})
    # The sole connector resource is the audit log group; it is a logs group, not a
    # source-control write resource. No DynamoDB / Step Functions / Lambda write infra.
    for logical_id, resource in resources.items():
        rtype = resource.get("Type", "") if isinstance(resource, dict) else ""
        assert rtype not in (
            "AWS::DynamoDB::Table",
            "AWS::StepFunctions::StateMachine",
            "AWS::Lambda::Function",
        ), f"unexpected write-infra resource {logical_id} of type {rtype}"


# --- Task 11.4: scoped read grant ---------------------------------------------------------


def test_scm_read_credential_secret_arn_parameter_exists_with_empty_default(template: dict):
    """(Req 6.2) A String parameter for the READ secret ARN exists, is distinct from any
    write param, and defaults to empty so deployments that never set it are unaffected."""
    params = template.get("Parameters", {})
    assert _SCM_READ_PARAMETER_NAME in params, f"missing parameter {_SCM_READ_PARAMETER_NAME}"
    assert _SCM_READ_PARAMETER_NAME != _REMOVED_WRITE_PARAMETER_NAME
    assert _REMOVED_WRITE_PARAMETER_NAME not in params, "read param must be distinct; no write param"
    param = params[_SCM_READ_PARAMETER_NAME]
    assert param.get("Type") == "String"
    assert param.get("Default") == "", "parameter default must be empty string"


def test_scm_read_credential_configured_condition_exists(template: dict):
    """(Req 6.2) The condition gating the read grant on a non-empty ARN exists."""
    conditions = template.get("Conditions", {})
    assert _SCM_READ_CONDITION_NAME in conditions, f"missing condition {_SCM_READ_CONDITION_NAME}"


def test_scm_read_policy_is_conditional_on_read_credential_configured(role_policies: list):
    """(Req 6.2) The read grant is added only via the ``ScmReadCredentialConfigured`` cond."""
    found = _find_scm_read_conditional_policy(role_policies)
    assert found is not None, f"{_SCM_READ_POLICY_NAME} policy is not present as an Fn::If entry"
    condition_name, _policy = found
    assert condition_name == _SCM_READ_CONDITION_NAME


def test_only_get_secret_value_grant_targets_the_read_arn(role_policies: list):
    """(Req 6.2, 6.3) The single ``secretsmanager:GetSecretValue`` grant across the whole
    chat-runtime role targets ONLY the read ARN (``!Ref ScmReadCredentialSecretArn``),
    scoped (never ``*`` or a prefix wildcard)."""
    found = _find_scm_read_conditional_policy(role_policies)
    assert found is not None
    _condition_name, policy = found

    statements = _as_list(policy.get("PolicyDocument", {}).get("Statement"))
    assert len(statements) == 1, "read policy must contain exactly one statement"
    statement = statements[0]
    assert statement.get("Effect") == "Allow"

    actions = _as_list(statement.get("Action"))
    assert actions == ["secretsmanager:GetSecretValue"], "the only added action must be GetSecretValue"

    resources = _as_list(statement.get("Resource"))
    assert resources == [
        {"Ref": _SCM_READ_PARAMETER_NAME}
    ], "GetSecretValue must be scoped to the ScmReadCredentialSecretArn parameter ref"
    for resource in resources:
        assert resource != "*", "read grant must not be Resource: '*'"
        assert not (
            isinstance(resource, str) and resource.endswith("*")
        ), "read grant must not use a prefix/suffix wildcard ARN"

    # Across the ENTIRE role, GetSecretValue is the only Secrets Manager action, and it is
    # only ever scoped to the read ARN ref (no other GetSecretValue grant exists).
    secretsmanager_actions = sorted({a for a in _all_actions(role_policies) if a.startswith("secretsmanager:")})
    assert secretsmanager_actions == [
        "secretsmanager:GetSecretValue"
    ], f"unexpected secretsmanager actions: {secretsmanager_actions}"
    getsecret_resources = [
        _as_list(s.get("Resource"))
        for s in _iter_statements(role_policies)
        if "secretsmanager:GetSecretValue" in _as_list(s.get("Action"))
    ]
    assert getsecret_resources == [
        [{"Ref": _SCM_READ_PARAMETER_NAME}]
    ], "the only GetSecretValue grant must target the read ARN ref"


def test_no_mutating_live_infrastructure_actions_present(role_policies: list):
    """(Req 3.4) No action against a live-infrastructure service uses a mutating verb; the
    chat-runtime role remains read-only against live AWS resources."""
    offending = []
    for action in _all_actions(role_policies):
        if not any(action.startswith(prefix) for prefix in _LIVE_INFRA_SERVICE_PREFIXES):
            continue
        verb = _action_verb(action)
        if any(verb.startswith(mutating) for mutating in _MUTATING_VERBS):
            offending.append(action)
    assert offending == [], f"mutating live-infrastructure actions present: {sorted(offending)}"


# --- Task 11.5: minimal, KB-independent audit grant ---------------------------------------


def test_scm_audit_policy_is_conditional_on_audit_log_group_configured(role_policies: list):
    """(Req 9.3, 9.5) The audit-sink grant is added only via the ``ScmAuditLogGroupConfigured``
    condition — a non-empty ``ScmAuditLogGroupName``, independent of any Knowledge Base."""
    found = _find_conditional_policy(role_policies, _SCM_AUDIT_POLICY_NAME)
    assert found is not None, f"{_SCM_AUDIT_POLICY_NAME} policy is not present as an Fn::If entry"
    condition_name, _policy = found
    assert (
        condition_name == _SCM_AUDIT_CONDITION_NAME
    ), "audit grant must gate on ScmAuditLogGroupConfigured, not a KB condition"
    assert (
        "KB" not in condition_name and "KnowledgeBase" not in condition_name
    ), "audit grant condition must be independent of Knowledge Base configuration"


def test_scm_audit_policy_only_actions_are_scoped_log_writes(role_policies: list):
    """(Req 9.5) The audit statement grants ONLY logs:CreateLogStream + logs:PutLogEvents
    scoped to the dedicated audit log group ARN (and its ``:*`` stream children) — nothing
    else, and no live-infrastructure or KB resource. This is the read-audit target."""
    found = _find_conditional_policy(role_policies, _SCM_AUDIT_POLICY_NAME)
    assert found is not None
    _condition_name, policy = found

    statements = _as_list(policy.get("PolicyDocument", {}).get("Statement"))
    assert len(statements) == 1, "audit policy must contain exactly one statement"
    statement = statements[0]
    assert statement.get("Effect") == "Allow"

    actions = _as_list(statement.get("Action"))
    assert sorted(actions) == [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    ], "the only added audit actions must be logs:CreateLogStream + logs:PutLogEvents"

    resources = _as_list(statement.get("Resource"))
    assert len(resources) == 2, "audit grant must scope to the log group ARN and its ':*' children"
    assert {
        "Fn::GetAtt": f"{_SCM_AUDIT_LOG_GROUP_LOGICAL_ID}.Arn"
    } in resources, "audit grant must include the ScmAuditLogGroup ARN via GetAtt"
    stream_children = [
        r
        for r in resources
        if isinstance(r, dict) and isinstance(r.get("Fn::Sub"), str) and r["Fn::Sub"].endswith(":*")
    ]
    assert len(stream_children) == 1, "audit grant must include the ':*' log-stream children"
    for resource in resources:
        assert resource != "*", "audit grant must not be Resource: '*'"
        rendered = str(resource)
        assert (
            _SCM_AUDIT_LOG_GROUP_LOGICAL_ID in rendered
        ), f"audit resource must reference {_SCM_AUDIT_LOG_GROUP_LOGICAL_ID}: {resource!r}"
        assert (
            "knowledge-base" not in rendered and "KnowledgeBase" not in rendered
        ), "audit grant must be independent of any Knowledge Base resource"


def test_scm_audit_log_group_is_dedicated_resource(template: dict):
    """(Req 9.3, 9.5) The read-audit destination is a dedicated CloudWatch Logs log group
    provisioned independently of Knowledge Base configuration (gated on
    ScmAuditLogGroupConfigured)."""
    resources = template.get("Resources", {})
    log_group = resources.get(_SCM_AUDIT_LOG_GROUP_LOGICAL_ID)
    assert log_group is not None, f"missing {_SCM_AUDIT_LOG_GROUP_LOGICAL_ID} resource"
    assert log_group.get("Type") == "AWS::Logs::LogGroup"
    assert log_group.get("Condition") == _SCM_AUDIT_CONDITION_NAME
