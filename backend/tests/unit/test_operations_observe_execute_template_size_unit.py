"""Read-only-preview + large-template deploy safety for the E1/E2 06 observation
and E3 07 execution wrappers (issue #416).

This ports the reviewed size-safe deployment pattern already proven for the 08
control-plane wrapper (see ``test_operations_control_template_size_unit.py``) to
the 06 and 07 wrappers.

The 06 observation template already exceeds CloudFormation's 51,200-byte inline
limit (``validate-template --template-body`` and ``deploy`` without
``--s3-bucket`` both fail against the service for an over-limit body), and the
07 execution template is close enough that a modest addition would push it over.
Both wrappers must therefore:

* keep PREVIEW strictly read-only and size-aware — always lint (error-only gate)
  and locally parse, call ``validate-template --template-body`` only when the
  template is ``<= 51,200`` bytes, and otherwise clearly defer service
  validation to the write-gated deploy. Preview uploads NOTHING and never
  deploys.
* hand an over-limit template to CloudFormation through the ALREADY-verified,
  owner/region-checked artifact bucket on the gated ``--enable`` deploy: upload
  the template under a deterministic content-hash key, service-validate it via
  ``--template-url`` before any stack mutation, deploy with
  ``--s3-bucket``/``--s3-prefix``, and always delete the transient template
  object on exit.
* keep the 06 emergency ``--disable`` REBUILD-FREE and fast: it must target the
  existing stack with ``--use-previous-template`` (changing only parameters),
  never re-uploading or re-sending the template body.

The PREVIEW behavior is exercised end to end by running the REAL wrappers with
tiny fake ``cfn-lint``/``aws``/``python3`` executables placed first on ``PATH``
so they never touch AWS and never build a package. The over/under-limit split is
driven against the real over-limit 06 template and a synthetic over-limit
fixture for 07 via the test-only ``GBAW_OPERATIONS_TEMPLATE`` override the
wrappers honor. The packaging-heavy ``--enable`` deploy is asserted at the
source level (the same convention the sibling behavior/size tests follow), since
its real path performs pip/zip/docker packaging that is not unit-testable end to
end. Synthetic account ids (``000000000000``) keep the tree public-content clean.
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
OBSERVE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/06-operations-observation.yaml"
EXECUTE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/07-operations-execution.yaml"

INLINE_LIMIT = 51200
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


# --------------------------------------------------------------------------- #
# Fake executables (first on PATH) so preview never touches AWS / never builds.
# --------------------------------------------------------------------------- #
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

    The wrapper's local parse is a self-contained ``python3 - <template>`` smoke
    check. For these PATH-isolated tests a benign success models a clean parse
    without depending on PyYAML being importable by the interpreter on PATH.
    """
    return """#!/usr/bin/env bash
exit 0
"""


def _fake_aws(
    validate_body_sentinel: pathlib.Path,
    validate_url_sentinel: pathlib.Path,
    upload_sentinel: pathlib.Path,
    deploy_sentinel: pathlib.Path,
    update_sentinel: pathlib.Path,
    *,
    real_bucket_owner: str = SYNTHETIC_ACCOUNT,
    bucket_region: str = DEPLOY_REGION,
) -> str:
    """A fake ``aws`` covering only the calls the PREVIEW + gating paths make."""
    return f"""#!/usr/bin/env bash
svc="$1"; shift || true
argline="$svc $*"
case "$argline" in
    "sts get-caller-identity"*)
        echo "{SYNTHETIC_ACCOUNT}	AROAEXAMPLEUSERID	arn:aws:sts::{SYNTHETIC_ACCOUNT}:assumed-role/dev/session"
        exit 0 ;;
    "cloudformation validate-template"*"--template-url"*)
        : > {validate_url_sentinel!s}
        exit 0 ;;
    "cloudformation validate-template"*"--template-body"*)
        : > {validate_body_sentinel!s}
        exit 0 ;;
    "cloudformation deploy"*)
        : > {deploy_sentinel!s}
        exit 0 ;;
    "cloudformation update-stack"*)
        : > {update_sentinel!s}
        # Echo the args so a test can assert the body/previous-template choice.
        echo "$@"
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


@pytest.fixture()
def fakes(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return {
        "bindir": bindir,
        "validate_body": tmp_path / "validate_body.touch",
        "validate_url": tmp_path / "validate_url.touch",
        "upload": tmp_path / "upload.touch",
        "deploy": tmp_path / "deploy.touch",
        "update": tmp_path / "update.touch",
    }


def _install_fakes(fakes, **aws_kwargs):
    _make_exe(fakes["bindir"] / "cfn-lint", _fake_cfn_lint())
    _make_exe(fakes["bindir"] / "python3", _fake_python3())
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws(
            fakes["validate_body"],
            fakes["validate_url"],
            fakes["upload"],
            fakes["deploy"],
            fakes["update"],
            **aws_kwargs,
        ),
    )


def _run_with_fakes(script, *args, fakes, env_extra=None):
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


def _over_limit_template(tmp_path) -> pathlib.Path:
    """A syntactically-real CFN template padded comfortably over the limit."""
    path = tmp_path / "over-limit.yaml"
    filler = "\n".join(f"  # pad line {i} keeps this fixture over the inline limit" for i in range(1200))
    path.write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Description: over-limit fixture\n"
        f"{filler}\n"
        "Resources:\n"
        "  Noop:\n"
        "    Type: AWS::CloudFormation::WaitConditionHandle\n",
        encoding="utf-8",
    )
    assert path.stat().st_size > INLINE_LIMIT
    return path


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


# --------------------------------------------------------------------------- #
# Fixture sanity: the shipped 06 template is genuinely over the inline limit.
# --------------------------------------------------------------------------- #
def test_shipped_observe_template_is_over_the_inline_limit():
    assert OBSERVE_TEMPLATE.stat().st_size > INLINE_LIMIT, (
        "the 06 observation template is expected to exceed the 51,200-byte inline "
        "limit; if it shrank below the limit these size-aware tests no longer "
        "exercise the over-limit path"
    )


# --------------------------------------------------------------------------- #
# (1) PREVIEW is read-only and size-aware — 06 over-limit (real shipped template)
# --------------------------------------------------------------------------- #
def test_observe_preview_over_limit_defers_and_never_writes(fakes):
    """Over-limit 06 preview must lint + parse, SKIP validate-template
    --template-body, clearly report deferral, and NEVER upload or deploy."""
    _install_fakes(fakes)
    # Default template is the shipped over-limit 06.
    result = _run_with_fakes(DEPLOY_OBSERVE, fakes=fakes)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"over-limit preview must succeed read-only: {combined}"
    assert not fakes["validate_body"].exists(), "over-limit preview must NOT call validate-template --template-body"
    assert not fakes["upload"].exists(), "preview must never upload to S3"
    assert not fakes["deploy"].exists(), "preview must never call cloudformation deploy"
    assert not fakes["update"].exists(), "preview must never call cloudformation update-stack"
    assert "defer" in combined.lower(), "over-limit preview must clearly report service validation is deferred"


# --------------------------------------------------------------------------- #
# (1) PREVIEW is read-only and size-aware — 07 SIMULATED over-limit (override)
# --------------------------------------------------------------------------- #
def test_execute_preview_simulated_over_limit_defers_and_never_writes(fakes, tmp_path):
    """A simulated over-limit 07 template must defer service validation and never
    write, proving the size-safe pattern protects 07 if it grows over the limit."""
    _install_fakes(fakes)
    over = _over_limit_template(tmp_path)
    result = _run_with_fakes(
        DEPLOY_EXECUTE,
        fakes=fakes,
        env_extra={"GBAW_OPERATIONS_TEMPLATE": str(over)},
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"simulated over-limit preview must succeed read-only: {combined}"
    assert not fakes["validate_body"].exists(), "over-limit preview must NOT call validate-template --template-body"
    assert not fakes["upload"].exists(), "preview must never upload to S3"
    assert not fakes["deploy"].exists(), "preview must never call cloudformation deploy"
    assert "defer" in combined.lower(), "over-limit preview must clearly report service validation is deferred"


# --------------------------------------------------------------------------- #
# (1) PREVIEW under-limit still reaches validate-template --template-body.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_preview_under_limit_calls_template_body_validation(script, fakes, tmp_path):
    """Under-limit preview must reach validate-template --template-body and still
    never upload or deploy."""
    _install_fakes(fakes)
    under = _under_limit_template(tmp_path)
    result = _run_with_fakes(
        script,
        fakes=fakes,
        env_extra={"GBAW_OPERATIONS_TEMPLATE": str(under)},
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"under-limit preview must succeed: {combined}"
    assert fakes["validate_body"].exists(), "under-limit preview must call validate-template --template-body"
    assert not fakes["upload"].exists(), "preview must never upload to S3"
    assert not fakes["deploy"].exists(), "preview must never call cloudformation deploy"


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_preview_always_lints_and_parses(script):
    """Preview must always lint (error-only gate) and locally parse."""
    text = script.read_text(encoding="utf-8")
    assert "--non-zero-exit-code error" in text
    # A local parse independent of the service.
    assert "python3" in text and ("safe_load" in text or "yaml" in text or "json.load" in text)


# --------------------------------------------------------------------------- #
# (2) 06 EMERGENCY --disable is rebuild-free and sends NO template body.
# --------------------------------------------------------------------------- #
def test_observe_disable_uses_previous_template_no_body():
    """The 06 emergency --disable must reuse the deployed template
    (--use-previous-template) and never re-send the template body, so it stays
    fast/rebuild-free and cannot hit the inline limit."""
    text = DEPLOY_OBSERVE.read_text(encoding="utf-8")
    idx = text.index('if [ "$ACTION" = "disable" ]')
    # The disable branch ends at the ENABLE-path deploy section.
    end = text.index("ENABLE path deploy", idx)
    disable_branch = text[idx:end]
    assert "--use-previous-template" in disable_branch, "disable must reuse the deployed template"
    assert "--template-body" not in disable_branch, "disable must NOT re-send the template body"
    assert "--template-file" not in disable_branch, "disable must NOT re-upload the template file"


def test_execute_disable_uses_previous_template_no_body():
    """The separate 07 disable already reuses the deployed template; pin it."""
    disable = PROJECT_ROOT / "scripts/infrastructure/disable-operations-execution.sh"
    text = disable.read_text(encoding="utf-8")
    assert "--use-previous-template" in text
    assert "--template-body" not in text
    assert "--template-file" not in text


# --------------------------------------------------------------------------- #
# (3) Source-level guarantees for the (packaging-heavy) enable S3-backed deploy.
# --------------------------------------------------------------------------- #
def _enable_deploy_branch(script) -> str:
    text = script.read_text(encoding="utf-8")
    idx = text.rindex("aws cloudformation deploy")
    return text[idx:]


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_enable_deploy_uses_s3_bucket_for_large_template(script):
    """The enable deploy must hand the (possibly over-limit) template to
    CloudFormation through the verified artifact bucket via
    --s3-bucket/--s3-prefix."""
    branch = _enable_deploy_branch(script)
    assert "--s3-bucket" in branch, "large-template deploy must upload via --s3-bucket"
    assert "--s3-prefix" in branch, "deploy must scope the template upload with --s3-prefix"
    # The bucket is the SAME explicit, verified artifact bucket. 07 references
    # GBAW_OPERATIONS_ARTIFACT_BUCKET directly; 06 holds it in CODE_S3_BUCKET
    # (set to the verified GBAW_OPERATIONS_ARTIFACT_BUCKET before this deploy).
    assert (
        "ARTIFACT_BUCKET" in branch or "CODE_S3_BUCKET" in branch
    ), "the S3-backed deploy must use the verified artifact bucket"


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_enable_deploy_service_validates_via_template_url_before_mutation(script):
    """The gated path service-validates the template via --template-url before
    the stack mutation (the deploy)."""
    text = script.read_text(encoding="utf-8")
    assert "--template-url" in text, "gated deploy should validate the large template via --template-url"
    assert text.index("--template-url") < text.rindex("aws cloudformation deploy")


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_enable_deploy_uses_deterministic_template_key(script):
    text = script.read_text(encoding="utf-8")
    assert "shasum" in text or "sha256" in text.lower()


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_enable_deploy_has_template_object_cleanup_policy(script):
    text = script.read_text(encoding="utf-8").lower()
    assert (
        "cleanup" in text or "delete-object" in text or "lifecycle" in text
    ), "the wrapper must document/enforce a cleanup policy for the uploaded template object"


# --------------------------------------------------------------------------- #
# Source-level guarantees for size-awareness and bucket safety (06 and 07).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_preview_gates_template_body_on_inline_limit_in_source(script):
    text = script.read_text(encoding="utf-8")
    assert "51200" in text, "wrapper must reference the 51,200-byte inline limit"


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_no_hidden_bucket_creation_or_discovery(script):
    text = script.read_text(encoding="utf-8")
    for forbidden in ("create-bucket", "mb s3://", "list-buckets"):
        assert forbidden not in text, f"wrapper must not '{forbidden}' (no bucket creation/discovery)"


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_bucket_verification_asserts_owner_and_region(script):
    text = script.read_text(encoding="utf-8")
    assert "--expected-bucket-owner" in text, "bucket check must assert owner"
    assert "get-bucket-location" in text, "bucket check must confirm region"


@pytest.mark.parametrize("script", [DEPLOY_OBSERVE, DEPLOY_EXECUTE])
def test_aws_calls_thread_explicit_profile_args(script):
    text = script.read_text(encoding="utf-8")
    assert 'AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")' in text
