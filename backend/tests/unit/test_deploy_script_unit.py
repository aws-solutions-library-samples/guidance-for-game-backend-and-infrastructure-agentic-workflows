"""Behavioral tests for deployment shell helpers."""

# Standard library
import os
import pathlib
import re
import subprocess

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts/deploy.sh"
FUNCTION_NAMES = (
    "is_resolved_deployment_value",
    "append_agentcore_env_if_resolved",
    "build_agentcore_env_args",
)
OPTIONAL_VARIABLES = (
    "GUARDRAIL_ID",
    "GBAW_ORCHESTRATOR_PROMPT_ARN",
    "GBAW_GAMELIFT_PROMPT_ARN",
    "GBAW_EKS_PROMPT_ARN",
    "GBAW_COST_PROMPT_ARN",
    "GAMELIFT_KB_ID",
    "EKS_KB_ID",
    "COST_KB_ID",
)


def _function_source(script: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", script)
    assert match, f"{name} not found in {DEPLOY_SCRIPT}"
    return match.group(0)


def _build_agentcore_env_args(overrides: dict[str, str] | None = None) -> list[str]:
    content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    functions = "\n".join(_function_source(content, name) for name in FUNCTION_NAMES)
    command = f'{functions}\nbuild_agentcore_env_args\nprintf "%s\\n" "${{AGENTCORE_ENV_ARGS[@]}}"'

    env = os.environ.copy()
    for name in OPTIONAL_VARIABLES:
        env.pop(name, None)
    env.update(
        {
            "GBAW_ORCHESTRATOR_MODEL_ID": "orchestrator-model",
            "GBAW_SPECIALIST_MODEL_ID": "specialist-model",
        }
    )
    env.update(overrides or {})

    result = subprocess.run(
        ["bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_deploy_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(DEPLOY_SCRIPT)], check=True)


@pytest.mark.parametrize("unresolved_value", ["", "None"])
def test_agentcore_env_args_omit_unresolved_optional_values(unresolved_value):
    args = _build_agentcore_env_args({name: unresolved_value for name in OPTIONAL_VARIABLES})

    assert args == [
        "-env",
        "GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model",
        "-env",
        "GBAW_SPECIALIST_MODEL_ID=specialist-model",
    ]


def test_agentcore_env_args_include_resolved_optional_values():
    args = _build_agentcore_env_args(
        {
            "GUARDRAIL_ID": "guardrail-id",
            "GBAW_ORCHESTRATOR_PROMPT_ARN": "orchestrator-prompt",
            "GBAW_GAMELIFT_PROMPT_ARN": "gamelift-prompt",
            "GBAW_EKS_PROMPT_ARN": "eks-prompt",
            "GBAW_COST_PROMPT_ARN": "cost-prompt",
            "GAMELIFT_KB_ID": "gamelift-kb",
            "EKS_KB_ID": "eks-kb",
            "COST_KB_ID": "cost-kb",
        }
    )

    assert args == [
        "-env",
        "GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model",
        "-env",
        "GBAW_SPECIALIST_MODEL_ID=specialist-model",
        "-env",
        "GBAW_BEDROCK_GUARDRAIL_ID=guardrail-id",
        "-env",
        "GBAW_BEDROCK_GUARDRAIL_VERSION=DRAFT",
        "-env",
        "GBAW_ORCHESTRATOR_PROMPT_ARN=orchestrator-prompt",
        "-env",
        "GBAW_GAMELIFT_PROMPT_ARN=gamelift-prompt",
        "-env",
        "GBAW_EKS_PROMPT_ARN=eks-prompt",
        "-env",
        "GBAW_COST_PROMPT_ARN=cost-prompt",
        "-env",
        "GBAW_GAMELIFT_KB_ID=gamelift-kb",
        "-env",
        "GBAW_EKS_KB_ID=eks-kb",
        "-env",
        "GBAW_COST_KB_ID=cost-kb",
    ]


def test_agentcore_env_args_filter_optional_values_independently():
    args = _build_agentcore_env_args(
        {
            "GUARDRAIL_ID": "None",
            "GBAW_ORCHESTRATOR_PROMPT_ARN": "orchestrator-prompt",
            "GBAW_GAMELIFT_PROMPT_ARN": "None",
            "GBAW_EKS_PROMPT_ARN": "eks-prompt",
            "GBAW_COST_PROMPT_ARN": "",
            "GAMELIFT_KB_ID": "gamelift-kb",
            "EKS_KB_ID": "None",
            "COST_KB_ID": "cost-kb",
        }
    )

    assert args == [
        "-env",
        "GBAW_ORCHESTRATOR_MODEL_ID=orchestrator-model",
        "-env",
        "GBAW_SPECIALIST_MODEL_ID=specialist-model",
        "-env",
        "GBAW_ORCHESTRATOR_PROMPT_ARN=orchestrator-prompt",
        "-env",
        "GBAW_EKS_PROMPT_ARN=eks-prompt",
        "-env",
        "GBAW_GAMELIFT_KB_ID=gamelift-kb",
        "-env",
        "GBAW_COST_KB_ID=cost-kb",
    ]
