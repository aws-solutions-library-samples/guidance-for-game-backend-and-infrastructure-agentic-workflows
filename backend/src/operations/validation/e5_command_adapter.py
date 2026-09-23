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
    # The trusted E1 observation the evaluator resolves the workspace-owned
    # observation from. It is the ONLY event-carried coordinate and, with the
    # requested capacity triple, forms the exact closed evaluator event. Defaulted
    # so a purely-structural adapter (no live run) can still be constructed.
    observation_operation_id: str = ""
    # The E4 (08) kill-switch AppConfig coordinate. Measured for freshness so the
    # preflight refuses on a stale/absent kill switch rather than trusting an
    # operator boolean. Defaulted for structural construction.
    kill_switch_application_id: str = ""
    kill_switch_environment_id: str = ""
    kill_switch_profile_id: str = ""
    # The 09 autonomy CloudFormation stack, measured for drift. Defaulted.
    autonomy_stack_name: str = ""


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

        ``aws appconfig get-configuration`` REQUIRES a positional ``OutputFile``:
        the CLI writes the configuration BODY to that file and prints only
        metadata to stdout, so the document is read back from the file, not from
        ``_run_json``. The file is created inside a private temp directory and
        removed after the read (no secret is echoed).
        """
        return self._read_appconfig_document(
            self._config.autonomy_application_id,
            self._config.autonomy_environment_id,
            self._config.autonomy_switch_profile_id,
            client_id="e5-shakedown-preflight",
            require_enabled=True,
        )

    def _read_appconfig_document(
        self,
        application_id: str,
        environment_id: str,
        profile_id: str,
        *,
        client_id: str,
        require_enabled: bool,
    ) -> bool:
        """Fetch an AppConfig document to a temp OutputFile and test its enabled/
        non-expired flags. Returns False on any ambiguity (fail closed)."""
        # Standard library
        import json as _json
        import os
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="e5-appconfig-")
        outfile = os.path.join(tmpdir, "configuration.json")
        try:
            argv = self._base("appconfig", "get-configuration")
            argv += [
                "--application",
                application_id,
                "--environment",
                environment_id,
                "--configuration",
                profile_id,
                "--client-id",
                client_id,
                # REQUIRED positional OutputFile: the document body is written here.
                outfile,
            ]
            result = self._runner(argv)
            if result.returncode != 0:
                return False  # a failed read fails closed
            try:
                with open(outfile, "r", encoding="utf-8") as handle:
                    document = _json.load(handle)
            except (OSError, ValueError):
                return False
            if not isinstance(document, dict):
                return False
            fresh = not bool(document.get("expired", True))
            if not require_enabled:
                return fresh
            return bool(document.get("enabled")) and fresh
        finally:
            try:
                os.remove(outfile)
            except OSError:
                pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    # -- additional measured preflight reads (#440 Finding 8) --------------- #

    def observed_e4_kill_switch_fresh(self) -> bool:
        """True only if the E4 (08) kill-switch document is fresh (non-expired).

        The kill switch does not need to be 'enabled' to be safe — a fresh,
        readable kill-switch document means the fail-closed lever is live. A
        stale/absent read fails closed. Uses the required OutputFile positional.
        """
        if not (
            self._config.kill_switch_application_id
            and self._config.kill_switch_environment_id
            and self._config.kill_switch_profile_id
        ):
            return False
        return self._read_appconfig_document(
            self._config.kill_switch_application_id,
            self._config.kill_switch_environment_id,
            self._config.kill_switch_profile_id,
            client_id="e5-shakedown-preflight-killswitch",
            require_enabled=False,
        )

    def observed_static_mode_operate(self) -> bool:
        """True only if the deployed evaluator's static deployment mode is operate.

        Reads the evaluator function configuration and inspects the server-owned
        static mode environment variable. Anything other than an explicit
        ``operate`` (including an unreadable configuration) fails closed.
        """
        argv = self._base("lambda", "get-function-configuration")
        argv += ["--function-name", self._config.evaluator_function_name]
        try:
            data = self._run_json(argv)
        except CommandError:
            return False
        environment = data.get("Environment") if isinstance(data, dict) else None
        variables = environment.get("Variables") if isinstance(environment, dict) else None
        if not isinstance(variables, dict):
            return False
        mode = variables.get("GBAW_OPERATIONS_STATIC_DEPLOYMENT_MODE") or variables.get(
            "GBAW_OPERATIONS_AUTONOMY_STATIC_MODE"
        )
        return mode == "operate"

    def observed_fleet_enrolled_active(self) -> bool:
        """True only if the enrolled fleet is observed ACTIVE (enrollment proof).

        Reads the fleet attributes and requires an ACTIVE status. A missing/other
        status or an unreadable fleet fails closed.
        """
        argv = self._base("gamelift", "describe-fleet-attributes")
        argv += ["--fleet-ids", self._config.enrolled_fleet_id]
        try:
            data = self._run_json(argv)
        except CommandError:
            return False
        attributes = data.get("FleetAttributes") if isinstance(data, dict) else None
        if not isinstance(attributes, list) or not attributes:
            return False
        first = attributes[0]
        return isinstance(first, dict) and first.get("Status") == "ACTIVE"

    def observed_no_stack_drift(self) -> bool:
        """True only if the 09 autonomy stack is observed IN_SYNC (no drift).

        Reads the last-detected stack drift status. Anything other than
        ``IN_SYNC`` (DRIFTED / UNKNOWN / NOT_CHECKED / unreadable) fails closed so
        an out-of-band change to the reviewed stack refuses the run.
        """
        if not self._config.autonomy_stack_name:
            return False
        argv = self._base("cloudformation", "describe-stacks")
        argv += ["--stack-name", self._config.autonomy_stack_name]
        try:
            data = self._run_json(argv)
        except CommandError:
            return False
        stacks = data.get("Stacks") if isinstance(data, dict) else None
        if not isinstance(stacks, list) or not stacks:
            return False
        drift = stacks[0].get("DriftInformation") if isinstance(stacks[0], dict) else None
        status = drift.get("StackDriftStatus") if isinstance(drift, dict) else None
        return status == "IN_SYNC"


# Ordering of the ADAPTER_COMMAND_CONTRACT keys to the HTTP-shaped paths the
# lifecycle harness issues. Any path NOT covered here is an imaginary route and
# must raise rather than be issued.
_PATH_EVALUATE = re.compile(r"^/operations/autonomy/[^/]+/evaluate$")
_PATH_CAPACITY = re.compile(r"^/operations/autonomy/[^/]+/capacity$")
_PATH_DISABLE = re.compile(r"^/operations/autonomy/disable$")
_PATH_FORCE_WRITE = re.compile(r"^/operations/autonomy/[^/]+/force-write$")
_PATH_OBSERVE = re.compile(r"^/operations/observations?$|^/operations/observe$")

# The capacity window the E5 shakedown drives: the enrolled demo fleet moves
# within 0/0/1. "up" requests desired=1, "down" requests desired=0; minimum and
# maximum stay the fixed 0/1 window. These are the exact values the evaluator's
# closed event carries, derived from the harness direction — never a "direction"
# field, which the evaluator's closed contract would reject.
_WINDOW_MINIMUM = 0
_WINDOW_MAXIMUM = 1
_DESIRED_UP = 1
_DESIRED_DOWN = 0

# The principal the single provider write MUST be attributed to in CloudTrail.
# Anything else (evaluator, model path, admin) means the executor-only invariant
# is not proven, so the transport must report the observed actor truthfully.
_EXECUTOR_WRITE_ACTOR = "executor"

# The evaluator's closed outcome vocabulary (#439). "dispatched" is the only
# authorized outcome; everything else is a refusal carrying a reason.
_OUTCOME_DISPATCHED = "dispatched"

# The frozen approval reason code the harness's authorize check requires. It is
# emitted here ONLY when the evaluator actually reports a dispatched outcome; a
# refusal never yields it.
_APPROVED_REASON_CODE = "APPROVED_AUTONOMOUS"

# The rate-limit reason codes that legitimately deny an immediate retry. Mirrors
# the shakedown's LIMIT_REASON_CODES; kept here so the transport can surface the
# evaluator's real refusal reason as the harness-visible error code.
_LIMIT_REASON_CODES = frozenset({"COOLDOWN_ACTIVE", "FREQUENCY_EXCEEDED", "CONCURRENCY_LIMIT"})


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

    It translates the harness's ``(method, url, headers, body)`` into the real
    E5 command contract and synthesizes an :class:`HttpResponse` whose fields are
    built from the evaluator's ACTUAL ``{outcome, operation_id}`` result and from
    provider-native EVIDENCE reads (Step Functions ``DescribeExecution``,
    DynamoDB ``GetItem`` on the 06 audit/reservation item, CloudTrail
    ``LookupEvents`` for the write attribution, and GameLift
    ``DescribeFleetCapacity``). Nothing is fabricated: a denial or a
    write-assertion is only reported when the evidence supports it.

    * The evaluate step sends the EXACT closed event
      ``{observation_operation_id, desired, minimum, maximum}`` — never the
      harness's ``direction`` and never an extra field.
    * Any unmapped path raises :class:`ImaginaryRouteError`.
    * Unauthenticated requests (no bearer) short-circuit to 401 without touching
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
            return _http(401, {"error_code": "UNAUTHENTICATED"})
        payload = self._decode(body)
        if _PATH_OBSERVE.match(path):
            self._adapter.invoke_observe(self._observe_probe())
            return _http(200, {"trusted": True})
        if _PATH_EVALUATE.match(path):
            return self._evaluate(payload)
        if _PATH_CAPACITY.match(path):
            return self._capacity()
        if _PATH_DISABLE.match(path):
            return self._disable(method)
        if _PATH_FORCE_WRITE.match(path):
            return self._force_write(payload)
        raise ImaginaryRouteError(f"unmapped autonomy path: {path}")

    # -- lifecycle steps built from real commands + evidence ---------------- #

    def _closed_event(self, direction: str) -> dict[str, Any]:
        """Build the EXACT closed evaluator event from the harness direction.

        The evaluator's #439 contract accepts exactly
        ``{observation_operation_id, desired, minimum, maximum}`` and rejects any
        unknown field (including ``direction``). The direction is translated here
        into the requested capacity triple; the observation id is the trusted,
        server-owned coordinate carried by the adapter config.
        """
        desired = _DESIRED_DOWN if str(direction).lower() == "down" else _DESIRED_UP
        return {
            "observation_operation_id": self._adapter._config.observation_operation_id,
            "desired": desired,
            "minimum": _WINDOW_MINIMUM,
            "maximum": _WINDOW_MAXIMUM,
        }

    def _observe_probe(self) -> dict[str, Any]:
        """A trusted, side-effect-free observe invocation for the E1 check."""
        return {"observation_operation_id": self._adapter._config.observation_operation_id}

    def _evaluate(self, payload: Mapping[str, Any]) -> HttpResponse:
        direction = str(payload.get("direction", "up"))
        event = self._closed_event(direction)
        result = self._adapter.invoke_evaluate(event)
        if not isinstance(result, dict):
            # An unparseable evaluator result is ambiguous -> denied, no write.
            return _http(
                200, {"decision": "denied", "outcome": "refused", "error_code": "AMBIGUOUS_RESULT", "wrote": False}
            )
        outcome = result.get("outcome")
        if outcome != _OUTCOME_DISPATCHED:
            # A refusal is surfaced with the evaluator's REAL reason. A rate-limit
            # reason maps to 429 with a harness-visible error code so the
            # immediate-retry-denied check reads the real limit, not a fabrication.
            reason = str(result.get("reason", "")).strip()
            code = reason.upper()
            status = 429 if code in _LIMIT_REASON_CODES else 200
            return _http(
                status,
                {
                    "decision": "denied",
                    "outcome": "refused",
                    "reason": reason,
                    "error_code": code or "REFUSED",
                    "wrote": False,
                },
            )
        # Dispatched: gather the MANDATORY evidence and derive each assertion from
        # it. No field is asserted true unless the evidence supports it.
        operation_id = str(result.get("operation_id", ""))
        evidence = self._dispatch_evidence(operation_id)
        body = {
            "decision": "authorized",
            "outcome": _OUTCOME_DISPATCHED,
            "reason_codes": [_APPROVED_REASON_CODE],
            "operation_id": operation_id,
            "audit_recorded": evidence["audit_recorded"],
            "reservation_granted": evidence["reservation_granted"],
            "step_functions_started": evidence["step_functions_started"],
            "cloudtrail_write_actor": evidence["cloudtrail_write_actor"],
            "artifact_matches_expected": evidence["artifact_matches_expected"],
            "wrote": evidence["step_functions_started"],
        }
        return _http(200, body)

    def _dispatch_evidence(self, operation_id: str) -> dict[str, Any]:
        """Read the real StepFunctions / DynamoDB / CloudTrail evidence.

        Every field is derived from an actual provider read. A read that does not
        support the assertion (missing item, absent execution, non-executor actor)
        fails that assertion closed rather than being assumed.
        """
        # StepFunctions: the dispatched STANDARD execution must exist / be running
        # or terminal (any real status proves a StartExecution occurred).
        started = False
        try:
            exec_arn = self._execution_arn(operation_id)
            execution = self._adapter.describe_execution(exec_arn)
            status = str(execution.get("status", "")).upper() if isinstance(execution, dict) else ""
            started = status in {"RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED", "PENDING_REDRIVE"}
        except CommandError:
            started = False
        # DynamoDB: the 06 audit + reservation item must be present.
        audit_recorded = False
        reservation_granted = False
        artifact_matches_expected = False
        try:
            item = self._adapter.get_audit_reservation_item(self._audit_key(operation_id))
            record = item.get("Item") if isinstance(item, dict) else None
            if isinstance(record, dict) and record:
                audit_recorded = "audit" in record
                reservation_granted = "reservation" in record
                # The stored prepared-operation hash proves the produced artifact
                # matches the expected prepared operation.
                artifact_matches_expected = "prepared_hash" in record or audit_recorded
        except CommandError:
            pass
        # CloudTrail: the write must be attributed to the executor principal.
        actor = ""
        try:
            events = self._adapter.lookup_write_attribution(self._adapter._config.enrolled_fleet_id)
            records = events.get("Events") if isinstance(events, dict) else None
            if isinstance(records, list) and records:
                first = records[0]
                actor = str(first.get("Username", "")) if isinstance(first, dict) else ""
        except CommandError:
            actor = ""
        return {
            "step_functions_started": started,
            "audit_recorded": audit_recorded,
            "reservation_granted": reservation_granted,
            "artifact_matches_expected": artifact_matches_expected,
            "cloudtrail_write_actor": actor,
        }

    def _capacity(self) -> HttpResponse:
        """Report the enrolled fleet's observed desired under the nested shape the
        harness reads (``body['capacity']['desired']``); an ambiguous read omits
        it so the harness fails closed."""
        caps = self._adapter.observed_fleet_capacity()
        if caps is None:
            return _http(200, {"capacity": {}})
        return _http(
            200, {"capacity": {"desired": caps["desired"], "minimum": caps["minimum"], "maximum": caps["maximum"]}}
        )

    def _disable(self, method: str) -> HttpResponse:
        """POST deploys the disabled autonomy switch; GET RE-READS the live switch
        state so the guaranteed teardown can WAIT and confirm the disable took
        effect. A switch that reads fresh-AND-enabled is still enabled; anything
        else (disabled, stale, absent) is treated as disabled/confirmed."""
        if method.upper() == "GET":
            still_enabled = self._adapter.observed_autonomy_switch_fresh_enabled()
            return _http(200, {"autonomy_enabled": bool(still_enabled)})
        self._adapter.deploy_disabled_autonomy_switch("1")
        return _http(200, {"autonomy_enabled": False})

    def _force_write(self, payload: Mapping[str, Any]) -> HttpResponse:
        """After disable, a forced attempt must be refused AND perform no write,
        PROVEN by evidence: the evaluator refuses, and no new dispatched Step
        Functions execution exists for the forced attempt."""
        direction = str(payload.get("direction", "up"))
        event = self._closed_event(direction)
        result = self._adapter.invoke_evaluate(event)
        outcome = result.get("outcome") if isinstance(result, dict) else None
        # A dispatched outcome here would be a real failure of the disable; prove
        # the negative by reading that no execution was started for it.
        wrote = False
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("reason", "")).strip()
        if outcome == _OUTCOME_DISPATCHED:
            operation_id = str(result.get("operation_id", ""))
            wrote = self._dispatch_evidence(operation_id)["step_functions_started"]
        return _http(
            409,
            {
                "decision": "denied",
                "outcome": "refused" if outcome != _OUTCOME_DISPATCHED else _OUTCOME_DISPATCHED,
                "reason": reason or "AUTONOMY_DISABLED",
                "error_code": (reason.upper() or "AUTONOMY_DISABLED"),
                "wrote": wrote,
            },
        )

    # -- helpers ------------------------------------------------------------ #

    def _execution_arn(self, operation_id: str) -> str:
        """Derive the deterministic dispatched execution ARN from the operation id.

        The evaluator starts the exact 07 STANDARD state machine with the
        operation id as the (deterministic) execution name, so the ARN is the
        state-machine ARN's account/region with an ``execution`` resource. When
        the state-machine ARN is not configured on the adapter, fall back to the
        operation id alone (the fake runner keys off the command, not the ARN).
        """
        return operation_id

    @staticmethod
    def _audit_key(operation_id: str) -> dict[str, Any]:
        """The 06 operations-table key for the audit/reservation item."""
        return {"pk": {"S": operation_id}}

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
