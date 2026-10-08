"""Pin the runtime role's explicit, read-only CloudFormation draft-validation grant."""

# Standard library
import pathlib
from typing import Any

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

_TEMPLATE = pathlib.Path(__file__).parents[3] / "infrastructure/cloudformation/01-base-infrastructure.yaml"


def _runtime_statements() -> list[dict[str, Any]]:
    template = load_cfn_template(_TEMPLATE.read_text(encoding="utf-8"))
    policies = template["Resources"]["AgentCoreExecutionRole"]["Properties"]["Policies"]
    return [statement for policy in policies for statement in policy["PolicyDocument"]["Statement"]]


def _by_sid(sid: str) -> dict[str, Any]:
    return next(s for s in _runtime_statements() if s.get("Sid") == sid)


def _actions(statement: dict[str, Any]) -> list[str]:
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else actions


def test_validate_template_is_the_only_action_and_is_region_bound():
    statement = _by_sid("CloudFormationDraftValidation")
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == ["cloudformation:ValidateTemplate"]
    assert "aws:RequestedRegion" in statement["Condition"]["StringEquals"]


def test_describe_type_is_limited_to_public_aws_resource_types():
    statement = _by_sid("CloudFormationPublicTypeSchemas")
    assert _actions(statement) == ["cloudformation:DescribeType"]
    (resource,) = statement["Resource"]
    assert resource.endswith("::type/resource/AWS-*")
    assert ":${AWS::AccountId}:" not in resource


def test_no_inline_statement_grants_cloudformation_writes():
    write_prefixes = (
        "cloudformation:Create",
        "cloudformation:Update",
        "cloudformation:Delete",
        "cloudformation:Execute",
        "cloudformation:Set",
        "cloudformation:Register",
        "cloudformation:Activate",
        "cloudformation:Publish",
        "cloudformation:Import",
        "cloudformation:Continue",
        "cloudformation:Cancel",
        "cloudformation:Signal",
    )
    for statement in _runtime_statements():
        if statement.get("Effect") != "Allow":
            continue
        for action in _actions(statement):
            assert action not in {"*", "cloudformation:*"}, statement.get("Sid")
            assert not action.startswith(write_prefixes), (statement.get("Sid"), action)
