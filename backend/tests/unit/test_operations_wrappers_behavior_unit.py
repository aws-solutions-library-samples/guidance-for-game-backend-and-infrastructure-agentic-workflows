"""Behavioral tests for the optional E1 deploy/teardown wrappers (issue #413).

These execute the wrappers' *refusal* and *help* paths only, which exit before
any ``aws`` invocation, so they never touch AWS. They prove the opt-in and
confirmation gates fail closed under the frozen ``observe`` mode vocabulary and
that the scripts parse under ``bash -n``.
"""

# Standard library
import os
import pathlib
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
