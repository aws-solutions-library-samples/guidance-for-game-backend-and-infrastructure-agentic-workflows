"""Behavioral tests for the optional E1 deploy/teardown wrappers (issue #413).

These execute the wrappers' *refusal* and *help* paths only, which exit before
any ``aws`` invocation, so they never touch AWS. They prove the opt-in and
confirmation gates fail closed and that the scripts parse under ``bash -n``.
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


def _run(script, *args, env_extra=None):
    env = os.environ.copy()
    env.pop("GBAW_OPERATIONS_MODE", None)
    env.pop("COGNITO_ISSUER", None)
    env.pop("COGNITO_CLIENT_ID", None)
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


def test_deploy_enable_without_env_refuses():
    result = _run(DEPLOY, "--enable")
    assert result.returncode == 3
    assert "Refusing to enable" in result.stdout + result.stderr


def test_deploy_enable_without_cognito_refuses():
    result = _run(DEPLOY, "--enable", env_extra={"GBAW_OPERATIONS_MODE": "enabled"})
    assert result.returncode == 3
    assert "COGNITO" in (result.stdout + result.stderr).upper()


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
