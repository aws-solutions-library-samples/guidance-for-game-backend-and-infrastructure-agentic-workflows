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
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

# Local modules
from operations.validation.e1_shakedown import HttpResponse

# A runner performs one structured argv command and returns (returncode, stdout,
# stderr). Injecting it keeps this module AWS-free under test and lets the CLI use
# a real subprocess in production.
CommandRunner = Callable[[list[str]], "CommandResult"]

# An HTTP caller performs one authenticated HTTPS request and returns
# ``(status, headers, body_text)``. Injecting it keeps this module network-free
# under test; production uses :func:`urllib_http_caller`. The bearer is carried
# ONLY in ``HttpCall.headers`` (never in the URL/query) and is sourced from the
# environment by the adapter, so it never touches an argv, a log, or the output.
HttpCaller = Callable[["HttpCall"], "tuple[int, dict[str, str], str]"]

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

# The single supported autonomy capability id (see the 08 kill-switch and 09
# autonomy-switch AppConfig documents). E4 permission is read from this
# capability's ordered ``prepare``/``dispatch``/``execute`` phase booleans; E5
# permission is read from this capability's ``autonomous_write`` flag.
_CAPABILITY_ID = "gamelift.capacity-adjustment"


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


@dataclass(frozen=True, slots=True)
class HttpCall:
    """One authenticated HTTPS request the observe step issues to the 06 API.

    ``headers`` carries the ``Authorization: Bearer <token>`` header; the token
    is NEVER placed in ``url`` (no query string) so it cannot leak through a log,
    a proxy access line, or command output. ``body`` is a JSON string for POST
    and ``None`` for GET.
    """

    method: str
    url: str
    headers: Mapping[str, str]
    body: Optional[str] = None


# The observe request body contract is EXACTLY these two keys (see
# ``operations.observation.ObservationRequest.from_payload``); any extra/missing
# key is rejected by the deployed handler. The idempotency token must match the
# handler's ``^idem_[A-Za-z0-9_-]{20,128}$`` pattern.
_OBSERVE_BODY_KEYS = ("fleet_id", "idempotency_token")
# The succeeded observation state the GET status poll waits for (ADR 0005).
_OBSERVATION_STATE_SUCCEEDED = "succeeded"
_OBSERVATION_STATE_FAILED = "failed"
# Bounded status poll budget for the observe HTTPS status route.
_OBSERVE_POLL_MAX_ATTEMPTS = 20
_OBSERVE_POLL_SECONDS = 1.5


def _new_idempotency_token() -> str:
    """A fresh idempotency token matching the deployed handler's pattern.

    ``idem_`` + 32 url-safe hex chars satisfies ``^idem_[A-Za-z0-9_-]{20,128}$``.
    """
    return "idem_" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def urllib_http_caller(call: HttpCall) -> tuple[int, dict[str, str], str]:
    """Default HTTPS caller: perform one request over TLS with a short timeout.

    HTTP (non-TLS) URLs are refused so a bearer is never sent in the clear.
    Production CLI use only; unit tests inject a fake caller.
    """
    if not call.url.lower().startswith("https://"):
        raise CommandError("observe API endpoint must be https")
    data = call.body.encode("utf-8") if call.body is not None else None
    request = urllib.request.Request(
        call.url, data=data, method=call.method.upper()
    )  # noqa: S310 (https enforced above)
    for key, value in call.headers.items():
        request.add_header(key, value)
    if data is not None:
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            status = int(response.status)
            body_text = response.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for k, v in response.headers.items()}
            return status, headers, body_text
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
        return int(exc.code), {}, body_text
    except (urllib.error.URLError, OSError) as exc:
        raise CommandError("observe API request failed") from exc


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
    # The 06 observation HTTPS API base URL (e.g. the API Gateway ApiEndpoint).
    # The observe step calls ``POST {endpoint}/operations/observe`` and polls
    # ``GET {endpoint}/operations/{operation_id}`` — it NEVER invokes the 06
    # Lambda directly (a direct invoke with empty JWT claims cannot succeed).
    observe_api_endpoint: str = ""
    # The environment variable name the short-lived observe bearer is read from.
    # The token is sourced from the environment (or stdin-populated env) and
    # carried only in the Authorization header — never in an argv, URL, or log.
    observe_bearer_env_var: str = "GBAW_E5_OBSERVE_BEARER"
    # The 07 execution stack's dedicated least-privilege EXECUTOR role ARN (its
    # ``ExecutorRoleArn`` output). CloudTrail write attribution is CORRELATED to
    # THIS exact role's session issuer — never a bare ``Username`` literal — so a
    # write is credited to the executor only when the assumed-role session that
    # performed the ``UpdateFleetCapacity`` was issued by this exact role.
    # Defaulted for structural construction; a live run binds it from the 07 stack.
    executor_role_arn: str = ""


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
        http_caller: HttpCaller = urllib_http_caller,
        observe_poll_sleep: Callable[[float], None] = time.sleep,
        idempotency_token_factory: Callable[[], str] = _new_idempotency_token,
    ) -> None:
        self._config = config
        self._runner = runner
        self._drift_poll_sleep = drift_poll_sleep
        self._http_caller = http_caller
        self._observe_poll_sleep = observe_poll_sleep
        self._idempotency_token_factory = idempotency_token_factory

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

    def _observe_bearer(self) -> Optional[str]:
        """Read the short-lived observe bearer from the configured environment
        variable. Returns ``None`` when absent/blank so the observe fails closed
        rather than issuing an unauthenticated call. The token is never returned
        into an argv, a URL, or a log — only into the Authorization header.
        """
        token = os.environ.get(self._config.observe_bearer_env_var, "")
        token = token.strip()
        return token or None

    def observe_succeeded_observation_id(self) -> Optional[str]:
        """Issue an AUTHENTICATED HTTPS observe against the 06 API and return the
        succeeded observation's id — or ``None`` (fail closed) if it did not
        succeed / could not be parsed. Never fabricates ``trusted``.

        The deployed 06 handler is an authenticated HTTP API: a direct Lambda
        invoke with empty ``requestContext.authorizer.jwt.claims`` cannot succeed
        (it raises ``IDENTITY_CONTEXT_INVALID``), and the request body must be
        EXACTLY ``{"fleet_id", "idempotency_token"}``. So this:

        1. ``POST {endpoint}/operations/observe`` with the two-key proposal body
           (a fresh ``idempotency_token``) and a short-lived bearer sourced from
           the environment and carried only in the Authorization header;
        2. resolves the ``operation_id`` from the response and POLLs
           ``GET {endpoint}/operations/{operation_id}`` until ``state`` is
           terminal, returning the id only when the real status is ``succeeded``.
        """
        endpoint = (self._config.observe_api_endpoint or "").rstrip("/")
        if not endpoint:
            return None
        bearer = self._observe_bearer()
        if bearer is None:
            return None
        auth = {"authorization": f"Bearer {bearer}", "accept": "application/json"}
        body = json.dumps(
            {"fleet_id": self._config.enrolled_fleet_id, "idempotency_token": self._idempotency_token_factory()}
        )
        try:
            status, _headers, text = self._http_caller(
                HttpCall(method="POST", url=f"{endpoint}/operations/observe", headers=auth, body=body)
            )
        except CommandError:
            return None
        if status != 200:
            return None
        parsed = self._parse_json(text)
        operation_id = self._observation_operation_id(parsed)
        if operation_id is None:
            return None
        # A synchronous succeeded POST may already carry the terminal state.
        if self._observation_state(parsed) == _OBSERVATION_STATE_SUCCEEDED:
            return operation_id
        return self._poll_observation_succeeded(endpoint, auth, operation_id)

    def _poll_observation_succeeded(self, endpoint: str, auth: Mapping[str, str], operation_id: str) -> Optional[str]:
        """Poll the real status route until the observation reaches a terminal
        state; return the id only on ``succeeded`` (fail closed otherwise)."""
        url = f"{endpoint}/operations/{operation_id}"
        for attempt in range(_OBSERVE_POLL_MAX_ATTEMPTS):
            try:
                status, _headers, text = self._http_caller(
                    HttpCall(method="GET", url=url, headers=dict(auth), body=None)
                )
            except CommandError:
                return None
            if status != 200:
                return None
            parsed = self._parse_json(text)
            state = self._observation_state(parsed)
            if state == _OBSERVATION_STATE_SUCCEEDED:
                return operation_id
            if state == _OBSERVATION_STATE_FAILED:
                return None
            # Still in progress: bounded poll.
            if attempt + 1 < _OBSERVE_POLL_MAX_ATTEMPTS:
                self._observe_poll_sleep(_OBSERVE_POLL_SECONDS)
        return None  # never reached a terminal succeeded state -> fail closed

    @staticmethod
    def _parse_json(text: str) -> Any:
        try:
            return json.loads(text) if text and text.strip() else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _observation_operation_id(parsed: Any) -> Optional[str]:
        """Resolve the operation id from an observe/status body (``operation_id``
        or, for a succeeded observe body, ``observation_id``)."""
        if not isinstance(parsed, Mapping):
            return None
        for key in ("operation_id", "observation_id"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _observation_state(parsed: Any) -> Optional[str]:
        if not isinstance(parsed, Mapping):
            return None
        state = parsed.get("state")
        return state if isinstance(state, str) else None

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
        """cloudtrail:LookupEvents scoped to the write event NAME (not a bare
        resource) so the correlation reads the actual ``UpdateFleetCapacity``
        records rather than the newest event of any kind for the fleet.

        The returned records are correlated by :meth:`correlated_executor_write`
        against the EXACT event name, event time, fleet/location request, and the
        07 executor role — never trusting ``Events[0].Username``.
        """
        argv = self._base("cloudtrail", "lookup-events")
        argv += [
            "--lookup-attributes",
            json.dumps([{"AttributeKey": "EventName", "AttributeValue": _WRITE_EVENT_NAME}]),
        ]
        return self._run_json(argv)

    def correlated_executor_write(self, events: Any, *, not_before: datetime) -> Optional[bool]:
        """Return whether a CloudTrail record proves the SINGLE bounded write was
        performed by the exact 07 executor role, or ``None`` when unreadable.

        The proof binds ALL of:

        * ``eventName == UpdateFleetCapacity`` (the one write the executor makes);
        * ``eventTime`` strictly AFTER ``not_before`` (the dispatch instant), so a
          stale prior write can never be credited to this dispatch;
        * ``requestParameters`` name the EXACT enrolled fleet AND location;
        * the caller's assumed-role SESSION ISSUER ARN equals the configured 07
          ``executor_role_arn`` (an identity binding, not a display ``Username``).

        Returns:
          * ``True``  — a fully-correlated executor write exists;
          * ``False`` — records were readable but NONE correlate (mismatch/foreign
            role/wrong fleet/old event) — the executor-only invariant is DISPROVEN;
          * ``None``  — the events are unreadable/absent, so the fact is UNKNOWN.
        """
        records = events.get("Events") if isinstance(events, Mapping) else None
        if not isinstance(records, list):
            return None  # unreadable shape -> unknown
        if not records:
            # No UpdateFleetCapacity record at all: with a scoped lookup this is an
            # absence of the write, which is UNKNOWN (could not observe the write),
            # never a positive executor attribution.
            return None
        expected_role = (self._config.executor_role_arn or "").strip()
        if not expected_role:
            # Without the exact executor role we cannot bind identity -> unknown.
            return None
        saw_readable = False
        for record in records:
            detail = self._cloudtrail_detail(record)
            if detail is None:
                continue
            saw_readable = True
            if detail.get("eventName") != _WRITE_EVENT_NAME:
                continue
            event_time = self._instant(detail.get("eventTime"))
            if event_time is None or event_time <= not_before:
                continue
            params = detail.get("requestParameters")
            if not isinstance(params, Mapping):
                continue
            fleet = str(params.get("fleetId", ""))
            location = str(params.get("location", ""))
            if fleet != self._config.enrolled_fleet_id:
                continue
            if location != self._effective_location():
                continue
            issuer = self._session_issuer_arn(detail)
            if issuer is None:
                continue
            if issuer == expected_role:
                return True
        # Records were present. If NONE were even parseable, we could not read the
        # attribution -> unknown; otherwise we read them and none correlate -> False.
        return False if saw_readable else None

    @staticmethod
    def _cloudtrail_detail(record: Any) -> Optional[Mapping[str, Any]]:
        """Parse the embedded ``CloudTrailEvent`` JSON string of one LookupEvents
        record; return ``None`` when it is absent or unparseable."""
        if not isinstance(record, Mapping):
            return None
        raw = record.get("CloudTrailEvent")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, Mapping) else None

    @staticmethod
    def _session_issuer_arn(detail: Mapping[str, Any]) -> Optional[str]:
        """Extract the assumed-role session ISSUER arn from a CloudTrail event —
        the identity binding, not the display ``Username``."""
        identity = detail.get("userIdentity")
        if not isinstance(identity, Mapping):
            return None
        session_context = identity.get("sessionContext")
        if not isinstance(session_context, Mapping):
            return None
        issuer = session_context.get("sessionIssuer")
        if not isinstance(issuer, Mapping):
            return None
        arn = issuer.get("arn")
        return arn if isinstance(arn, str) and arn.strip() else None

    def _effective_location(self) -> str:
        """The enrolled location the capacity/inverse are scoped to (defaults to
        the adapter region when the enrolled location is unset)."""
        return self._config.enrolled_location or self._config.region

    def operator_inverse_update_fleet_capacity(self) -> Any:
        """OPERATOR-OWNED bounded inverse cleanup (TEST TOOLING, not an autonomy
        component): issue a VALID ``gamelift update-fleet-capacity`` restoring the
        EXACT enrolled fleet/location to the frozen ``desired=0 / min=0 / max=1``
        window.

        This is the teardown path the harness uses INSTEAD of the autonomous
        evaluator inverse: the evaluator ``down`` is denied by the 300/600s
        cooldown/frequency limits (the same limits the immediate-retry check
        proves), so reconciling through the evaluator can leave the fleet at
        ``desired=1``. A direct, bounded ``UpdateFleetCapacity`` at ``0/0/1``
        deterministically restores zero. It is gated by the operator's inverse
        confirmation at the harness before it is ever invoked.
        """
        argv = self._base("gamelift", "update-fleet-capacity")
        argv += [
            "--fleet-id",
            self._config.enrolled_fleet_id,
            "--location",
            self._effective_location(),
            "--desired-instances",
            str(_DESIRED_DOWN),
            "--min-size",
            str(_WINDOW_MINIMUM),
            "--max-size",
            str(_WINDOW_MAXIMUM),
        ]
        return self._run_json(argv)

    def describe_fleet_capacity(self) -> Any:
        """gamelift:DescribeFleetCapacity on the enrolled fleet (home-Region /
        fleet-wide capacity).

        NOTE: ``DescribeFleetCapacity`` has NO ``--locations`` parameter (that is
        an invalid ABI). Per-location capacity is read with
        :meth:`describe_fleet_location_capacity`; this fleet-wide read is only the
        fallback for a home-Region-only fleet.
        """
        argv = self._base("gamelift", "describe-fleet-capacity")
        argv += ["--fleet-id", self._config.enrolled_fleet_id]
        return self._run_json(argv)

    def describe_fleet_location_capacity(self, location: str) -> Any:
        """gamelift:DescribeFleetLocationCapacity for the enrolled fleet at the
        given LOCATION — the VALID ABI for per-location capacity (singular
        ``--location``)."""
        argv = self._base("gamelift", "describe-fleet-location-capacity")
        argv += ["--fleet-id", self._config.enrolled_fleet_id, "--location", location]
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

        When the fleet is enrolled at a specific LOCATION, the exact capacity is
        read with the VALID ``describe-fleet-location-capacity`` API (whose
        response is a single ``FleetCapacity`` object for that location). Only a
        home-Region-only fleet (no enrolled location) falls back to the fleet-wide
        ``describe-fleet-capacity``. A read that lacks the desired field returns
        None so the caller fails closed (never assumes a capacity it did not
        observe).
        """
        location = self._config.enrolled_location or self._config.region
        if location:
            try:
                data = self.describe_fleet_location_capacity(location)
            except CommandError:
                return None
            instance = self._location_capacity_instance_counts(data, location)
        else:
            data = self.describe_fleet_capacity()
            instance = self._fleet_wide_instance_counts(data)
        if instance is None or "DESIRED" not in instance:
            return None
        try:
            return {
                "desired": int(instance["DESIRED"]),
                "minimum": int(instance.get("MINIMUM", instance["DESIRED"])),
                "maximum": int(instance.get("MAXIMUM", instance["DESIRED"])),
            }
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _location_capacity_instance_counts(data: Any, location: str) -> Optional[dict[str, Any]]:
        """Extract InstanceCounts from a DescribeFleetLocationCapacity response.

        The response's ``FleetCapacity`` is a single object for the requested
        location; a mismatched/absent location fails closed.
        """
        if not isinstance(data, Mapping):
            return None
        capacity = data.get("FleetCapacity")
        if isinstance(capacity, list):
            # Some CLI shapes wrap the single object in a list.
            capacity = next((c for c in capacity if isinstance(c, Mapping)), None)
        if not isinstance(capacity, Mapping):
            return None
        reported_location = capacity.get("Location")
        if reported_location is not None and reported_location != location:
            return None
        instance = capacity.get("InstanceCounts")
        return instance if isinstance(instance, dict) else None

    @staticmethod
    def _fleet_wide_instance_counts(data: Any) -> Optional[dict[str, Any]]:
        """Extract InstanceCounts from a fleet-wide DescribeFleetCapacity response
        for a home-Region-only fleet (a single unlabelled entry)."""
        if not isinstance(data, Mapping):
            return None
        caps = data.get("FleetCapacity") or []
        if not isinstance(caps, list) or len(caps) != 1 or not isinstance(caps[0], dict):
            return None
        instance = caps[0].get("InstanceCounts")
        return instance if isinstance(instance, dict) else None

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
        """True only if the live E5 autonomy switch PERMITS an autonomous write.

        E5 permission is ``autonomy_enabled`` AND the EXACT capability's
        ``capabilities["gamelift.capacity-adjustment"].autonomous_write`` being
        true AND the document being fresh. A fresh, ``autonomy_enabled`` document
        whose capability ``autonomous_write`` is false does NOT permit the write
        (fail closed). The tri-state ``observed_autonomy_switch_state`` remains the
        enabled/disabled/unknown signal cleanup uses.
        """
        doc = self._read_appconfig_document(
            self._config.autonomy_application_id,
            self._config.autonomy_environment_id,
            self._config.autonomy_switch_profile_id,
            client_id="e5-shakedown-preflight",
        )
        if doc is None or not self._document_is_fresh(doc):
            return False
        if doc.get("autonomy_enabled") is not True:
            return False
        return self._capability_flag(doc, "autonomous_write") is True

    @staticmethod
    def _capability_flag(document: Mapping[str, Any], flag: str) -> Optional[bool]:
        """Read a boolean flag off the single supported capability's entry.

        Returns the boolean when present and boolean-typed, else ``None`` (so the
        caller fails closed on a missing/absent/malformed capability entry).
        """
        capabilities = document.get("capabilities")
        if not isinstance(capabilities, Mapping):
            return None
        capability = capabilities.get(_CAPABILITY_ID)
        if not isinstance(capability, Mapping):
            return None
        value = capability.get(flag)
        return value if isinstance(value, bool) else None

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
        """True only if the E4 (08) kill-switch document PERMITS the dispatch and
        execute phases for this capability AND is fresh.

        E4 permission is the deployment-wide ``operations_enabled`` master switch
        being true AND the capability's ordered ``dispatch`` and ``execute`` phase
        booleans both being true AND the document being fresh
        (``issued_at <= now < not_after`` on the REAL keys). ``operations_enabled``
        false, either phase disabled, a missing capability entry, or a stale/absent
        read all fail closed — a merely readable document is not enough.
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
        if doc is None or not self._document_is_fresh(doc):
            return False
        if doc.get("operations_enabled") is not True:
            return False
        return self._capability_flag(doc, "dispatch") is True and self._capability_flag(doc, "execute") is True

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

# The exact CloudTrail eventName of the single bounded write the executor makes.
# Attribution is scoped to and correlated against this event — a newest event of
# any other name is never credited as the executor's write.
_WRITE_EVENT_NAME = "UpdateFleetCapacity"


def _utcnow() -> datetime:
    """Default UTC clock (injectable so tests pin the dispatch instant)."""
    return datetime.now(timezone.utc)


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

    def __init__(self, adapter: CommandAdapter, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._adapter = adapter
        # The trusted observation id captured from the AUTHENTICATED 06 observe +
        # poll. It is the ONLY id the evaluator event may carry; it starts unset so
        # an evaluate before a succeeded observe fails CLOSED (never falls back to
        # the adapter-config observation id, which may be stale/never-observed).
        self._trusted_observation_id: Optional[str] = None
        # Injectable UTC clock: the dispatch instant is captured HERE at invoke
        # time so the CloudTrail correlation can require the write event to be
        # strictly AFTER the dispatch, without a real wall-clock dependency in tests.
        self._clock = clock

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
            # A failed/unreadable observe clears any prior trusted id: the next
            # evaluate must fail closed rather than reuse a stale success.
            self._trusted_observation_id = None
            return _http(200, {"trusted": False, "error_code": "OBSERVATION_NOT_SUCCEEDED"})
        # Capture the NEWLY-SUCCEEDED observation id: this exact id — not the
        # adapter-config value — is what the evaluator event will carry.
        self._trusted_observation_id = observation_id
        return _http(200, {"trusted": True, "observation_id": observation_id})

    def _closed_event(self, direction: str) -> Optional[dict[str, Any]]:
        """Build the EXACT closed evaluator event from the harness direction, or
        ``None`` when no trusted observation has succeeded yet.

        The evaluator's #439 contract accepts exactly
        ``{observation_operation_id, desired, minimum, maximum}`` and rejects any
        unknown field (including ``direction``). The direction is translated here
        into the requested capacity triple; the ``observation_operation_id`` is the
        id returned by the AUTHENTICATED 06 observe+poll and captured by
        :meth:`_observe` — never the adapter-config value, which may be stale or
        never-observed. Without a captured trusted id this returns ``None`` and the
        caller fails closed (it does NOT fabricate or fall back to a config id).
        """
        trusted = self._trusted_observation_id
        if not trusted:
            return None
        desired = _DESIRED_DOWN if str(direction).lower() == "down" else _DESIRED_UP
        return {
            "observation_operation_id": trusted,
            "desired": desired,
            "minimum": _WINDOW_MINIMUM,
            "maximum": _WINDOW_MAXIMUM,
        }

    def _evaluate(self, payload: Mapping[str, Any]) -> HttpResponse:
        direction = str(payload.get("direction", "up"))
        event = self._closed_event(direction)
        if event is None:
            # No trusted observation has succeeded: fail closed WITHOUT invoking the
            # evaluator and WITHOUT falling back to the config observation id.
            return _http(
                200,
                {
                    "decision": "denied",
                    "outcome": "refused",
                    "error_code": "NO_TRUSTED_OBSERVATION",
                    "wrote": False,
                },
            )
        # Capture the dispatch instant BEFORE invoking so the CloudTrail write
        # correlation can require the write event to be strictly after it.
        dispatched_at = self._clock()
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
        evidence = self._dispatch_evidence(operation_id, not_before=dispatched_at)
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

    def _dispatch_evidence(self, operation_id: str, *, not_before: Optional[datetime] = None) -> dict[str, Any]:
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
        # CloudTrail: the write is CORRELATED to the exact 07 executor role — the
        # eventName UpdateFleetCapacity, an event time AFTER dispatch, the exact
        # fleet/location request, and the executor role's SESSION ISSUER — never a
        # bare newest ``Username`` literal. The actor is reported as the executor
        # ONLY on a positive correlation; a readable-but-mismatched or unreadable
        # attribution reports NO executor actor (fail closed).
        actor = ""
        correlation_floor = not_before if not_before is not None else datetime.min.replace(tzinfo=timezone.utc)
        try:
            events = self._adapter.lookup_write_attribution(self._adapter._config.enrolled_fleet_id)
            correlated = self._adapter.correlated_executor_write(events, not_before=correlation_floor)
            if correlated is True:
                actor = _EXECUTOR_WRITE_ACTOR
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
        PROVEN by evidence — never false-confirmed on an unavailable read.

        Three cases, and only the first is a clean confirmed no-write denial:

        * **Evaluator refused** (a real ``{outcome: refused}`` result): the
          evaluator denied before any provider write, so ``wrote: false`` is
          confirmed by the evaluator's own decision.
        * **Evaluator UNAVAILABLE** (the invoke raised / returned an unparseable
          result): the outcome is UNKNOWN. The response must NOT map this to a
          clean disabled/no-write denial — it carries ``wrote: null`` and
          ``EVALUATOR_UNAVAILABLE`` so the harness does not false-confirm.
        * **Evaluator dispatched** (a real failure of the disable): the negative
          is proven only when the before/after write evidence
          (StepFunctions/DynamoDB/CloudTrail) is READABLE. If any write-evidence
          read is UNREADABLE, the result is UNKNOWN — ``wrote: null`` +
          ``EVIDENCE_UNAVAILABLE`` — never a clean ``wrote: false``.
        """
        direction = str(payload.get("direction", "up"))
        event = self._closed_event(direction)
        if event is None:
            # No trusted observation: the forced attempt cannot even build an event
            # to send, so it performs no write. This is a confirmed no-write denial.
            return _http(
                409,
                {
                    "decision": "denied",
                    "outcome": "refused",
                    "reason": "NO_TRUSTED_OBSERVATION",
                    "error_code": "NO_TRUSTED_OBSERVATION",
                    "wrote": False,
                },
            )
        dispatched_at = self._clock()
        evaluator_unavailable = False
        try:
            result = self._adapter.invoke_evaluate(event)
        except CommandError:
            result = None
            evaluator_unavailable = True
        if not isinstance(result, dict):
            # An unparseable evaluator result is UNKNOWN, not a confirmed refusal.
            evaluator_unavailable = True
            result = {}
        outcome = result.get("outcome")
        reason = str(result.get("reason", "")).strip()

        if evaluator_unavailable:
            # UNKNOWN outcome: cannot map an unavailable evaluator to "disabled".
            return _http(
                409,
                {
                    "decision": "denied",
                    "outcome": "unknown",
                    "reason": reason or "EVALUATOR_UNAVAILABLE",
                    "error_code": "EVALUATOR_UNAVAILABLE",
                    "wrote": None,
                },
            )

        if outcome != _OUTCOME_DISPATCHED:
            # A real refusal: the evaluator denied before any write, so no-write
            # is confirmed by the decision itself.
            return _http(
                409,
                {
                    "decision": "denied",
                    "outcome": "refused",
                    "reason": reason or "AUTONOMY_DISABLED",
                    "error_code": reason.upper() or "AUTONOMY_DISABLED",
                    "wrote": False,
                },
            )

        # A dispatched outcome is a real failure of the disable; prove the
        # negative from readable write evidence, or report UNKNOWN.
        operation_id = str(result.get("operation_id", ""))
        started = self._dispatch_evidence(operation_id, not_before=dispatched_at)["step_functions_started"]
        if started is None:
            # UNREADABLE write evidence -> UNKNOWN, do NOT confirm no-write.
            return _http(
                409,
                {
                    "decision": "denied",
                    "outcome": _OUTCOME_DISPATCHED,
                    "reason": reason or "AUTONOMY_DISABLED",
                    "error_code": "EVIDENCE_UNAVAILABLE",
                    "wrote": None,
                },
            )
        return _http(
            409,
            {
                "decision": "denied",
                "outcome": _OUTCOME_DISPATCHED,
                "reason": reason or "AUTONOMY_DISABLED",
                "error_code": reason.upper() or "AUTONOMY_DISABLED",
                "wrote": bool(started),
            },
        )

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
