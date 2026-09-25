"""Behavioral tests for deployment shell helpers."""

# Standard library
import os
import pathlib
import re
import subprocess
import tempfile

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
WAF_FUNCTION_NAMES = (
    "is_resolved_deployment_value",
    "is_transient_waf_error",
    "get_active_web_acl_arn",
    "web_acl_has_required_rules",
    "reconcile_waf_association",
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
    "GBAW_TENANT_ID",
    "GBAW_WORKSPACE_ID",
    "COGNITO_ISSUER",
    "COGNITO_CLIENT_ID",
    "COST_SNAPSHOT_TABLE_NAME",
    "COST_SNAPSHOT_REQUIRED",
    "COST_SNAPSHOT_TTL_SECONDS",
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


def _run_waf_reconciliation(
    active_sequence: str,
    *,
    rule_names: str = (
        "RateLimitAuthPaths RateLimitAdminPaths RateLimitPerIP "
        "AWSManagedRulesCommonRuleSet AWSManagedRulesSQLiRuleSet AWSManagedRulesKnownBadInputsRuleSet"
    ),
    transient_association_failures: int = 0,
    association_error_marker: str = "TRANSIENT_UNAVAILABLE",
    association_max_attempts: int = 3,
    verification_max_attempts: int = 3,
) -> subprocess.CompletedProcess[str]:
    content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    functions = "\n".join(_function_source(content, name) for name in WAF_FUNCTION_NAMES)
    command = "\n".join(
        (
            functions,
            r"""
state_dir="$WAF_TEST_STATE_DIR"
next_sequence_value() {
  local sequence="$1"
  local counter_name="$2"
  local count_file="$state_dir/$counter_name"
  local count=0
  local values
  if [ -f "$count_file" ]; then
    count="$(<"$count_file")"
  fi
  count=$((count + 1))
  printf '%s\n' "$count" > "$count_file"
  IFS=',' read -r -a values <<< "$sequence"
  if [ "$count" -gt "${#values[@]}" ]; then
    count="${#values[@]}"
  fi
  printf '%s\n' "${values[$((count - 1))]}"
}
aws() {
  local count
  local value
  if [ "$1" = "wafv2" ] && [ "$2" = "get-web-acl-for-resource" ]; then
    value="$(next_sequence_value "$WAF_TEST_ACTIVE_SEQUENCE" active_count)"
    case "$value" in
      TRANSIENT_UNAVAILABLE)
        echo 'WAFUnavailableEntityException' >&2
        return 255
        ;;
      TRANSIENT_INTERNAL)
        echo 'WAFInternalErrorException' >&2
        return 255
        ;;
    esac
    printf '%s\n' "$value"
    return 0
  fi
  if [ "$1" = "wafv2" ] && [ "$2" = "get-web-acl" ]; then
    printf '%s\n' "$WAF_TEST_RULE_NAMES"
    return 0
  fi
  if [ "$1" = "wafv2" ] && [ "$2" = "associate-web-acl" ]; then
    count="$(next_sequence_value '1,2,3,4,5' association_count)"
    if [ "$count" -le "$WAF_TEST_TRANSIENT_ASSOCIATION_FAILURES" ]; then
      case "$WAF_TEST_ASSOCIATION_ERROR_MARKER" in
        TRANSIENT_INTERNAL) echo 'WAFInternalErrorException' >&2 ;;
        *) echo 'WAFUnavailableEntityException' >&2 ;;
      esac
      return 255
    fi
    return 0
  fi
  return 1
}
sleep() { :; }
GBAW_WAF_ASSOCIATION_MAX_ATTEMPTS="$WAF_TEST_ASSOCIATION_MAX_ATTEMPTS"
GBAW_WAF_VERIFICATION_MAX_ATTEMPTS="$WAF_TEST_VERIFICATION_MAX_ATTEMPTS"
GBAW_WAF_RETRY_SECONDS=0
reconcile_waf_association expected-acl resource-arn
exit_code=$?
for count_name in association_count active_count; do
  count=0
  if [ -f "$state_dir/$count_name" ]; then
    count="$(<"$state_dir/$count_name")"
  fi
  printf '%s=%s\n' "$count_name" "$count"
done
exit "$exit_code"
""",
        )
    )
    with tempfile.TemporaryDirectory() as state_dir:
        env = os.environ.copy()
        env.update(
            {
                "WAF_TEST_STATE_DIR": state_dir,
                "WAF_TEST_ACTIVE_SEQUENCE": active_sequence,
                "WAF_TEST_RULE_NAMES": rule_names,
                "WAF_TEST_TRANSIENT_ASSOCIATION_FAILURES": str(transient_association_failures),
                "WAF_TEST_ASSOCIATION_ERROR_MARKER": association_error_marker,
                "WAF_TEST_ASSOCIATION_MAX_ATTEMPTS": str(association_max_attempts),
                "WAF_TEST_VERIFICATION_MAX_ATTEMPTS": str(verification_max_attempts),
            }
        )
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)


def test_deploy_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(DEPLOY_SCRIPT)], check=True)


@pytest.mark.parametrize("unresolved_value", ["", "None"])
def test_agentcore_env_args_omit_unresolved_optional_values(unresolved_value):
    args = _build_agentcore_env_args({name: unresolved_value for name in OPTIONAL_VARIABLES})

    assert args == [
        "-env",
        "GBAW_HOSTED_RUNTIME=true",
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
            "GBAW_TENANT_ID": "tenant-1",
            "GBAW_WORKSPACE_ID": "workspace-1",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-1",
            "COST_SNAPSHOT_TABLE_NAME": "game-agent-cost-report-snapshots",
            "COST_SNAPSHOT_REQUIRED": "true",
            "COST_SNAPSHOT_TTL_SECONDS": "1800",
        }
    )

    assert args == [
        "-env",
        "GBAW_HOSTED_RUNTIME=true",
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
        "-env",
        "GBAW_TENANT_ID=tenant-1",
        "-env",
        "GBAW_WORKSPACE_ID=workspace-1",
        "-env",
        "GBAW_COGNITO_ISSUER=https://issuer.example",
        "-env",
        "GBAW_COGNITO_CLIENT_ID=client-1",
        "-env",
        "GBAW_COST_SNAPSHOT_TABLE_NAME=game-agent-cost-report-snapshots",
        "-env",
        "GBAW_COST_SNAPSHOT_REQUIRED=true",
        "-env",
        "GBAW_COST_SNAPSHOT_TTL_SECONDS=1800",
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
        "GBAW_HOSTED_RUNTIME=true",
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


def test_waf_reconciliation_skips_association_when_expected_acl_and_rules_are_active():
    result = _run_waf_reconciliation("expected-acl")

    assert result.returncode == 0
    assert result.stdout.splitlines()[-2:] == ["association_count=0", "active_count=1"]


@pytest.mark.parametrize("transient_marker", ["TRANSIENT_UNAVAILABLE", "TRANSIENT_INTERNAL"])
def test_waf_reconciliation_recovers_from_retryable_association_and_lookup_failures(transient_marker):
    result = _run_waf_reconciliation(
        f"platform-acl,{transient_marker},expected-acl",
        transient_association_failures=1,
        association_error_marker=transient_marker,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[-2:] == ["association_count=2", "active_count=3"]


@pytest.mark.parametrize("transient_marker", ["TRANSIENT_UNAVAILABLE", "TRANSIENT_INTERNAL"])
def test_waf_reconciliation_fails_when_retryable_association_errors_exhaust_budget(transient_marker):
    result = _run_waf_reconciliation(
        "platform-acl",
        transient_association_failures=3,
        association_error_marker=transient_marker,
        association_max_attempts=3,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines()[-2:] == ["association_count=3", "active_count=1"]


def test_waf_reconciliation_fails_when_expected_acl_does_not_converge():
    result = _run_waf_reconciliation("platform-acl", verification_max_attempts=3)

    assert result.returncode == 1
    assert result.stdout.splitlines()[-2:] == ["association_count=1", "active_count=4"]


def test_waf_reconciliation_fails_when_required_rules_are_missing():
    result = _run_waf_reconciliation(
        "expected-acl",
        rule_names="RateLimitPerIP AWSManagedRulesCommonRuleSet",
        verification_max_attempts=3,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines()[-2:] == ["association_count=0", "active_count=4"]
