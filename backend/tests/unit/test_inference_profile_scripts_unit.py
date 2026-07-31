"""Smoke tests for role-based inference-profile shell helpers."""

# Standard library
import os
import pathlib
import subprocess

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
MANAGE_SCRIPT = PROJECT_ROOT / "scripts/infrastructure/manage-inference-profile.sh"
GET_SCRIPT = PROJECT_ROOT / "scripts/infrastructure/get-inference-profile-ids.sh"
BASE_INFRASTRUCTURE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml"
SCRIPTS = (MANAGE_SCRIPT, GET_SCRIPT)


def _write_executable(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _cloudformation_resource(template: str, logical_id: str) -> str:
    start = template.index(f"  {logical_id}:\n")
    lines = template[start:].splitlines(keepends=True)
    resource = [lines[0]]
    for line in lines[1:]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        resource.append(line)
    return "".join(resource)


def _stub_environment(tmp_path: pathlib.Path, uv_exit_code: int = 0, aws_mode: str = "profiles") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    if uv_exit_code:
        uv_content = f"#!/bin/sh\nexit {uv_exit_code}\n"
    else:
        uv_content = """#!/bin/sh
printf "%s\\n" \
  "export GBAW_ORCHESTRATOR_MODEL_ID='global.anthropic.claude-haiku-4-5-20251001-v1:0'" \
  "export GBAW_SPECIALIST_MODEL_ID='global.anthropic.claude-sonnet-4-6'"
"""
    _write_executable(bin_dir / "uv", uv_content)

    if aws_mode == "profiles":
        aws_content = """#!/bin/sh
case "$*" in
  *GameAgent-Orchestrator*) printf "%s\\n" "orchestrator-profile-id" ;;
  *GameAgent-Specialist*) printf "%s\\n" "specialist-profile-id" ;;
  *) exit 1 ;;
esac
"""
    elif aws_mode == "empty":
        aws_content = "#!/bin/sh\nexit 0\n"
    else:
        aws_content = """#!/bin/sh
printf "called" > "$AWS_CALLED_MARKER"
exit 99
"""
    _write_executable(bin_dir / "aws", aws_content)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AWS_CALLED_MARKER"] = str(tmp_path / "aws-called")
    return env


@pytest.mark.parametrize("script", SCRIPTS)
def test_inference_profile_script_has_valid_bash_syntax(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS)
def test_inference_profile_script_uses_uv_model_loader(script):
    content = script.read_text(encoding="utf-8")
    assert 'uv run --directory "$PROJECT_ROOT/backend" python' in content
    assert "load_deployment_settings.py" in content
    assert "GBAW_ORCHESTRATOR_MODEL_ID" in content
    assert "GBAW_SPECIALIST_MODEL_ID" in content


def test_get_profile_ids_prefers_role_application_profiles(tmp_path):
    result = subprocess.run(
        ["bash", str(GET_SCRIPT), "us-west-2"],
        env=_stub_environment(tmp_path, aws_mode="profiles"),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "GBAW_ORCHESTRATOR_MODEL_ID='orchestrator-profile-id'" in result.stdout
    assert "GBAW_SPECIALIST_MODEL_ID='specialist-profile-id'" in result.stdout


def test_agentcore_role_can_invoke_preferred_application_profiles():
    template = BASE_INFRASTRUCTURE_TEMPLATE.read_text(encoding="utf-8")
    execution_role = _cloudformation_resource(template, "AgentCoreExecutionRole")

    assert "bedrock:InvokeModel" in execution_role
    assert "!Sub 'arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:application-inference-profile/*'" in execution_role


def test_get_profile_ids_falls_back_to_canonical_system_profiles(tmp_path):
    result = subprocess.run(
        ["bash", str(GET_SCRIPT), "us-west-2"],
        env=_stub_environment(tmp_path, aws_mode="empty"),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "GBAW_ORCHESTRATOR_MODEL_ID='global.anthropic.claude-haiku-4-5-20251001-v1:0'" in result.stdout
    assert "GBAW_SPECIALIST_MODEL_ID='global.anthropic.claude-sonnet-4-6'" in result.stdout


def test_manage_profile_check_uses_role_application_profiles(tmp_path):
    result = subprocess.run(
        ["bash", str(MANAGE_SCRIPT), "check", "us-west-2"],
        env=_stub_environment(tmp_path, aws_mode="profiles"),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "orchestrator-profile-id" in result.stdout
    assert "specialist-profile-id" in result.stdout


@pytest.mark.parametrize(
    ("script", "arguments"),
    [(MANAGE_SCRIPT, ["check", "us-west-2"]), (GET_SCRIPT, ["us-west-2"])],
)
def test_loader_failure_aborts_before_aws(script, arguments, tmp_path):
    env = _stub_environment(tmp_path, uv_exit_code=42, aws_mode="fail")
    result = subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Unable to resolve" in result.stderr
    assert not pathlib.Path(env["AWS_CALLED_MARKER"]).exists()
