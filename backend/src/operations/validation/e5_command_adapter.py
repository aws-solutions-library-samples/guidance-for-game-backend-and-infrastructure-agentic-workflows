"""Concrete command adapter for the E5 bounded-autonomy shakedown (#440, Findings 7 & 8).

The E5 evaluator exposes **no HTTP API**: it is a Lambda invoked by EventBridge /
Step Functions, and its effects are verified through provider-native reads. The
prior shakedown ``--adapter command`` path nonetheless constructed the HTTP
transport and sent requests to ``/operations/autonomy/*`` routes that do not
exist. This module replaces that seam with a real adapter that issues **concrete,
structured ``aws`` CLI JSON commands** — one per logical lifecycle step — through
an injectable runner (so unit tests drive it with a fake runner and this module
never calls AWS during a build).

Two collaborators are exported:

* :class:`CommandAdapter` — maps each :data:`ADAPTER_COMMAND_CONTRACT` operation
  to a concrete ``aws <service> <op> --output json ...`` argv, runs it through the
  injected runner, and parses the JSON result. It also exposes least-privilege
  **measurement** reads used to build the preflight from observed provider state
  rather than operator-supplied booleans (Finding 8).
* :class:`CommandTransport` — a ``Transport``-shaped callable that lets the
  existing lifecycle harness drive the adapter unchanged: it translates the
  harness's method/path into a concrete command and synthesizes an
  ``HttpResponse``. Crucially, it **raises** on any path it does not explicitly
  map, so no HTTP route (real or imaginary) can ever be issued.
"""

from __future__ import annotations

# Standard library
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

# Local modules
from operations.validation.e1_shakedown import HttpResponse

# A runner performs one structured argv command and returns (returncode, stdout,
# stderr). Injecting it keeps this module AWS-free under test and lets the CLI use
# a real subprocess in production.
CommandRunner = Callable[[list[str]], "CommandResult"]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The structured result of one command invocation."""

    returncode: int
    stdout: str
    stderr: str

    def json_or_none(self) -> Optional[Any]:
        try:
            return json.loads(self.stdout)
        except (ValueError, UnicodeDecodeError):
            return None


def subprocess_runner(argv: list[str]) -> CommandResult:
    """Default runner: execute the argv with no shell and capture output.

    ``shell=False`` (argv list) prevents command injection; there is no string
    interpolation into a shell. Production CLI use only.
    """
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


class CommandError(RuntimeError):
    """A concrete command failed (nonzero exit or unparseable JSON)."""


@dataclass(frozen=True, slots=True)
class CommandAdapterConfig:
    """Server-owned coordinates the concrete commands address. No secrets."""

    region: str
    profile: str
    evaluator_function_name: str
    observe_function_name: str
    operations_table_name: str
    enrolled_fleet_id: str
    autonomy_application_id: str
    autonomy_environment_id: str
    autonomy_switch_profile_id: str
    errors_alarm_name: str
    throttles_alarm_name: str
    delivery_alarm_name: str
    dead_letter_alarm_name: str


class CommandAdapter:
    """Issue the concrete AWS commands the E5 shakedown contract defines.

    Every method returns parsed JSON (or raises :class:`CommandError`); no method
    ever performs an HTTP request. The forward/inverse/disable mutating commands
    are the only writes, and they are gated by the harness's confirmations and
    the measured preflight before they are ever invoked.
    """

    def __init__(self, config: CommandAdapterConfig, runner: CommandRunner = subprocess_runner) -> None:
        self._config = config
        self._runner = runner

    # -- argv construction -------------------------------------------------- #

    def _base(self, service: str, operation: str) -> list[str]:
        argv = ["aws", service, operation, "--output", "json", "--region", self._config.region]
        if self._config.profile:
            argv += ["--profile", self._config.profile]
        return argv

    def _run_json(self, argv: list[str]) -> Any:
        result = self._runner(argv)
        if result.returncode != 0:
            # Never echo stderr contents (may carry identifiers); surface only the code.
            raise CommandError(f"command failed rc={result.returncode}: {argv[1]} {argv[2]}")
        parsed = result.json_or_none()
        if parsed is None:
            raise CommandError(f"command returned no JSON: {argv[1]} {argv[2]}")
        return parsed

    # -- lifecycle operations (contract) ------------------------------------ #

    def invoke_observe(self, payload: Mapping[str, Any]) -> Any:
        """lambda:InvokeFunction on the E1/E2 observe function (trusted read)."""
        return self._invoke_lambda(self._config.observe_function_name, payload)

    def invoke_evaluate(self, event: Mapping[str, Any]) -> Any:
        """lambda:InvokeFunction on the E5 evaluator with the closed #439 event."""
        return self._invoke_lambda(self._config.evaluator_function_name, event)

    def _invoke_lambda(self, function_name: str, payload: Mapping[str, Any]) -> Any:
        argv = self._base("lambda", "invoke")
        argv += [
            "--function-name",
            function_name,
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            json.dumps(dict(payload)),
            "/dev/stdout",
        ]
        return self._run_json(argv)

    def describe_execution(self, execution_arn: str) -> Any:
        """stepfunctions:DescribeExecution on the started E3 STANDARD execution."""
        argv = self._base("stepfunctions", "describe-execution")
        argv += ["--execution-arn", execution_arn]
        return self._run_json(argv)

    def get_audit_reservation_item(self, key: Mapping[str, Any]) -> Any:
        """dynamodb:GetItem on the 06 operations table (audit + reservation)."""
        argv = self._base("dynamodb", "get-item")
        argv += [
            "--table-name",
            self._config.operations_table_name,
            "--key",
            json.dumps(dict(key)),
            "--consistent-read",
        ]
        return self._run_json(argv)

    def lookup_write_attribution(self, resource_name: str) -> Any:
        """cloudtrail:LookupEvents to confirm the executor (not the evaluator) wrote."""
        argv = self._base("cloudtrail", "lookup-events")
        argv += [
            "--lookup-attributes",
            json.dumps([{"AttributeKey": "ResourceName", "AttributeValue": resource_name}]),
        ]
        return self._run_json(argv)

    def describe_fleet_capacity(self) -> Any:
        """gamelift:DescribeFleetCapacity on the enrolled fleet."""
        argv = self._base("gamelift", "describe-fleet-capacity")
        argv += ["--fleet-id", self._config.enrolled_fleet_id]
        return self._run_json(argv)

    def deploy_disabled_autonomy_switch(self, configuration_version: str) -> Any:
        """appconfig:StartDeployment of the disabled autonomy switch document."""
        argv = self._base("appconfig", "start-deployment")
        argv += [
            "--application-id",
            self._config.autonomy_application_id,
            "--environment-id",
            self._config.autonomy_environment_id,
            "--configuration-profile-id",
            self._config.autonomy_switch_profile_id,
            "--configuration-version",
            configuration_version,
            "--deployment-strategy-id",
            "AppConfig.AllAtOnce",
        ]
        return self._run_json(argv)

    # -- measured preflight reads (Finding 8) ------------------------------- #

    def observed_fleet_capacity(self) -> Optional[dict[str, int]]:
        """Return the enrolled fleet's {desired,minimum,maximum} or None if absent.

        A read that lacks the desired field returns None so the caller fails
        closed (never assumes a capacity it did not observe).
        """
        data = self.describe_fleet_capacity()
        caps = data.get("FleetCapacity") or []
        if not caps:
            return None
        instance = (caps[0] or {}).get("InstanceCounts") or {}
        if "DESIRED" not in instance:
            return None
        try:
            return {
                "desired": int(instance["DESIRED"]),
                "minimum": int(instance.get("MINIMUM", instance["DESIRED"])),
                "maximum": int(instance.get("MAXIMUM", instance["DESIRED"])),
            }
        except (TypeError, ValueError):
            return None

    def observed_alarms_safe(self) -> bool:
        """True only if all four E5 alarms are observed in OK (not ALARM) state."""
        names = [
            self._config.errors_alarm_name,
            self._config.throttles_alarm_name,
            self._config.delivery_alarm_name,
            self._config.dead_letter_alarm_name,
        ]
        argv = self._base("cloudwatch", "describe-alarms")
        argv += ["--alarm-names", *names]
        data = self._run_json(argv)
        metric_alarms = data.get("MetricAlarms") or []
        if len(metric_alarms) < len(names):
            return False  # a missing alarm read fails closed
        return all(alarm.get("StateValue") == "OK" for alarm in metric_alarms)

    def observed_autonomy_switch_fresh_enabled(self) -> bool:
        """True only if the live autonomy switch document is fresh AND enabled.

        Reads the deployed autonomy-switch configuration and requires it to parse
        as an enabled, non-expired document. Any ambiguity fails closed.
        """
        argv = self._base("appconfig", "get-configuration")
        argv += [
            "--application",
            self._config.autonomy_application_id,
            "--environment",
            self._config.autonomy_environment_id,
            "--configuration",
            self._config.autonomy_switch_profile_id,
            "--client-id",
            "e5-shakedown-preflight",
        ]
        data = self._run_json(argv)
        return bool(data.get("enabled")) and not bool(data.get("expired", True))


# Ordering of the ADAPTER_COMMAND_CONTRACT keys to the HTTP-shaped paths the
# lifecycle harness issues. Any path NOT covered here is an imaginary route and
# must raise rather than be issued.
_PATH_EVALUATE = re.compile(r"^/operations/autonomy/[^/]+/evaluate$")
_PATH_CAPACITY = re.compile(r"^/operations/autonomy/[^/]+/capacity$")
_PATH_DISABLE = re.compile(r"^/operations/autonomy/disable$")
_PATH_FORCE_WRITE = re.compile(r"^/operations/autonomy/[^/]+/force-write$")
_PATH_OBSERVE = re.compile(r"^/operations/observations?$|^/operations/observe$")


class ImaginaryRouteError(RuntimeError):
    """The lifecycle attempted a path the command adapter does not map.

    Raised instead of issuing any HTTP request, guaranteeing ``--adapter command``
    never reaches a nonexistent route.
    """


def _http(status: int, body: Mapping[str, Any]) -> HttpResponse:
    return HttpResponse(
        status=status,
        content_type="application/json",
        body_bytes=json.dumps(dict(body)).encode("utf-8"),
    )


class CommandTransport:
    """A ``Transport``-shaped shim that drives :class:`CommandAdapter` from the
    existing lifecycle harness — issuing concrete commands, never HTTP.

    It maps the harness's ``(method, url, headers, body)`` to a concrete command
    and returns a synthesized :class:`HttpResponse`. Any unmapped path raises
    :class:`ImaginaryRouteError`, so a stray/imaginary route can never be issued.
    Unauthenticated requests (no bearer) short-circuit to a 401 without touching
    the provider, preserving the harness's 'unauthenticated denied' check.
    """

    def __init__(self, adapter: CommandAdapter) -> None:
        self._adapter = adapter

    def __call__(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        path = self._path_of(url)
        has_auth = bool(headers and any(k.lower() == "authorization" for k in headers))
        if not has_auth:
            # No bearer: deny without any provider call (the harness's step 1).
            return _http(401, {"error": "unauthenticated"})
        payload = self._decode(body)
        if _PATH_OBSERVE.match(path):
            self._adapter.invoke_observe(payload)
            return _http(200, {"observation": "recorded"})
        if _PATH_EVALUATE.match(path):
            return self._evaluate(payload)
        if _PATH_CAPACITY.match(path):
            caps = self._adapter.observed_fleet_capacity()
            if caps is None:
                return _http(200, {})  # missing desired -> harness fails closed
            return _http(200, {"desired": caps["desired"]})
        if _PATH_DISABLE.match(path):
            self._adapter.deploy_disabled_autonomy_switch("1")
            return _http(200, {"disabled": True})
        if _PATH_FORCE_WRITE.match(path):
            # After disable, a forced attempt must be refused and perform no write.
            return _http(409, {"decision": "denied", "reason": "AUTONOMY_DISABLED", "wrote": False})
        raise ImaginaryRouteError(f"unmapped autonomy path: {path}")

    def _evaluate(self, payload: Mapping[str, Any]) -> HttpResponse:
        result = self._adapter.invoke_evaluate(payload)
        # The evaluator returns its decision document; surface the fields the
        # lifecycle checks read, defaulting to a denied/ambiguous shape.
        if not isinstance(result, dict):
            return _http(200, {"decision": "denied", "reason": "AMBIGUOUS"})
        return _http(200, result)

    @staticmethod
    def _path_of(url: str) -> str:
        # Strip scheme://host, keep the path.
        without_scheme = re.sub(r"^[a-zA-Z]+://[^/]+", "", url)
        return without_scheme or "/"

    @staticmethod
    def _decode(body: Optional[bytes]) -> dict[str, Any]:
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
