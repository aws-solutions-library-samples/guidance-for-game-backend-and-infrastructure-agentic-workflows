"""Read-only-preview, double-opt-in, and safety contract for the E5 autonomy
deploy / disable / teardown wrappers (issue #440, Track B).

The autonomy wrappers are the only path that provisions or enables the E5
bounded-autonomy control plane, and they must be safe by construction:

* the DEFAULT (no flag) is a strictly READ-ONLY preview that lints + parses the
  09 template, is size-aware against CloudFormation's 51,200-byte inline limit,
  and creates/uploads NOTHING;
* enabling requires BOTH an explicit ``--enable`` AND a matching confirmation
  token (``GBAW_OPERATIONS_AUTONOMY_CONFIRM``) — a single flag is not enough;
* enabling requires ``GBAW_OPERATIONS_MODE=operate`` (the static ceiling) and the
  full set of server-side bindings, failing closed before any AWS call;
* the artifact bucket is EXPLICIT and verified (owner + region), never created or
  discovered;
* the disable wrapper flips only ``AutonomyMode=disabled`` (keeps
  Provisioned=true), requires ``--confirm``, and deletes nothing;
* the teardown wrapper requires the exact token
  ``--confirm delete-operations-autonomy`` and is never called by
  teardown-all.sh.

These tests run the REAL wrappers with tiny fake ``cfn-lint``/``aws``/``python3``
executables first on ``PATH`` so they never touch AWS and never build a package.
Synthetic account ids (``000000000000``) keep the tree public-content clean.
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
DISABLE = PROJECT_ROOT / "scripts/infrastructure/disable-operations-autonomy.sh"
TEARDOWN = PROJECT_ROOT / "scripts/infrastructure/teardown-operations-autonomy.sh"
SHIPPED_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/09-operations-autonomy.yaml"
TEARDOWN_ALL = PROJECT_ROOT / "scripts/teardown.sh"

INLINE_LIMIT = 51200
SYNTHETIC_ACCOUNT = "000000000000"
DEPLOY_REGION = "us-west-2"

_CLEARED_ENV = (
    "GBAW_OPERATIONS_MODE",
    "GBAW_OPERATIONS_AUTONOMY_CONFIRM",
    "GBAW_OPERATIONS_ARTIFACT_BUCKET",
    "GBAW_OPERATIONS_TABLE_NAME",
    "GBAW_OPERATIONS_KMS_KEY_ARN",
    "GBAW_OPERATIONS_TENANT_ID",
    "GBAW_OPERATIONS_WORKSPACE_ID",
    "GBAW_OPERATIONS_ENROLLED_FLEET_ID",
    "GBAW_OPERATIONS_ENROLLED_FLEET_ARN",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE",
    "GBAW_OPERATIONS_EXECUTION_STATE_MACHINE_ARN",
    "GBAW_OPERATIONS_AUTONOMY_SUBJECT",
    "GBAW_OPERATIONS_AUTONOMY_CLIENT",
    "GBAW_OPERATIONS_AUTONOMY_POLICY_ID",
    "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION",
    "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH",
    "GBAW_OPERATIONS_AUTONOMY_STATE_ID",
    "GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID",
    "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT_ID",
    "GBAW_OPERATIONS_APPCONFIG_PROFILE_ID",
    "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE_ID",
    "GBAW_OPERATIONS_AUTONOMY_TEMPLATE",
    "GBAW_OPERATIONS_AUTONOMY_BACKEND_SRC",
)


def _make_exe(path: pathlib.Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fake_ok() -> str:
    return "#!/usr/bin/env bash\nexit 0\n"


def _fake_aws(
    validate_body_sentinel: pathlib.Path,
    upload_sentinel: pathlib.Path,
    deploy_sentinel: pathlib.Path,
    *,
    real_bucket_owner: str = SYNTHETIC_ACCOUNT,
    bucket_region: str = DEPLOY_REGION,
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
    "cloudformation deploy"*|"cloudformation update-stack"*|"cloudformation delete-stack"*)
        : > {deploy_sentinel!s}
        exit 0 ;;
    "cloudformation describe-stacks"*"ParameterKey"*)
        echo "AutonomyMode\tProjectName\tExecutorCodeS3Key"
        exit 0 ;;
    "cloudformation describe-stacks"*"Outputs"*)
        echo "auto-app-id"
        exit 0 ;;
    "cloudformation describe-stacks"*)
        exit 0 ;;
    "stepfunctions describe-state-machine"*)
        echo "STANDARD"
        exit 0 ;;
    "cloudformation wait"*)
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
    _make_exe(fakes["bindir"] / "cfn-lint", _fake_ok())
    _make_exe(fakes["bindir"] / "python3", _fake_ok())
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws(fakes["validate_body"], fakes["upload"], fakes["deploy"], **aws_kwargs),
    )


def _run(script, *args, fakes, env_extra=None):
    bindir = fakes["bindir"]
    env = os.environ.copy()
    for key in _CLEARED_ENV:
        env.pop(key, None)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["AWS_REGION"] = DEPLOY_REGION
    env["AWS_PROFILE"] = "unit-test-profile"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True)


def _stub_backend_src(tmp_path) -> pathlib.Path:
    src = tmp_path / "backend-src"
    module = src / "operations" / "autonomy_runtime" / "evaluator_entry.py"
    module.parent.mkdir(parents=True)
    module.write_text("def handler(event, context):\n    return {}\n", encoding="utf-8")
    return src


def _enable_env(bucket="artifact-bucket-unit", backend_src=None, **overrides):
    env = {
        "GBAW_OPERATIONS_MODE": "operate",
        "GBAW_OPERATIONS_AUTONOMY_CONFIRM": "operate",
        "GBAW_OPERATIONS_ARTIFACT_BUCKET": bucket,
        "GBAW_OPERATIONS_TABLE_NAME": "game-agent-operations",
        "GBAW_OPERATIONS_KMS_KEY_ARN": f"arn:aws:kms:{DEPLOY_REGION}:{SYNTHETIC_ACCOUNT}:key/abc",
        "GBAW_OPERATIONS_TENANT_ID": "tenant-demo",
        "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-demo",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ID": "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
        "GBAW_OPERATIONS_ENROLLED_FLEET_ARN": (
            f"arn:aws:gamelift:{DEPLOY_REGION}:{SYNTHETIC_ACCOUNT}:fleet/fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
        ),
        "GBAW_OPERATIONS_TRUSTED_AUDIENCE": "client.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_EXECUTION_STATE_MACHINE_ARN": (
            f"arn:aws:states:{DEPLOY_REGION}:{SYNTHETIC_ACCOUNT}:stateMachine:game-agent-operations-execution"
        ),
        "GBAW_OPERATIONS_AUTONOMY_SUBJECT": "automation.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_CLIENT": "client.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_ID": "policy.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_VERSION": "2026-09-01",
        "GBAW_OPERATIONS_AUTONOMY_POLICY_HASH": "sha256:" + "a" * 64,
        "GBAW_OPERATIONS_AUTONOMY_STATE_ID": "state.gamelift-capacity-autonomy",
        "GBAW_OPERATIONS_APPCONFIG_APPLICATION_ID": "app-abc",
        "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT_ID": "env-abc",
        "GBAW_OPERATIONS_APPCONFIG_PROFILE_ID": "killswitch-profile",
        "GBAW_OPERATIONS_AUTONOMY_SWITCH_PROFILE_ID": "autonomy-profile",
    }
    env.update(overrides)
    if backend_src is not None:
        env["GBAW_OPERATIONS_AUTONOMY_BACKEND_SRC"] = str(backend_src)
    return env


# --------------------------------------------------------------------------- #
# Wrappers exist and are executable.
# --------------------------------------------------------------------------- #
def test_wrappers_exist():
    for w in (DEPLOY, DISABLE, TEARDOWN):
        assert w.exists(), f"missing wrapper: {w}"


# --------------------------------------------------------------------------- #
# PREVIEW is the default, read-only, and size-aware.
# --------------------------------------------------------------------------- #
def test_default_is_read_only_preview(fakes):
    _install_fakes(fakes)
    result = _run(DEPLOY, fakes=fakes)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"preview must succeed read-only: {combined}"
    assert not fakes["upload"].exists(), "preview must never upload"
    assert not fakes["deploy"].exists(), "preview must never deploy"


def test_preview_references_inline_limit_in_source():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "51200" in text, "wrapper must reference the 51,200-byte inline limit"


# --------------------------------------------------------------------------- #
# ENABLE requires BOTH --enable AND the confirmation token.
# --------------------------------------------------------------------------- #
def test_enable_without_confirmation_token_refuses(fakes, tmp_path):
    _install_fakes(fakes)
    backend_src = _stub_backend_src(tmp_path)
    env = _enable_env(backend_src=backend_src)
    env.pop("GBAW_OPERATIONS_AUTONOMY_CONFIRM")
    result = _run(DEPLOY, "--enable", fakes=fakes, env_extra=env)
    assert result.returncode != 0, "enable without the confirmation token must refuse"
    assert not fakes["deploy"].exists()
    assert not fakes["upload"].exists()


def test_enable_without_operate_mode_refuses(fakes, tmp_path):
    _install_fakes(fakes)
    backend_src = _stub_backend_src(tmp_path)
    env = _enable_env(backend_src=backend_src)
    env["GBAW_OPERATIONS_MODE"] = "disabled"
    result = _run(DEPLOY, "--enable", fakes=fakes, env_extra=env)
    assert result.returncode != 0, "enable requires GBAW_OPERATIONS_MODE=operate"
    assert not fakes["deploy"].exists()


def test_enable_foreign_owner_bucket_aborts_before_packaging(fakes, tmp_path):
    _install_fakes(fakes, real_bucket_owner="123456789012")
    backend_src = _stub_backend_src(tmp_path)
    result = _run(DEPLOY, "--enable", fakes=fakes, env_extra=_enable_env(backend_src=backend_src))
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode != 0, "foreign-owned bucket must abort enable"
    assert not fakes["upload"].exists()
    assert not fakes["deploy"].exists()
    assert "bucket" in combined


# --------------------------------------------------------------------------- #
# Source-level safety guarantees.
# --------------------------------------------------------------------------- #
def test_no_bucket_creation_or_discovery():
    text = DEPLOY.read_text(encoding="utf-8")
    for forbidden in ("create-bucket", "mb s3://", "list-buckets"):
        assert forbidden not in text, f"wrapper must not '{forbidden}'"


def test_bucket_verification_asserts_owner_and_region():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--expected-bucket-owner" in text
    assert "get-bucket-location" in text


def test_deploy_resolves_appconfig_layer_from_ssm():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "aws-appconfig/lambda-extension" in text, "must resolve the official AppConfig extension layer"


def test_deploy_uses_deterministic_content_hash_key():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "shasum" in text or "sha256" in text.lower()


def test_deploy_verifies_deployed_artifact_hash():
    text = DEPLOY.read_text(encoding="utf-8")
    # After deploy, the wrapper reads the live function's code hash and compares
    # it against the deployed artifact.
    assert "get-function" in text or "CodeSha256" in text or "codesha256" in text.lower()


def test_deploy_seeds_policy_and_window_state_conditionally():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "seed" in text.lower()
    assert "DynamoDbAutonomyPolicyLoader" in text or "seed_window_state" in text


def test_deploy_integrates_by_calling_07_execution_workflow():
    """E5 must reuse the existing reviewed E3 executor via the 07 workflow rather
    than mutating provider resources directly — it references the 07 state
    machine ARN and never grants a GameLift action in the wrapper."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "07-operations-execution" in text or "EXECUTION_STATE_MACHINE_ARN" in text.upper()
    # Finding 6 binds the enrolled fleet ARN to the gamelift SERVICE segment
    # (arn:...:gamelift:<region>:...:fleet/<id>), so a naive 'gamelift:'
    # substring ban is wrong. A gamelift IAM ACTION is always PascalCase
    # (gamelift:UpdateFleetCapacity, gamelift:DescribeFleetCapacity, ...); a
    # service-ARN segment is 'gamelift:' followed by a region (lowercase or
    # a '*' glob), never a capital. Forbid only a PascalCase action grant.
    import re as _re

    action_grants = _re.findall(r"gamelift:[A-Z]\w+", text)
    assert not action_grants, f"the autonomy wrapper must never grant a GameLift action: {action_grants}"


def _fake_aws_express(validate_body, upload, deploy):
    """A fake aws whose 07 state machine is EXPRESS (must be refused)."""
    base = _fake_aws(validate_body, upload, deploy)
    return base.replace(
        '"stepfunctions describe-state-machine"*)\n        echo "STANDARD"',
        '"stepfunctions describe-state-machine"*)\n        echo "EXPRESS"',
    )


def test_enable_refuses_a_non_standard_07_workflow(fakes, tmp_path):
    """Finding 8: an EXPRESS (non-STANDARD) 07 workflow must abort before deploy."""
    _make_exe(fakes["bindir"] / "cfn-lint", _fake_ok())
    _make_exe(fakes["bindir"] / "python3", _fake_ok())
    _make_exe(
        fakes["bindir"] / "aws",
        _fake_aws_express(fakes["validate_body"], fakes["upload"], fakes["deploy"]),
    )
    backend_src = _stub_backend_src(tmp_path)
    result = _run(DEPLOY, "--enable", fakes=fakes, env_extra=_enable_env(backend_src=backend_src))
    assert result.returncode != 0, "a non-STANDARD 07 workflow must be refused"
    assert not fakes["deploy"].exists(), "must abort before the 09 deploy"


def test_deploy_verifies_07_is_standard_same_account_region():
    """Finding 8: the deploy wrapper must verify the exact 07 workflow is a
    STANDARD Step Functions state machine in this account/region before enabling."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "stepfunctions describe-state-machine" in text, "must describe the 07 state machine"
    assert "STANDARD" in text, "must require the 07 workflow to be STANDARD"
    # Account + region of the ARN are checked against the deploy account/region.
    assert "SM_REGION" in text and "SM_ACCOUNT" in text


def test_deploy_enables_the_07_executor_prewrite_hook():
    """Finding 7: after deploying 09, the wrapper must update the 07 executor to
    AutonomyMode=operate and bind the autonomy AppConfig ids so the pre-write
    hook is actually enabled — reusing every other 07 value."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "operations-execution" in text, "deploy must update the 07 execution stack"
    assert "AutonomyMode,ParameterValue=operate" in text.replace(" ", ""), "must set 07 AutonomyMode=operate"
    assert "Stacks[0].Parameters[].ParameterKey" in text, "must reuse 07 params dynamically"
    assert "AutonomySwitchProfileId" in text, "must bind the autonomy switch profile id on 07"


def test_deploy_activation_requires_explicit_enable_and_token_in_source():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--enable" in text
    assert "GBAW_OPERATIONS_AUTONOMY_CONFIRM" in text


def test_deploy_default_params_are_zero_resource():
    """Provisioned=true / AutonomyMode=operate appear ONLY under the double-opt-in
    --enable deploy path; the default (no-flag) run is a read-only preview that
    passes no parameters and creates nothing, so a default stack stays $0."""
    text = DEPLOY.read_text(encoding="utf-8")
    # The only Provisioned=true / AutonomyMode=operate overrides live in the
    # enable deploy, which is reached only after the double-opt-in gate.
    assert "Provisioned=true" in text
    assert "AutonomyMode=operate" in text
    gate = text.index("GBAW_OPERATIONS_AUTONOMY_CONFIRM")
    assert text.index("Provisioned=true") > gate, "Provisioned=true must be gated behind the confirmation token"
    assert text.index("AutonomyMode=operate") > gate, "AutonomyMode=operate must be gated behind the confirmation token"


def test_aws_calls_thread_explicit_profile_args():
    text = DEPLOY.read_text(encoding="utf-8")
    assert 'AWS_PROFILE_ARGS=(--profile "$AWS_PROFILE")' in text


# --------------------------------------------------------------------------- #
# DISABLE wrapper: reversible, confirm-gated, deletes nothing.
# --------------------------------------------------------------------------- #
def test_disable_requires_confirm(fakes):
    _install_fakes(fakes)
    result = _run(DISABLE, fakes=fakes)
    assert result.returncode != 0, "disable without --confirm must refuse"
    assert not fakes["deploy"].exists()


def test_disable_flips_only_mode_keeps_provisioned():
    text = DISABLE.read_text(encoding="utf-8")
    assert "AutonomyMode,ParameterValue=disabled" in text.replace(" ", "")
    assert "Provisioned,ParameterValue=true" in text.replace(" ", "")
    assert "delete-stack" not in text, "disable must never delete resources"


def test_disable_does_not_rebuild_or_upload():
    text = DISABLE.read_text(encoding="utf-8")
    assert "use-previous-template" in text
    assert "put-object" not in text and "s3 cp" not in text


# --------------------------------------------------------------------------- #
# TEARDOWN wrapper: exact token, never automatic.
# --------------------------------------------------------------------------- #
def test_teardown_requires_exact_token(fakes):
    _install_fakes(fakes)
    result = _run(TEARDOWN, "--confirm", "wrong-token", fakes=fakes)
    assert result.returncode != 0, "teardown must reject a wrong token"
    assert not fakes["deploy"].exists()


def test_teardown_token_is_delete_operations_autonomy():
    text = TEARDOWN.read_text(encoding="utf-8")
    assert "delete-operations-autonomy" in text


def test_teardown_not_wired_into_teardown_all():
    assert TEARDOWN_ALL.exists()
    text = TEARDOWN_ALL.read_text(encoding="utf-8")
    assert "teardown-operations-autonomy" not in text, "teardown must never be automatic"
