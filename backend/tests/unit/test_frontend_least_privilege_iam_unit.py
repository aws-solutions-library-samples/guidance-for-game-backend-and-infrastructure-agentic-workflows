"""Identity-policy regressions for the frontend ECS task role (issue #458).

The frontend is a Next.js proxy. Its ECS task role intentionally has no
identity-based permissions:

* AgentCore is invoked over HTTPS with the user's verified Cognito access token,
  not with task-role SigV4 credentials.
* Container log delivery is performed by the separate ECS task execution role.
* The one supported task-credential call, ``sts:GetCallerIdentity``, requires no
  IAM allow.
* Dormant account-administration routes reference Cognito Admin/List operations,
  but the default deployment has never granted them. Issue #473 removes that
  unsupported surface after #469; this role must not make it reachable.

The base template models role grants inline. This test therefore prohibits both
policy properties on ``ECSTaskRole`` and standalone ``AWS::IAM::Policy`` or
``AWS::IAM::ManagedPolicy`` resources in this template. The latter conservative
rule makes dynamic ``!If``/``!Join`` attachment forms fail closed without a
partial IAM evaluator. If the template later needs standalone policies, that
change must introduce an equally reviewable attachment analysis.

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
    forbidden_types = {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}
    offenders = sorted(
        logical_id
        for logical_id, resource in _template()["Resources"].items()
        if isinstance(resource, dict) and resource.get("Type") in forbidden_types
    )
    assert offenders == [], f"Standalone identity policies bypass the inline-role invariant: {offenders}"


def test_get_caller_identity_call_remains_without_an_sts_grant():
    """Preserve account discovery while keeping its unnecessary IAM allow absent."""
    source = CHAT_ROUTE.read_text(encoding="utf-8")
    assert "GetCallerIdentityCommand" in source
    assert "sts:GetCallerIdentity" not in str(_resource("ECSTaskRole"))


def test_dormant_cognito_admin_routes_remain_unprivileged():
    """Do not make the unsupported account-administration surface reachable."""
    admin_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            PROJECT_ROOT / "ui/src/pages/api/admin/users.ts",
            PROJECT_ROOT / "ui/src/pages/api/admin/approve.ts",
        )
    )
    assert "AdminListGroupsForUserCommand" in admin_source
    assert "AdminConfirmSignUpCommand" in admin_source
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
