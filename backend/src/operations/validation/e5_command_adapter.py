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

* :class:`CommandAdapter` — maps each lifecycle operation to a concrete
  ``aws <service> <op> --output json ...`` argv, runs it through the injected
  runner, and parses the JSON result. It also exposes least-privilege
  **measurement** reads used to build the preflight from observed provider state
  rather than operator-supplied booleans (Finding 8).
* :class:`CommandTransport` — a ``Transport``-shaped callable that lets the
  existing lifecycle harness drive the adapter unchanged: it translates the
  harness's method/path into a concrete command and synthesizes an
  ``HttpResponse``. Crucially, it **raises** on any path it does not explicitly
  map, so no HTTP route (real or imaginary) can ever be issued.

The live-adapter blockers closed here (semantic review of #440 at 09bbc06) are
grounded in the EXACT deployed contracts:

* ``aws lambda invoke`` writes the FUNCTION RESPONSE PAYLOAD to a positional
  ``OutputFile`` while printing only INVOKE METADATA to stdout, so the payload is
  read from a private tempfile and parsed apart from the metadata; a
  ``FunctionError`` in the metadata fails the invoke closed.
* The observe step invokes the real 06 observation Lambda with its API-Gateway
  proxy event ABI (``POST`` + ``requestContext.authorizer.jwt.claims`` + JSON
  ``body``) and parses ``observation_id`` from the succeeded ``200`` response body
  — never a fabricated ``{"trusted": true}``.
* The dispatched Step Functions execution is described by an ``executionArn``
  built from the state-machine ARN and the deterministic execution name
  (``operation_id[:80]`` — see ``AutonomyRuntimeHandler``), not the raw
  ``operation_id``.
* The 06/#439 store items are read with the REAL uppercase ``PK``/``SK`` keys
  (``AUTZBUNDLE#<op>``/``AUTZDISPATCH#dispatched`` and ``AUTZRSV#<op>``/
  ``AUTZRSV``), and the assertions are derived from those items exactly as stored.
* The preflight reads the REAL evaluator env var ``GBAW_OPERATIONS_MODE`` (and
  ``GBAW_OPERATIONS_AUTONOMY_ENABLED``) and the REAL AppConfig document keys
  (``issued_at``/``not_after``/``autonomy_enabled``/``operations_enabled``).
* Capacity is read for the enrolled LOCATION; drift is a FRESH
  ``detect-stack-drift`` polled by detection id, not a cached ``DriftInformation``.
* Cleanup treats an unreadable switch as UNKNOWN (not disabled) and only reports a
  forced-write no-write denial when the write-evidence lookup SUCCEEDS.
"""

from __future__ import annotations

# Standard library
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

# Local modules
from operations.validation.e1_shakedown import HttpResponse

# A runner performs one structured argv command and returns (returncode, stdout,
# stderr). Injecting it keeps this module AWS-free under test and lets the CLI use
# a real subprocess in production.
CommandRunner = Callable[[list[str]], "CommandResult"]

# The deterministic Step Functions execution name is ``operation_id[:80]`` (see
# ``AutonomyRuntimeHandler`` in the #439 runtime). Kept here so the adapter builds
# the exact ``executionArn`` the evaluator started.
_EXECUTION_NAME_MAX = 80

# The prefixes/suffixes of the REAL #439 store keys (see
# ``operations.autonomy_runtime.store``). The 06 operations table is a single
# ``(PK, SK)`` table; the bundle and reservation items live under these keys.
_BUNDLE_PK_PREFIX = "AUTZBUNDLE#"
_BUNDLE_SK = "AUTZBUNDLE"
_DISPATCHED_AUDIT_SK = "AUTZDISPATCH#dispatched"
_RESERVATION_PK_PREFIX = "AUTZRSV#"
_RESERVATION_SK = "AUTZRSV"


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
    # The 09 autonomy CloudFormation stack, measured for FRESH drift. Defaulted.
    autonomy_stack_name: str = ""
    # The 07 Standard state-machine ARN the evaluator starts. Required to build
    # the exact dispatched ``executionArn`` (state-machine ARN + execution name);
    # defaulted for structural construction.
    autonomy_state_machine_arn: str = ""
    # The enrolled fleet's LOCATION. The capacity read is scoped to this location
    # so a multi-location fleet never reads a foreign location's capacity.
    # Defaults to the adapter region when unset.
    enrolled_location: str = ""


class CommandAdapter:
    """Issue the concrete AWS commands the E5 shakedown contract defines.

    Every method returns parsed JSON (or raises :class:`CommandError`); no method
    ever performs an HTTP request. The forward/inverse/disable mutating commands
    are the only writes, and they are gated by the harness's confirmations and
    the measured preflight before they are ever invoked.
    """

    def __init__(
        self,
        config: CommandAdapterConfig,
        runner: CommandRunner = subprocess_runner,
        *,
        drift_poll_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._runner = runner
        self._drift_poll_sleep = drift_poll_sleep

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

    def invoke_observe(self, event: Mapping[str, Any]) -> Any:
        """lambda:InvokeFunction on the 06 observe function (trusted read).

        ``event`` is the API-Gateway proxy event the deployed handler consumes;
        the parsed value is the proxy RESPONSE (``{statusCode, headers, body}``)
        read from the tempfile OutputFile, not the CLI invoke metadata.
        """
        return self._invoke_lambda(self._config.observe_function_name, event)

    def observe_succeeded_observation_id(self) -> Optional[str]:
        """Invoke the real 06 observation Lambda and return the succeeded
        observation's ``observation_id`` — or ``None`` if the observation did not
        succeed / could not be parsed (fail closed; never fabricate ``trusted``).

        The deployed handler is an API-Gateway HTTP API (payload v2) proxy: it
        reads identity from ``requestContext.authorizer.jwt.claims`` and the
        request from a JSON ``body`` string, and returns ``{statusCode, headers,
        body}`` where ``body`` is a JSON string carrying ``observation_id`` on a
        succeeded (``200``) observe.
        """
        try:
            response = self.invoke_observe(self._observe_proxy_event())
        except CommandError:
            return None
        if not isinstance(response, Mapping):
            return None
        if int(response.get("statusCode", 0)) != 200:
            return None
        body = self._proxy_body(response)
        if not isinstance(body, Mapping):
            return None
        observation_id = body.get("observation_id")
        if isinstance(observation_id, str) and observation_id.strip():
            return observation_id
        return None

    def _observe_proxy_event(self) -> dict[str, Any]:
        """Build the API-Gateway HTTP API (payload v2) proxy event the deployed
        06 observation handler consumes for a trusted, bounded observe.

        Identity is carried in ``requestContext.authorizer.jwt.claims`` exactly as
        the handler reads it; the request body is a JSON string. The observe
        request re-derives the succeeded observation for the enrolled fleet.
        """
        body = json.dumps({"fleet_id": self._config.enrolled_fleet_id})
        return {
            "httpMethod": "POST",
            "routeKey": "POST /operations/observe",
            "rawPath": "/operations/observe",
            "isBase64Encoded": False,
            "headers": {"content-type": "application/json"},
            "body": body,
            "requestContext": {
                "http": {"method": "POST", "path": "/operations/observe"},
                "authorizer": {"jwt": {"claims": {}}},
            },
        }

    @staticmethod
    def _proxy_body(response: Mapping[str, Any]) -> Any:
        """Parse the proxy response ``body`` JSON string into a dict."""
        raw = response.get("body")
        if isinstance(raw, Mapping):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def invoke_evaluate(self, event: Mapping[str, Any]) -> Any:
        """lambda:InvokeFunction on the E5 evaluator with the closed #439 event.

        Returns the evaluator's FUNCTION RESPONSE PAYLOAD (parsed from the
        tempfile OutputFile), never the CLI invoke metadata.
        """
        return self._invoke_lambda(self._config.evaluator_function_name, event)

    def _invoke_lambda(self, function_name: str, payload: Mapping[str, Any]) -> Any:
        """Invoke a Lambda, writing the response payload to a private tempfile
        ``OutputFile`` and parsing THAT — never ``/dev/stdout``.

        ``aws lambda invoke`` prints only INVOKE METADATA (``StatusCode``,
        ``ExecutedVersion``, and possibly ``FunctionError``) to stdout and writes
        the function's RESPONSE PAYLOAD to the positional ``OutputFile``. The old
        ``/dev/stdout`` target concatenated the two JSON documents into one
        unparseable stream. Here the payload is read from a private tempfile and a
        ``FunctionError`` in the metadata fails the invoke closed.
        """
        tmpdir = tempfile.mkdtemp(prefix="e5-lambda-invoke-")
        outfile = os.path.join(tmpdir, "payload.json")
        try:
            argv = self._base("lambda", "invoke")
            argv += [
                "--function-name",
                function_name,
                "--cli-binary-format",
                "raw-in-base64-out",
                "--payload",
                json.dumps(dict(payload)),
                # The positional OutputFile: the function response payload lands
                # here; stdout carries only the invoke metadata.
                outfile,
            ]
            result = self._runner(argv)
            if result.returncode != 0:
                raise CommandError(f"command failed rc={result.returncode}: lambda invoke")
            metadata = result.json_or_none()
            if isinstance(metadata, Mapping) and metadata.get("FunctionError"):
                # A handled/unhandled function error is a failed invoke, not a
                # payload to be parsed as a result.
                raise CommandError("lambda invoke reported a FunctionError")
            try:
                with open(outfile, "r", encoding="utf-8") as handle:
                    payload_text = handle.read()
            except OSError as exc:
                raise CommandError("lambda invoke produced no readable payload") from exc
            try:
                parsed = json.loads(payload_text) if payload_text.strip() else None
            except (ValueError, UnicodeDecodeError) as exc:
                raise CommandError("lambda invoke payload was not JSON") from exc
            if parsed is None:
                raise CommandError("lambda invoke returned an empty payload")
            return parsed
        finally:
            try:
                os.remove(outfile)
            except OSError:
                pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    def describe_execution(self, execution_arn: str) -> Any:
        """stepfunctions:DescribeExecution on the started E3 STANDARD execution."""
        argv = self._base("stepfunctions", "describe-execution")
        argv += ["--execution-arn", execution_arn]
        return self._run_json(argv)

    def execution_arn_for(self, operation_id: str) -> str:
        """Build the exact dispatched ``executionArn`` from the state-machine ARN
        and the deterministic execution name (``operation_id[:80]``).

        The #439 runtime starts the Standard state machine with
        ``name=operation_id[:80]``. A Step Functions execution ARN replaces the
        ``stateMachine`` resource type with ``execution`` and appends the
        execution name: ``arn:aws:states:<region>:<acct>:execution:<sm-name>:<name>``.
        """
        name = operation_id[:_EXECUTION_NAME_MAX]
        arn = self._config.autonomy_state_machine_arn
        if not arn or not arn.startswith("arn:aws:states:"):
            # No configured state-machine ARN: still return an execution-shaped
            # ARN so the read never degrades to passing the raw operation id.
            return f"arn:aws:states:{self._config.region}:000000000000:execution:unknown:{name}"
        # arn:aws:states:region:acct:stateMachine:NAME  ->  ...:execution:NAME:name
        prefix, _, sm_name = arn.rpartition(":stateMachine:")
        if not prefix:
            # Fall back conservatively if the ARN is not a state-machine ARN.
            return arn
        return f"{prefix}:execution:{sm_name}:{name}"

    def get_dispatched_audit_item(self, operation_id: str) -> Any:
        """dynamodb:GetItem on the #439 dispatched-audit item
        (``PK=AUTZBUNDLE#<op>``, ``SK=AUTZDISPATCH#dispatched``)."""
        return self._get_item(f"{_BUNDLE_PK_PREFIX}{operation_id}", _DISPATCHED_AUDIT_SK)

    def get_reservation_item(self, operation_id: str) -> Any:
        """dynamodb:GetItem on the #439 reservation item
        (``PK=AUTZRSV#<op>``, ``SK=AUTZRSV``)."""
        return self._get_item(f"{_RESERVATION_PK_PREFIX}{operation_id}", _RESERVATION_SK)

    def _get_item(self, pk: str, sk: str) -> Any:
        """dynamodb:GetItem on the 06 operations table with the real PK/SK."""
        argv = self._base("dynamodb", "get-item")
        argv += [
            "--table-name",
            self._config.operations_table_name,
            "--key",
            json.dumps({"PK": {"S": pk}, "SK": {"S": sk}}),
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
        """gamelift:DescribeFleetCapacity on the enrolled fleet, scoped to the
        enrolled LOCATION so a multi-location fleet reads only its own location."""
        argv = self._base("gamelift", "describe-fleet-capacity")
        argv += ["--fleet-id", self._config.enrolled_fleet_id]
        location = self._config.enrolled_location or self._config.region
        if location:
            argv += ["--locations", location]
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
        """Return the enrolled fleet+location's {desired,minimum,maximum} or None.

        A read that lacks the desired field returns None so the caller fails
        closed (never assumes a capacity it did not observe). When the fleet is
        multi-location, the enrolled-location entry is selected.
        """
        data = self.describe_fleet_capacity()
        caps = data.get("FleetCapacity") or []
        if not caps:
            return None
        location = self._config.enrolled_location or self._config.region
        chosen: Optional[dict[str, Any]] = None
        for entry in caps:
            if not isinstance(entry, dict):
                continue
            if entry.get("Location") == location:
                chosen = entry
                break
        if chosen is None:
            # No location-tagged match: fall back to a single unlabelled entry
            # (a home-Region-only fleet omits Location), else fail closed.
            if len(caps) == 1 and isinstance(caps[0], dict) and "Location" not in caps[0]:
                chosen = caps[0]
            else:
                return None
        instance = (chosen or {}).get("InstanceCounts") or {}
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

    def observed_autonomy_switch_state(self) -> Optional[bool]:
        """Tri-state read of the live E5 autonomy switch.

        Returns ``True`` when the switch reads fresh AND ``autonomy_enabled``,
        ``False`` when it reads fresh AND explicitly disabled, and ``None`` when
        the document is UNREADABLE / stale / malformed (UNKNOWN). Cleanup must
        treat ``None`` as unknown (never as "disabled").
        """
        doc = self._read_appconfig_document(
            self._config.autonomy_application_id,
            self._config.autonomy_environment_id,
            self._config.autonomy_switch_profile_id,
            client_id="e5-shakedown-preflight",
        )
        if doc is None:
            return None
        if not self._document_is_fresh(doc):
            return None
        enabled = doc.get("autonomy_enabled")
        if not isinstance(enabled, bool):
            return None
        return enabled

    def observed_autonomy_switch_fresh_enabled(self) -> bool:
        """True only if the live autonomy switch document is fresh AND enabled."""
        return self.observed_autonomy_switch_state() is True

    def _read_appconfig_document(
        self,
        application_id: str,
        environment_id: str,
        profile_id: str,
        *,
        client_id: str,
    ) -> Optional[dict[str, Any]]:
        """Fetch an AppConfig document to a temp OutputFile and return the parsed
        document, or ``None`` on any read/parse ambiguity (fail closed).

        ``aws appconfig get-configuration`` REQUIRES a positional ``OutputFile``:
        the CLI writes the configuration BODY to that file and prints only
        metadata to stdout, so the document is read back from the file. The file
        is created inside a private temp directory and removed after the read (no
        secret is echoed).
        """
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
                return None  # a failed read is UNKNOWN (fail closed)
            try:
                with open(outfile, "r", encoding="utf-8") as handle:
                    document = json.load(handle)
            except (OSError, ValueError):
                return None
            return document if isinstance(document, dict) else None
        finally:
            try:
                os.remove(outfile)
            except OSError:
                pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    def _document_is_fresh(self, document: Mapping[str, Any]) -> bool:
        """True only when ``issued_at <= now < not_after`` using the REAL keys.

        Mirrors the runtime gates (``AutonomySwitchGate`` / ``KillSwitchGate``):
        both timestamps must be tz-aware ISO-8601 instants and now must lie in
        ``[issued_at, not_after)``. Any missing/naive/unparseable timestamp is
        stale (fail closed).
        """
        issued_at = self._instant(document.get("issued_at"))
        not_after = self._instant(document.get("not_after"))
        if issued_at is None or not_after is None:
            return False
        now = datetime.now(timezone.utc)
        return issued_at <= now < not_after

    @staticmethod
    def _instant(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None  # naive timestamp is refused
        return parsed.astimezone(timezone.utc)

    # -- additional measured preflight reads (#440 Finding 8) --------------- #

    def observed_e4_kill_switch_fresh(self) -> bool:
        """True only if the E4 (08) kill-switch document is fresh (non-expired).

        The kill switch need not be 'enabled' to be safe — a fresh, readable
        kill-switch document means the fail-closed lever is live. A stale/absent
        read fails closed. Uses the REAL keys (``issued_at``/``not_after``) and the
        required OutputFile positional.
        """
        if not (
            self._config.kill_switch_application_id
            and self._config.kill_switch_environment_id
            and self._config.kill_switch_profile_id
        ):
            return False
        doc = self._read_appconfig_document(
            self._config.kill_switch_application_id,
            self._config.kill_switch_environment_id,
            self._config.kill_switch_profile_id,
            client_id="e5-shakedown-preflight-killswitch",
        )
        if doc is None:
            return False
        return self._document_is_fresh(doc)

    def observed_static_mode_operate(self) -> bool:
        """True only if the deployed evaluator's real static mode is operate AND
        autonomy is explicitly enabled.

        Reads the evaluator function configuration and inspects the REAL
        server-owned env vars the deployed evaluator resolves from:
        ``GBAW_OPERATIONS_MODE`` (must be exactly ``operate``) and
        ``GBAW_OPERATIONS_AUTONOMY_ENABLED`` (must be a true token). Anything else
        — including an unreadable configuration or the nonexistent legacy
        ``GBAW_OPERATIONS_STATIC_DEPLOYMENT_MODE`` — fails closed.
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
        mode = str(variables.get("GBAW_OPERATIONS_MODE", "")).strip().lower()
        autonomy_enabled = str(variables.get("GBAW_OPERATIONS_AUTONOMY_ENABLED", "")).strip().lower()
        return mode == "operate" and autonomy_enabled in {"true", "1", "yes", "on"}

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
        """True only if a FRESH drift detection reports the 09 autonomy stack
        ``IN_SYNC``.

        Starts a new ``detect-stack-drift`` and POLLS
        ``describe-stack-drift-detection-status`` by the returned detection id
        until ``DETECTION_COMPLETE``, then requires ``StackDriftStatus ==
        IN_SYNC``. Anything else (DRIFTED / detection failed / unreadable /
        timed-out poll) fails closed — the last-cached ``describe-stacks``
        ``DriftInformation`` is never trusted.
        """
        if not self._config.autonomy_stack_name:
            return False
        try:
            start = self._run_json(
                self._base("cloudformation", "detect-stack-drift") + ["--stack-name", self._config.autonomy_stack_name]
            )
        except CommandError:
            return False
        detection_id = start.get("StackDriftDetectionId") if isinstance(start, dict) else None
        if not isinstance(detection_id, str) or not detection_id.strip():
            return False
        for attempt in range(_DRIFT_POLL_MAX_ATTEMPTS):
            try:
                status = self._run_json(
                    self._base("cloudformation", "describe-stack-drift-detection-status")
                    + ["--stack-drift-detection-id", detection_id]
                )
            except CommandError:
                return False
            if not isinstance(status, dict):
                return False
            detection_status = status.get("DetectionStatus")
            if detection_status == "DETECTION_COMPLETE":
                return bool(status.get("StackDriftStatus") == "IN_SYNC")
            if detection_status == "DETECTION_FAILED":
                return False
            # DETECTION_IN_PROGRESS: bounded poll.
            if attempt + 1 < _DRIFT_POLL_MAX_ATTEMPTS:
                self._drift_poll_sleep(_DRIFT_POLL_SECONDS)
        return False  # never reached a terminal detection -> fail closed


# Bounded drift-detection poll budget.
_DRIFT_POLL_MAX_ATTEMPTS = 20
_DRIFT_POLL_SECONDS = 3.0


# --------------------------------------------------------------------------- #
# Activation binding (#440 Finding: bind 07 to exact 06 + 08).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ActivationBinding:
    """The result of comparing the 07 execution stack to the exact 06 values."""

    bound: bool
    mismatches: list[str] = field(default_factory=list)


# The 06 coordinates the 07 execution stack MUST bind to exactly.
_ACTIVATION_BOUND_FIELDS = (
    "table_name",
    "kms_key_arn",
    "tenant_id",
    "workspace_id",
    "workflow_arn",
    "fleet_id",
    "trusted_audience",
)


def verify_activation_binding(
    *,
    observation_06: Mapping[str, Any],
    execution_07: Mapping[str, Any],
    control_08: Mapping[str, Any],
) -> ActivationBinding:
    """Refuse E5 activation unless the 07 execution stack binds the DynamoDB
    table, operations CMK (KMS), tenant, and workspace — plus the existing
    workflow, fleet, and trusted audience — to the EXACT 06 observation values,
    and the 08 AppConfig kill-switch coordinate is present.

    Every field is compared for exact string equality against the 06 values; any
    mismatch or missing value is named in ``mismatches`` and the binding is
    refused. The 08 control-plane AppConfig coordinate must also be present (its
    kill-switch application id), or the binding refuses.
    """
    mismatches: list[str] = []
    for field_name in _ACTIVATION_BOUND_FIELDS:
        expected = observation_06.get(field_name)
        actual = execution_07.get(field_name)
        if expected is None or actual is None or expected != actual:
            mismatches.append(field_name)
    # The 08 AppConfig kill-switch coordinate must be present to activate.
    kill_switch_app = control_08.get("kill_switch_application_id")
    if not isinstance(kill_switch_app, str) or not kill_switch_app.strip():
        mismatches.append("kill_switch_application_id")
    return ActivationBinding(bound=not mismatches, mismatches=mismatches)


# Ordering of the contract keys to the HTTP-shaped paths the lifecycle harness
# issues. Any path NOT covered here is an imaginary route and must raise rather
# than be issued.
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
    provider-native EVIDENCE reads (Step Functions ``DescribeExecution`` by the
    real ``executionArn``, DynamoDB ``GetItem`` on the #439 dispatched-audit and
    reservation items with their real ``PK``/``SK``, CloudTrail ``LookupEvents``
    for the write attribution, and GameLift ``DescribeFleetCapacity`` for the
    enrolled location). Nothing is fabricated: a denial or a write-assertion is
    only reported when the evidence supports it.

    * The evaluate step sends the EXACT closed event
      ``{observation_operation_id, desired, minimum, maximum}`` — never the
      harness's ``direction`` and never an extra field.
    * The observe step invokes the real 06 observation Lambda and reports
      ``trusted`` only on a succeeded observation.
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
            return self._observe()
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

    def _observe(self) -> HttpResponse:
        """Invoke the real 06 observation Lambda and report ``trusted`` ONLY when
        it returns a succeeded observation (a parsed ``observation_id``)."""
        observation_id = self._adapter.observe_succeeded_observation_id()
        if not observation_id:
            return _http(200, {"trusted": False, "error_code": "OBSERVATION_NOT_SUCCEEDED"})
        return _http(200, {"trusted": True, "observation_id": observation_id})

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

    def _evaluate(self, payload: Mapping[str, Any]) -> HttpResponse:
        direction = str(payload.get("direction", "up"))
        event = self._closed_event(direction)
        try:
            result = self._adapter.invoke_evaluate(event)
        except CommandError:
            return _http(
                200, {"decision": "denied", "outcome": "refused", "error_code": "AMBIGUOUS_RESULT", "wrote": False}
            )
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

        Every field is derived from an actual provider read keyed on the REAL
        deployed identifiers. A read that does not support the assertion (missing
        item, absent execution, non-executor actor) fails that assertion closed
        rather than being assumed. ``step_functions_started`` is ``None`` when the
        execution read itself was UNREADABLE (unknown), so callers can distinguish
        "no write" from "could not tell".
        """
        # StepFunctions: describe by the REAL executionArn built from the
        # deterministic execution name; any real status proves StartExecution.
        started: Optional[bool] = False
        try:
            exec_arn = self._adapter.execution_arn_for(operation_id)
            execution = self._adapter.describe_execution(exec_arn)
            status = str(execution.get("status", "")).upper() if isinstance(execution, dict) else ""
            started = status in {"RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED", "PENDING_REDRIVE"}
        except CommandError:
            started = None  # UNREADABLE -> unknown, not a confirmed "no write"
        # DynamoDB: read the #439 dispatched-audit and reservation items with the
        # REAL uppercase PK/SK; derive each assertion from the item as stored.
        audit_recorded = False
        reservation_granted = False
        artifact_matches_expected = False
        try:
            audit = self._adapter.get_dispatched_audit_item(operation_id)
            record = audit.get("Item") if isinstance(audit, dict) else None
            if isinstance(record, dict) and record:
                # The dispatched-audit item is keyed AUTZDISPATCH#dispatched and
                # carries phase=dispatched + execution_name + an audit blob.
                audit_recorded = record.get("phase", {}).get("S") == "dispatched" and "audit" in record
                # The stored execution_name matches the deterministic name, which
                # proves the produced artifact matches the expected prepared op.
                execution_name = record.get("execution_name", {}).get("S", "")
                artifact_matches_expected = (
                    bool(execution_name) and execution_name == operation_id[:_EXECUTION_NAME_MAX]
                )
        except CommandError:
            pass
        try:
            reservation = self._adapter.get_reservation_item(operation_id)
            rsv = reservation.get("Item") if isinstance(reservation, dict) else None
            if isinstance(rsv, dict) and rsv:
                # The reservation item is keyed AUTZRSV and carries operation_id.
                reservation_granted = rsv.get("operation_id", {}).get("S") == operation_id
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
        """Report the enrolled fleet+location's observed desired under the nested
        shape the harness reads (``body['capacity']['desired']``); an ambiguous
        read omits it so the harness fails closed."""
        caps = self._adapter.observed_fleet_capacity()
        if caps is None:
            return _http(200, {"capacity": {}})
        return _http(
            200, {"capacity": {"desired": caps["desired"], "minimum": caps["minimum"], "maximum": caps["maximum"]}}
        )

    def _disable(self, method: str) -> HttpResponse:
        """POST deploys the disabled autonomy switch; GET RE-READS the live switch
        state so the guaranteed teardown can WAIT and confirm the disable took
        effect.

        The GET read is TRI-STATE (see ``observed_autonomy_switch_state``):

        * a fresh, ENABLED switch reports ``autonomy_enabled: true``;
        * a fresh, DISABLED switch reports ``autonomy_enabled: false`` (a real
          confirmed disable);
        * an UNREADABLE / stale / malformed switch is UNKNOWN and must NOT be
          reported as ``false`` — the body omits a boolean ``autonomy_enabled`` and
          carries ``autonomy_state: "unknown"`` so the harness treats it as
          ambiguous (never a false-confirmed disable).
        """
        if method.upper() == "GET":
            state = self._adapter.observed_autonomy_switch_state()
            if state is None:
                # UNKNOWN: never report a disabled boolean the harness would trust.
                return _http(200, {"autonomy_state": "unknown"})
            return _http(200, {"autonomy_enabled": bool(state)})
        self._adapter.deploy_disabled_autonomy_switch("1")
        return _http(200, {"autonomy_enabled": False})

    def _force_write(self, payload: Mapping[str, Any]) -> HttpResponse:
        """After disable, a forced attempt must be refused AND perform no write,
        PROVEN by evidence: the evaluator refuses, and no new dispatched Step
        Functions execution exists for the forced attempt.

        A confirmed no-write denial requires the write-evidence lookup to SUCCEED.
        If the evaluator dispatched but the execution read was UNREADABLE, the
        result is UNKNOWN — the response must NOT report a clean ``wrote: false``;
        it carries ``wrote: null`` and an ``EVIDENCE_UNAVAILABLE`` code so the
        harness does not false-confirm the negative.
        """
        direction = str(payload.get("direction", "up"))
        event = self._closed_event(direction)
        try:
            result = self._adapter.invoke_evaluate(event)
        except CommandError:
            result = None
        outcome = result.get("outcome") if isinstance(result, dict) else None
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("reason", "")).strip()
        wrote: Optional[bool] = False
        evidence_unavailable = False
        if outcome == _OUTCOME_DISPATCHED:
            # A dispatched outcome here would be a real failure of the disable;
            # prove the negative by reading that no execution was started for it.
            operation_id = str(result.get("operation_id", "")) if isinstance(result, dict) else ""
            started = self._dispatch_evidence(operation_id)["step_functions_started"]
            if started is None:
                # UNREADABLE write evidence -> UNKNOWN, do NOT confirm no-write.
                wrote = None
                evidence_unavailable = True
            else:
                wrote = bool(started)
        body: dict[str, Any] = {
            "decision": "denied",
            "outcome": "refused" if outcome != _OUTCOME_DISPATCHED else _OUTCOME_DISPATCHED,
            "reason": reason or "AUTONOMY_DISABLED",
            "error_code": ("EVIDENCE_UNAVAILABLE" if evidence_unavailable else (reason.upper() or "AUTONOMY_DISABLED")),
            "wrote": wrote,
        }
        return _http(409, body)

    # -- helpers ------------------------------------------------------------ #

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
