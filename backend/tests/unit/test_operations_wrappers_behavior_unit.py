"""Behavioral tests for the optional E1 deploy/teardown wrappers (issue #413).

These execute the wrappers' *refusal* and *help* paths only, which exit before
any ``aws`` invocation, so they never touch AWS. They prove the opt-in and
confirmation gates fail closed under the frozen ``observe`` mode vocabulary and
that the scripts parse under ``bash -n``.
"""

# Standard library
import os
import pathlib
import re
import subprocess

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DEPLOY = PROJECT_ROOT / "scripts/infrastructure/deploy-operations.sh"
TEARDOWN = PROJECT_ROOT / "scripts/infrastructure/teardown-operations.sh"

_CLEARED_ENV = (
    "GBAW_OPERATIONS_MODE",
    "COGNITO_ISSUER",
    "COGNITO_CLIENT_ID",
    "TENANT_ID",
    "WORKSPACE_ID",
    "GBAW_OPERATIONS_ARTIFACT_BUCKET",
    "CODE_S3_BUCKET",
    "CODE_S3_KEY",
)


def _run(script, *args, env_extra=None):
    env = os.environ.copy()
    for key in _CLEARED_ENV:
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("script", [DEPLOY, TEARDOWN])
def test_scripts_parse_under_bash_n(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_deploy_enable_without_observe_mode_refuses():
    # Enable requires GBAW_OPERATIONS_MODE=observe; absence fails closed.
    result = _run(DEPLOY, "--enable")
    assert result.returncode == 3
    assert "Refusing to enable" in result.stdout + result.stderr


def test_deploy_enable_with_wrong_mode_refuses():
    result = _run(DEPLOY, "--enable", env_extra={"GBAW_OPERATIONS_MODE": "enabled"})
    assert result.returncode == 3


def test_deploy_enable_without_cognito_refuses():
    result = _run(DEPLOY, "--enable", env_extra={"GBAW_OPERATIONS_MODE": "observe"})
    assert result.returncode == 3
    assert "COGNITO" in (result.stdout + result.stderr).upper()


def test_deploy_enable_without_tenant_refuses():
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "observe",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-123",
        },
    )
    assert result.returncode == 3
    combined = (result.stdout + result.stderr).upper()
    assert "TENANT" in combined or "WORKSPACE" in combined


def test_deploy_enable_without_artifact_bucket_refuses():
    # With mode + cognito + tenant/workspace but no explicit artifact bucket, the
    # wrapper must refuse (exit 6) before any AWS write: it never discovers or
    # creates a bucket. This path exits before the credential/AWS calls.
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "observe",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-123",
            "TENANT_ID": "tenant-abc",
            "WORKSPACE_ID": "workspace-abc",
        },
    )
    assert result.returncode == 6
    combined = (result.stdout + result.stderr).upper()
    assert "GBAW_OPERATIONS_ARTIFACT_BUCKET" in combined


def test_deploy_unknown_arg_exits_nonzero():
    result = _run(DEPLOY, "--yolo")
    assert result.returncode != 0


def test_teardown_without_confirm_refuses():
    result = _run(TEARDOWN)
    assert result.returncode == 3
    assert "Refusing to tear down" in result.stdout + result.stderr


def test_teardown_wrong_token_refuses():
    result = _run(TEARDOWN, "--confirm", "wrong")
    assert result.returncode == 3


def test_teardown_help_is_clean():
    result = _run(TEARDOWN, "--help")
    assert result.returncode == 0
    assert "never called by teardown-all.sh" in result.stdout


def test_teardown_help_has_no_delete_data_flag():
    result = _run(TEARDOWN, "--help")
    assert "--delete-data" not in result.stdout


# --------------------------------------------------------------------------- #
# Rollback design: the --disable path must be a data-preserving, no-rebuild
# update that reuses the stack's existing parameter values and only flips the
# runtime authority. These assertions read the wrapper source (the AWS-dependent
# branch cannot execute without credentials/a live stack), which is the same
# static-contract style the infra test uses for wrapper invariants.
# --------------------------------------------------------------------------- #
_DEPLOY_TEXT = DEPLOY.read_text(encoding="utf-8")


def _disable_branch():
    """Return the source of the `if [ "$ACTION" = "disable" ]; then ... fi`
    branch so assertions target the disable path specifically."""
    marker = 'if [ "$ACTION" = "disable" ]; then'
    start = _DEPLOY_TEXT.index(marker)
    # The enable-path deploy comment reliably follows the disable branch.
    end = _DEPLOY_TEXT.index("# ENABLE path deploy", start)
    return _DEPLOY_TEXT[start:end]


def test_disable_keeps_provisioned_true_and_sets_mode_disabled():
    branch = _disable_branch()
    assert (
        "ParameterKey=Provisioned,ParameterValue=true" in branch
    ), "disable must KEEP Provisioned=true so resources are not deleted"
    assert (
        "ParameterKey=OperationsMode,ParameterValue=disabled" in branch
    ), "disable must set OperationsMode=disabled (fail closed)"


def test_disable_reuses_existing_parameter_values():
    """Every binding the provisioned resources reference must be reused via
    UsePreviousValue, never blanked — blanking would violate the template Rules
    and (previously) orphan/rename resources."""
    branch = _disable_branch()
    for reused in (
        "CognitoIssuer",
        "CognitoClientId",
        "TenantId",
        "WorkspaceId",
        "TrustedAudience",
        "CodeS3Bucket",
        "CodeS3Key",
    ):
        assert (
            f"ParameterKey={reused},UsePreviousValue=true" in branch
        ), f"disable must reuse {reused} via UsePreviousValue"


def test_disable_does_not_rebuild_code_or_run_docker():
    branch = _disable_branch()
    for forbidden in ("zip", "pip install", "uv pip", "docker", "shasum", "s3 cp", "head-bucket"):
        assert forbidden not in branch, f"disable path must not '{forbidden}': emergency disable rebuilds no code"


def test_disable_verifies_target_stack_and_refuses_unprovisioned():
    branch = _disable_branch()
    assert "describe-stacks" in branch, "disable must verify the target stack exists"
    assert (
        "Provisioned" in branch and "not provisioned" in branch
    ), "disable must refuse a stack that is not provisioned"
    # Distinct exit codes for 'no/unprovisioned stack' (7) and 'update failed' (8).
    assert "exit 7" in branch
    assert "exit 8" in branch


def test_disable_updates_through_cloudformation_reversibly():
    branch = _disable_branch()
    assert "update-stack" in branch, "disable must update through CloudFormation"
    # The emergency disable is rebuild-free and size-safe: it reuses the
    # already-deployed template via --use-previous-template and NEVER re-sends
    # the (over-inline-limit) template body, changing only parameters.
    assert "--use-previous-template" in branch, "disable must reuse the deployed template"
    assert "--template-body" not in branch, "disable must not re-send the template body"
    assert "stack-update-complete" in branch, "disable should wait for the update"
    assert "--enable" in branch, "disable messaging must point at the reversible re-enable"


def test_enable_path_provisions_resources():
    """The enable deploy must pass Provisioned=true so resources are created
    (observe requires Provisioned=true per the template Rule)."""
    # The enable PARAM_OVERRIDES block sets Provisioned=true.
    idx = _DEPLOY_TEXT.index("# ENABLE path deploy")
    enable = _DEPLOY_TEXT[idx:]
    assert '"Provisioned=true"' in enable, "enable must provision resources (Provisioned=true)"
    assert '"OperationsMode=${OPERATIONS_MODE}"' in enable


# --------------------------------------------------------------------------- #
# E2 (issue #414) advise-mode wrapper behavior: explicit --mode with a MATCHING
# GBAW_OPERATIONS_MODE double opt-in. These run only the refusal paths, which
# exit before any aws call, so they never touch AWS.
# --------------------------------------------------------------------------- #
def test_deploy_enable_advise_without_matching_mode_env_refuses():
    """--mode advise requires GBAW_OPERATIONS_MODE=advise to match; a mismatched
    (or observe) env value is refused so an enable never silently escalates."""
    result = _run(DEPLOY, "--enable", "--mode", "advise", env_extra={"GBAW_OPERATIONS_MODE": "observe"})
    assert result.returncode == 3
    assert "Refusing to enable" in result.stdout + result.stderr


def test_deploy_enable_observe_with_advise_env_refuses():
    """The reverse mismatch (--mode observe, env advise) is equally refused."""
    result = _run(DEPLOY, "--enable", "--mode", "observe", env_extra={"GBAW_OPERATIONS_MODE": "advise"})
    assert result.returncode == 3


def test_deploy_enable_invalid_mode_refuses():
    """--mode must be observe or advise; any other value fails closed."""
    result = _run(DEPLOY, "--enable", "--mode", "operate", env_extra={"GBAW_OPERATIONS_MODE": "operate"})
    assert result.returncode == 3
    assert "observe or advise" in (result.stdout + result.stderr)


def test_deploy_enable_advise_matching_mode_reaches_input_validation():
    """With --mode advise and a MATCHING GBAW_OPERATIONS_MODE=advise, the mode
    double opt-in passes and the wrapper proceeds to the enabling-input checks
    (Cognito/tenant), still refusing (no creds/inputs) before any aws call."""
    result = _run(DEPLOY, "--enable", "--mode", "advise", env_extra={"GBAW_OPERATIONS_MODE": "advise"})
    assert result.returncode == 3
    combined = (result.stdout + result.stderr).upper()
    # It got past the mode gate: the failure is now the missing Cognito input.
    assert "COGNITO" in combined


def test_deploy_enable_passes_e2_settings_in_overrides():
    """The enable PARAM_OVERRIDES must pass the three E2 settings so the
    advise-mode runtime posture is applied."""
    idx = _DEPLOY_TEXT.index("# ENABLE path deploy")
    enable = _DEPLOY_TEXT[idx:]
    assert '"LowRiskSelfApproval=${LOW_RISK_SELF_APPROVAL}"' in enable
    assert '"PreparationExpirySeconds=${PREPARATION_EXPIRY_SECONDS}"' in enable
    assert '"ApprovalExpirySeconds=${APPROVAL_EXPIRY_SECONDS}"' in enable


def test_disable_reuses_e2_settings_unchanged():
    """An emergency disable must reuse the three E2 settings via UsePreviousValue
    so a disabled-but-provisioned advise stack keeps its posture on re-enable."""
    idx = _DEPLOY_TEXT.index("DISABLE_PARAMS=(")
    disable = _DEPLOY_TEXT[idx:]
    for key in ("LowRiskSelfApproval", "PreparationExpirySeconds", "ApprovalExpirySeconds"):
        assert f'"ParameterKey={key},UsePreviousValue=true"' in disable


def test_disable_help_describes_data_preserving_no_rebuild():
    result = _run(DEPLOY, "--help")
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "data-preserving" in combined.lower()
    assert "does not rebuild" in combined.lower() or "rebuild" in combined.lower()


# --------------------------------------------------------------------------- #
# E2 (issue #414) server-owned CAPACITY POLICY wrapper behavior.
#
# The three capacity-policy settings are server-owned and injected as frozen
# core env names. The wrapper accepts EXPLICIT bounded overrides ONLY from
# dedicated environment variables (never from a request body), passes them on
# enable, and reuses the stack's existing values via UsePreviousValue on an
# emergency disable. Safe defaults (0/1/1) cap a fresh enable at one instance.
# --------------------------------------------------------------------------- #
def test_deploy_capacity_defaults_are_safe_zero_one_one():
    """The wrapper's capacity defaults match the template safe defaults (0/1/1)
    so a fresh enable with no override never scales past one instance."""
    assert re.search(r'CAPACITY_FLOOR="\$\{GBAW_OPERATIONS_CAPACITY_FLOOR:-0\}"', _DEPLOY_TEXT)
    assert re.search(r'CAPACITY_CEILING="\$\{GBAW_OPERATIONS_CAPACITY_CEILING:-1\}"', _DEPLOY_TEXT)
    assert re.search(r'CAPACITY_MAX_STEP="\$\{GBAW_OPERATIONS_CAPACITY_MAX_STEP:-1\}"', _DEPLOY_TEXT)


def test_deploy_capacity_overrides_come_from_env_not_request_body():
    """The bounded overrides are read from explicit environment variables. The
    wrapper never parses a request body / JSON payload for capacity values."""
    # The three overrides are sourced from GBAW_OPERATIONS_CAPACITY_* env vars.
    for env_name in (
        "GBAW_OPERATIONS_CAPACITY_FLOOR",
        "GBAW_OPERATIONS_CAPACITY_CEILING",
        "GBAW_OPERATIONS_CAPACITY_MAX_STEP",
    ):
        assert env_name in _DEPLOY_TEXT, f"{env_name} must be an accepted explicit override"
    # No request-body / JSON parsing feeds the capacity values.
    for forbidden in ("request_body", "REQUEST_BODY", "jq ", "--data", "request.json"):
        assert forbidden not in _DEPLOY_TEXT, f"capacity overrides must not come from {forbidden}"


def test_deploy_enable_passes_capacity_settings_in_overrides():
    """The enable PARAM_OVERRIDES must pass all three capacity settings so the
    server-owned bound is applied."""
    idx = _DEPLOY_TEXT.index("# ENABLE path deploy")
    enable = _DEPLOY_TEXT[idx:]
    assert '"CapacityFloor=${CAPACITY_FLOOR}"' in enable
    assert '"CapacityCeiling=${CAPACITY_CEILING}"' in enable
    assert '"CapacityMaxStep=${CAPACITY_MAX_STEP}"' in enable


def test_disable_reuses_capacity_settings_unchanged():
    """An emergency disable must reuse all three capacity settings via
    UsePreviousValue so a disabled-but-provisioned advise stack keeps its bound
    on re-enable."""
    idx = _DEPLOY_TEXT.index("DISABLE_PARAMS=(")
    disable = _DEPLOY_TEXT[idx:]
    for key in ("CapacityFloor", "CapacityCeiling", "CapacityMaxStep"):
        assert f'"ParameterKey={key},UsePreviousValue=true"' in disable


def test_deploy_enable_incoherent_capacity_floor_above_ceiling_refuses():
    """An explicit override with floor > ceiling is refused before any AWS call.
    The wrapper enforces the FULL numeric coherent bound the template Rules
    cannot express with equality-only functions."""
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "observe",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-123",
            "TENANT_ID": "tenant-abc",
            "WORKSPACE_ID": "workspace-abc",
            "GBAW_OPERATIONS_ARTIFACT_BUCKET": "some-bucket",
            "GBAW_OPERATIONS_CAPACITY_FLOOR": "5",
            "GBAW_OPERATIONS_CAPACITY_CEILING": "3",
        },
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).upper()
    assert "CAPACITY" in combined


def test_deploy_enable_incoherent_capacity_max_step_too_large_refuses():
    """max_step larger than (ceiling - floor) is refused before any AWS call."""
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "observe",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-123",
            "TENANT_ID": "tenant-abc",
            "WORKSPACE_ID": "workspace-abc",
            "GBAW_OPERATIONS_ARTIFACT_BUCKET": "some-bucket",
            "GBAW_OPERATIONS_CAPACITY_FLOOR": "0",
            "GBAW_OPERATIONS_CAPACITY_CEILING": "2",
            "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "5",
        },
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).upper()
    assert "CAPACITY" in combined


def test_deploy_enable_zero_max_step_refuses():
    """A non-positive max_step is refused: the step must be strictly positive."""
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "observe",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-123",
            "TENANT_ID": "tenant-abc",
            "WORKSPACE_ID": "workspace-abc",
            "GBAW_OPERATIONS_ARTIFACT_BUCKET": "some-bucket",
            "GBAW_OPERATIONS_CAPACITY_FLOOR": "0",
            "GBAW_OPERATIONS_CAPACITY_CEILING": "3",
            "GBAW_OPERATIONS_CAPACITY_MAX_STEP": "0",
        },
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).upper()
    assert "CAPACITY" in combined


def test_deploy_enable_non_integer_capacity_refuses():
    """A non-integer capacity override is refused before any AWS call."""
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "observe",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-123",
            "TENANT_ID": "tenant-abc",
            "WORKSPACE_ID": "workspace-abc",
            "GBAW_OPERATIONS_ARTIFACT_BUCKET": "some-bucket",
            "GBAW_OPERATIONS_CAPACITY_CEILING": "3.5",
        },
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).upper()
    assert "CAPACITY" in combined
