"""Contract tests for optional IAM role path and permissions-boundary placement.

Some accounts only allow deployers to create workload roles under a fixed IAM
path and with a required permissions boundary. Every role the deployment
creates must accept both settings, the defaults must leave existing stacks
unchanged, and both deploy paths must pass the settings to every role-creating
stack. The AgentCore CodeBuild role is pre-created so the starter toolkit never
creates an unplaced role of its own.
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
CFN_DIR = PROJECT_ROOT / "infrastructure/cloudformation"
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts/deploy.sh"
KB_DEPLOY_SCRIPT = PROJECT_ROOT / "scripts/infrastructure/deploy-kb.sh"
PS_DEPLOY = PROJECT_ROOT / "scripts/powershell/Public/Deploy-GameAgent.ps1"
PS_KB_DEPLOY = PROJECT_ROOT / "scripts/powershell/Private/Invoke-GameAgentKBDeploy.ps1"
PS_IAM_PARAMETERS = PROJECT_ROOT / "scripts/powershell/Private/Get-GameAgentIamRoleParameters.ps1"

EXPECTED_ROLE_TEMPLATES = {
    "01-base-infrastructure.yaml",
    "05-security-infrastructure.yaml",
    "knowledge-base-cost.yaml",
    "knowledge-base-eks.yaml",
    "knowledge-base-gamelift.yaml",
}

# The short-form intrinsics load as plain lists: !If [Condition, !Ref X, !Ref AWS::NoValue].
CONDITIONAL_PATH = ["HasCustomRolePath", "RolePath", "AWS::NoValue"]
CONDITIONAL_BOUNDARY = ["HasPermissionsBoundary", "PermissionsBoundaryArn", "AWS::NoValue"]


def _templates() -> dict[str, dict]:
    return {path.name: load_cfn_template(path.read_text(encoding="utf-8")) for path in sorted(CFN_DIR.glob("*.yaml"))}


def _roles(template: dict) -> dict[str, dict]:
    return {
        logical_id: resource
        for logical_id, resource in template.get("Resources", {}).items()
        if resource.get("Type") == "AWS::IAM::Role"
    }


def _role_templates() -> dict[str, dict]:
    return {name: template for name, template in _templates().items() if _roles(template)}


def test_role_creating_templates_are_the_expected_set():
    assert set(_role_templates()) == EXPECTED_ROLE_TEMPLATES


def test_any_instance_profile_takes_its_path_from_the_parameter():
    # No template creates one today; a future instance profile must follow the roles.
    for name, template in _templates().items():
        for logical_id, resource in template.get("Resources", {}).items():
            if resource.get("Type") == "AWS::IAM::InstanceProfile":
                assert resource["Properties"].get("Path") == CONDITIONAL_PATH, f"{name}:{logical_id} Path"


@pytest.mark.parametrize("template_name", sorted(EXPECTED_ROLE_TEMPLATES))
def test_template_declares_backward_compatible_placement_parameters(template_name):
    template = _templates()[template_name]
    parameters = template["Parameters"]

    assert parameters["RolePath"]["Type"] == "String"
    assert parameters["RolePath"]["Default"] == "/"
    assert parameters["PermissionsBoundaryArn"]["Type"] == "String"
    assert parameters["PermissionsBoundaryArn"]["Default"] == ""

    # !Not [!Equals [!Ref X, value]] loads as [["X", value]].
    conditions = template["Conditions"]
    assert conditions["HasCustomRolePath"] == [["RolePath", "/"]]
    assert conditions["HasPermissionsBoundary"] == [["PermissionsBoundaryArn", ""]]


@pytest.mark.parametrize("template_name", sorted(EXPECTED_ROLE_TEMPLATES))
def test_every_role_takes_path_and_boundary_from_the_parameters(template_name):
    for logical_id, role in _roles(_templates()[template_name]).items():
        properties = role["Properties"]
        # Conditional so a default deployment leaves the property unset: adding an
        # explicit Path to an existing named role would force an impossible replacement.
        assert properties.get("Path") == CONDITIONAL_PATH, f"{template_name}:{logical_id} Path"
        assert (
            properties.get("PermissionsBoundary") == CONDITIONAL_BOUNDARY
        ), f"{template_name}:{logical_id} PermissionsBoundary"


@pytest.mark.parametrize("template_name", sorted(EXPECTED_ROLE_TEMPLATES))
def test_parameter_patterns_accept_valid_placements_and_reject_invalid_ones(template_name):
    parameters = _templates()[template_name]["Parameters"]
    path_pattern = re.compile(parameters["RolePath"]["AllowedPattern"])
    boundary_pattern = re.compile(parameters["PermissionsBoundaryArn"]["AllowedPattern"])

    for valid in ("/", "/workloads/", "/team/apps/"):
        assert path_pattern.fullmatch(valid), valid
    for invalid in ("", "workloads", "/workloads", "workloads/", "/work loads/"):
        assert not path_pattern.fullmatch(invalid), invalid

    for valid in (
        "",
        "arn:aws:iam::123456789012:policy/ExampleBoundary",
        "arn:aws-us-gov:iam::123456789012:policy/a/b",
    ):
        assert boundary_pattern.fullmatch(valid), valid
    for invalid in ("ExampleBoundary", "arn:aws:iam::123456789012:role/ExampleRole", "arn:aws:iam::12345:policy/x"):
        assert not boundary_pattern.fullmatch(invalid), invalid


def test_codebuild_role_is_precreated_for_the_starter_toolkit():
    template = _templates()["01-base-infrastructure.yaml"]
    role = template["Resources"]["AgentCoreCodeBuildRole"]["Properties"]

    statement = role["AssumeRolePolicyDocument"]["Statement"]
    assert len(statement) == 1
    assert statement[0]["Principal"] == {"Service": "codebuild.amazonaws.com"}
    assert statement[0]["Condition"] == {"StringEquals": {"aws:SourceAccount": "AWS::AccountId"}}
    assert "ManagedPolicyArns" not in role

    statements = {item["Sid"]: item for item in role["Policies"][0]["PolicyDocument"]["Statement"]}
    assert statements["EcrAuthorization"]["Action"] == ["ecr:GetAuthorizationToken"]
    for sid in ("EcrPushRuntimeImage", "CodeBuildLogs", "CodeBuildSourceBucket"):
        assert statements[sid]["Resource"] != "*", sid
        assert all("*" not in action for action in statements[sid]["Action"]), sid

    output = template["Outputs"]["AgentCoreCodeBuildRoleArn"]
    assert output["Value"] == "AgentCoreCodeBuildRole.Arn"


def test_bash_deploy_passes_placement_to_every_role_creating_stack():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    kb_deploy = KB_DEPLOY_SCRIPT.read_text(encoding="utf-8")

    for script in (deploy, kb_deploy):
        assert "GBAW_IAM_ROLE_PATH" in script
        assert "GBAW_IAM_PERMISSIONS_BOUNDARY_ARN" in script
        assert '"RolePath=' in script
        assert '"PermissionsBoundaryArn=' in script

    for template_name in ("01-base-infrastructure.yaml", "05-security-infrastructure.yaml"):
        block = deploy[deploy.index(template_name) :].split("--region", 1)[0]
        assert '"${IAM_ROLE_PARAMETER_OVERRIDES[@]}"' in block, template_name
    for template_name in ("knowledge-base-gamelift.yaml", "knowledge-base-eks.yaml", "knowledge-base-cost.yaml"):
        block = kb_deploy[kb_deploy.index(template_name) :].split("--region", 1)[0]
        assert '"${IAM_ROLE_PARAMETER_OVERRIDES[@]}"' in block, template_name


def test_powershell_deploy_passes_placement_to_every_role_creating_stack():
    deploy = PS_DEPLOY.read_text(encoding="utf-8")
    kb_deploy = PS_KB_DEPLOY.read_text(encoding="utf-8")
    parameters = PS_IAM_PARAMETERS.read_text(encoding="utf-8")

    assert "GBAW_IAM_ROLE_PATH" in parameters
    assert "GBAW_IAM_PERMISSIONS_BOUNDARY_ARN" in parameters
    assert '"RolePath=$rolePath"' in parameters
    assert '"PermissionsBoundaryArn=$boundaryArn"' in parameters

    assert "$iamRoleParams = Get-GameAgentIamRoleParameters" in deploy
    for template_name in ("01-base-infrastructure.yaml", "05-security-infrastructure.yaml"):
        block = deploy[deploy.index(template_name) :].split("\n\n", 1)[0]
        assert "+ $iamRoleParams" in block, template_name
    assert "-IamRoleParams $iamRoleParams" in deploy
    assert "+ $IamRoleParams +" in kb_deploy


def test_both_deploy_paths_pass_the_precreated_codebuild_role():
    bash = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    powershell = PS_DEPLOY.read_text(encoding="utf-8")

    assert "AgentCoreCodeBuildRoleArn" in bash
    assert '--code-build-execution-role "$CODEBUILD_ROLE_ARN"' in bash
    assert "AgentCoreCodeBuildRoleArn" in powershell
    assert "--code-build-execution-role $codeBuildRoleArn" in powershell
