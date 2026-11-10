"""Behavior tests for runtime log-group retention in setup-app-observability.sh.

deploy.sh Step 7b runs scripts/infrastructure/setup-app-observability.sh to set
14-day retention on log groups that AgentCore creates without one. These tests
run it against a fake ``aws`` command and check that the runtime and ADOT log
groups are matched by exact name and get retention only when they have none,
so an existing policy is never shortened. Nothing here uses
credentials or cloud resources.

The fake rejects ``--log-group-name-exact`` the way the AWS CLI does. That
option does not exist; the old lookup hid the error and reported every log
group as missing. The fake answers only the queries the script is expected to
send and fails any other call, so a changed query fails these tests.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "infrastructure" / "setup-app-observability.sh"
POWERSHELL_HELPER = REPOSITORY_ROOT / "scripts" / "powershell" / "Private" / "Invoke-GameAgentAppObservability.ps1"

RUNTIME_ID = "gameagentruntime-EXAMPLE"
RUNTIME_GROUP = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
ADOT_GROUP = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}/adot-rt-logs"

FAKE_AWS = r"""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(args)


def save():
    state_path.write_text(json.dumps(state))


def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def fail(code, message):
    save()
    print(message, file=sys.stderr)
    sys.exit(code)


command = tuple(args[:2])
query = option("--query")

if command == ("logs", "describe-log-groups"):
    if "--log-group-name-exact" in args:
        # The AWS CLI has no such option and rejects the call before sending it.
        fail(252, "Unknown options: --log-group-name-exact")
    prefix = option("--log-group-name-prefix", "")
    exact_query = "logGroups[?logGroupName=='%s'].[logGroupName, retentionInDays]" % prefix
    if query == exact_query and prefix in state["lookup_errors"]:
        fail(254, "An error occurred (ThrottlingException) when calling the DescribeLogGroups operation")
    groups = sorted(
        (group for group in state["log_groups"] if group["logGroupName"].startswith(prefix)),
        key=lambda group: group["logGroupName"],
    )
    if option("--output") == "table":
        print("| DescribeLogGroups |")
    elif query == "logGroups[?!retentionInDays].logGroupName":
        names = [group["logGroupName"] for group in groups if not group.get("retentionInDays")]
        if names:
            print("\t".join(names))
    elif query == exact_query:
        for group in groups:
            if group["logGroupName"] == prefix:
                retention = group.get("retentionInDays")
                print("%s\t%s" % (group["logGroupName"], "None" if retention is None else retention))
    else:
        fail(2, "fake aws: unexpected query %r" % query)
elif command == ("logs", "put-retention-policy"):
    if option("--log-group-name") in state.get("put_errors", []):
        fail(254, "An error occurred (AccessDeniedException) when calling the PutRetentionPolicy operation")
    for group in state["log_groups"]:
        if group["logGroupName"] == option("--log-group-name"):
            group["retentionInDays"] = int(option("--retention-in-days"))
else:
    fail(2, "fake aws: unexpected call %r" % (args,))
save()
"""


def log_group(name: str, retention: int | None = None) -> dict:
    return {"logGroupName": name, "retentionInDays": retention}


class RuntimeLogRetention(unittest.TestCase):
    """setup-app-observability.sh, run by deploy.sh Step 7b."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "aws"
        fake.write_text(f"#!{sys.executable}\n{FAKE_AWS.lstrip()}", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.state_path = self.tmp / "state.json"
        self.write_state()

    def write_state(
        self,
        *,
        log_groups: list[dict] | None = None,
        lookup_errors: list[str] | None = None,
        put_errors: list[str] | None = None,
    ) -> None:
        state = {
            "calls": [],
            "log_groups": log_groups or [],
            "lookup_errors": lookup_errors or [],
            "put_errors": put_errors or [],
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))["calls"]

    def retention_updates(self) -> list[tuple[str, str]]:
        def value(call: list[str], name: str) -> str:
            return call[call.index(name) + 1]

        return [
            (value(call, "--log-group-name"), value(call, "--retention-in-days"))
            for call in self.calls()
            if call[:2] == ["logs", "put-retention-policy"]
        ]

    def run_script(self, runtime_id: str = RUNTIME_ID) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), runtime_id],
            env={
                "PATH": os.pathsep.join((str(self.bin), "/usr/bin", "/bin")),
                "HOME": str(self.tmp),
                "FAKE_STATE": str(self.state_path),
                "AWS_REGION": "us-west-2",
            },
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    def test_sets_retention_on_runtime_and_adot_log_groups_that_have_none(self) -> None:
        self.write_state(log_groups=[log_group(RUNTIME_GROUP), log_group(ADOT_GROUP)])

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [(RUNTIME_GROUP, "14"), (ADOT_GROUP, "14")])
        self.assertIn("AgentCore Runtime logs: set 14-day retention", result.stdout)
        self.assertIn("ADOT runtime logs: set 14-day retention", result.stdout)

    def test_a_log_group_already_at_14_days_is_left_alone(self) -> None:
        self.write_state(log_groups=[log_group(RUNTIME_GROUP, retention=14)])

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [])
        self.assertIn(f"AgentCore Runtime logs: {RUNTIME_GROUP} already has 14-day retention", result.stdout)

    def test_an_existing_longer_or_shorter_retention_is_kept(self) -> None:
        # Shortening retention deletes older events, so an operator's policy is never changed.
        for retention in (1, 30, 3653):
            with self.subTest(retention=retention):
                self.write_state(
                    log_groups=[log_group(RUNTIME_GROUP, retention=retention), log_group(ADOT_GROUP, retention=retention)]
                )

                result = self.run_script()

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.retention_updates(), [])
                self.assertIn(f"already has {retention}-day retention", result.stdout)

    def test_a_failed_retention_update_stops_the_script(self) -> None:
        self.write_state(log_groups=[log_group(RUNTIME_GROUP)], put_errors=[RUNTIME_GROUP])

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("AgentCore Runtime logs: set 14-day retention", result.stdout)

    def test_an_invalid_runtime_id_is_refused_before_any_call(self) -> None:
        result = self.run_script("bad'] | [*].logGroupName | ['x")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_only_the_exact_log_group_name_matches(self) -> None:
        self.write_state(log_groups=[log_group(f"{RUNTIME_GROUP}-previous")])

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [])
        self.assertIn("AgentCore Runtime logs: log group not found", result.stdout)

    def test_absent_log_groups_are_reported_and_left_alone(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [])
        self.assertIn("AgentCore Runtime logs: log group not found", result.stdout)
        self.assertIn("ADOT runtime logs: log group not found", result.stdout)

    def test_a_failed_lookup_is_reported_and_leaves_retention_unchanged(self) -> None:
        self.write_state(log_groups=[log_group(RUNTIME_GROUP)], lookup_errors=[RUNTIME_GROUP])

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [])
        self.assertIn(f"AgentCore Runtime logs: could not read {RUNTIME_GROUP}; retention unchanged", result.stdout)
        self.assertNotIn("AgentCore Runtime logs: log group not found", result.stdout)

    def test_never_sends_the_nonexistent_exact_name_option(self) -> None:
        self.write_state(log_groups=[log_group(RUNTIME_GROUP), log_group(ADOT_GROUP)])

        self.run_script()

        self.assertEqual([call for call in self.calls() if "--log-group-name-exact" in call], [])


class RetentionLookupParity(unittest.TestCase):
    """The shell and PowerShell deployment paths share the same lookup."""

    def test_neither_path_uses_the_nonexistent_exact_name_option(self) -> None:
        for path in (SCRIPT, POWERSHELL_HELPER):
            with self.subTest(path.name):
                self.assertNotIn("--log-group-name-exact", path.read_text(encoding="utf-8"))

    def test_both_paths_match_the_exact_name_under_a_prefix_lookup(self) -> None:
        shell = SCRIPT.read_text(encoding="utf-8")
        powershell = POWERSHELL_HELPER.read_text(encoding="utf-8")
        self.assertIn("logGroups[?logGroupName=='${log_group}']", shell)
        self.assertIn("--log-group-name-prefix $LogGroup", powershell)
        self.assertIn("logGroups[?logGroupName=='$LogGroup']", powershell)

    def test_both_paths_route_runtime_groups_through_the_keep_existing_helper(self) -> None:
        shell = SCRIPT.read_text(encoding="utf-8")
        powershell = POWERSHELL_HELPER.read_text(encoding="utf-8")
        self.assertIn('ensure_retention_if_unset \\\n    "/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"', shell)
        self.assertIn('ensure_retention_if_unset \\\n    "/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}/adot-rt-logs"', shell)
        self.assertIn("Set-RetentionIfUnset \"/aws/bedrock-agentcore/runtimes/$RuntimeId-DEFAULT\"", powershell)
        self.assertIn("Set-RetentionIfUnset \"/aws/bedrock-agentcore/runtimes/$RuntimeId/adot-rt-logs\"", powershell)
        for removed in ("ensure_log_group_retention", "Set-LogGroupRetention"):
            self.assertNotIn(removed, shell + powershell)


if __name__ == "__main__":
    unittest.main()
