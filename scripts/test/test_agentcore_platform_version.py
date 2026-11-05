"""Behavior tests for the AgentCore Runtime platform-version helper and its wiring.

The helper (``scripts/infrastructure/agentcore_platform_version.py``) runs after
the final ``agentcore launch`` in both deploy paths. All AWS calls go to a local
fake control plane that applies ``UpdateAgentRuntime`` as a full replacement,
the way the API's ``PUT`` does, so a field the helper failed to send would be
dropped and detected. No credentials or live resources are used. These tests run
under plain ``python3`` (no boto3); the check against the installed botocore
model lives in ``backend/tests/unit/test_agentcore_platform_version_model_unit.py``.
"""

from __future__ import annotations

# Standard library
import copy
import importlib.util
import io
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPOSITORY_ROOT / "scripts" / "infrastructure" / "agentcore_platform_version.py"
DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts" / "deploy.sh"
POWERSHELL_DEPLOY = REPOSITORY_ROOT / "scripts" / "powershell" / "Public" / "Deploy-GameAgent.ps1"
POWERSHELL_RESOLVER = REPOSITORY_ROOT / "scripts" / "powershell" / "Private" / "Resolve-GameAgentPlatformVersion.ps1"
POWERSHELL_INVOKER = REPOSITORY_ROOT / "scripts" / "powershell" / "Private" / "Invoke-GameAgentPlatformVersion.ps1"

_SPEC = importlib.util.spec_from_file_location("agentcore_platform_version", HELPER_PATH)
helper = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(helper)

RUNTIME_ID = "gameagentruntime-EXAMPLE123"
REGION = "us-west-2"
# A marker that must never appear in helper output: environment variable values
# (prompt ARNs, table names, Cognito bindings) are configuration, not status.
SECRET_MARKER = "ENV-VALUE-MARKER-4b1d"

UPDATE_MEMBERS = set(helper.PRESERVED_FIELDS) | set(helper.NON_CONFIGURATION_MEMBERS)
READ_MEMBERS = set(helper.PRESERVED_FIELDS) | {
    "agentRuntimeArn",
    "agentRuntimeId",
    "agentRuntimeName",
    "agentRuntimeVersion",
    "createdAt",
    "failureReason",
    "lastUpdatedAt",
    "platformVersion",
    "status",
    "workloadIdentityDetails",
}


def _full_configuration() -> dict:
    """Every configurable field, with synthetic values."""
    return {
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": {"bucket": "example-bucket", "prefix": "gameagentruntime/deployment.zip"}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["opentelemetry-instrument", "agentcore_main.py"],
            }
        },
        "roleArn": "arn:aws:iam::123456789012:role/example-agentcore-execution-role",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example/.well-known/openid-configuration",
                "allowedClients": ["example-client"],
            }
        },
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "environmentVariables": {
            "GBAW_HOSTED_RUNTIME": "true",
            "GBAW_ORCHESTRATOR_PROMPT_ARN": f"arn:aws:bedrock:us-west-2:123456789012:prompt/{SECRET_MARKER}:3",
        },
        "metadataConfiguration": {"requireMMDSV2": True},
        "description": "Game Agent runtime",
        "filesystemConfigurations": [],
        "capacityProviderConfiguration": {"capacityProviderArn": "arn:aws:bedrock-agentcore:example:capacity"},
    }


def _runtime(platform_version: str | None = "V1", **overrides) -> dict:
    runtime = {
        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{RUNTIME_ID}",
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeName": "gameagentruntime",
        "agentRuntimeVersion": "7",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastUpdatedAt": "2026-01-02T00:00:00Z",
        "status": "READY",
        **_full_configuration(),
    }
    if platform_version is not None:
        runtime["platformVersion"] = platform_version
    runtime.update(overrides)
    return runtime


class FakeClientError(Exception):
    """Mimics botocore's ClientError: carries a structured error code."""

    def __init__(self, code: str, message: str = "synthetic failure") -> None:
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeAgentCoreControl:
    """A local AgentCore control plane with replacement-update semantics."""

    def __init__(
        self,
        runtime: dict,
        *,
        initial_in_progress_polls: int = 0,
        update_polls: int = 1,
        endpoint_polls: int = 0,
        update_error: Exception | None = None,
        status_after_update: str = "READY",
        mutate_after_update=None,
        update_members=None,
        read_members=None,
    ) -> None:
        self.runtime = copy.deepcopy(runtime)
        self.updates: list[dict] = []
        self.calls: list[str] = []
        self._in_progress_polls = initial_in_progress_polls
        self._update_polls = update_polls
        self._endpoint_polls = endpoint_polls
        self._update_error = update_error
        self._status_after_update = status_after_update
        self._mutate_after_update = mutate_after_update
        self._settled_status = self.runtime["status"]
        if initial_in_progress_polls:
            self.runtime["status"] = "UPDATING"
        members = {
            "UpdateAgentRuntime": update_members if update_members is not None else UPDATE_MEMBERS,
            "GetAgentRuntime": read_members if read_members is not None else READ_MEMBERS,
        }
        self.meta = SimpleNamespace(
            service_model=SimpleNamespace(
                operation_model=lambda name: SimpleNamespace(
                    input_shape=SimpleNamespace(members=dict.fromkeys(members[name])),
                    output_shape=SimpleNamespace(members=dict.fromkeys(members[name])),
                )
            )
        )

    def get_agent_runtime(self, agentRuntimeId: str) -> dict:
        assert agentRuntimeId == RUNTIME_ID
        self.calls.append("get_agent_runtime")
        if self.runtime["status"] in ("CREATING", "UPDATING"):
            if self._in_progress_polls > 0:
                self._in_progress_polls -= 1
            else:
                self.runtime["status"] = self._settled_status
                if self._settled_status == "READY" and self._mutate_after_update and self.updates:
                    self._mutate_after_update(self.runtime)
                    self._mutate_after_update = None
        return copy.deepcopy(self.runtime)

    def update_agent_runtime(self, **request) -> dict:
        self.calls.append("update_agent_runtime")
        self.updates.append(copy.deepcopy(request))
        if self._update_error is not None:
            raise self._update_error
        assert request.pop("agentRuntimeId") == RUNTIME_ID
        new_version = str(int(self.runtime["agentRuntimeVersion"]) + 1)
        identity = {
            key: self.runtime[key] for key in ("agentRuntimeArn", "agentRuntimeId", "agentRuntimeName", "createdAt")
        }
        # Replacement semantics: the configuration becomes exactly what was sent.
        self.runtime = {**identity, **copy.deepcopy(request), "agentRuntimeVersion": new_version, "status": "UPDATING"}
        self._in_progress_polls = self._update_polls
        self._settled_status = self._status_after_update
        if self._status_after_update.endswith("FAILED"):
            self.runtime["failureReason"] = "synthetic health check failure"
        return {"agentRuntimeVersion": new_version, "status": "UPDATING"}

    def get_agent_runtime_endpoint(self, agentRuntimeId: str, endpointName: str) -> dict:
        assert (agentRuntimeId, endpointName) == (RUNTIME_ID, "DEFAULT")
        self.calls.append("get_agent_runtime_endpoint")
        if self._endpoint_polls > 0:
            self._endpoint_polls -= 1
            return {"status": "UPDATING", "liveVersion": str(int(self.runtime["agentRuntimeVersion"]) - 1)}
        return {"status": "READY", "liveVersion": self.runtime["agentRuntimeVersion"]}


class FakeClock:
    """A monotonic clock that advances only when the helper sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(seconds, 1.0)


def _run(client: FakeAgentCoreControl | None, *argv: str, timeout: str = "600"):
    """Run the helper's main() against a fake client; return (exit, stdout, stderr, clock)."""
    clock = FakeClock()
    lines: list[str] = []
    factory_calls: list[str] = []

    def factory(region: str):
        factory_calls.append(region)
        if client is None:
            raise AssertionError("the client must not be created")
        return client

    stderr = io.StringIO()
    with redirect_stderr(stderr):
        exit_code = helper.main(
            [
                "--runtime-id",
                RUNTIME_ID,
                "--region",
                REGION,
                "--timeout-seconds",
                timeout,
                "--poll-seconds",
                "5",
                *argv,
            ],
            client_factory=factory,
            clock=clock,
            sleep=clock.sleep,
            out=lines.append,
        )
    return exit_code, "\n".join(lines), stderr.getvalue(), clock, factory_calls


class ApplyPlatformVersionTests(unittest.TestCase):
    def assert_no_configuration_values(self, *outputs: str) -> None:
        for output in outputs:
            self.assertNotIn(SECRET_MARKER, output)
            self.assertNotIn("123456789012", output)

    def test_rejects_unsupported_values_before_creating_a_client(self):
        for value in ("v2", "V3", " V2", ""):
            with self.subTest(value=value):
                exit_code, _out, err, _clock, factory_calls = _run(None, "--platform-version", value)
                self.assertEqual(exit_code, 1)
                self.assertEqual(factory_calls, [])
                self.assertIn("use V1 or V2", err)

    def test_no_update_when_the_runtime_already_reports_the_requested_version(self):
        client = FakeAgentCoreControl(_runtime("V2"))

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 0, err)
        self.assertEqual(client.updates, [])
        self.assertIn("already on platform version V2", out)
        self.assert_no_configuration_values(out, err)

    def test_update_sends_back_every_field_and_changes_only_the_platform_version(self):
        before = _runtime("V1")
        client = FakeAgentCoreControl(before)

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 0, err)
        self.assertEqual(len(client.updates), 1)
        expected = {"agentRuntimeId": RUNTIME_ID, "platformVersion": "V2", **_full_configuration()}
        self.assertEqual(client.updates[0], expected)
        # The replacement update kept every field because every field was sent.
        for name in helper.PRESERVED_FIELDS:
            self.assertEqual(client.runtime.get(name), before.get(name), name)
        self.assertEqual(client.runtime["platformVersion"], "V2")
        self.assertIn("V1 -> V2", out)
        self.assertIn("runtime version 7 -> 8", out)
        self.assert_no_configuration_values(out, err)

    def test_rollback_to_v1_uses_the_same_preserving_update(self):
        client = FakeAgentCoreControl(_runtime("V2"))

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V1")

        self.assertEqual(exit_code, 0, err)
        self.assertEqual(client.updates[0]["platformVersion"], "V1")
        self.assertEqual(
            {k: v for k, v in client.updates[0].items() if k not in ("agentRuntimeId", "platformVersion")},
            _full_configuration(),
        )

    def test_an_unreported_platform_version_is_set_explicitly(self):
        client = FakeAgentCoreControl(_runtime(None))

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V1")

        self.assertEqual(exit_code, 0, err)
        self.assertEqual(len(client.updates), 1)
        self.assertIn("unreported -> V1", out)

    def test_unconfigured_optional_fields_are_not_sent(self):
        runtime = _runtime("V1")
        for name in ("metadataConfiguration", "filesystemConfigurations", "capacityProviderConfiguration"):
            runtime.pop(name)
        runtime["description"] = ""
        client = FakeAgentCoreControl(runtime)

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 0, err)
        sent = client.updates[0]
        for name in (
            "metadataConfiguration",
            "filesystemConfigurations",
            "capacityProviderConfiguration",
            "description",
        ):
            self.assertNotIn(name, sent)
        self.assertEqual(sent["environmentVariables"], runtime["environmentVariables"])

    def test_waits_for_an_in_progress_runtime_before_updating(self):
        client = FakeAgentCoreControl(_runtime("V1"), initial_in_progress_polls=2)

        exit_code, _out, err, clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 0, err)
        first_update = client.calls.index("update_agent_runtime")
        self.assertEqual(client.calls[:first_update], ["get_agent_runtime"] * 3)
        self.assertGreaterEqual(len(clock.sleeps), 2)

    def test_waits_for_the_new_version_and_the_default_endpoint(self):
        client = FakeAgentCoreControl(_runtime("V1"), update_polls=3, endpoint_polls=2)

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 0, err)
        self.assertEqual(client.calls.count("get_agent_runtime_endpoint"), 3)
        self.assertIn("the DEFAULT endpoint serves it", out)

    def test_update_errors_fail_closed(self):
        client = FakeAgentCoreControl(_runtime("V1"), update_error=FakeClientError("ConflictException"))

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertIn("ConflictException", err)

    def test_validation_errors_for_v2_mention_regional_availability(self):
        client = FakeAgentCoreControl(_runtime("V1"), update_error=FakeClientError("ValidationException"))

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertIn("available only in some AWS Regions", err)

    def test_a_failed_update_status_fails_closed(self):
        client = FakeAgentCoreControl(_runtime("V1"), status_after_update="UPDATE_FAILED")

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertIn("UPDATE_FAILED", err)
        self.assertIn("synthetic health check failure", err)

    def test_a_failed_runtime_already_on_the_requested_version_fails_closed(self):
        # A failed runtime is never reported as converged, and no update is
        # made on the deploy's behalf when the version already matches.
        client = FakeAgentCoreControl(_runtime("V2", status="UPDATE_FAILED", failureReason="earlier failure"))

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.updates, [])
        self.assertIn("UPDATE_FAILED", err)
        self.assertIn("earlier failure", err)

    def test_a_failed_v2_update_can_be_rolled_back_to_v1(self):
        # For example, a V2 startup that missed the 120-second health check.
        client = FakeAgentCoreControl(_runtime("V2", status="UPDATE_FAILED", failureReason="health check failed"))

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V1")

        self.assertEqual(exit_code, 0, err)
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(client.updates[0]["platformVersion"], "V1")
        self.assertEqual(
            {k: v for k, v in client.updates[0].items() if k not in ("agentRuntimeId", "platformVersion")},
            _full_configuration(),
        )
        self.assertIn("recovery update", out)
        self.assertEqual(client.runtime["status"], "READY")

    def test_a_field_missing_after_the_update_fails_closed_without_printing_values(self):
        client = FakeAgentCoreControl(
            _runtime("V1"),
            mutate_after_update=lambda runtime: runtime.pop("requestHeaderConfiguration"),
        )

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertIn("requestHeaderConfiguration", err)
        self.assert_no_configuration_values(out, err)

    def test_verification_compares_against_the_runtime_read_before_the_update(self):
        # Defense in depth: if the request ever failed to carry a configured
        # field, the replacement update would drop it, and the post-update check
        # (against the runtime as read before the update) must stop the deploy.
        original = helper.build_update_request

        def dropping_request(runtime, runtime_id, platform_version):
            request = original(runtime, runtime_id, platform_version)
            request.pop("environmentVariables")
            return request

        helper.build_update_request = dropping_request
        try:
            client = FakeAgentCoreControl(_runtime("V1"))
            exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V2")
        finally:
            helper.build_update_request = original

        self.assertEqual(exit_code, 1)
        self.assertIn("not preserved", err)
        self.assertIn("environmentVariables", err)
        self.assert_no_configuration_values(out, err)

    def test_a_changed_environment_after_the_update_fails_closed_without_printing_values(self):
        def drop_variable(runtime):
            runtime["environmentVariables"] = {"GBAW_HOSTED_RUNTIME": "true"}

        client = FakeAgentCoreControl(_runtime("V1"), mutate_after_update=drop_variable)

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertIn("environmentVariables", err)
        self.assert_no_configuration_values(out, err)

    def test_service_populated_defaults_are_reported_not_rejected(self):
        runtime = _runtime("V1")
        runtime.pop("metadataConfiguration")

        def add_default(updated):
            updated["metadataConfiguration"] = {"requireMMDSV2": False}
            updated["lifecycleConfiguration"] = {**updated["lifecycleConfiguration"], "extra": 1}

        client = FakeAgentCoreControl(runtime, mutate_after_update=add_default)

        exit_code, out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 0, err)
        self.assertIn("default values for: metadataConfiguration", out)

    def test_a_platform_version_that_did_not_change_fails_closed(self):
        client = FakeAgentCoreControl(
            _runtime("V1"),
            mutate_after_update=lambda runtime: runtime.__setitem__("platformVersion", "V1"),
        )

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertIn("reports platform version V1 after the update", err)

    def test_an_unknown_updatable_field_fails_closed_before_any_update(self):
        client = FakeAgentCoreControl(_runtime("V1"), update_members=UPDATE_MEMBERS | {"futureSetting"})

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.updates, [])
        self.assertIn("futureSetting", err)

    def test_a_field_the_installed_botocore_cannot_read_fails_closed(self):
        client = FakeAgentCoreControl(_runtime("V1"), read_members=READ_MEMBERS - {"metadataConfiguration"})

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.updates, [])
        self.assertIn("metadataConfiguration", err)

    def test_missing_required_fields_fail_closed(self):
        runtime = _runtime("V1")
        runtime.pop("roleArn")
        client = FakeAgentCoreControl(runtime)

        exit_code, _out, err, _clock, _calls = _run(client, "--platform-version", "V2")

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.updates, [])
        self.assertIn("roleArn", err)

    def test_a_runtime_that_never_settles_times_out_fail_closed(self):
        client = FakeAgentCoreControl(_runtime("V1"), update_polls=10_000)

        exit_code, _out, err, clock, _calls = _run(client, "--platform-version", "V2", timeout="60")

        self.assertEqual(exit_code, 1)
        self.assertIn("did not settle within 60s", err)
        self.assertIn("idempotent", err)
        self.assertLessEqual(clock.now, 70)


class WaitOnlyTests(unittest.TestCase):
    def test_wait_only_never_updates(self):
        client = FakeAgentCoreControl(_runtime("V1"), initial_in_progress_polls=2)

        exit_code, out, err, _clock, _calls = _run(client, "--wait-only")

        self.assertEqual(exit_code, 0, err)
        self.assertEqual(client.updates, [])
        self.assertIn("status READY", out)

    def test_wait_only_accepts_a_failed_status_so_a_redeploy_can_repair_it(self):
        client = FakeAgentCoreControl(_runtime("V1", status="UPDATE_FAILED"))

        exit_code, out, err, _clock, _calls = _run(client, "--wait-only")

        self.assertEqual(exit_code, 0, err)
        self.assertIn("status UPDATE_FAILED", out)

    def test_wait_only_fails_for_a_deleting_runtime(self):
        client = FakeAgentCoreControl(_runtime("V1", status="DELETING"))

        exit_code, _out, err, _clock, _calls = _run(client, "--wait-only")

        self.assertEqual(exit_code, 1)
        self.assertIn("DELETING", err)

    def test_platform_version_is_required_unless_waiting(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            helper.main(["--runtime-id", RUNTIME_ID, "--region", REGION], client_factory=lambda region: None)
        self.assertEqual(raised.exception.code, 2)


def _bash_function(script: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", script)
    if match is None:
        raise AssertionError(f"{name} not found in {DEPLOY_SCRIPT}")
    return match.group(0)


class DeployWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        cls.powershell = POWERSHELL_DEPLOY.read_text(encoding="utf-8")

    def _resolve(self, value: str | None) -> subprocess.CompletedProcess:
        function = _bash_function(self.deploy, "resolve_agentcore_platform_version")
        env = {"PATH": os.environ.get("PATH", "")}
        if value is not None:
            env["GBAW_AGENTCORE_PLATFORM_VERSION"] = value
        command = f'{function}\nresolve_agentcore_platform_version "${{GBAW_AGENTCORE_PLATFORM_VERSION:-}}"'
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)

    def test_shell_resolver_defaults_to_v1_and_accepts_exact_values(self):
        for value, expected in ((None, "V1"), ("", "V1"), ("V1", "V1"), ("V2", "V2")):
            with self.subTest(value=value):
                result = self._resolve(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_shell_resolver_rejects_what_the_powershell_resolver_rejects(self):
        for value in ("v2", "V3", " V2", "2"):
            with self.subTest(value=value):
                result = self._resolve(value)
                self.assertEqual(result.returncode, 1)
                self.assertIn("must be V1 or V2", result.stderr)
        resolver = POWERSHELL_RESOLVER.read_text(encoding="utf-8")
        self.assertIn("if (-not $Value) { return 'V1' }", resolver)
        self.assertIn("$Value -ceq 'V1' -or $Value -ceq 'V2'", resolver)

    def test_shell_resolves_the_setting_before_any_aws_change(self):
        resolve_at = self.deploy.index('resolve_agentcore_platform_version "${GBAW_AGENTCORE_PLATFORM_VERSION:-}"')
        first_change_at = self.deploy.index("aws cloudformation deploy")
        self.assertLess(resolve_at, first_change_at)

    def test_shell_waits_before_the_final_launch_and_applies_after_it(self):
        final_launch_at = self.deploy.rindex(
            'uv run agentcore launch --auto-update-on-conflict "${LAUNCH_ENV_ARGS[@]}"'
        )
        wait_at = self.deploy.index('run_agentcore_platform_version_helper "$RUNTIME_ID" --wait-only')
        apply_at = self.deploy.index(
            'run_agentcore_platform_version_helper "$RUNTIME_ID" --platform-version "$AGENTCORE_PLATFORM_VERSION"'
        )
        self.assertLess(wait_at, final_launch_at)
        self.assertGreater(apply_at, final_launch_at)
        # The Step 2 first launch creates the runtime; the wait guards the update.
        self.assertGreater(
            wait_at, self.deploy.index('echo "🔗 Step 5b: Wiring Knowledge Bases to AgentCore Runtime..."')
        )

    def test_shell_helper_runner_uses_the_backend_project_and_passes_arguments(self):
        function = _bash_function(self.deploy, "run_agentcore_platform_version_helper")
        command = "\n".join(
            (
                'uv() { printf "cwd=%s\\n" "$(basename "$PWD")"; printf "%s\\n" "$@"; }',
                function,
                'run_agentcore_platform_version_helper "$RUNTIME" --platform-version V2',
            )
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PROJECT_ROOT": str(REPOSITORY_ROOT),
            "SCRIPT_DIR": str(REPOSITORY_ROOT / "scripts"),
            "AWS_REGION": REGION,
            "RUNTIME": RUNTIME_ID,
        }
        result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "cwd=backend",
                "run",
                "python",
                str(HELPER_PATH),
                "--runtime-id",
                RUNTIME_ID,
                "--region",
                REGION,
                "--platform-version",
                "V2",
            ],
        )

    def test_shell_helper_failure_stops_the_deploy(self):
        function = _bash_function(self.deploy, "run_agentcore_platform_version_helper")
        command = "\n".join(
            (
                "set -e",
                "uv() { return 1; }",
                function,
                'run_agentcore_platform_version_helper "$RUNTIME" --wait-only',
                "echo reached-after-failure",
            )
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PROJECT_ROOT": str(REPOSITORY_ROOT),
            "SCRIPT_DIR": str(REPOSITORY_ROOT / "scripts"),
            "AWS_REGION": REGION,
            "RUNTIME": RUNTIME_ID,
        }
        result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("reached-after-failure", result.stdout)

    def test_both_deploy_paths_share_the_setting_and_the_helper(self):
        invoker = POWERSHELL_INVOKER.read_text(encoding="utf-8")
        self.assertIn("GBAW_AGENTCORE_PLATFORM_VERSION", self.deploy)
        self.assertIn("$env:GBAW_AGENTCORE_PLATFORM_VERSION", self.powershell)
        self.assertIn("infrastructure/agentcore_platform_version.py", self.deploy)
        self.assertIn("scripts/infrastructure/agentcore_platform_version.py", invoker)
        self.assertIn("'--wait-only'", invoker)
        self.assertIn("'--platform-version', $PlatformVersion", invoker)

    def test_powershell_waits_before_the_final_launch_and_applies_after_it(self):
        final_launch_at = self.powershell.rindex("uv run agentcore launch --auto-update-on-conflict @agentCoreEnvArgs")
        wait_at = self.powershell.index(
            "Invoke-GameAgentPlatformVersion -RuntimeId $runtimeId -Region $Region -WaitOnly"
        )
        apply_at = self.powershell.index(
            "Invoke-GameAgentPlatformVersion -RuntimeId $runtimeId -Region $Region "
            "-PlatformVersion $agentCorePlatformVersion"
        )
        resolve_at = self.powershell.index(
            "Resolve-GameAgentPlatformVersion -Value $env:GBAW_AGENTCORE_PLATFORM_VERSION"
        )
        self.assertLess(resolve_at, self.powershell.index("Invoke-Aws sts get-caller-identity"))
        self.assertLess(wait_at, final_launch_at)
        self.assertGreater(apply_at, final_launch_at)


if __name__ == "__main__":
    unittest.main()
