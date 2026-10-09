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
    # The disable path queries Provisioned; return true so it proceeds.
    case "$*" in
      *Provisioned*) echo "true" ;;
      *)             echo '{"Stacks":[{"StackName":"game-agent-operations"}]}' ;;
    esac
    ;;
  "cloudformation describe-stack-resources")
    echo "AWS::DynamoDB::Table	game-agent-operations-state"
    ;;
  "cloudformation update-stack") echo '{}' ;;
  "cloudformation delete-stack") echo '{}' ;;
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
