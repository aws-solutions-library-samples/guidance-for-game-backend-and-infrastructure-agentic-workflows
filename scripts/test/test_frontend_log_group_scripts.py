"""Behavior tests for the scripts that find the frontend's CloudWatch log group.

ECS Express writes the frontend's logs to a group it names
``/aws/ecs/default/<project>-frontend-<generated suffix>``. These tests run
setup-app-observability.sh and watch-traces.sh against a fake ``aws`` command
and check that both read the log group from the service's current task
definition, fall back to the ECS Express name prefix, and still behave when the
log group is absent. Nothing here uses credentials or cloud resources.

The fake answers only the queries the scripts are expected to send and fails
any other call, so a changed query fails these tests instead of passing against
an unexamined default.
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
RETENTION_SCRIPT = REPOSITORY_ROOT / "scripts" / "infrastructure" / "setup-app-observability.sh"
WATCH_SCRIPT = REPOSITORY_ROOT / "scripts" / "dev" / "watch-traces.sh"

RUNTIME_ID = "gameagentruntime-EXAMPLE"
FRONTEND_GROUP = "/aws/ecs/default/game-agent-frontend-a1b2"
TASK_DEFINITION = "arn:aws:ecs:us-west-2:123456789012:task-definition/default-game-agent-frontend:7"

FAKE_AWS = r'''
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


def emit(value):
    """Print a value the way the AWS CLI renders it with --output text or json."""
    if option("--output") == "json":
        print(json.dumps(value, indent=4))
    elif value is None:
        print("None")
    elif isinstance(value, list):
        if value and isinstance(value[0], list):
            for row in value:
                print("\t".join("None" if item is None else str(item) for item in row))
        elif value:
            print("\t".join(str(item) for item in value))
    else:
        print(value)


command = tuple(args[:2])
query = option("--query")

if command[0] == "ecs" and state.get("ecs_error"):
    fail(254, "An error occurred (%s) when calling the %s operation" % (state["ecs_error"], command[1]))

if command == ("ecs", "describe-services"):
    if query != "services[?status=='ACTIVE'] | [0].deployments[?status=='PRIMARY'] | [0].taskDefinition":
        fail(2, "fake aws: unexpected query %r" % query)
    active = [
        service for service in state["services"]
        if service["cluster"] == option("--cluster")
        and service["name"] == option("--services")
        and service["status"] == "ACTIVE"
    ]
    emit(active[0]["task_definition"] if active else None)
elif command == ("ecs", "describe-task-definition"):
    expected = (
        "taskDefinition.containerDefinitions[?logConfiguration.logDriver=='awslogs'] "
        '| [0].logConfiguration.options."awslogs-group"'
    )
    if query != expected:
        fail(2, "fake aws: unexpected query %r" % query)
    if option("--task-definition") not in state["task_definitions"]:
        fail(254, "An error occurred (ClientException) when calling the DescribeTaskDefinition operation")
    emit(state["task_definitions"][option("--task-definition")])
elif command == ("logs", "describe-log-groups"):
    if "--log-group-name-exact" in args:
        # The AWS CLI has no such option and rejects the call before sending it.
        fail(252, "Unknown options: --log-group-name-exact")
    if state.get("logs_error"):
        fail(254, "An error occurred (%s) when calling the DescribeLogGroups operation" % state["logs_error"])
    prefix = option("--log-group-name-prefix", "")
    groups = sorted(
        (group for group in state["log_groups"] if group["logGroupName"].startswith(prefix)),
        key=lambda group: group["logGroupName"],
    )
    if option("--output") == "table":
        print("| DescribeLogGroups |")
    elif query == "logGroups[?!retentionInDays].logGroupName":
        emit([group["logGroupName"] for group in groups if not group.get("retentionInDays")])
    elif query == "logGroups[?logGroupName=='%s'].[logGroupName, retentionInDays]" % prefix:
        emit([[group["logGroupName"], group.get("retentionInDays")] for group in groups if group["logGroupName"] == prefix])
    elif query == "logGroups[].[creationTime, logGroupName]":
        emit([[group["creationTime"], group["logGroupName"]] for group in groups])
    elif query == "logGroups[0].logGroupName":
        emit(groups[0]["logGroupName"] if groups else None)
    else:
        fail(2, "fake aws: unexpected query %r" % query)
elif command == ("logs", "put-retention-policy"):
    if state.get("put_error"):
        fail(254, "An error occurred (%s) when calling the PutRetentionPolicy operation" % state["put_error"])
    for group in state["log_groups"]:
        if group["logGroupName"] == option("--log-group-name"):
            group["retentionInDays"] = int(option("--retention-in-days"))
elif command == ("logs", "tail"):
    print("2026-01-01T00:00:00 frontend ready")
else:
    fail(2, "fake aws: unexpected call %r" % (args,))
save()
'''

FAKE_YQ = f"""
print("{RUNTIME_ID}")
"""


def service(name: str = "game-agent-frontend", task_definition: str = TASK_DEFINITION) -> dict:
    return {"cluster": "default", "name": name, "status": "ACTIVE", "task_definition": task_definition}


def log_group(name: str, retention: int | None = None, created: int = 1_700_000_000_000) -> dict:
    return {"logGroupName": name, "retentionInDays": retention, "creationTime": created}


class _FakeAwsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self._write_executable("aws", FAKE_AWS)
        self._write_executable("yq", FAKE_YQ)
        self.state_path = self.tmp / "state.json"
        self.write_state()

    def _write_executable(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{body.lstrip()}", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write_state(
        self,
        *,
        services: list[dict] | None = None,
        task_definitions: dict[str, str] | None = None,
        log_groups: list[dict] | None = None,
        **flags: str,
    ) -> None:
        state = {
            "calls": [],
            "services": services or [],
            "task_definitions": task_definitions or {},
            "log_groups": log_groups or [],
            **flags,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def calls(self, service_name: str, operation: str) -> list[list[str]]:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        return [call for call in state["calls"] if call[:2] == [service_name, operation]]

    def run_script(self, script: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *args],
            env={
                "PATH": os.pathsep.join((str(self.bin), "/usr/bin", "/bin")),
                "HOME": str(self.tmp),
                "FAKE_STATE": str(self.state_path),
                "AWS_REGION": "us-west-2",
                **env,
            },
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )


class FrontendLogRetention(_FakeAwsCase):
    """setup-app-observability.sh, run by deploy.sh Step 7b."""

    def run_retention(self, **env: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(RETENTION_SCRIPT, RUNTIME_ID, **env)

    def retention_updates(self) -> list[tuple[str, str]]:
        def value(call: list[str], name: str) -> str:
            return call[call.index(name) + 1]

        return [
            (value(call, "--log-group-name"), value(call, "--retention-in-days"))
            for call in self.calls("logs", "put-retention-policy")
        ]

    def test_sets_retention_on_the_log_group_the_task_definition_names(self) -> None:
        self.write_state(
            services=[service()],
            task_definitions={TASK_DEFINITION: FRONTEND_GROUP},
            log_groups=[log_group(FRONTEND_GROUP)],
        )

        result = self.run_retention()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [(FRONTEND_GROUP, "14")])
        self.assertIn(f"set 14-day retention: {FRONTEND_GROUP}", result.stdout)

    def test_the_task_definition_log_group_is_used_even_outside_the_ecs_express_prefix(self) -> None:
        custom_group = "/example/game-agent/frontend"
        self.write_state(
            services=[service()],
            task_definitions={TASK_DEFINITION: custom_group},
            log_groups=[log_group(custom_group), log_group(FRONTEND_GROUP)],
        )

        result = self.run_retention()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [(custom_group, "14")])

    def test_falls_back_to_the_ecs_express_prefix_when_the_service_cannot_be_read(self) -> None:
        self.write_state(
            ecs_error="AccessDeniedException",
            log_groups=[log_group(FRONTEND_GROUP), log_group("/aws/ecs/default/other-app-frontend-c3d4")],
        )

        result = self.run_retention()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [(FRONTEND_GROUP, "14")])
        self.assertIn("checking /aws/ecs/default/game-agent-frontend*", result.stdout)

    def test_an_existing_retention_policy_is_kept(self) -> None:
        self.write_state(
            services=[service()],
            task_definitions={TASK_DEFINITION: FRONTEND_GROUP},
            log_groups=[log_group(FRONTEND_GROUP, retention=30)],
        )

        result = self.run_retention()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [])
        self.assertIn(f"{FRONTEND_GROUP} already has 30-day retention", result.stdout)

    def test_an_absent_frontend_log_group_is_reported_and_left_alone(self) -> None:
        cases = {
            "no frontend service": (
                {},
                "/aws/ecs/default/game-agent-frontend*",
            ),
            "service without its log group": (
                {"services": [service()], "task_definitions": {TASK_DEFINITION: FRONTEND_GROUP}},
                f"log group not found: {FRONTEND_GROUP}",
            ),
        }
        for label, (state, expected_output) in cases.items():
            with self.subTest(label):
                self.write_state(**state)

                result = self.run_retention()

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.retention_updates(), [])
                self.assertIn(expected_output, result.stdout)

    def test_project_name_selects_the_frontend_service(self) -> None:
        other_task_definition = TASK_DEFINITION.replace("game-agent", "example-game")
        other_group = "/aws/ecs/default/example-game-frontend-e5f6"
        self.write_state(
            services=[service(), service("example-game-frontend", other_task_definition)],
            task_definitions={TASK_DEFINITION: FRONTEND_GROUP, other_task_definition: other_group},
            log_groups=[log_group(FRONTEND_GROUP), log_group(other_group)],
        )

        result = self.run_retention(PROJECT_NAME="example-game")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.retention_updates(), [(other_group, "14")])

    def test_a_failed_retention_update_stops_the_script(self) -> None:
        self.write_state(
            services=[service()],
            task_definitions={TASK_DEFINITION: FRONTEND_GROUP},
            log_groups=[log_group(FRONTEND_GROUP)],
            put_error="AccessDeniedException",
        )

        result = self.run_retention()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.retention_updates(), [(FRONTEND_GROUP, "14")])
        self.assertNotIn("Application observability setup complete", result.stdout)


class WatchFrontendTraces(_FakeAwsCase):
    """watch-traces.sh --frontend."""

    def setUp(self) -> None:
        super().setUp()
        # watch-traces.sh reads backend/.bedrock_agentcore.yaml relative to itself.
        repo = self.tmp / "repo"
        (repo / "scripts" / "dev").mkdir(parents=True)
        (repo / "backend").mkdir()
        (repo / "backend" / ".bedrock_agentcore.yaml").write_text("agents: {}\n", encoding="utf-8")
        self.script = repo / "scripts" / "dev" / "watch-traces.sh"
        shutil.copy(WATCH_SCRIPT, self.script)

    def watch_frontend(self, **env: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(self.script, "--frontend", **env)

    def followed_log_groups(self) -> list[str]:
        return [call[2] for call in self.calls("logs", "tail")]

    def test_follows_the_log_group_the_task_definition_names(self) -> None:
        self.write_state(
            services=[service()],
            task_definitions={TASK_DEFINITION: FRONTEND_GROUP},
            log_groups=[log_group(FRONTEND_GROUP)],
        )

        result = self.watch_frontend()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.followed_log_groups(), [FRONTEND_GROUP])
        self.assertIn("--follow", self.calls("logs", "tail")[0])
        self.assertIn("frontend ready", result.stdout)

    def test_project_name_selects_the_frontend_service(self) -> None:
        other_task_definition = TASK_DEFINITION.replace("game-agent", "example-game")
        other_group = "/aws/ecs/default/example-game-frontend-e5f6"
        self.write_state(
            services=[service(), service("example-game-frontend", other_task_definition)],
            task_definitions={TASK_DEFINITION: FRONTEND_GROUP, other_task_definition: other_group},
            log_groups=[log_group(FRONTEND_GROUP), log_group(other_group)],
        )

        result = self.watch_frontend(PROJECT_NAME="example-game")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.followed_log_groups(), [other_group])

    def test_falls_back_to_the_newest_log_group_under_the_ecs_express_prefix(self) -> None:
        # The older group sorts first by name, so only a creation-time choice
        # selects the newer one.
        self.write_state(
            ecs_error="AccessDeniedException",
            log_groups=[
                log_group(FRONTEND_GROUP, created=1_700_000_000_200),
                log_group("/aws/ecs/default/game-agent-frontend-0000", created=1_700_000_000_100),
            ],
        )

        result = self.watch_frontend()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.followed_log_groups(), [FRONTEND_GROUP])

    def test_reports_a_missing_frontend_log_group(self) -> None:
        self.write_state()

        result = self.watch_frontend()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("/aws/ecs/default/game-agent-frontend", result.stdout)
        self.assertEqual(self.followed_log_groups(), [])

    def test_an_invalid_log_group_name_from_the_task_definition_is_not_used(self) -> None:
        # watch-traces.sh runs its tail command through eval, so a name outside
        # the CloudWatch Logs character set must never reach it.
        marker = self.tmp / "injected"
        self.write_state(
            services=[service()],
            task_definitions={TASK_DEFINITION: f'{FRONTEND_GROUP}"; touch "{marker}"; echo "'},
            log_groups=[log_group(FRONTEND_GROUP)],
        )

        result = self.watch_frontend()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.followed_log_groups(), [FRONTEND_GROUP])
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
