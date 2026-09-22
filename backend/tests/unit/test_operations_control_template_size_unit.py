"""Read-only-preview + large-template deploy safety for the E4 08 control-plane
wrapper (issue #416).

The 08 control-plane template embeds the AppConfig kill-switch JSON-schema
validator byte-for-byte, which pushes ``template-body`` past CloudFormation's
51,200-byte inline limit. The wrapper previously ran
``aws cloudformation validate-template --template-body file://<08>``
unconditionally on the READ-ONLY preview path, so preview *failed* against the
service for the over-limit template — and the ``--enable`` deploy used
``aws cloudformation deploy --template-file`` with no ``--s3-bucket``, so
CloudFormation received the body inline and hit the same 51,200-byte ceiling.

These tests pin the guarantees with red-green coverage. The PREVIEW behavior is
exercised end to end by running the REAL wrapper with tiny fake
``cfn-lint``/``aws``/``python3`` executables placed first on ``PATH`` so they
never touch AWS and never build a package. The over/under-limit split is driven
against a real over-limit fixture (the shipped 08 template, > 51,200 bytes) and
a synthetic under-limit fixture via the test-only ``GBAW_OPERATIONS_CONTROL_TEMPLATE``
override the wrapper honors.

The ``--enable`` deploy path performs real Lambda packaging (pip/zip/docker) and
so is not unit-testable end to end (the same convention the sibling behavior
tests follow: they cover refusal/verification paths that abort *before*
packaging, and assert the deploy-invocation details at the source level). The
foreign-owner abort is exercised behaviorally because it aborts at bucket
verification — after the cheap local module gate but before any packaging — with
a stubbed backend module staged via the test-only
``GBAW_OPERATIONS_CONTROL_BACKEND_SRC`` override. Synthetic account ids
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
DEPLOY_CONTROL = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-control.sh"
SHIPPED_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/08-operations-control-plane.yaml"

INLINE_LIMIT = 51200
SYNTHETIC_ACCOUNT = "000000000000"
DEPLOY_REGION = "us-west-2"

_CLEARED_ENV = (
    "GBAW_OPERATIONS_CONTROL_MODE",
    "COGNITO_ISSUER",
    "COGNITO_CLIENT_ID",
    "GBAW_OPERATIONS_ARTIFACT_BUCKET",
    "GBAW_OPERATIONS_TABLE_NAME",
    "GBAW_OPERATIONS_KMS_KEY_ARN",
    "GBAW_OPERATIONS_TENANT_ID",
    "GBAW_OPERATIONS_WORKSPACE_ID",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE",
    "GBAW_OPERATIONS_MODE",
    "GBAW_OPERATIONS_CONTROL_TEMPLATE",
    "GBAW_OPERATIONS_CONTROL_BACKEND_SRC",
)


def _make_exe(path: pathlib.Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fake_cfn_lint() -> str:
    """A benign cfn-lint that always succeeds (models a clean lint)."""
    return """#!/usr/bin/env bash
exit 0
"""


def _fake_python3() -> str:
    """A python3 that only needs to succeed for the local-parse probe.

    The wrapper's local parse is a self-contained ``python3 -c`` smoke check.
    Delegating to the real interpreter would require PyYAML on PATH; for these
    PATH-isolated tests a benign success models a clean parse.
    """
    return """#!/usr/bin/env bash
exit 0
"""


def _fake_aws(
    validate_body_sentinel: pathlib.Path,
    upload_sentinel: pathlib.Path,
    deploy_sentinel: pathlib.Path,
    *,
    real_bucket_owner: str = SYNTHETIC_ACCOUNT,
    bucket_region: str = DEPLOY_REGION,
) -> str:
    """A fake ``aws`` covering only the calls the PREVIEW + bucket-verify paths make."""
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
    "ssm get-parameter"*)
        echo "arn:aws:lambda:{DEPLOY_REGION}:{SYNTHETIC_ACCOUNT}:layer:AWS-AppConfig-Extension:1"
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


def _install_fakes(fakes, **aws_kwargs):
    _make_exe(fakes["bindir"] / "cfn-lint", _fake_cfn_lint())
    _make_exe(fakes["bindir"] / "python3", _fake_python3())
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws(
            fakes["validate_body"],
            fakes["upload"],
            fakes["deploy"],
            **aws_kwargs,
        ),
    )


def _run_with_fakes(*args, fakes, env_extra=None):
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
        ["bash", str(DEPLOY_CONTROL), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _under_limit_template(tmp_path) -> pathlib.Path:
    """A syntactically-real, minimal CFN template comfortably under the limit."""
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


def _stub_backend_src(tmp_path) -> pathlib.Path:
    """Stage the single frozen control module so the enable path clears the cheap
    local module gate and reaches bucket verification (before any packaging)."""
    src = tmp_path / "backend-src"
    module = src / "operations" / "control" / "control_entry.py"
    module.parent.mkdir(parents=True)
    module.write_text("def handler(event, context):\n    return {}\n", encoding="utf-8")
    return src


def _enable_env(bucket="artifact-bucket-unit", backend_src=None):
    env = {
        "GBAW_OPERATIONS_CONTROL_MODE": "enabled",
        "COGNITO_ISSUER": "https://issuer.example",
        "COGNITO_CLIENT_ID": "client-abc",
        "GBAW_OPERATIONS_ARTIFACT_BUCKET": bucket,
        "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
        "GBAW_OPERATIONS_KMS_KEY_ARN": f"arn:aws:kms:{DEPLOY_REGION}:{SYNTHETIC_ACCOUNT}:key/abc",
        "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
    }
    if backend_src is not None:
        env["GBAW_OPERATIONS_CONTROL_BACKEND_SRC"] = str(backend_src)
    return env


# --------------------------------------------------------------------------- #
# Fixture sanity: the shipped 08 template is genuinely over the inline limit.
# --------------------------------------------------------------------------- #
def test_shipped_control_template_is_over_the_inline_limit():
    assert SHIPPED_TEMPLATE.stat().st_size > INLINE_LIMIT, (
        "the 08 control-plane template is expected to exceed the 51,200-byte "
        "inline limit; if it shrank below the limit these size-aware tests no "
        "longer exercise the over-limit path"
    )


# --------------------------------------------------------------------------- #
# (1) PREVIEW is read-only and size-aware.
# --------------------------------------------------------------------------- #
def test_preview_over_limit_defers_service_validation_and_never_uploads(fakes):
    """Over-limit preview must lint + parse, SKIP validate-template --template-body,
    clearly report deferral, and NEVER upload or deploy."""
    _install_fakes(fakes)
    # Default template is the shipped over-limit 08.
    result = _run_with_fakes(fakes=fakes)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"over-limit preview must succeed read-only: {combined}"
    assert not fakes["validate_body"].exists(), "over-limit preview must NOT call validate-template --template-body"
    assert not fakes["upload"].exists(), "preview must never upload to S3"
    assert not fakes["deploy"].exists(), "preview must never call cloudformation deploy"
    assert "defer" in combined.lower(), "over-limit preview must clearly report service validation is deferred"


def test_preview_under_limit_calls_template_body_validation(fakes, tmp_path):
    """Under-limit preview must reach validate-template --template-body and still
    never upload or deploy."""
    _install_fakes(fakes)
    under = _under_limit_template(tmp_path)
    result = _run_with_fakes(
        fakes=fakes,
        env_extra={"GBAW_OPERATIONS_CONTROL_TEMPLATE": str(under)},
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"under-limit preview must succeed: {combined}"
    assert fakes["validate_body"].exists(), "under-limit preview must call validate-template --template-body"
    assert not fakes["upload"].exists(), "preview must never upload to S3"
    assert not fakes["deploy"].exists(), "preview must never call cloudformation deploy"


def test_preview_always_lints_and_parses():
    """Preview must always lint (error-only gate) and locally parse."""
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    assert "--non-zero-exit-code error" in text
    # A local parse independent of the service.
    assert "python3" in text and ("safe_load" in text or "yaml" in text or "json.load" in text)


# --------------------------------------------------------------------------- #
# (2) ENABLE deploy: foreign owner aborts before any packaging/upload/deploy.
# --------------------------------------------------------------------------- #
def test_enable_foreign_owner_aborts_before_any_packaging(fakes, tmp_path):
    """A foreign-owned bucket must abort at verification, before packaging,
    upload, or deploy. Uses a stubbed backend module so the run reaches bucket
    verification without depending on the (separate-branch) backend."""
    _install_fakes(fakes, real_bucket_owner="123456789012")
    backend_src = _stub_backend_src(tmp_path)
    result = _run_with_fakes(
        "--enable",
        fakes=fakes,
        env_extra=_enable_env(backend_src=backend_src),
    )
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode != 0, "foreign-owned bucket must abort enable"
    assert not fakes["upload"].exists(), "no upload against a foreign-owned bucket"
    assert not fakes["deploy"].exists(), "no deploy against a foreign-owned bucket"
    assert "bucket" in combined


# --------------------------------------------------------------------------- #
# Source-level guarantees for the (packaging-heavy) enable deploy.
# --------------------------------------------------------------------------- #
def _enable_deploy_branch() -> str:
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    idx = text.rindex("aws cloudformation deploy")
    return text[idx:]


def test_enable_deploy_uses_s3_bucket_for_large_template():
    """The enable deploy must hand the (over-limit) template to CloudFormation
    through the verified artifact bucket via --s3-bucket/--s3-prefix."""
    branch = _enable_deploy_branch()
    assert "--s3-bucket" in branch, "large-template deploy must upload via --s3-bucket"
    assert "--s3-prefix" in branch, "deploy must scope the template upload with --s3-prefix"
    # The bucket is the SAME explicit, verified artifact bucket.
    assert "GBAW_OPERATIONS_ARTIFACT_BUCKET" in branch or "ARTIFACT_BUCKET" in branch


def test_enable_deploy_service_validates_via_template_url_before_mutation():
    """Where feasible, the gated path service-validates the large template via
    --template-url before the stack mutation (the deploy)."""
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    assert (
        "--template-url" in text
    ), "gated deploy should validate the large template via --template-url before mutating"
    # The validate-template --template-url call must precede the deploy call.
    assert text.index("--template-url") < text.rindex("aws cloudformation deploy")


def test_enable_deploy_uses_deterministic_template_key():
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    # A deterministic content-hash key for the uploaded template object.
    assert "shasum" in text or "sha256" in text.lower()


def test_enable_deploy_has_template_object_cleanup_policy():
    text = DEPLOY_CONTROL.read_text(encoding="utf-8").lower()
    assert (
        "cleanup" in text or "delete-object" in text or "lifecycle" in text
    ), "the wrapper must document/enforce a cleanup policy for the uploaded template object"


# --------------------------------------------------------------------------- #
# Source-level guarantees for size-awareness and bucket safety.
# --------------------------------------------------------------------------- #
def test_preview_gates_template_body_on_inline_limit_in_source():
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    assert "51200" in text, "wrapper must reference the 51,200-byte inline limit"


def test_no_hidden_bucket_creation_or_discovery():
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    for forbidden in ("create-bucket", "mb s3://", "list-buckets"):
        assert forbidden not in text, f"wrapper must not '{forbidden}' (no bucket creation/discovery)"


def test_bucket_verification_asserts_owner_and_region():
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    assert "--expected-bucket-owner" in text, "bucket check must assert owner"
    assert "get-bucket-location" in text, "bucket check must confirm region"


def test_aws_calls_thread_explicit_profile_args():
    text = DEPLOY_CONTROL.read_text(encoding="utf-8")
    assert 'AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")' in text
