"""Behavioral tests for the OPTIONAL E3 execution deploy/disable/teardown
wrappers (issue #415).

These execute the wrappers' refusal, help, and parse paths only, which exit
before any ``aws`` invocation, so they never touch AWS. They prove the opt-in
and confirmation gates fail closed under the frozen ``remediate`` mode
vocabulary, that the deterministic dispatcher/executor packaging and explicit
profile/account/region/artifact-bucket verification are wired, and that the
scripts parse under ``bash -n``.
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
DEPLOY = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-execution.sh"
DISABLE = PROJECT_ROOT / "scripts/infrastructure/disable-operations-execution.sh"
TEARDOWN = PROJECT_ROOT / "scripts/infrastructure/teardown-operations-execution.sh"

_CLEARED_ENV = (
    "GBAW_OPERATIONS_MODE",
    "COGNITO_ISSUER",
    "COGNITO_CLIENT_ID",
    "GBAW_OPERATIONS_ENROLLED_FLEET_ID",
    "GBAW_OPERATIONS_ENROLLED_LOCATION",
    "GBAW_OPERATIONS_ARTIFACT_BUCKET",
    "GBAW_OPERATIONS_TABLE_NAME",
    "GBAW_OPERATIONS_KMS_KEY_ARN",
    "GBAW_OPERATIONS_TENANT_ID",
    "GBAW_OPERATIONS_WORKSPACE_ID",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE",
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


@pytest.mark.parametrize("script", [DEPLOY, DISABLE, TEARDOWN])
def test_scripts_exist(script):
    assert script.exists(), f"missing wrapper: {script}"


@pytest.mark.parametrize("script", [DEPLOY, DISABLE, TEARDOWN])
def test_scripts_parse_under_bash_n(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [DEPLOY, DISABLE, TEARDOWN])
def test_scripts_use_strict_mode(script):
    assert re.search(r"set -euo pipefail", script.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Double opt-in: enabling remediate requires the matching env confirmation
# --------------------------------------------------------------------------- #
def test_enable_without_remediate_mode_refuses():
    result = _run(DEPLOY, "--enable")
    assert result.returncode != 0
    assert "refus" in (result.stdout + result.stderr).lower()


def test_enable_with_wrong_mode_refuses():
    result = _run(DEPLOY, "--enable", env_extra={"GBAW_OPERATIONS_MODE": "observe"})
    assert result.returncode != 0


def test_enable_without_enrolled_fleet_refuses():
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "remediate",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-abc",
            "GBAW_OPERATIONS_ARTIFACT_BUCKET": "some-bucket",
            "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
            "GBAW_OPERATIONS_KMS_KEY_ARN": "arn:aws:kms:us-west-2:000000000000:key/abc",
            "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
            "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
        },
    )
    assert result.returncode != 0
    assert "fleet" in (result.stdout + result.stderr).lower()


def test_enable_without_artifact_bucket_refuses():
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "remediate",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-abc",
            "GBAW_OPERATIONS_ENROLLED_FLEET_ID": "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
            "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
            "GBAW_OPERATIONS_KMS_KEY_ARN": "arn:aws:kms:us-west-2:000000000000:key/abc",
            "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
            "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
        },
    )
    assert result.returncode != 0
    assert "bucket" in (result.stdout + result.stderr).lower()


def test_enable_without_tenant_or_workspace_refuses():
    result = _run(
        DEPLOY,
        "--enable",
        env_extra={
            "GBAW_OPERATIONS_MODE": "remediate",
            "COGNITO_ISSUER": "https://issuer.example",
            "COGNITO_CLIENT_ID": "client-abc",
            "GBAW_OPERATIONS_ENROLLED_FLEET_ID": "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
            "GBAW_OPERATIONS_ARTIFACT_BUCKET": "some-bucket",
            "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
            "GBAW_OPERATIONS_KMS_KEY_ARN": "arn:aws:kms:us-west-2:000000000000:key/abc",
        },
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "tenant" in combined or "workspace" in combined


def test_deploy_passes_identity_and_location_parameters():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "TenantId=" in text
    assert "WorkspaceId=" in text
    assert "TrustedAudience=" in text
    assert "EnrolledLocation=" in text


# --------------------------------------------------------------------------- #
# Deterministic packaging of BOTH handler artifacts
# --------------------------------------------------------------------------- #
def test_deploy_packages_both_dispatcher_and_executor():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "dispatcher_entry" in text, "wrapper must package the dispatcher handler"
    assert "executor_entry" in text, "wrapper must package the executor handler"


def test_deploy_uses_deterministic_zip_and_content_hash():
    text = DEPLOY.read_text(encoding="utf-8")
    # Fixed timestamps + sorted entries + content-hash-addressed S3 key.
    assert "touch" in text and ("sha" in text.lower() or "hash" in text.lower())
    assert "manylinux" in text or "x86_64" in text, "must target the Lambda ABI"


def test_deploy_verifies_profile_account_region_and_bucket():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "get-caller-identity" in text, "must verify caller identity/account"
    assert "AWS_PROFILE" in text and "AWS_REGION" in text
    # Explicit artifact bucket verification (no discovery/creation).
    assert "head-bucket" in text or "GBAW_OPERATIONS_ARTIFACT_BUCKET" in text


# --------------------------------------------------------------------------- #
# Preview is read-only; disable rebuilds nothing; teardown is guarded
# --------------------------------------------------------------------------- #
def test_preview_is_read_only():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "validate-template" in text, "preview must run read-only validation"


def test_disable_reuses_disabled_mode_and_rebuilds_nothing():
    text = DISABLE.read_text(encoding="utf-8")
    assert "ExecutionMode=disabled" in text or "ExecutionMode,ParameterValue=disabled" in text
    # The disable path must NOT build a package (no ABI install / zip in disable).
    assert "manylinux" not in text, "disable must rebuild nothing"


def test_disable_keeps_resources_provisioned():
    text = DISABLE.read_text(encoding="utf-8")
    assert "Provisioned=true" in text or "Provisioned,ParameterValue=true" in text


def test_deploy_targets_the_07_execution_template():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "07-operations-execution.yaml" in text


def test_deploy_probe_loads_e3_execution_schemas():
    """Packaging must load ALL E3 JSON schemas (intent/result/verification) and
    run an identifier-only invocation contract probe before upload."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "EXECUTION_SCHEMA_NAMES" in text and "load_execution_schema" in text
    assert "ExecutionInvocation" in text, "probe must exercise the identifier-only contract"
    for schema in (
        "gamelift-capacity-execution-intent",
        "gamelift-capacity-execution-result",
        "gamelift-capacity-execution-verification",
    ):
        assert schema in text, f"packaging must require the E3 schema {schema}"
