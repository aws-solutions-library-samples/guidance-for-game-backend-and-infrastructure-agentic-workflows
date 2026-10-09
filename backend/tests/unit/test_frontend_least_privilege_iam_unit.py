"""Identity-policy regressions for the frontend ECS task role (issue #458).

The frontend is a Next.js proxy. Its ECS task role intentionally has no
identity-based permissions:

* AgentCore is invoked over HTTPS with the user's verified Cognito access token,
  not with task-role SigV4 credentials.
* Container log delivery is performed by the separate ECS task execution role.
* The one supported task-credential call, ``sts:GetCallerIdentity``, requires no
  IAM allow.
* The sample provisions access out-of-band with ``add-admin-user.sh`` (or the
  PowerShell ``Add-GameAgentAdmin``), so the frontend never performs Cognito
  administrator operations and this role carries no ``cognito-idp:`` grant.

The base template models role grants inline. This test therefore prohibits both
policy properties on ``ECSTaskRole`` and standalone ``AWS::IAM::Policy``,
``AWS::IAM::ManagedPolicy``, or ``AWS::IAM::RolePolicy`` resources in this
template. The latter conservative rule makes dynamic ``!If``/``!Join``
attachment forms fail closed without a partial IAM evaluator. If the template
later needs standalone policies, that change must introduce an equally
reviewable attachment analysis.

This contract is intentionally scoped to identity-based policies. Resource-based
``Principal`` grants are separate authorization surfaces.

Reference: https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html#grant-least-privilege
"""

# Standard library
import pathlib
from typing import Any

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
BASE_INFRASTRUCTURE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml"
CHAT_ROUTE = PROJECT_ROOT / "ui/src/pages/api/copilot/chat.ts"


def _template() -> dict[str, Any]:
    parsed: dict[str, Any] = load_cfn_template(BASE_INFRASTRUCTURE_TEMPLATE.read_text(encoding="utf-8"))
    return parsed


def _resource(logical_id: str) -> dict[str, Any]:
    resource: dict[str, Any] = _template()["Resources"][logical_id]
    return resource


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def test_task_role_has_no_inline_or_managed_identity_policies():
    properties = _resource("ECSTaskRole")["Properties"]
    assert _as_list(properties.get("Policies")) == []
    assert _as_list(properties.get("ManagedPolicyArns")) == []


def test_base_template_has_no_standalone_identity_policy_resources():
    """Disallow every alternate role-policy attachment form in this template."""
    forbidden_types = {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::RolePolicy"}
    offenders = sorted(
        logical_id
        for logical_id, resource in _template()["Resources"].items()
        if isinstance(resource, dict) and resource.get("Type") in forbidden_types
    )
    assert offenders == [], f"Standalone identity policies bypass the inline-role invariant: {offenders}"


def test_standalone_role_policy_resource_is_rejected():
    """A standalone ``AWS::IAM::RolePolicy`` is an attachment form the guard must reject.

    Such a resource grants actions to a role by name without appearing in the
    role's own ``Policies``/``ManagedPolicyArns``. Mirror the guard's own type
    set against a synthetic template so the invariant stays explicit.
    """
    forbidden_types = {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::RolePolicy"}
    synthetic = {
        "Resources": {
            "ECSTaskRole": {"Type": "AWS::IAM::Role", "Properties": {}},
            "SneakyRolePolicy": {
                "Type": "AWS::IAM::RolePolicy",
                "Properties": {
                    "RoleName": {"Ref": "ECSTaskRole"},
                    "PolicyName": "cognito-admin",
                    "PolicyDocument": {
                        "Statement": [{"Effect": "Allow", "Action": "cognito-idp:AdminListGroupsForUser"}]
                    },
                },
            },
        }
    }
    offenders = sorted(
        logical_id
        for logical_id, resource in synthetic["Resources"].items()
        if isinstance(resource, dict) and resource.get("Type") in forbidden_types
    )
    assert offenders == ["SneakyRolePolicy"]


def test_get_caller_identity_call_remains_without_an_sts_grant():
    """Preserve account discovery while keeping its unnecessary IAM allow absent."""
    source = CHAT_ROUTE.read_text(encoding="utf-8")
    assert "GetCallerIdentityCommand" in source
    assert "sts:GetCallerIdentity" not in str(_resource("ECSTaskRole"))


def test_task_role_has_no_cognito_administrator_grant():
    """The internet-facing frontend task role must never gain Cognito admin actions.

    Access is administrator-provisioned out-of-band, so the proxy performs no
    Cognito administrator call. This asserts no ``cognito-idp:`` action is
    written on the role resource itself. Standalone attachment forms that grant
    actions to the role by name are rejected separately by
    ``test_base_template_has_no_standalone_identity_policy_resources``.
    """
    assert "cognito-idp:" not in str(_resource("ECSTaskRole"))


def test_task_role_retains_scoped_ecs_trust():
    """The empty role still has the expected ECS task trust boundary."""
    trust = _resource("ECSTaskRole")["Properties"]["AssumeRolePolicyDocument"]
    statement = _as_list(trust["Statement"])
    assert len(statement) == 1
    assert statement[0]["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert statement[0]["Action"] == "sts:AssumeRole"
    assert "aws:SourceAccount" in statement[0]["Condition"]["StringEquals"]


def test_execution_role_retains_cloudwatch_log_delivery():
    """The separate execution role still delivers container stdout and stderr."""
    arns = _as_list(_resource("ECSTaskExecutionRole")["Properties"].get("ManagedPolicyArns"))
    assert any(str(arn).endswith("AmazonECSTaskExecutionRolePolicy") for arn in arns), arns
