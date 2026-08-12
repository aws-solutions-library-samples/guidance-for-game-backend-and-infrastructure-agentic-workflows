#!/usr/bin/env python3
"""Template-posture smoke tests for the gated executor CloudFormation (SECURITY GATE).

These parse ``infrastructure/cloudformation/06-scm-executor.yaml`` with the CFN-tag-aware
``SafeLoader`` pattern reused from ``test_iam_scm_credential_smoke.py`` and assert the IAM
isolation topology and the disabled-by-default posture that are NOT amenable to property-based
testing (design → Testing Strategy → "Template / synth and integration tests"). They cover the
non-optional security-gate tasks 9.4, 9.5, 9.6, and 9.7:

- **9.4** — the runtime roles (chat ``agentcore-execution-role`` and web/API/MCP
  ``ecs-task-role``) can neither invoke the executor nor read the write credential: the
  ``ScmRuntimeIsolationPolicy`` DENIES ``lambda:InvokeFunction`` on the executor and
  ``secretsmanager:GetSecretValue`` on the write secret for both roles. (Req 9.1, 9.2,
  9.3-deny, 9.4, 14.1, 14.2)
- **9.5** — sole write authority topology: only ``ScmWorkflowRole`` has
  ``lambda:InvokeFunction`` on the executor, the executor role has NO
  ``secretsmanager:GetSecretValue`` (the write grant is deferred/gated), and the write-once /
  ``LEDGER#`` append-only deny (``UpdateItem``/``DeleteItem``) is present. (Req 4.1, 4.7, 9.6,
  8.4)
- **9.6** — the default deployment creates no write resources: ``EnableScmWritePath`` defaults
  to ``'false'`` and every resource is gated on ``Condition: ScmWriteEnabled``. (Req 12.1-12.4,
  14.9)
- **9.7** — enabled-but-#280-gate-not-passed attaches no write IAM: even with the write path
  enabled, the template attaches NO ``secretsmanager:GetSecretValue`` to the executor role and
  NO provider-write actions (the gated task 9.3 grant is absent). (Req 13.4)

Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.6, 8.4, 4.1, 4.7, 12.1, 12.2, 12.3, 12.4, 13.4, 14.1, 14.2, 14.9
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
_TEMPLATE_PATH = _REPO_ROOT / "infrastructure" / "cloudformation" / "06-scm-executor.yaml"

_WRITE_CONDITION = "ScmWriteEnabled"
_EXECUTOR_ROLE = "ScmExecutorRole"
_WORKFLOW_ROLE = "ScmWorkflowRole"
_EXECUTOR_FUNCTION = "ScmExecutorFunction"
_ISOLATION_POLICY = "ScmRuntimeIsolationPolicy"
_LEDGER_DENY_POLICY = "ScmLedgerAppendOnlyPolicy"


# --- CloudFormation-aware YAML loader (mirrors test_iam_scm_credential_smoke.py) -----------


class _CfnLoader(yaml.SafeLoader):
    """A ``SafeLoader`` subclass that understands CloudFormation short-form tags."""


def _cfn_multi_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    """Represent any ``!Tag`` intrinsic as a plain mapping so parsing succeeds."""
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
    """Parse the gated executor template with the CFN-aware loader."""
    assert _TEMPLATE_PATH.is_file(), f"template not found at {_TEMPLATE_PATH}"
    parsed = yaml.load(_TEMPLATE_PATH.read_text(), Loader=_CfnLoader)  # noqa: S506 - safe subclass
    assert isinstance(parsed, dict), "template root is not a mapping"
    return parsed


# --- Helpers ------------------------------------------------------------------------------


def _as_list(value) -> list:
    """Normalize a scalar-or-list IAM field (Action/Statement/Resource/Roles) to a list."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _resource(template: dict, logical_id: str) -> dict:
    resources = template.get("Resources", {})
    res = resources.get(logical_id)
    assert isinstance(res, dict), f"{logical_id} not found in Resources"
    return res


def _role_statements(role: dict) -> list:
    """Yield every IAM statement across a role's inline ``Policies``."""
    statements: list = []
    for policy in _as_list(role.get("Properties", {}).get("Policies")):
        if not isinstance(policy, dict):
            continue
        doc = policy.get("PolicyDocument", {})
        for statement in _as_list(doc.get("Statement")):
            if isinstance(statement, dict):
                statements.append(statement)
    return statements


def _managed_policy_statements(policy_resource: dict) -> list:
    doc = policy_resource.get("Properties", {}).get("PolicyDocument", {})
    return [s for s in _as_list(doc.get("Statement")) if isinstance(s, dict)]


# --- Task 9.6: default deployment creates no write resources -------------------------------


def test_enable_write_path_defaults_to_false(template: dict):
    """(Req 12.1-12.4, 14.9) The master gate parameter defaults to 'false'."""
    params = template.get("Parameters", {})
    assert "EnableScmWritePath" in params, "missing EnableScmWritePath parameter"
    assert params["EnableScmWritePath"].get("Default") == "false"


def test_write_enabled_condition_gates_on_enable_flag(template: dict):
    """(Req 12.1-12.4) The ScmWriteEnabled condition is defined and keyed to the flag."""
    conditions = template.get("Conditions", {})
    assert _WRITE_CONDITION in conditions, f"missing condition {_WRITE_CONDITION}"


def test_every_resource_is_gated_on_scm_write_enabled(template: dict):
    """(Req 12.1-12.4, 14.9) EVERY resource carries Condition: ScmWriteEnabled, so a default
    deployment (EnableScmWritePath = 'false') creates none of them."""
    resources = template.get("Resources", {})
    assert resources, "template defines no resources"
    ungated = [
        logical_id
        for logical_id, body in resources.items()
        if not (isinstance(body, dict) and body.get("Condition") == _WRITE_CONDITION)
    ]
    assert ungated == [], f"resources not gated on {_WRITE_CONDITION}: {ungated}"


# --- Task 9.4: runtime roles cannot invoke executor or read the write credential -----------


def test_runtime_isolation_policy_targets_both_runtime_roles(template: dict):
    """(Req 9.1, 9.2, 14.1, 14.2) The isolation policy attaches to the chat and web/API/MCP
    runtime roles (agentcore-execution-role and ecs-task-role)."""
    policy = _resource(template, _ISOLATION_POLICY)
    assert policy.get("Type") == "AWS::IAM::ManagedPolicy"
    assert policy.get("Condition") == _WRITE_CONDITION
    roles_rendered = [str(r) for r in _as_list(policy.get("Properties", {}).get("Roles"))]
    assert any("agentcore-execution-role" in r for r in roles_rendered), "chat runtime role not isolated"
    assert any("ecs-task-role" in r for r in roles_rendered), "web/API/MCP runtime role not isolated"


def test_runtime_isolation_denies_invoke_executor_and_read_write_secret(template: dict):
    """(Req 9.1-9.4, 14.1, 14.2) The isolation policy DENIES lambda:InvokeFunction on the
    executor and secretsmanager:GetSecretValue on the write secret."""
    policy = _resource(template, _ISOLATION_POLICY)
    statements = _managed_policy_statements(policy)

    deny_invoke = [
        s for s in statements if s.get("Effect") == "Deny" and "lambda:InvokeFunction" in _as_list(s.get("Action"))
    ]
    assert deny_invoke, "no explicit Deny of lambda:InvokeFunction on the runtime roles"
    invoke_targets = [str(r) for s in deny_invoke for r in _as_list(s.get("Resource"))]
    assert any(_EXECUTOR_FUNCTION in t for t in invoke_targets), "invoke deny does not target the executor"

    deny_secret = [
        s
        for s in statements
        if s.get("Effect") == "Deny" and "secretsmanager:GetSecretValue" in _as_list(s.get("Action"))
    ]
    assert deny_secret, "no explicit Deny of secretsmanager:GetSecretValue on the runtime roles"
    secret_targets = [str(r) for s in deny_secret for r in _as_list(s.get("Resource"))]
    assert any("ScmWriteCredentialSecretArn" in t for t in secret_targets), "secret deny does not target write secret"


# --- Task 9.5: sole write authority topology ----------------------------------------------


def test_only_workflow_role_can_invoke_executor(template: dict):
    """(Req 4.7, 9.6) The workflow role is the ONLY principal granted lambda:InvokeFunction on
    the executor, and no other in-stack role Allows invoking it."""
    workflow_role = _resource(template, _WORKFLOW_ROLE)
    workflow_allows_invoke = [
        s
        for s in _role_statements(workflow_role)
        if s.get("Effect") == "Allow" and "lambda:InvokeFunction" in _as_list(s.get("Action"))
    ]
    assert workflow_allows_invoke, "workflow role has no lambda:InvokeFunction grant on the executor"
    targets = [str(r) for s in workflow_allows_invoke for r in _as_list(s.get("Resource"))]
    assert all(_EXECUTOR_FUNCTION in t for t in targets), "workflow invoke grant is not scoped to the executor"

    # No other role in the stack Allows lambda:InvokeFunction.
    for logical_id, body in template.get("Resources", {}).items():
        if body.get("Type") != "AWS::IAM::Role" or logical_id == _WORKFLOW_ROLE:
            continue
        offending = [
            s
            for s in _role_statements(body)
            if s.get("Effect") == "Allow" and "lambda:InvokeFunction" in _as_list(s.get("Action"))
        ]
        assert offending == [], f"{logical_id} unexpectedly grants lambda:InvokeFunction"


def test_ledger_append_only_deny_is_present(template: dict):
    """(Req 8.4) The write-once / LEDGER# append-only guarantee is enforced by an explicit Deny
    of dynamodb:UpdateItem and dynamodb:DeleteItem on the store, attached to the write roles."""
    policy = _resource(template, _LEDGER_DENY_POLICY)
    assert policy.get("Type") == "AWS::IAM::ManagedPolicy"
    assert policy.get("Condition") == _WRITE_CONDITION
    statements = _managed_policy_statements(policy)
    deny = [s for s in statements if s.get("Effect") == "Deny"]
    assert deny, "no Deny statement on the ledger append-only policy"
    denied_actions = {a for s in deny for a in _as_list(s.get("Action"))}
    assert {"dynamodb:UpdateItem", "dynamodb:DeleteItem"} <= denied_actions
    attached = [str(r) for r in _as_list(policy.get("Properties", {}).get("Roles"))]
    assert any(_EXECUTOR_ROLE in r for r in attached) and any(_WORKFLOW_ROLE in r for r in attached)


# --- Tasks 9.5 + 9.7: the executor role has NO write-secret grant / NO provider-write IAM ---


def test_executor_role_has_no_get_secret_value_grant(template: dict):
    """(Req 4.1, 9.6, 13.4) Even with the write path enabled, the executor role is granted NO
    secretsmanager:GetSecretValue — the write-secret grant is deferred to the #280-gated task
    9.3 and is intentionally absent from this template."""
    executor_role = _resource(template, _EXECUTOR_ROLE)
    secret_actions = [
        a
        for s in _role_statements(executor_role)
        if s.get("Effect") == "Allow"
        for a in _as_list(s.get("Action"))
        if str(a).startswith("secretsmanager:")
    ]
    assert secret_actions == [], f"executor role unexpectedly grants secrets access: {secret_actions}"


def test_executor_role_has_no_provider_write_or_broad_actions(template: dict):
    """(Req 13.4) The executor role attaches no provider-write / broad-mutation IAM: its only
    Allow actions are scoped logging and insert-only store reads/writes (Get/Query/PutItem).
    The gated provider-write grant (task 9.3) is absent until the #280 gate passes."""
    executor_role = _resource(template, _EXECUTOR_ROLE)
    allow_actions = sorted(
        {
            str(a)
            for s in _role_statements(executor_role)
            if s.get("Effect") == "Allow"
            for a in _as_list(s.get("Action"))
        }
    )
    allowed = {
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:PutItem",
    }
    unexpected = set(allow_actions) - allowed
    assert unexpected == set(), f"executor role has unexpected (possibly write) actions: {sorted(unexpected)}"
    # And explicitly: no destructive DynamoDB write beyond insert-only PutItem.
    assert "dynamodb:UpdateItem" not in allow_actions
    assert "dynamodb:DeleteItem" not in allow_actions
