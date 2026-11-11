"""Behavior tests for scripts/infrastructure/ensure-service-linked-role.sh (#772).

In an account that has never used ECS, the first deployment failed at Step 7:
ECS Express could not assume ``AWSServiceRoleForECS``, which IAM had created
1.7 s earlier. deploy.sh now runs this helper before the frontend stack. These
tests run it against a stateful fake ``aws`` command that answers only the
expected IAM calls and fails any other, so a changed request fails a test.
Nothing here uses credentials or cloud resources.

Static checks at the end keep the helper ahead of the frontend stack deploy in
both deployment paths (scripts/deploy.sh and Deploy-GameAgent.ps1).
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
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "infrastructure" / "ensure-service-linked-role.sh"
DEPLOY_SH = REPOSITORY_ROOT / "scripts" / "deploy.sh"
DEPLOY_PS1 = REPOSITORY_ROOT / "scripts" / "powershell" / "Public" / "Deploy-GameAgent.ps1"
POWERSHELL_HELPER = REPOSITORY_ROOT / "scripts" / "powershell" / "Private" / "Initialize-GameAgentServiceLinkedRole.ps1"

SERVICE = "ecs.amazonaws.com"
ROLE = "AWSServiceRoleForECS"
SYNTHETIC_ARN = "arn:aws:sts::123456789012:assumed-role/Example/session"

FAKE_AWS = r"""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(args)

GET_ROLE = ["iam", "get-role", "--role-name", "AWSServiceRoleForECS", "--region", "us-west-2",
            "--query", "Role.RoleName", "--output", "text"]
CREATE = ["iam", "create-service-linked-role", "--aws-service-name", "ecs.amazonaws.com",
          "--region", "us-west-2", "--query", "Role.RoleName", "--output", "text"]


def finish(code=0, out="", err=""):
    state_path.write_text(json.dumps(state))
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    sys.exit(code)


if args == GET_ROLE:
    if state["role_exists"] and not state["never_visible"]:
        finish(out="AWSServiceRoleForECS")
    finish(254, err=state["get_role_error"])
elif args == CREATE:
    if state["create_makes_role"]:
        state["role_exists"] = True
    if state["create_error"]:
        finish(254, err=state["create_error"])
    finish(out="AWSServiceRoleForECS")
else:
    finish(2, err="fake aws: unexpected call %r" % (args,))
"""

NO_SUCH_ENTITY = (
    "An error occurred (NoSuchEntity) when calling the GetRole operation: "
    "The role with name AWSServiceRoleForECS cannot be found."
)
TAKEN = (
    "An error occurred (InvalidInput) when calling the CreateServiceLinkedRole operation: "
    "Service role name AWSServiceRoleForECS has been taken in this account, please try a different suffix."
)

GET_ROLE = ["iam", "get-role"]
CREATE = ["iam", "create-service-linked-role"]


class EnsureServiceLinkedRole(unittest.TestCase):
    """ensure-service-linked-role.sh, run by deploy.sh before Step 7."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "aws"
        fake.write_text(f"#!{sys.executable}\n{FAKE_AWS.lstrip()}", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        # A fake sleep records each wait instead of sleeping.
        self.sleeps_path = self.tmp / "sleeps.txt"
        fake_sleep = self.bin / "sleep"
        fake_sleep.write_text(f'#!/bin/sh\necho "$1" >> "{self.sleeps_path}"\n', encoding="utf-8")
        fake_sleep.chmod(fake_sleep.stat().st_mode | stat.S_IXUSR)
        self.state_path = self.tmp / "state.json"
        self.write_state()

    def write_state(
        self,
        *,
        role_exists: bool = False,
        get_role_error: str = NO_SUCH_ENTITY,
        create_error: str = "",
        create_makes_role: bool = True,
        never_visible: bool = False,
    ) -> None:
        state = {
            "calls": [],
            "role_exists": role_exists,
            "get_role_error": get_role_error,
            "create_error": create_error,
            "create_makes_role": create_makes_role,
            "never_visible": never_visible,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def operations(self) -> list[list[str]]:
        return [call[:2] for call in json.loads(self.state_path.read_text(encoding="utf-8"))["calls"]]

    def sleeps(self) -> list[str]:
        if not self.sleeps_path.exists():
            return []
        return self.sleeps_path.read_text(encoding="utf-8").split()

    def run_script(
        self, *args: str, propagation: str | None = "0", poll: str = "0"
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.pathsep.join((str(self.bin), "/usr/bin", "/bin")),
            "HOME": str(self.tmp),
            "FAKE_STATE": str(self.state_path),
            "AWS_REGION": "us-west-2",
            "GBAW_SLR_POLL_SECONDS": poll,
        }
        if propagation is not None:
            env["GBAW_SLR_PROPAGATION_SECONDS"] = propagation
        return subprocess.run(
            ["bash", str(SCRIPT), *(args or (SERVICE, ROLE))],
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    def test_an_existing_role_is_neither_created_nor_waited_for(self) -> None:
        self.write_state(role_exists=True)

        result = self.run_script(propagation="120", poll="3")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.operations(), [GET_ROLE])
        self.assertEqual(self.sleeps(), [])
        self.assertIn(f"✅ Service-linked role {ROLE} already exists", result.stdout)
        self.assertNotIn("Waiting", result.stdout)

    def test_a_missing_role_is_created_and_checked_until_visible(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.operations(), [GET_ROLE, CREATE, GET_ROLE])
        self.assertEqual(self.sleeps(), [])
        self.assertIn(f"✅ Created service-linked role {ROLE}", result.stdout)
        self.assertIn(f"✅ Service-linked role {ROLE} ready", result.stdout)

    def test_a_role_that_has_been_taken_counts_as_created(self) -> None:
        self.write_state(create_error=TAKEN)

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.operations(), [GET_ROLE, CREATE, GET_ROLE])
        self.assertIn(f"Service-linked role {ROLE} was created concurrently", result.stdout)
        self.assertIn(f"✅ Service-linked role {ROLE} ready", result.stdout)

    def test_a_denied_get_role_fails_closed_without_a_create(self) -> None:
        self.write_state(
            get_role_error=f"An error occurred (AccessDenied) when calling the GetRole operation: User: {SYNTHETIC_ARN}"
        )

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), [GET_ROLE])
        self.assertIn(f"❌ Read service-linked role {ROLE} failed (AccessDenied)", result.stderr)
        self.assertIn(f"aws iam create-service-linked-role --aws-service-name {SERVICE}", result.stderr)
        self.assertNotIn(SYNTHETIC_ARN, result.stdout + result.stderr)

    def test_a_throttled_get_role_fails_closed_without_a_create(self) -> None:
        self.write_state(get_role_error="An error occurred (Throttling) when calling the GetRole operation: Rate exceeded")

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), [GET_ROLE])
        self.assertIn("failed (Throttling)", result.stderr)

    def test_a_failed_create_stops_with_a_bounded_message(self) -> None:
        self.write_state(
            create_error=(
                "An error occurred (AccessDenied) when calling the CreateServiceLinkedRole operation: "
                f"User: {SYNTHETIC_ARN} is not authorized"
            ),
            create_makes_role=False,
        )

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), [GET_ROLE, CREATE])
        self.assertEqual(self.sleeps(), [])
        self.assertIn(f"❌ Create service-linked role {ROLE} failed (AccessDenied)", result.stderr)
        self.assertNotIn(SYNTHETIC_ARN, result.stdout + result.stderr)
        self.assertNotIn("ready", result.stdout)

    def test_taken_text_without_the_invalid_input_code_is_a_failure(self) -> None:
        self.write_state(
            create_error="An error occurred (LimitExceeded) when calling the operation: has been taken in this account",
            create_makes_role=False,
        )

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed (LimitExceeded)", result.stderr)

    def test_a_role_that_never_becomes_visible_fails_after_bounded_checks(self) -> None:
        self.write_state(never_visible=True)

        result = self.run_script(poll="2")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), [GET_ROLE, CREATE] + [GET_ROLE] * 10)
        # Nine waits between ten checks, and no propagation wait after the failure.
        self.assertEqual(self.sleeps(), ["2"] * 9)
        self.assertIn(f"❌ Service-linked role {ROLE} not visible after 10 checks (NoSuchEntity)", result.stderr)

    def test_an_invalid_propagation_delay_is_refused_before_any_call(self) -> None:
        for value in ("121", "-1", "abc", "1.5", " 5", "1000"):
            with self.subTest(value=value):
                self.write_state()

                result = self.run_script(propagation=value)

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.operations(), [])
                self.assertIn("GBAW_SLR_PROPAGATION_SECONDS must be an integer from 0 to 120", result.stdout)

    def test_the_propagation_delay_defaults_to_15_seconds_after_a_create(self) -> None:
        for value in (None, ""):
            with self.subTest(value=value):
                self.write_state()
                self.sleeps_path.unlink(missing_ok=True)

                result = self.run_script(propagation=value)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.sleeps(), ["15"])
                self.assertIn(f"⏳ Waiting 15s for {ROLE} to propagate...", result.stdout)

    def test_the_maximum_propagation_delay_is_accepted(self) -> None:
        result = self.run_script(propagation="120")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sleeps(), ["120"])

    def test_an_invalid_poll_interval_is_refused_before_any_call(self) -> None:
        result = self.run_script(poll="31")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), [])
        self.assertIn("GBAW_SLR_POLL_SECONDS must be an integer from 0 to 30", result.stdout)

    def test_invalid_arguments_are_refused_before_any_call(self) -> None:
        for args in ((SERVICE,), (SERVICE, "bad role"), ("ecs.amazonaws.com; true", ROLE)):
            with self.subTest(args=args):
                result = self.run_script(*args)

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.operations(), [])


class FrontendDeployOrdering(unittest.TestCase):
    """Both deployment paths ensure the role before the frontend stack deploy."""

    def test_deploy_sh_runs_the_helper_before_the_frontend_stack(self) -> None:
        text = DEPLOY_SH.read_text(encoding="utf-8")
        step7 = text.index("# Step 7: Deploy frontend")
        frontend_template = text.index("02-frontend-ecs-express.yaml", step7)
        frontend_deploy = text.rindex("aws cloudformation deploy", step7, frontend_template)
        helper = text.find(
            'ensure-service-linked-role.sh" ecs.amazonaws.com AWSServiceRoleForECS', step7, frontend_deploy
        )

        self.assertNotEqual(helper, -1, "deploy.sh must ensure the ECS role inside Step 7, before the frontend deploy")
        # A helper failure must stop the deployment.
        self.assertRegex(text[step7:frontend_deploy], r"if ! AWS_REGION=\"\$AWS_REGION\" \\\n\s+bash \"\$SCRIPT_DIR/")
        self.assertRegex(text[helper:frontend_deploy], r"\n\s+exit 1\n")
        # Inside the Docker-gated frontend block, so it runs only when the frontend deploys.
        docker_gate = text.rindex('if [ "$DOCKER_AVAILABLE" = true ]; then', 0, step7)
        self.assertLess(docker_gate, helper)

    def test_deploy_ps1_initializes_the_role_before_the_frontend_stack(self) -> None:
        text = DEPLOY_PS1.read_text(encoding="utf-8")
        step7 = text.index("# Step 7: Deploy frontend")
        frontend_deploy = text.index('Deploy-Stack -StackName "$ProjectName-frontend"', step7)
        helper = text.find("Initialize-GameAgentServiceLinkedRole", step7, frontend_deploy)

        self.assertNotEqual(helper, -1, "Deploy-GameAgent must ensure the ECS role before the frontend Deploy-Stack")
        call = text[helper:frontend_deploy]
        self.assertIn("-ServiceName 'ecs.amazonaws.com'", call)
        self.assertIn("-RoleName 'AWSServiceRoleForECS'", call)
        self.assertIn("-PropagationSeconds $env:GBAW_SLR_PROPAGATION_SECONDS", call)
        self.assertRegex(text[step7:helper], r"New-GameAgentAwsInvoker -ProfileArgs \$profileArgs")
        docker_gate = text.rindex("if ($dockerAvailable) {", 0, step7)
        self.assertLess(docker_gate, helper)

    def test_both_paths_share_the_same_bounds_and_classification(self) -> None:
        shell = SCRIPT.read_text(encoding="utf-8")
        powershell = POWERSHELL_HELPER.read_text(encoding="utf-8")
        for text in (shell, powershell):
            self.assertIn("NoSuchEntity", text)
            self.assertIn("InvalidInput", text)
            self.assertIn("has been taken in this account", text)
        self.assertIn("validate_seconds GBAW_SLR_PROPAGATION_SECONDS", shell)
        self.assertRegex(shell, r"validate_seconds GBAW_SLR_PROPAGATION_SECONDS \"\$SLR_PROPAGATION_SECONDS\" 120")
        self.assertRegex(powershell, r"'GBAW_SLR_PROPAGATION_SECONDS' \$PropagationSeconds 15 120")
        self.assertRegex(shell, r"SLR_POLL_ATTEMPTS=10\b")
        self.assertRegex(powershell, re.escape("[int]$PollAttempts = 10"))


if __name__ == "__main__":
    unittest.main()
