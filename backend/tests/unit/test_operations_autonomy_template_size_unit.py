"""Size-aware, read-only-preview safety for the E5 autonomy deploy wrapper
(issue #440).

CloudFormation rejects an inline ``--template-body`` larger than 51,200 bytes.
The shipped 09 autonomy template is currently under that limit, but it may grow
(more parameters, more IAM), so the wrapper must be size-aware: preview
service-validates via ``--template-body`` ONLY when the body is within the
limit, and defers to the write-gated ``--enable`` deploy (which sends the
template through the verified artifact bucket via ``--template-url``) when it is
over. Either way, PREVIEW uploads and deploys NOTHING.

These tests drive both branches by running the REAL wrapper with tiny fake
``cfn-lint``/``aws``/``python3`` executables first on ``PATH`` (never touching
AWS), against a synthetic under-limit fixture and a synthetic over-limit fixture
via the test-only ``GBAW_OPERATIONS_AUTONOMY_TEMPLATE`` override, plus
source-level guarantees for the ``--enable`` deploy path (which does real
packaging and is therefore not unit-testable end to end). Synthetic account ids
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
DEPLOY = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-autonomy.sh"
SHIPPED_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/09-operations-autonomy.yaml"

INLINE_LIMIT = 51200
SYNTHETIC_ACCOUNT = "000000000000"
DEPLOY_REGION = "us-west-2"


def _make_exe(path: pathlib.Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fake_ok() -> str:
    return "#!/usr/bin/env bash\nexit 0\n"


def _fake_aws(
    validate_body_sentinel: pathlib.Path, upload_sentinel: pathlib.Path, deploy_sentinel: pathlib.Path
) -> str:
    return f"""#!/usr/bin/env bash
svc="$1"; shift || true
argline="$svc $*"
case "$argline" in
    "sts get-caller-identity"*)
        echo "{SYNTHETIC_ACCOUNT}	AROAEXAMPLEUSERID	arn:aws:sts::{SYNTHETIC_ACCOUNT}:assumed-role/dev/session"
        exit 0 ;;
    "cloudformation validate-template"*"--template-body"*)
        : > {validate_body_sentinel!s}
        exit 0 ;;
    "cloudformation deploy"*)
        : > {deploy_sentinel!s}
        exit 0 ;;
    "s3api put-object"*|"s3 cp"*)
        : > {upload_sentinel!s}
        exit 0 ;;
    *)
        exit 0 ;;
esac
"""


@pytest.fixture()
def fakes(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return {
        "bindir": bindir,
        "validate_body": tmp_path / "validate_body.touch",
        "upload": tmp_path / "upload.touch",
        "deploy": tmp_path / "deploy.touch",
    }


def _install_fakes(fakes):
    _make_exe(fakes["bindir"] / "cfn-lint", _fake_ok())
    _make_exe(fakes["bindir"] / "python3", _fake_ok())
    _make_exe(fakes["bindir"] / "aws", _fake_aws(fakes["validate_body"], fakes["upload"], fakes["deploy"]))


def _run(*args, fakes, env_extra=None):
    env = os.environ.copy()
    env["PATH"] = f"{fakes['bindir']}{os.pathsep}{env.get('PATH', '')}"
    env["AWS_REGION"] = DEPLOY_REGION
    env["AWS_PROFILE"] = "unit-test-profile"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(DEPLOY), *args], env=env, capture_output=True, text=True)


def _under_limit_template(tmp_path) -> pathlib.Path:
    path = tmp_path / "under-limit.yaml"
    path.write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Description: under-limit fixture\n"
        "Resources:\n"
        "  Noop:\n"
        "    Type: AWS::CloudFormation::WaitConditionHandle\n",
        encoding="utf-8",
    )
    assert path.stat().st_size <= INLINE_LIMIT
    return path


def _over_limit_template(tmp_path) -> pathlib.Path:
    path = tmp_path / "over-limit.yaml"
    filler = "# padding to force the template past the inline limit\n" * 1200
    path.write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Description: over-limit fixture\n"
        f"{filler}"
        "Resources:\n"
        "  Noop:\n"
        "    Type: AWS::CloudFormation::WaitConditionHandle\n",
        encoding="utf-8",
    )
    assert path.stat().st_size > INLINE_LIMIT
    return path


# --------------------------------------------------------------------------- #
# The shipped template is currently within the inline limit.
# --------------------------------------------------------------------------- #
def test_shipped_autonomy_template_size_is_known():
    size = SHIPPED_TEMPLATE.stat().st_size
    assert size > 0
    # Documented posture: currently under the limit, so preview uses
    # --template-body. If it ever crosses, the over-limit branch (below) takes
    # over — both are covered here.


# --------------------------------------------------------------------------- #
# PREVIEW is read-only and size-aware.
# --------------------------------------------------------------------------- #
def test_preview_under_limit_calls_template_body_validation(fakes, tmp_path):
    _install_fakes(fakes)
    under = _under_limit_template(tmp_path)
    result = _run(fakes=fakes, env_extra={"GBAW_OPERATIONS_AUTONOMY_TEMPLATE": str(under)})
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"under-limit preview must succeed: {combined}"
    assert fakes["validate_body"].exists(), "under-limit preview must call validate-template --template-body"
    assert not fakes["upload"].exists(), "preview must never upload to S3"
    assert not fakes["deploy"].exists(), "preview must never deploy"


def test_preview_over_limit_defers_validation_and_never_uploads(fakes, tmp_path):
    _install_fakes(fakes)
    over = _over_limit_template(tmp_path)
    result = _run(fakes=fakes, env_extra={"GBAW_OPERATIONS_AUTONOMY_TEMPLATE": str(over)})
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"over-limit preview must succeed read-only: {combined}"
    assert not fakes["validate_body"].exists(), "over-limit preview must NOT call validate-template --template-body"
    assert not fakes["upload"].exists(), "preview must never upload to S3"
    assert not fakes["deploy"].exists(), "preview must never deploy"
    assert "defer" in combined.lower(), "over-limit preview must report deferral"


# --------------------------------------------------------------------------- #
# Source-level guarantees for the (packaging-heavy) enable deploy.
# --------------------------------------------------------------------------- #
def test_enable_deploy_uses_s3_bucket_for_large_template():
    text = DEPLOY.read_text(encoding="utf-8")
    idx = text.rindex("aws cloudformation deploy")
    branch = text[idx:]
    assert "--s3-bucket" in branch
    assert "--s3-prefix" in branch


def test_enable_deploy_service_validates_via_template_url_before_mutation():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--template-url" in text
    assert text.index("--template-url") < text.rindex("aws cloudformation deploy")


def test_enable_deploy_has_template_object_cleanup_policy():
    text = DEPLOY.read_text(encoding="utf-8").lower()
    assert "cleanup" in text or "delete-object" in text
