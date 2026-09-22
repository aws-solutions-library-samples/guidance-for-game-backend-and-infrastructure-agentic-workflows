"""Safety tests for the operations deploy wrappers' preview lint and artifact
bucket verification (issue #415, pre-live-deploy hardening).

Two wrapper safety defects are pinned here with red-green coverage:

1. Read-only PREVIEW must run ``cfn-lint`` so that warning-only findings (for
   example W1030) are *shown* but do NOT abort the wrapper under ``set -e``;
   only error-class (E-level) findings stop the preview. Before the fix, plain
   ``cfn-lint`` exits non-zero (4) on warning-only findings, so under
   ``set -euo pipefail`` the read-only preview never reached the downstream AWS
   ``validate-template`` call. The fix invokes cfn-lint with
   ``--non-zero-exit-code error``.

2. The artifact-bucket verification on the ENABLE path must, like the hardened
   E1/E2 (06) wrapper, assert the bucket owner with ``--expected-bucket-owner``
   from the verified STS account AND confirm the bucket's region matches
   ``AWS_REGION`` via ``get-bucket-location`` — aborting BEFORE any upload when
   the bucket is foreign-owned or in the wrong region. Before the fix the E3
   (07) wrapper used ``head-bucket`` alone.

The behavior tests run the real wrappers with tiny fake ``cfn-lint``/``aws``
executables placed first on ``PATH``. They therefore never touch AWS and never
build a package: the foreign-owner / foreign-region cases fail closed at the
bucket-verification step, before staging or upload. Synthetic account ids
(``000000000000``) keep the tree public-content clean.
"""

# Standard library
import os
import pathlib
import stat
import subprocess

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
DEPLOY_OBSERVE = PROJECT_ROOT / "scripts/infrastructure/deploy-operations.sh"
DEPLOY_EXECUTE = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-execution.sh"

SYNTHETIC_ACCOUNT = "000000000000"
DEPLOY_REGION = "us-west-2"

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
    "GBAW_OPERATIONS_TEMPLATE",
)


def _make_exe(path: pathlib.Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fake_cfn_lint(sentinel: pathlib.Path, warn_exit: int, error_exit: int) -> str:
    """A fake cfn-lint that models real exit-code semantics.

    Records the args it was invoked with. When invoked with
    ``--non-zero-exit-code error`` it prints its warning to stderr and exits
    ``error_exit`` (so warning-only findings return 0). Otherwise it exits
    ``warn_exit`` (the buggy behavior: non-zero on warnings).
    """
    return f"""#!/usr/bin/env bash
echo "$@" > {sentinel!s}
echo "W1030 fake warning finding" >&2
case "$*" in
    *"--non-zero-exit-code error"*) exit {error_exit} ;;
    *) exit {warn_exit} ;;
esac
"""


def _fake_aws(
    validate_sentinel: pathlib.Path,
    upload_sentinel: pathlib.Path,
    *,
    real_bucket_owner: str = SYNTHETIC_ACCOUNT,
    bucket_region: str = DEPLOY_REGION,
) -> str:
    """A fake ``aws`` covering only the calls these paths make.

    * ``sts get-caller-identity`` -> a synthetic ``Account UserId Arn`` line.
    * ``cloudformation validate-template`` -> touch ``validate_sentinel``.
    * ``s3api head-bucket`` -> models a bucket actually owned by
      ``real_bucket_owner``. It succeeds when no ``--expected-bucket-owner`` is
      given (the pre-fix blind spot) OR the asserted owner matches the real
      owner; it fails (254) only when an asserted owner disagrees. So a
      foreign-owned bucket passes the unhardened check and is caught ONLY once
      the wrapper asserts ``--expected-bucket-owner``.
    * ``s3api get-bucket-location`` -> print ``bucket_region``.
    * ``s3api put-object`` / ``s3 cp`` -> touch ``upload_sentinel`` (an upload
      must NEVER happen in the abort cases).
    """
    return f"""#!/usr/bin/env bash
svc="$1"; shift || true
argline="$svc $*"
case "$argline" in
    "sts get-caller-identity"*)
        echo "{SYNTHETIC_ACCOUNT}	AROAEXAMPLEUSERID	arn:aws:sts::{SYNTHETIC_ACCOUNT}:assumed-role/dev/session"
        exit 0 ;;
    "cloudformation validate-template"*)
        : > {validate_sentinel!s}
        exit 0 ;;
    "cloudformation deploy"*)
        exit 0 ;;
    "s3api head-bucket"*)
        expected=""
        prev=""
        for a in "$@"; do
            if [ "$prev" = "--expected-bucket-owner" ]; then expected="$a"; fi
            prev="$a"
        done
        if [ -n "$expected" ] && [ "$expected" != "{real_bucket_owner}" ]; then
            exit 254
        fi
        exit 0 ;;
    "s3api get-bucket-location"*)
        echo "{bucket_region}"
        exit 0 ;;
    "s3api put-object"*|"s3 cp"*)
        : > {upload_sentinel!s}
        exit 0 ;;
    *)
        exit 0 ;;
esac
"""


def _run_with_fakes(script, *args, fakes, env_extra=None):
    """Run ``script`` with a tmp bin dir (containing ``fakes``) first on PATH."""
    bindir = fakes["bindir"]
    env = os.environ.copy()
    for key in _CLEARED_ENV:
        env.pop(key, None)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["AWS_REGION"] = DEPLOY_REGION
    env["AWS_PROFILE"] = "unit-test-profile"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _enable_env(bucket="artifact-bucket-unit"):
    return {
        "GBAW_OPERATIONS_MODE": "remediate",
        "COGNITO_ISSUER": "https://issuer.example",
        "COGNITO_CLIENT_ID": "client-abc",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
        "GBAW_OPERATIONS_ARTIFACT_BUCKET": bucket,
        "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
        "GBAW_OPERATIONS_KMS_KEY_ARN": f"arn:aws:kms:{DEPLOY_REGION}:{SYNTHETIC_ACCOUNT}:key/abc",
        "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
    }


@pytest.fixture()
def fakes(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return {
        "bindir": bindir,
        "cfn_args": tmp_path / "cfn_args.txt",
        "validate": tmp_path / "validate.touch",
        "upload": tmp_path / "upload.touch",
    }


# --------------------------------------------------------------------------- #
# (1) Preview: warning-only lint CONTINUES to validate-template; errors STOP.
# --------------------------------------------------------------------------- #
def _under_limit_template(tmp_path) -> pathlib.Path:
    """A syntactically-real, minimal CFN template comfortably under the inline
    limit, so the size-aware preview reaches validate-template --template-body."""
    path = tmp_path / "under-limit.yaml"
    path.write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Description: under-limit fixture\n"
        "Resources:\n"
        "  Noop:\n"
        "    Type: AWS::CloudFormation::WaitConditionHandle\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_preview_warning_only_lint_continues_to_validate(script, fakes, tmp_path):
    """Warning-only cfn-lint (exit 4 by default, 0 with the error gate) must not
    abort preview; the wrapper must still reach AWS validate-template.

    The wrappers now run a size-aware preview that only calls
    ``validate-template --template-body`` when the template is within the inline
    limit, so this lint-gate test is driven against an UNDER-limit fixture (via
    the test-only ``GBAW_OPERATIONS_TEMPLATE`` override) to keep it focused on
    the cfn-lint exit-code semantics rather than template size. A benign
    ``python3`` fake satisfies the new local-parse probe under PATH isolation."""
    _make_exe(
        fakes["bindir"] / "cfn-lint",
        _fake_cfn_lint(fakes["cfn_args"], warn_exit=4, error_exit=0),
    )
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws(fakes["validate"], fakes["upload"]),
    )
    _make_exe(fakes["bindir"] / "python3", "#!/usr/bin/env bash\nexit 0\n")
    under = _under_limit_template(tmp_path)
    result = _run_with_fakes(
        script,
        fakes=fakes,
        env_extra={"GBAW_OPERATIONS_TEMPLATE": str(under)},
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"preview aborted on warning-only lint: {combined}"
    assert fakes["validate"].exists(), "preview never reached validate-template"
    # The fix must pass the error-only exit gate to cfn-lint.
    assert "--non-zero-exit-code error" in fakes["cfn_args"].read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_preview_error_level_lint_stops_before_validate(script, fakes):
    """Error-class cfn-lint findings must stop preview before validate-template."""
    _make_exe(
        fakes["bindir"] / "cfn-lint",
        # Even with the error gate, this fake returns 2 (an E-level finding).
        _fake_cfn_lint(fakes["cfn_args"], warn_exit=2, error_exit=2),
    )
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws(fakes["validate"], fakes["upload"]),
    )
    result = _run_with_fakes(script, fakes=fakes)
    assert result.returncode != 0, "preview must fail on error-level lint findings"
    assert not fakes["validate"].exists(), "validate-template ran despite lint error"


# --------------------------------------------------------------------------- #
# (2) Enable: foreign owner / foreign region abort BEFORE any upload (07 = E3).
# --------------------------------------------------------------------------- #
def test_enable_foreign_bucket_owner_aborts_before_upload(fakes):
    # The bucket is really owned by a DIFFERENT account. Only an
    # --expected-bucket-owner assertion (the fix) catches it before upload.
    foreign_account = "123456789012"
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws(
            fakes["validate"],
            fakes["upload"],
            real_bucket_owner=foreign_account,
        ),
    )
    result = _run_with_fakes(DEPLOY_EXECUTE, "--enable", fakes=fakes, env_extra=_enable_env())
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode != 0, "foreign-owned bucket must abort enable"
    assert not fakes["upload"].exists(), "artifact was uploaded to a foreign-owned bucket"
    assert "bucket" in combined


def test_enable_foreign_bucket_region_aborts_before_upload(fakes):
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws(
            fakes["validate"],
            fakes["upload"],
            bucket_region="eu-west-1",
        ),
    )
    result = _run_with_fakes(DEPLOY_EXECUTE, "--enable", fakes=fakes, env_extra=_enable_env())
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode != 0, "wrong-region bucket must abort enable"
    assert not fakes["upload"].exists(), "artifact was uploaded to a wrong-region bucket"
    assert "region" in combined


# --------------------------------------------------------------------------- #
# Source-level guarantees (apply to BOTH 06 and 07 wrappers)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_preview_lint_uses_error_only_exit_gate(script):
    text = script.read_text(encoding="utf-8")
    assert "--non-zero-exit-code error" in text, (
        "preview cfn-lint must use --non-zero-exit-code error so warnings are " "shown but only E-level findings fail"
    )


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_bucket_verification_asserts_owner_and_region(script):
    text = script.read_text(encoding="utf-8")
    assert "--expected-bucket-owner" in text, "bucket check must assert owner"
    assert "get-bucket-location" in text, "bucket check must confirm region"


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_aws_calls_thread_explicit_profile_args(script):
    """Every ``aws`` call threads the explicit profile-args array; the region is
    passed wherever the API is regional."""
    text = script.read_text(encoding="utf-8")
    assert 'AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")' in text
    assert "aws s3api head-bucket" in text and "AWS_PROFILE_ARGS" in text
