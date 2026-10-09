"""Offline wrapper drive tests using a stub ``aws`` on ``PATH``.

These run the real ``deploy-operations.sh`` disable path and
``teardown-operations.sh`` end to end with a fake ``aws`` executable on ``PATH``
that logs its argv and returns canned JSON. No real AWS call is made and no
network is touched. Unlike the static source-assertion tests, these prove the
wrappers' *runtime behavior*: the order of ``aws`` subcommands, the exact disable
command line (``--use-previous-template`` and the two overridden parameters), and
that teardown surfaces a non-zero exit when stack deletion fails rather than
reporting success.
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
DEPLOY = PROJECT_ROOT / "scripts/infrastructure/deploy-operations.sh"
TEARDOWN = PROJECT_ROOT / "scripts/infrastructure/teardown-operations.sh"

# A stub ``aws`` that records every invocation's argv (one line per call, args
# tab-joined) to $STUB_AWS_LOG and emits canned responses keyed on the
# subcommand. ``wait stack-delete-complete`` fails when $STUB_AWS_FAIL_WAIT=1 so
# a DELETE_FAILED can be exercised.
_STUB_AWS = r"""#!/usr/bin/env bash
printf '%s\n' "$(printf '%s\t' "$@")" >> "$STUB_AWS_LOG"

# Drop a leading --profile X so the matcher sees the service/subcommand.
args=("$@")
if [ "${args[0]}" = "--profile" ]; then
    args=("${args[@]:2}")
fi

svc="${args[0]}"
sub="${args[1]}"

case "$svc $sub" in
  "sts get-caller-identity")
    # Account query -> a 12-digit id; Arn query -> a role ARN.
    case "$*" in
      *Account*) echo "123456789012" ;;
      *Arn*)     echo "arn:aws:sts::123456789012:assumed-role/ops-role/session" ;;
      *)         echo "123456789012" ;;
    esac
    ;;
  "cloudformation describe-stacks")
    # The disable path queries Provisioned, then the full parameter-key list.
    # The enable path queries individual current bindings via _current_param.
    if [ "${STUB_AWS_NO_STACK:-0}" = "1" ]; then
      echo "An error occurred (ValidationError): Stack with id game-agent-operations does not exist" >&2
      exit 254
    fi
    case "$*" in
      *Provisioned*) echo "true" ;;
      *"ParameterKey=='Environment'"*)     echo "${STUB_AWS_CUR_ENV:-beta}" ;;
      *"ParameterKey=='TenantId'"*)        echo "${STUB_AWS_CUR_TENANT:-}" ;;
      *"ParameterKey=='WorkspaceId'"*)     echo "${STUB_AWS_CUR_WS:-}" ;;
      *"ParameterKey=='TrustedAudience'"*) echo "${STUB_AWS_CUR_AUD:-}" ;;
      *ParameterKey*)
        # An OLDER deployed stack: a reduced key set that predates the newer
        # template parameters (no LambdaTimeoutSeconds/ObserverGroups/etc.).
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          ProjectName Environment OperationsMode Provisioned \
          CognitoIssuer CognitoClientId CodeS3Bucket CodeS3Key ;;
      *)             echo '{"Stacks":[{"StackName":"game-agent-operations"}]}' ;;
    esac
    ;;
  "cloudformation describe-stack-resources")
    echo "AWS::DynamoDB::Table	game-agent-operations-state"
    ;;
  "cloudformation update-stack") echo '{}' ;;
  "cloudformation delete-stack") echo '{}' ;;
  "s3api get-bucket-location")
    # Report the deploy region so the in-region check passes.
    echo "${AWS_REGION:-us-west-2}" ;;
  "s3api head-bucket") echo '{}' ;;
  "s3 cp") echo '{}' ;;
  "cloudformation deploy") echo '{}' ;;
  "cloudformation wait")
    if [ "${STUB_AWS_FAIL_WAIT:-0}" = "1" ]; then
      echo "Waiter StackDeleteComplete failed" >&2
      exit 255
    fi
    ;;
  *) echo '{}' ;;
esac
exit 0
"""


def _make_stub_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a directory holding the stub ``aws`` and return it (for PATH)."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    stub = bindir / "aws"
    stub.write_text(_STUB_AWS, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


# A stub ``uv`` that emulates ``uv pip install ... --target DIR ...`` by writing
# the pinned dependency closure (as importable top-level packages) plus the one
# native extension the wrapper requires, so the enable path's native-wheel and
# structural checks pass offline without a real cross-platform install.
_STUB_UV = r"""#!/usr/bin/env bash
target=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--target" ]; then target="$a"; fi
  prev="$a"
done
if [ -n "$target" ]; then
  mkdir -p "$target"
  # Pure-python deps as top-level packages.
  for pkg in rfc8785 jsonschema jsonschema_specifications referencing attrs; do
    mkdir -p "$target/$pkg"
    printf '' > "$target/$pkg/__init__.py"
  done
  # The native dependency as a compiled Linux/x86_64 extension plus its package.
  mkdir -p "$target/rpds"
  printf '' > "$target/rpds/__init__.py"
  printf '' > "$target/rpds.cpython-313-x86_64-linux-gnu.so"
fi
exit 0
"""

# A stub ``docker`` that reports unavailable so the wrapper takes its structural
# fail-closed probe instead of attempting to run a container image.
_STUB_DOCKER = r"""#!/usr/bin/env bash
if [ "$1" = "info" ]; then exit 1; fi
exit 1
"""


def _make_enable_stub_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """A PATH dir with stub ``aws``, ``uv``, and ``docker`` for the enable path."""
    bindir = _make_stub_path(tmp_path)
    for name, body in (("uv", _STUB_UV), ("docker", _STUB_DOCKER)):
        p = bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _run_with_stub(script, *args, tmp_path, env_extra=None):
    log = tmp_path / "aws-calls.log"
    log.write_text("", encoding="utf-8")
    bindir = _make_stub_path(tmp_path)
    env = os.environ.copy()
    # Stub first on PATH so the real aws is never used; keep the rest of PATH so
    # bash/coreutils still resolve.
    env["PATH"] = os.pathsep.join([str(bindir), env.get("PATH", "")])
    env["STUB_AWS_LOG"] = str(log)
    # A profile keeps the --profile drop in the stub exercised and avoids reading
    # ui/.env.local in the test environment.
    env["AWS_PROFILE"] = "test-profile"
    env["AWS_REGION"] = "us-west-2"
    # Bind the write to the stub's account so the non-interactive guard allows it
    # (tests that assert a mismatch override this to a different account).
    env.setdefault("GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID", "123456789012")
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        ["bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return result, calls


def _run_enable_with_stub(tmp_path, args=(), *, env_extra=None):
    """Run the real deploy wrapper's --enable path with stub aws/uv/docker."""
    log = tmp_path / "aws-calls.log"
    log.write_text("", encoding="utf-8")
    bindir = _make_enable_stub_path(tmp_path)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(bindir), env.get("PATH", "")])
    env["STUB_AWS_LOG"] = str(log)
    env["AWS_PROFILE"] = "test-profile"
    env["AWS_REGION"] = "us-west-2"
    # The enable opt-in inputs.
    env.update(
        {
            "GBAW_OPERATIONS_MODE": "observe",
            "COGNITO_ISSUER": "https://issuer.example/realm",
            "COGNITO_CLIENT_ID": "client-abc",
            "TENANT_ID": "tenant-abc",
            "WORKSPACE_ID": "workspace-abc",
            "TRUSTED_AUDIENCE": "operations-api",
            "GBAW_OPERATIONS_ARTIFACT_BUCKET": "ops-artifacts-bucket",
            # Bind the write to the stub account so the non-interactive guard allows it.
            "GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID": "123456789012",
        }
    )
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        ["bash", str(DEPLOY), "--enable", *args],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return result, calls


def _subcommands(calls):
    """Reduce each recorded argv to its '<service> <subcommand>' pair, ignoring a
    leading --profile X."""
    out = []
    for line in calls:
        parts = [p for p in line.split("\t") if p != ""]
        if parts and parts[0] == "--profile":
            parts = parts[2:]
        if len(parts) >= 2:
            out.append(f"{parts[0]} {parts[1]}")
        elif parts:
            out.append(parts[0])
    return out


# --------------------------------------------------------------------------- #
# Disable path: real wrapper, stub aws.
# --------------------------------------------------------------------------- #


def test_disable_calls_aws_in_order_and_uses_previous_template(tmp_path):
    result, calls = _run_with_stub(DEPLOY, "--disable", tmp_path=tmp_path)
    assert result.returncode == 0, f"disable should succeed with the stub.\n{result.stdout}\n{result.stderr}"

    subs = _subcommands(calls)
    # Identity is verified before any stack read or write.
    assert subs[0] == "sts get-caller-identity"
    # The stack existence/provisioned checks precede the update.
    assert "cloudformation describe-stacks" in subs
    assert "cloudformation update-stack" in subs
    first_update = subs.index("cloudformation update-stack")
    first_describe = subs.index("cloudformation describe-stacks")
    assert first_describe < first_update, "disable must verify the stack before updating it"
    # The update is followed by a wait.
    assert "cloudformation wait" in subs
    assert subs.index("cloudformation wait") > first_update

    # The disable update command line ships the deployed template and only flips
    # the two safety levers.
    update_line = next(line for line in calls if "update-stack" in line)
    assert "--use-previous-template" in update_line
    assert "--template-body" not in update_line
    assert "ParameterKey=Provisioned,ParameterValue=true" in update_line
    assert "ParameterKey=OperationsMode,ParameterValue=disabled" in update_line


def test_disable_parameter_list_follows_the_deployed_stack_keys_not_the_template(tmp_path):
    # The stub reports an OLDER deployed stack whose parameter set predates the
    # current template (no LambdaTimeoutSeconds / ObserverGroups / etc.). The
    # disable update must reuse exactly those deployed keys via UsePreviousValue
    # and override only the two safety levers, so --use-previous-template never
    # sends a key the deployed template does not declare.
    result, calls = _run_with_stub(DEPLOY, "--disable", tmp_path=tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    update_line = next(line for line in calls if "update-stack" in line)

    # Standard library
    import re as _re

    sent_keys = set(_re.findall(r"ParameterKey=([A-Za-z0-9]+),", update_line))
    deployed_keys = {
        "ProjectName",
        "Environment",
        "OperationsMode",
        "Provisioned",
        "CognitoIssuer",
        "CognitoClientId",
        "CodeS3Bucket",
        "CodeS3Key",
    }
    assert sent_keys == deployed_keys, f"disable must send exactly the deployed keys, got {sent_keys}"
    # The two safety levers are overridden with explicit values; every other
    # deployed key is reused.
    assert "ParameterKey=Provisioned,ParameterValue=true" in update_line
    assert "ParameterKey=OperationsMode,ParameterValue=disabled" in update_line
    for reused in ("ProjectName", "Environment", "CognitoIssuer", "CodeS3Key"):
        assert f"ParameterKey={reused},UsePreviousValue=true" in update_line
    # A newer template-only key must NOT be sent to an older deployed template.
    assert "LambdaTimeoutSeconds" not in update_line
    assert "ObserverGroups" not in update_line


def test_disable_refuses_when_expected_account_mismatches(tmp_path):
    # The stub resolves account 123456789012; demanding a different expected
    # account must refuse before any stack write.
    result, calls = _run_with_stub(
        DEPLOY,
        "--disable",
        tmp_path=tmp_path,
        env_extra={"GBAW_OPERATIONS_EXPECTED_ACCOUNT_ID": "000000000000"},
    )
    assert result.returncode != 0
    subs = _subcommands(calls)
    assert "cloudformation update-stack" not in subs, "a mismatched account must block the write"


# --------------------------------------------------------------------------- #
# Enable path: real wrapper, stub aws/uv/docker. These exercise the staging,
# native-wheel, structural probe, upload, and deploy sequence end to end offline.
# --------------------------------------------------------------------------- #


def test_enable_new_stack_stages_the_handler_and_deploys_all_bindings(tmp_path):
    # A new stack: describe-stacks reports "does not exist", so the enable path
    # builds the artifact (staging the real operations package), uploads it, and
    # deploys with Provisioned=true, OperationsMode=observe, and all four
    # bindings. Reaching a successful deploy proves operations/observe/
    # lambda_entry.py was staged (the structural probe exits 5 otherwise), which
    # only holds with the corrected tar strip depth.
    result, calls = _run_enable_with_stub(tmp_path, env_extra={"STUB_AWS_NO_STACK": "1"})
    assert result.returncode == 0, f"enable should succeed on a new stack.\n{result.stdout}\n{result.stderr}"
    # The handler and schemas were staged (the structural probe reported success).
    assert "Structural probe passed" in result.stdout
    subs = _subcommands(calls)
    # The artifact was uploaded before the stack deploy.
    assert "s3 cp" in subs
    assert "cloudformation deploy" in subs
    assert subs.index("s3 cp") < subs.index("cloudformation deploy")
    deploy_line = next(line for line in calls if "parameter-overrides" in line)
    assert "Provisioned=true" in deploy_line
    assert "OperationsMode=observe" in deploy_line
    # A new stack receives all four trusted bindings.
    assert "Environment=beta" in deploy_line
    assert "TenantId=tenant-abc" in deploy_line
    assert "WorkspaceId=workspace-abc" in deploy_line
    assert "TrustedAudience=operations-api" in deploy_line


def test_enable_prod_to_beta_binding_change_is_refused_before_any_upload(tmp_path):
    # An existing prod stack re-enabled with the default --environment beta is a
    # prod -> beta binding change. The guard refuses with exit 9 BEFORE building
    # or uploading, so no artifact lands in the bucket.
    env_extra = {
        # describe-stacks exists; _current_param Environment returns 'prod'.
        "STUB_AWS_CUR_ENV": "prod",
    }
    result, calls = _run_enable_with_stub(tmp_path, env_extra=env_extra)
    assert result.returncode == 9, f"prod->beta must be refused with exit 9.\n{result.stdout}\n{result.stderr}"
    combined = result.stdout + result.stderr
    assert "Environment (prod -> beta)" in combined
    subs = _subcommands(calls)
    assert "s3 cp" not in subs, "the binding guard must run before any upload"
    assert "cloudformation deploy" not in subs


def test_enable_existing_stack_omits_bindings_when_unchanged(tmp_path):
    # An existing stack whose bindings already match sends only the always-updated
    # keys (Provisioned/OperationsMode/code identifiers), never the binding keys,
    # so an unchanged binding cannot be accidentally rewritten.
    env_extra = {
        "STUB_AWS_CUR_ENV": "beta",
        "STUB_AWS_CUR_TENANT": "tenant-abc",
        "STUB_AWS_CUR_WS": "workspace-abc",
        "STUB_AWS_CUR_AUD": "operations-api",
    }
    result, calls = _run_enable_with_stub(tmp_path, env_extra=env_extra)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    deploy_line = next(line for line in calls if "parameter-overrides" in line)
    assert "Provisioned=true" in deploy_line
    assert "OperationsMode=observe" in deploy_line
    # Unchanged bindings are not resent.
    assert "TenantId=tenant-abc" not in deploy_line
    assert "Environment=beta" not in deploy_line


# --------------------------------------------------------------------------- #
# Teardown path: real wrapper, stub aws.
# --------------------------------------------------------------------------- #


def test_teardown_deletes_then_waits_and_succeeds(tmp_path):
    result, calls = _run_with_stub(TEARDOWN, "--confirm", "delete-operations", tmp_path=tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    subs = _subcommands(calls)
    assert "cloudformation delete-stack" in subs
    assert "cloudformation wait" in subs
    assert subs.index("cloudformation delete-stack") < subs.index("cloudformation wait")


def test_teardown_surfaces_delete_failure_as_nonzero(tmp_path):
    # When the delete waiter fails, teardown must NOT report success.
    result, calls = _run_with_stub(
        TEARDOWN,
        "--confirm",
        "delete-operations",
        tmp_path=tmp_path,
        env_extra={"STUB_AWS_FAIL_WAIT": "1"},
    )
    assert result.returncode != 0, "teardown must exit non-zero on a failed stack deletion"
    combined = result.stdout + result.stderr
    assert "✅" not in combined or "did not complete" in combined
