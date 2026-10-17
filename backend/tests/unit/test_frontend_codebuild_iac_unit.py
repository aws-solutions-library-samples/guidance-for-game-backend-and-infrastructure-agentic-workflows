"""Contract tests for building the frontend image in CodeBuild.

Deploy hosts without Docker build the frontend in an amd64 CodeBuild project
from the base stack. The project must match the ECS Express runtime
architecture, its role must stay scoped to the frontend repository and build
sources, and both deploy paths must fall back to it.
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
BASE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml"
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts/deploy.sh"
BUILD_SCRIPT = PROJECT_ROOT / "scripts/infrastructure/build-frontend-codebuild.sh"
PS_DEPLOY = PROJECT_ROOT / "scripts/powershell/Public/Deploy-GameAgent.ps1"
PS_BUILD = PROJECT_ROOT / "scripts/powershell/Private/Invoke-GameAgentFrontendCodeBuild.ps1"
DOCKERFILE = PROJECT_ROOT / "ui/Dockerfile"


@pytest.fixture(scope="module")
def resources() -> dict:
    return load_cfn_template(BASE_TEMPLATE.read_text(encoding="utf-8"))["Resources"]


def test_project_builds_amd64_images_with_a_docker_daemon(resources):
    environment = resources["FrontendBuildProject"]["Properties"]["Environment"]

    assert environment["Type"] == "LINUX_CONTAINER"
    assert "x86_64" in environment["Image"]
    assert environment["PrivilegedMode"] is True
    buildspec = resources["FrontendBuildProject"]["Properties"]["Source"]["BuildSpec"]
    assert "--platform linux/amd64" in buildspec
    assert 'test -n "$IMAGE_TAG"' in buildspec
    assert "public.ecr.aws/docker/library/node:24-alpine" in buildspec


def test_project_uses_the_scoped_build_role_and_owned_log_group(resources):
    properties = resources["FrontendBuildProject"]["Properties"]

    assert properties["ServiceRole"] == "FrontendBuildRole.Arn"
    assert properties["LogsConfig"]["CloudWatchLogs"]["GroupName"] == "FrontendBuildLogGroup"
    assert resources["FrontendBuildLogGroup"]["Properties"]["RetentionInDays"] == 30


def test_build_role_is_scoped_to_the_frontend_repository_and_sources(resources):
    role = resources["FrontendBuildRole"]["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust == [
        {
            "Effect": "Allow",
            "Principal": {"Service": "codebuild.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": "AWS::AccountId"}},
        }
    ]
    assert "ManagedPolicyArns" not in role

    statements = {item["Sid"]: item for item in role["Policies"][0]["PolicyDocument"]["Statement"]}
    assert set(statements) == {
        "ReadBuildSource",
        "ReadBuildSourceBucket",
        "WriteBuildLogs",
        "EcrAuthorization",
        "EcrPushFrontendImage",
    }
    assert statements["EcrPushFrontendImage"]["Resource"] == ["FrontendRepository.Arn"]
    assert statements["EcrAuthorization"]["Action"] == ["ecr:GetAuthorizationToken"]
    assert statements["ReadBuildSource"]["Action"] == ["s3:GetObject", "s3:GetObjectVersion"]
    for sid, statement in statements.items():
        assert all(not action.endswith("*") for action in statement["Action"]), sid
        if sid != "EcrAuthorization":
            assert statement["Resource"] != "*", sid


def test_source_bucket_is_private_encrypted_and_short_lived(resources):
    bucket = resources["FrontendBuildSourceBucket"]["Properties"]

    assert all(bucket["PublicAccessBlockConfiguration"].values())
    assert bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]
    assert bucket["OwnershipControls"]["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"
    assert bucket["LifecycleConfiguration"]["Rules"][0]["ExpirationInDays"] == 7

    statement = resources["FrontendBuildSourceBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
    assert statement["Effect"] == "Deny"
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_dockerfile_keeps_the_docker_hub_default_base_image():
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG NODE_IMAGE=node:24-alpine" in text
    assert "FROM ${NODE_IMAGE} AS base" in text
    assert text.index("ARG NODE_IMAGE") < text.index("FROM ${NODE_IMAGE}")


def test_bash_deploy_falls_back_to_codebuild_without_docker():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'if [ "$DOCKER_AVAILABLE" = true ]; then FRONTEND_BUILD=docker; else FRONTEND_BUILD=codebuild; fi' in deploy
    assert 'build-frontend-codebuild.sh" build "$FRONTEND_IMAGE_TAG"' in deploy
    assert 'if [ "$FRONTEND_ENABLED" = true ]; then' in deploy
    # Steps 6-8 no longer depend on a local Docker daemon.
    assert 'if [ "$DOCKER_AVAILABLE" = true ]; then\n\n# Step 6' not in deploy

    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "git ls-files --cached --others --exclude-standard" in script
    assert "FrontendBuildProjectName" in script
    assert "FrontendBuildSourceBucketName" in script


def test_powershell_deploy_falls_back_to_codebuild_without_docker():
    deploy = PS_DEPLOY.read_text(encoding="utf-8")
    helper = PS_BUILD.read_text(encoding="utf-8")

    assert "elseif ($dockerAvailable) { 'docker' } else { 'codebuild' }" in deploy
    assert "Invoke-GameAgentFrontendCodeBuild" in deploy
    # No step is gated on a local Docker daemon any more.
    assert re.search(r"^\s*if \(\$dockerAvailable\)", deploy, re.MULTILINE) is None
    assert "ls-files --cached --others --exclude-standard" in helper
    assert "FrontendBuildProjectName" in helper
