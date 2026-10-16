"""Regression tests for account-wide observability setup on a fresh account.

X-Ray rejects UpdateTraceSegmentDestination with AccessDenied until a CloudWatch
Logs resource policy lets xray.amazonaws.com write spans. The setup script must
create that policy first and must report AWS failures instead of exiting
silently. All AWS calls are local fakes; no credentials or live resources.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "infrastructure" / "setup-account-observability.sh"
POWERSHELL = REPOSITORY_ROOT / "scripts" / "powershell" / "Private" / "Invoke-GameAgentAccountObservability.ps1"

AWS_FAKE = r'''
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(" ".join(args[:2]))


def save():
    state_path.write_text(json.dumps(state))


def option(name):
    return args[args.index(name) + 1] if name in args else None


command = tuple(args[:2])
if command == ("sts", "get-caller-identity"):
    save()
    print("123456789012")
elif command == ("logs", "put-resource-policy"):
    if state.get("policy_error"):
        save()
        print("An error occurred (AccessDeniedException) when calling PutResourcePolicy", file=sys.stderr)
        sys.exit(254)
    state["policy"] = True
    save()
elif command == ("logs", "describe-log-groups"):
    save()
    groups = [{"logGroupName": "aws/spans"}] if state.get("log_group") else []
    print(json.dumps({"logGroups": groups}))
elif command == ("xray", "get-trace-segment-destination"):
    save()
    query = option("--query")
    if query == "Destination":
        print(state["destination"])
    elif query == "Status":
        print("ACTIVE")
    else:
        print(json.dumps({"Destination": state["destination"], "Status": "ACTIVE"}))
elif command == ("xray", "update-trace-segment-destination"):
    if not state.get("policy"):
        save()
        print(
            "An error occurred (AccessDeniedException) when calling the "
            "UpdateTraceSegmentDestination operation: An unknown error occurred",
            file=sys.stderr,
        )
        sys.exit(254)
    state["destination"] = option("--destination")
    if state["destination"] == "CloudWatchLogs":
        state["log_group"] = True
    save()
else:
    save()
'''


class AccountObservabilityOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="observability-tests-")
        root = Path(self.temporary_directory.name)
        self.fake_bin = root / "fake-bin"
        self.fake_bin.mkdir()
        fake_aws = self.fake_bin / "aws"
        fake_aws.write_text(f"#!{sys.executable}\n{textwrap.dedent(AWS_FAKE).lstrip()}")
        fake_aws.chmod(fake_aws.stat().st_mode | stat.S_IXUSR)
        for tool in ("grep", "sed", "seq", "sleep"):
            source = shutil.which(tool)
            if source is None:
                raise RuntimeError(f"Required test utility not found: {tool}")
            (self.fake_bin / tool).symlink_to(source)
        self.state_path = root / "state.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_script(self, **state) -> tuple[subprocess.CompletedProcess[str], dict]:
        initial = {"calls": [], "destination": "XRay", "log_group": False, "policy": False}
        initial.update(state)
        self.state_path.write_text(json.dumps(initial))
        env = {
            "PATH": str(self.fake_bin),
            "FAKE_STATE": str(self.state_path),
            "AWS_REGION": "us-west-2",
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=60,
            check=False,
        )
        return result, json.loads(self.state_path.read_text())

    def test_fresh_account_succeeds_on_first_run(self) -> None:
        result, state = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(state["destination"], "CloudWatchLogs")
        self.assertTrue(state["log_group"])
        self.assertIn("Account-wide observability setup complete", result.stdout)

    def test_resource_policy_precedes_every_destination_change(self) -> None:
        for log_group in (False, True):
            with self.subTest(log_group_exists=log_group):
                _, state = self.run_script(log_group=log_group)

                calls = state["calls"]
                policy_index = calls.index("logs put-resource-policy")
                updates = [index for index, call in enumerate(calls) if call == "xray update-trace-segment-destination"]
                self.assertTrue(updates)
                self.assertLess(policy_index, min(updates))

    def test_destination_failure_is_reported_not_silent(self) -> None:
        result, state = self.run_script(policy_error=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Creating the CloudWatch Logs resource policy for X-Ray failed", result.stdout)
        self.assertIn("AccessDeniedException", result.stdout)
        self.assertNotIn("xray update-trace-segment-destination", state["calls"])

    def test_already_configured_account_is_left_unchanged(self) -> None:
        result, state = self.run_script(destination="CloudWatchLogs", log_group=True)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("xray update-trace-segment-destination", state["calls"])
        self.assertIn("already set to CloudWatch Logs", result.stdout)

    def test_powershell_creates_resource_policy_before_destination_changes(self) -> None:
        source = POWERSHELL.read_text(encoding="utf-8")
        body = source[source.index("Write-GameAgentStatus 'Setting up account-wide observability...'") :]

        policy_index = body.index("logs put-resource-policy")
        first_destination_change = body.index("Set-TraceDestination '")
        self.assertLess(policy_index, first_destination_change)
        self.assertNotRegex(body, r"update-trace-segment-destination[^\n]*2>\$null")


class StackDeployRerunTests(unittest.TestCase):
    def test_every_bash_stack_deploy_tolerates_an_unchanged_stack(self) -> None:
        for script in ("scripts/deploy.sh", "scripts/infrastructure/deploy-kb.sh"):
            text = (REPOSITORY_ROOT / script).read_text(encoding="utf-8")
            commands = re.findall(r"aws cloudformation deploy \\\n(?:[^\n]*\\\n)*[^\n]*\n", text)
            self.assertTrue(commands, script)
            for command in commands:
                with self.subTest(script=script, stack=re.search(r"--stack-name (\S+)", command).group(1)):
                    self.assertIn("--no-fail-on-empty-changeset", command)


if __name__ == "__main__":
    unittest.main()
