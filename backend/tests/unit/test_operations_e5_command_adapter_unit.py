"""Tests for the concrete E5 command adapter and measured preflight (#440, Findings 7 & 8).

These prove the ``--adapter command`` path issues concrete, structured ``aws``
CLI JSON commands through an injectable runner (never HTTP, never an imaginary
``/operations/autonomy/*`` route), and that the preflight facts are MEASURED from
the provider rather than trusted from operator booleans.

The fake runners here model the REAL deployed command shapes (semantic review of
#440 at 09bbc06): ``aws lambda invoke`` writes the function response PAYLOAD to a
positional ``OutputFile`` while stdout carries only invoke metadata; the #439
store items are keyed with the real uppercase ``PK``/``SK``; the AppConfig
documents carry ``issued_at``/``not_after``/``autonomy_enabled``. No test here
calls AWS: every command goes through a fake runner that records the argv and
returns canned JSON / writes a canned OutputFile.
"""

from __future__ import annotations

# Standard library
import json
import pathlib
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.validation.e5_command_adapter import (
    CommandAdapter,
    CommandAdapterConfig,
    CommandError,
    CommandResult,
    CommandTransport,
    ImaginaryRouteError,
)

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
SHAKEDOWN = PROJECT_ROOT / "backend/src/operations/validation/e5_shakedown.py"

_STATE_MACHINE_ARN = "arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-autonomy"
_EXECUTOR_ROLE_ARN = "arn:aws:iam::000000000000:role/game-agent-operations-executor"


def _observe_ok_caller(operation_id: str = "op_obs_prior"):
    # Standard library
    import json as _json

    def _caller(_call):
        return 200, {}, _json.dumps({"operation_id": operation_id, "state": "succeeded"})

    return _caller


def _seed_observation(
    transport, *, bearer_env: str = "GBAW_E5_OBSERVE_BEARER", observed_id: str = "op_obs_prior"
) -> None:
    """Establish a trusted observation (the evaluate step requires one first).

    Drives the real observe path; if the adapter short-circuits because the test
    config carries no observe endpoint, seeds the trusted id directly (test
    tooling) so the evaluate precondition holds without changing every config."""
    # Standard library
    import os

    os.environ[bearer_env] = "short-lived"
    transport("POST", "https://x/operations/observe", headers={"authorization": "Bearer x"}, body=b"{}")
    if getattr(transport, "_trusted_observation_id", None) is None:
        transport._trusted_observation_id = observed_id


def _correlated_cloudtrail_event(
    *,
    fleet_id: str = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
    location: str = "us-west-2",
    role_arn: str = _EXECUTOR_ROLE_ARN,
    request_id: str = "request-e5-evidence",
) -> dict:
    """A CloudTrail LookupEvents record whose embedded event correlates to the
    executor role's UpdateFleetCapacity write, with an event time comfortably
    after any plausible dispatch instant."""
    # Standard library
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    event_time = (_dt.now(_tz.utc) + _td(hours=1)).isoformat().replace("+00:00", "Z")
    detail = {
        "eventSource": "gamelift.amazonaws.com",
        "eventName": "UpdateFleetCapacity",
        "eventTime": event_time,
        "requestID": request_id,
        "requestParameters": {
            "fleetId": fleet_id,
            "location": location,
            "desiredInstances": 1,
            "minSize": 0,
            "maxSize": 1,
        },
        "userIdentity": {"sessionContext": {"sessionIssuer": {"arn": role_arn}}},
    }
    return {"EventName": "UpdateFleetCapacity", "CloudTrailEvent": _json.dumps(detail)}


def _config() -> CommandAdapterConfig:
    return CommandAdapterConfig(
        region="us-west-2",
        profile="unit",
        evaluator_function_name="game-agent-operations-autonomy-evaluator",
        observe_function_name="game-agent-operations-observation",
        operations_table_name="game-agent-operations",
        enrolled_fleet_id="fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
        autonomy_application_id="app-1",
        autonomy_environment_id="env-1",
        autonomy_switch_profile_id="prof-1",
        errors_alarm_name="game-agent-operations-AutonomyEvaluatorErrors",
        throttles_alarm_name="game-agent-operations-AutonomyEvaluatorThrottles",
        delivery_alarm_name="game-agent-operations-AutonomyEvaluationDeliveryFailures",
        dead_letter_alarm_name="game-agent-operations-AutonomyDeadLetter",
        autonomy_state_machine_arn=_STATE_MACHINE_ARN,
        enrolled_location="us-west-2",
    )


# --------------------------------------------------------------------------- #
# Real-shape helpers: the deployed AppConfig documents and the OutputFile-based
# ``aws lambda invoke`` protocol the adapter uses.
# --------------------------------------------------------------------------- #


_CAPABILITY_ID = "gamelift.capacity-adjustment"


def _fresh_switch_doc(*, autonomy_enabled: bool = True, autonomous_write: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "autonomy_switch_version": "1.0",
        "config_version": 1,
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "not_after": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "autonomy_enabled": autonomy_enabled,
        "capabilities": {_CAPABILITY_ID: {"autonomous_write": autonomous_write}},
    }


def _write_invoke_payload(argv: list[str], payload: dict) -> CommandResult:
    """Emulate ``aws lambda invoke``: write the function PAYLOAD to the positional
    OutputFile and return only invoke METADATA on stdout."""
    outfile = argv[-1]
    pathlib.Path(outfile).write_text(json.dumps(payload), encoding="utf-8")
    return CommandResult(returncode=0, stdout=json.dumps({"StatusCode": 200, "ExecutedVersion": "$LATEST"}), stderr="")


def _dispatched_audit_item(operation_id: str) -> dict:
    """The #439 dispatched-audit item exactly as stored (PK/SK + attributes)."""
    return {
        "Item": {
            "PK": {"S": f"AUTZBUNDLE#{operation_id}"},
            "SK": {"S": "AUTZDISPATCH#dispatched"},
            "operation_id": {"S": operation_id},
            "phase": {"S": "dispatched"},
            "execution_name": {"S": operation_id[:80]},
            "audit": {"S": "{}"},
        }
    }


def _reservation_item(operation_id: str) -> dict:
    """The #439 reservation item exactly as stored (PK/SK + attributes)."""
    return {
        "Item": {
            "PK": {"S": f"AUTZRSV#{operation_id}"},
            "SK": {"S": "AUTZRSV"},
            "operation_id": {"S": operation_id},
            "settled": {"BOOL": False},
        }
    }


def _provider_receipt_item(operation_id: str, *, request_id: str = "request-e5-evidence") -> dict:
    """The bounded E3 provider receipt used for exact CloudTrail correlation."""
    return {
        "Item": {
            "PK": {"S": f"OP#{operation_id}"},
            "SK": {"S": "EXECPROVIDER"},
            "operation_id": {"S": operation_id},
            "logical_action_id": {"S": "act_" + "a" * 64},
            "provider_request_id": {"S": request_id},
            "expected_capacity": {"S": json.dumps({"desired": 1, "minimum": 0, "maximum": 1})},
        }
    }


class FakeRunner:
    """Records every argv and returns queued JSON results; never touches AWS.

    For ``lambda invoke`` the mapped value is treated as the FUNCTION PAYLOAD and
    written to the positional OutputFile (stdout carries only invoke metadata),
    mirroring the real CLI contract.
    """

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses or {}

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        # Key by "<service> <operation>".
        key = f"{argv[1]} {argv[2]}"
        payload = self._responses.get(key, {})
        if key == "lambda invoke":
            return _write_invoke_payload(argv, payload)
        return CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")


def test_every_command_is_a_structured_aws_json_argv() -> None:
    """Each lifecycle operation issues an `aws <svc> <op> --output json` argv."""
    runner = FakeRunner(
        {
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
            "gamelift describe-fleet-location-capacity": {
                "FleetCapacity": {"Location": "us-west-2", "InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}
            },
            "lambda invoke": {"outcome": "dispatched", "operation_id": "op"},
        }
    )
    adapter = CommandAdapter(_config(), runner=runner)
    adapter.invoke_evaluate({"observation_operation_id": "op", "desired": 1, "minimum": 0, "maximum": 1})
    # NOTE: the observe step is an authenticated HTTPS API call (not a Lambda
    # invoke), so it is exercised in the live-final semantic suite, not here.
    adapter.describe_execution("arn:aws:states:us-west-2:000000000000:execution:sm:exec")
    adapter.get_dispatched_audit_item("op")
    adapter.get_reservation_item("op")
    adapter.lookup_write_attribution("fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555")
    adapter.describe_fleet_capacity()
    adapter.describe_fleet_location_capacity("us-west-2")
    adapter.deploy_disabled_autonomy_switch("1")
    assert runner.calls, "adapter must issue commands"
    for argv in runner.calls:
        assert argv[0] == "aws", f"every command must be an aws CLI invocation: {argv}"
        assert "--output" in argv and argv[argv.index("--output") + 1] == "json"
        assert "--region" in argv
        # No HTTP TRANSPORT anywhere in the argv: a command adapter never issues a
        # URL through the CLI runner. (The observe HTTPS call goes through the
        # injected http_caller, never the argv runner.)
        assert not any(("http://" in tok or "https://" in tok) for tok in argv), f"no HTTP URL in argv: {argv}"
        # The observe bearer is never placed on any argv.
        assert not any("Bearer" in tok for tok in argv), f"no bearer token in argv: {argv}"
    services = {argv[1] for argv in runner.calls}
    assert services == {"lambda", "stepfunctions", "dynamodb", "cloudtrail", "gamelift", "appconfig"}


def test_command_failure_raises_without_leaking_stderr() -> None:
    def failing(argv):
        return CommandResult(returncode=255, stdout="", stderr="arn:aws:secret leaked")

    adapter = CommandAdapter(_config(), runner=failing)
    with pytest.raises(CommandError) as exc:
        adapter.describe_fleet_capacity()
    assert "arn:aws:secret" not in str(exc.value), "adapter must not leak stderr contents"


def test_observed_capacity_fails_closed_when_desired_absent() -> None:
    runner = FakeRunner({"gamelift describe-fleet-capacity": {"FleetCapacity": [{"InstanceCounts": {}}]}})
    adapter = CommandAdapter(_config(), runner=runner)
    assert adapter.observed_fleet_capacity() is None


def test_observed_alarms_safe_requires_all_ok() -> None:
    ok = {
        "MetricAlarms": [
            {"StateValue": "OK"},
            {"StateValue": "OK"},
            {"StateValue": "OK"},
            {"StateValue": "OK"},
        ]
    }
    adapter = CommandAdapter(_config(), runner=FakeRunner({"cloudwatch describe-alarms": ok}))
    assert adapter.observed_alarms_safe() is True

    one_alarm = {
        "MetricAlarms": [{"StateValue": "ALARM"}, {"StateValue": "OK"}, {"StateValue": "OK"}, {"StateValue": "OK"}]
    }
    adapter2 = CommandAdapter(_config(), runner=FakeRunner({"cloudwatch describe-alarms": one_alarm}))
    assert adapter2.observed_alarms_safe() is False

    missing = {"MetricAlarms": [{"StateValue": "OK"}]}  # fewer than 4 -> fail closed
    adapter3 = CommandAdapter(_config(), runner=FakeRunner({"cloudwatch describe-alarms": missing}))
    assert adapter3.observed_alarms_safe() is False


def test_transport_denies_unauthenticated_without_any_command() -> None:
    runner = FakeRunner()
    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    resp = transport("POST", "https://x/operations/autonomy/op/evaluate", headers={}, body=b"{}")
    assert resp.status == 401
    assert runner.calls == [], "unauthenticated request must issue no provider command"


class _RecordingCallable:
    """Wrap a per-command function while recording every argv."""

    def __init__(self, fn) -> None:
        self._fn = fn
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        return self._fn(argv)


def test_transport_maps_known_paths_to_commands() -> None:
    """Known paths map to real commands and consume the REAL evaluator contract.

    The evaluator returns {outcome, operation_id}; a dispatched outcome plus the
    supporting evidence reads (real PK/SK items) yield an authorized decision.
    Capacity is reported under the nested body['capacity']['desired'] shape the
    harness reads, and disable reports autonomy_enabled=False."""
    op = "op_abc"

    def fn(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            return _write_invoke_payload(argv, {"outcome": "dispatched", "operation_id": op})
        if key == "stepfunctions describe-execution":
            return CommandResult(0, json.dumps({"status": "SUCCEEDED"}), "")
        if key == "dynamodb get-item":
            k = json.loads(argv[argv.index("--key") + 1])
            if k["SK"]["S"] == "AUTZDISPATCH#dispatched":
                return CommandResult(0, json.dumps(_dispatched_audit_item(op)), "")
            if k["SK"]["S"] == "EXECPROVIDER":
                return CommandResult(0, json.dumps(_provider_receipt_item(op)), "")
            return CommandResult(0, json.dumps(_reservation_item(op)), "")
        if key == "cloudtrail lookup-events":
            return CommandResult(0, json.dumps({"Events": [_correlated_cloudtrail_event()]}), "")
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return CommandResult(
                0, json.dumps({"FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]}), ""
            )
        if key == "appconfig start-deployment":
            return CommandResult(0, json.dumps({"DeploymentNumber": 1}), "")
        return CommandResult(0, "{}", "")

    # Standard library
    import dataclasses

    runner = _RecordingCallable(fn)
    cfg = dataclasses.replace(_config(), executor_role_arn=_EXECUTOR_ROLE_ARN)
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok_caller()))
    hdr = {"authorization": "Bearer x"}
    _seed_observation(transport)
    ev = transport("POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=b"{}")
    body = ev.json()
    assert body["decision"] == "authorized"
    assert body["outcome"] == "dispatched"
    assert body["cloudtrail_write_actor"] == "executor"
    cap = transport("GET", "https://x/operations/autonomy/op/capacity", headers=hdr, body=None)
    assert cap.json()["capacity"]["desired"] == 1
    dis = transport("POST", "https://x/operations/autonomy/disable", headers=hdr, body=b"{}")
    assert dis.json()["autonomy_enabled"] is False
    # Every issued command was an aws CLI argv.
    assert all(argv[0] == "aws" for argv in runner.calls)


def test_transport_raises_on_imaginary_route() -> None:
    transport = CommandTransport(CommandAdapter(_config(), runner=FakeRunner()))
    with pytest.raises(ImaginaryRouteError):
        transport(
            "POST", "https://x/operations/autonomy/op/teleport", headers={"authorization": "Bearer x"}, body=b"{}"
        )


def test_shakedown_main_never_constructs_requests_transport() -> None:
    """Finding 7: --adapter command must not construct _requests_transport."""
    src = SHAKEDOWN.read_text(encoding="utf-8")
    # No CODE reference to _requests_transport(): the only allowed occurrence is a
    # comment referencing the removed pattern.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "_requests_transport(" not in line, f"main path must not call _requests_transport: {line!r}"
    # It must build the CommandTransport instead.
    assert "CommandTransport(adapter)" in src


def test_measured_preflight_overrides_supplied_booleans() -> None:
    """Finding 8: measured capacity/alarms/switch override operator-supplied flags."""
    # Local modules
    from operations.validation.e5_shakedown import E5Preflight, _measure_preflight

    # Operator ASSERTS everything is safe and capacity is 0/0/1...
    supplied = E5Preflight(
        profile="p",
        region="us-west-2",
        fleet_id="fleet-x",
        enrolled_profile="p",
        enrolled_region="us-west-2",
        enrolled_fleet_id="fleet-x",
        starting_desired=0,
        starting_minimum=0,
        starting_maximum=1,
        static_gate_fresh_enabled=True,
        e4_gate_fresh_enabled=True,
        autonomy_switch_fresh_enabled=True,
        alarms_safe=True,
        drift_safe=True,
    )

    # ...but the PROVIDER shows an ALARM and a non-zero starting capacity.
    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return CommandResult(
                0, json.dumps({"FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]}), ""
            )
        if key == "cloudwatch describe-alarms":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "MetricAlarms": [
                            {"StateValue": "ALARM"},
                            {"StateValue": "OK"},
                            {"StateValue": "OK"},
                            {"StateValue": "OK"},
                        ]
                    }
                ),
                "",
            )
        if key == "appconfig get-configuration":
            pathlib.Path(argv[-1]).write_text(json.dumps(_fresh_switch_doc(autonomy_enabled=True)), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(), runner=runner)
    measured = _measure_preflight(adapter, supplied)
    # The measured (unsafe) facts win: the run must refuse.
    assert measured.starting_desired == 1
    assert measured.alarms_safe is False
    refusals = measured.refusals()
    assert "STARTING_CAPACITY_NOT_0_0_1" in refusals
    assert "ALARMS_UNSAFE" in refusals or any("ALARM" in r for r in refusals)


# --------------------------------------------------------------------------- #
# #440 review (Findings 7 & 8): the command transport must send the evaluator
# the EXACT closed event and consume its ACTUAL {outcome, operation_id} result,
# building the write assertions from REAL evidence reads (StepFunctions /
# DynamoDB / CloudTrail) rather than fabricating a decision/denial.
# --------------------------------------------------------------------------- #


class RecordingRunner:
    """Records argv and returns per-command JSON, capturing the evaluator payload.

    Honors the real ``aws lambda invoke`` OutputFile protocol for evaluator/observe
    invokes.
    """

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.evaluate_payloads: list[dict] = []
        self._responses = responses or {}

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            # Capture the --payload of the invoke (the closed evaluator event).
            if "--payload" in argv:
                raw = argv[argv.index("--payload") + 1]
                self.evaluate_payloads.append(json.loads(raw))
            return _write_invoke_payload(argv, self._responses.get(key, {}))
        payload = self._responses.get(key, {})
        return CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")


def _adapter_config_with_event() -> CommandAdapterConfig:
    """The config MUST carry the trusted observation operation id so the transport
    can build the exact closed event; a direction-only body is not the contract."""
    # Standard library
    import dataclasses

    cfg = dataclasses.replace(_config(), observation_operation_id="op_obs_trusted")
    assert hasattr(
        cfg, "observation_operation_id"
    ), "CommandAdapterConfig must carry the trusted observation_operation_id for the closed event"
    return cfg


def test_transport_evaluate_sends_exact_closed_event_not_direction() -> None:
    """The evaluate step must invoke the evaluator with EXACTLY
    {observation_operation_id, desired, minimum, maximum} — never a 'direction'
    body and never any extra field."""
    runner = RecordingRunner(
        {
            "lambda invoke": {"outcome": "dispatched", "operation_id": "op_abc"},
            "stepfunctions describe-execution": {"status": "RUNNING"},
            "dynamodb get-item": {},
            "cloudtrail lookup-events": {"Events": [{"Username": "executor"}]},
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
        }
    )
    cfg = _adapter_config_with_event()
    observed_id = "op_obs_prior"
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok_caller(observed_id)))
    hdr = {"authorization": "Bearer x"}
    _seed_observation(transport)
    transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=json.dumps({"direction": "up"}).encode()
    )
    assert runner.evaluate_payloads, "the evaluate step must invoke the evaluator Lambda"
    event = runner.evaluate_payloads[0]
    assert set(event) == {
        "observation_operation_id",
        "desired",
        "minimum",
        "maximum",
    }, f"evaluator event must be the exact closed set, got {sorted(event)}"
    assert "direction" not in event, "the harness 'direction' must NOT be forwarded to the evaluator"
    # The event carries the NEWLY-SUCCEEDED observed id, not the config value.
    assert event["observation_operation_id"] == observed_id
    assert isinstance(event["desired"], int) and isinstance(event["minimum"], int) and isinstance(event["maximum"], int)


def test_transport_evaluate_up_and_down_map_to_capacity_triples() -> None:
    """up -> desired 1, down -> desired 0; minimum/maximum stay the 0/1 window."""
    runner = RecordingRunner({"lambda invoke": {"outcome": "refused", "reason": "decision_denied"}})
    cfg = _adapter_config_with_event()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok_caller()))
    hdr = {"authorization": "Bearer x"}
    _seed_observation(transport)
    transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=json.dumps({"direction": "up"}).encode()
    )
    transport(
        "POST",
        "https://x/operations/autonomy/op/evaluate",
        headers=hdr,
        body=json.dumps({"direction": "down"}).encode(),
    )
    up, down = runner.evaluate_payloads[0], runner.evaluate_payloads[1]
    assert up["desired"] == 1 and up["minimum"] == 0 and up["maximum"] == 1
    assert down["desired"] == 0 and down["minimum"] == 0 and down["maximum"] == 1


def test_transport_consumes_real_outcome_operation_id() -> None:
    """A refused evaluator outcome MUST surface as a denied decision carrying the
    evaluator's real reason — never a fabricated 'authorized'."""
    runner = RecordingRunner({"lambda invoke": {"outcome": "refused", "reason": "cooldown_active"}})
    cfg = _adapter_config_with_event()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok_caller()))
    hdr = {"authorization": "Bearer x"}
    _seed_observation(transport)
    resp = transport("POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=b"{}")
    body = resp.json()
    assert body.get("decision") == "denied", "a refused outcome must map to a denied decision"
    assert body.get("outcome") == "refused"
    assert "cooldown_active" in str(body).lower()


def test_transport_write_assertions_come_from_real_evidence_reads() -> None:
    """On a dispatched outcome, the transport must READ StepFunctions, DynamoDB,
    and CloudTrail evidence and derive the audit/reservation/SFN/executor fields
    from them — not fabricate them. If the executor did NOT write, the executor
    attribution must NOT be reported as satisfied."""
    op = "op_abc"

    # CloudTrail shows a NON-executor actor -> the executor-only assertion fails.
    def fn(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            return _write_invoke_payload(argv, {"outcome": "dispatched", "operation_id": op})
        if key == "stepfunctions describe-execution":
            return CommandResult(0, json.dumps({"status": "SUCCEEDED"}), "")
        if key == "dynamodb get-item":
            k = json.loads(argv[argv.index("--key") + 1])
            if k["SK"]["S"] == "AUTZDISPATCH#dispatched":
                return CommandResult(0, json.dumps(_dispatched_audit_item(op)), "")
            if k["SK"]["S"] == "EXECPROVIDER":
                return CommandResult(0, json.dumps(_provider_receipt_item(op)), "")
            return CommandResult(0, json.dumps(_reservation_item(op)), "")
        if key == "cloudtrail lookup-events":
            # A correlated UpdateFleetCapacity event but by a FOREIGN role: the
            # correlation must NOT credit the executor.
            foreign = _correlated_cloudtrail_event(role_arn="arn:aws:iam::000000000000:role/some-other-principal")
            return CommandResult(0, json.dumps({"Events": [foreign]}), "")
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return CommandResult(
                0, json.dumps({"FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]}), ""
            )
        return CommandResult(0, "{}", "")

    # Standard library
    import dataclasses

    runner = _RecordingCallable(fn)
    cfg = dataclasses.replace(_adapter_config_with_event(), executor_role_arn=_EXECUTOR_ROLE_ARN)
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok_caller()))
    hdr = {"authorization": "Bearer x"}
    _seed_observation(transport)
    resp = transport("POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=b"{}")
    body = resp.json()
    # Evidence was actually read.
    issued = {f"{a[1]} {a[2]}" for a in runner.calls}
    assert "stepfunctions describe-execution" in issued, "must read the started execution as evidence"
    assert "dynamodb get-item" in issued, "must read the audit/reservation item as evidence"
    assert "cloudtrail lookup-events" in issued, "must read the write attribution as evidence"
    # The non-executor CloudTrail actor must be reflected truthfully, not faked.
    assert (
        body.get("cloudtrail_write_actor") != "executor"
    ), "a non-executor CloudTrail actor must NOT be reported as the executor"


def test_transport_force_write_denial_is_proven_by_evidence_not_fabricated() -> None:
    """After disable, the forced attempt's 'no write' must be PROVEN by evidence
    (no new dispatched execution / unchanged capacity), not a hardcoded body.

    The evaluator refuses (outcome=refused) and NO new Step Functions execution
    exists; the transport must read that evidence to conclude wrote=False."""

    def fn(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            return _write_invoke_payload(argv, {"outcome": "refused", "reason": "autonomy_disabled"})
        if key == "stepfunctions describe-execution":
            return CommandResult(0, json.dumps({"status": "ABSENT"}), "")
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return CommandResult(
                0, json.dumps({"FleetCapacity": [{"InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}]}), ""
            )
        if key == "cloudtrail lookup-events":
            return CommandResult(0, json.dumps({"Events": []}), "")
        return CommandResult(0, "{}", "")

    runner = _RecordingCallable(fn)
    cfg = _adapter_config_with_event()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok_caller()))
    hdr = {"authorization": "Bearer x"}
    _seed_observation(transport)
    resp = transport(
        "POST", "https://x/operations/autonomy/op/force-write", headers=hdr, body=json.dumps({"force": True}).encode()
    )
    body = resp.json()
    assert resp.status >= 400, "a forced attempt after disable must be refused"
    assert body.get("wrote") is False, "the forced attempt must perform no write"
    # It must have consulted real evidence rather than returning a canned body.
    issued = {f"{a[1]} {a[2]}" for a in runner.calls}
    assert issued & {
        "lambda invoke",
        "cloudtrail lookup-events",
        "gamelift describe-fleet-capacity",
    }, "the force-write denial must be backed by at least one real evidence read"


# --------------------------------------------------------------------------- #
# End-to-end: the REAL CommandTransport must satisfy EVERY harness check when
# the evaluator dispatches and the evidence is good, proving the contract fix
# works composed (not just per-method). This is the acceptance seam for #440
# Findings 7 & 8: exact closed event in, real {outcome, operation_id} +
# evidence out, no fabricated field.
# --------------------------------------------------------------------------- #


class ScriptedLifecycleRunner:
    """A fake runner that models the enrolled fleet's capacity as state and
    answers each command from real, evolving state — never canned per-check.

    It honors the real deployed shapes: ``aws lambda invoke`` writes the function
    payload to the OutputFile; the observe invoke returns an API-Gateway proxy
    response carrying a succeeded ``observation_id``; the #439 store items use the
    real uppercase PK/SK; the AppConfig switch document uses ``issued_at``/
    ``not_after``/``autonomy_enabled``.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.desired = 0
        self.enabled = True
        self.dispatched = False
        self.last_op = ""

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            event = json.loads(argv[argv.index("--payload") + 1]) if "--payload" in argv else {}
            fn = argv[argv.index("--function-name") + 1]
            # The observe function invoke: return a succeeded observation proxy.
            if "observation" in fn:
                body = json.dumps({"observation_contract_version": "1", "observation_id": "op_obs_e2e"})
                return _write_invoke_payload(argv, {"statusCode": 200, "headers": {}, "body": body})
            want = event.get("desired")
            if not self.enabled:
                # A refused follow-up does not erase the terminal evidence for a
                # previously started execution.
                return _write_invoke_payload(argv, {"outcome": "refused", "reason": "AUTONOMY_DISABLED"})
            if want == 1 and self.desired == 1:
                # Immediate retry at the top of the window is cooldown-denied;
                # it does not erase the execution evidence already recorded.
                return _write_invoke_payload(argv, {"outcome": "refused", "reason": "COOLDOWN_ACTIVE"})
            self.desired = int(want)
            self.dispatched = True
            self.last_op = "op_e2e"
            return _write_invoke_payload(argv, {"outcome": "dispatched", "operation_id": self.last_op})
        if key == "stepfunctions describe-execution":
            return self._json({"status": "SUCCEEDED"} if self.dispatched else {"status": "ABSENT"})
        if key == "dynamodb get-item":
            if not self.dispatched:
                return self._json({})
            k = json.loads(argv[argv.index("--key") + 1])
            if k["SK"]["S"] == "AUTZDISPATCH#dispatched":
                return self._json(_dispatched_audit_item(self.last_op))
            if k["SK"]["S"] == "EXECPROVIDER":
                return self._json(_provider_receipt_item(self.last_op))
            return self._json(_reservation_item(self.last_op))
        if key == "cloudtrail lookup-events":
            return self._json({"Events": [_correlated_cloudtrail_event()]} if self.dispatched else {"Events": []})
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return self._json(
                {
                    "FleetCapacity": [
                        {
                            "Location": "us-west-2",
                            "InstanceCounts": {"DESIRED": self.desired, "MINIMUM": 0, "MAXIMUM": 1},
                        }
                    ]
                }
            )
        if key == "cloudwatch describe-alarms":
            return self._json({"MetricAlarms": [{"StateValue": "OK"}] * 4})
        if key == "appconfig get-configuration":
            # get-configuration writes the document BODY to the positional
            # OutputFile; reflect the live enabled state so a disable re-read
            # observes the flip.
            pathlib.Path(argv[-1]).write_text(
                json.dumps(_fresh_switch_doc(autonomy_enabled=self.enabled)), encoding="utf-8"
            )
            return self._json({"ConfigurationVersion": "1"})
        if key == "appconfig start-deployment":
            self.enabled = False
            return self._json({"DeploymentNumber": 1})
        return self._json({})

    @staticmethod
    def _json(payload: dict) -> CommandResult:
        return CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")


def test_command_transport_drives_full_harness_to_accepted(monkeypatch) -> None:
    """The real CommandTransport, driven through the full E5 harness with good
    evidence, is ACCEPTED and every check passes."""
    # Standard library
    import dataclasses

    # Local modules
    from operations.validation.e5_command_adapter import HttpCall
    from operations.validation.e5_shakedown import (
        REQUIRED_CONFIRMATION,
        REQUIRED_INVERSE_CONFIRMATION,
        E5Preflight,
        E5ShakedownConfig,
        E5ShakedownHarness,
    )

    runner = ScriptedLifecycleRunner()
    cfg = _config()
    # Bind the trusted observation id the closed event carries and the 06 HTTPS
    # observe endpoint (observe is an authenticated API call, not a Lambda invoke).
    cfg = dataclasses.replace(
        cfg,
        observation_operation_id="op_obs_e2e",
        observe_api_endpoint="https://obs.example.aws.dev",
        executor_role_arn=_EXECUTOR_ROLE_ARN,
    )
    monkeypatch.setenv("GBAW_E5_OBSERVE_BEARER", "short-lived")

    def observe_http(call: "HttpCall"):
        # A succeeded observation for the enrolled fleet.
        return 200, {}, json.dumps({"operation_id": "op_obs_e2e", "state": "succeeded"})

    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=observe_http))

    preflight = E5Preflight(
        profile="unit",
        region="us-west-2",
        fleet_id=cfg.enrolled_fleet_id,
        enrolled_profile="unit",
        enrolled_region="us-west-2",
        enrolled_fleet_id=cfg.enrolled_fleet_id,
        starting_desired=0,
        starting_minimum=0,
        starting_maximum=1,
        static_gate_fresh_enabled=True,
        e4_gate_fresh_enabled=True,
        autonomy_switch_fresh_enabled=True,
        alarms_safe=True,
        drift_safe=True,
    )
    shakedown_cfg = E5ShakedownConfig(
        endpoint="https://api.example.execute-api.us-west-2.amazonaws.com",
        admin_bearer="admin-token",
        fleet_id=cfg.enrolled_fleet_id,
        observation_id="obs_e2e",
        operation_id="op_e2e",
        preflight=preflight,
        confirmation=REQUIRED_CONFIRMATION,
        inverse_confirmation=REQUIRED_INVERSE_CONFIRMATION,
    )
    summary = E5ShakedownHarness(shakedown_cfg, transport, sleep=lambda *_: None).run()
    failed = [c for c in summary["checks"] if not c["passed"]]
    assert summary["accepted"] is True, f"harness not accepted; failing checks: {[c['name'] for c in failed]}"
    assert summary["refused"] is False
    # The evaluate step never forwarded a 'direction' to the evaluator.
    for argv in runner.calls:
        if argv[1:3] == ["lambda", "invoke"] and "--payload" in argv:
            ev = json.loads(argv[argv.index("--payload") + 1])
            if "observation_operation_id" in ev:
                assert "direction" not in ev


# --------------------------------------------------------------------------- #
# #440 review (Finding 8, continued): the AppConfig get-configuration read MUST
# include the required OutputFile positional arg (the CLI refuses without it and
# writes the document body to that file, not stdout), and the measured preflight
# must MEASURE the static mode / E4 & E5 switch freshness / drift / enrollment /
# capacity from the provider rather than pass through operator-supplied booleans.
# --------------------------------------------------------------------------- #


def test_appconfig_get_configuration_includes_required_outfile() -> None:
    """`aws appconfig get-configuration` requires a positional output file; the
    adapter must pass one (a safe temp path) or the CLI fails at runtime."""
    recorded: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            recorded["argv"] = argv
            # Simulate the CLI writing the document to the OutputFile positional.
            outfile = argv[-1]
            pathlib.Path(outfile).write_text(json.dumps(_fresh_switch_doc(autonomy_enabled=True)), encoding="utf-8")
            # Stdout carries only metadata (no document body).
            return CommandResult(returncode=0, stdout=json.dumps({"ConfigurationVersion": "1"}), stderr="")
        return CommandResult(returncode=0, stdout="{}", stderr="")

    adapter = CommandAdapter(_config(), runner=runner)
    result = adapter.observed_autonomy_switch_fresh_enabled()
    argv = recorded.get("argv")
    assert argv is not None, "the autonomy-switch read must issue appconfig get-configuration"
    # The final positional token must be an output file path, not a flag/None.
    outfile = argv[-1]
    assert not outfile.startswith("--"), f"get-configuration must end with a positional OutputFile, got {outfile!r}"
    assert outfile, "OutputFile must be non-empty"
    # And the document body is read from the OutputFile, yielding the enabled flag.
    assert result is True


def _measure_config():
    # Standard library
    import dataclasses

    return dataclasses.replace(
        _config(),
        kill_switch_application_id="ks-app",
        kill_switch_environment_id="ks-env",
        kill_switch_profile_id="ks-prof",
        autonomy_stack_name="game-agent-operations-autonomy",
    )


def _kill_switch_doc(*, fresh: bool) -> dict:
    now = datetime.now(timezone.utc)
    issued = now - timedelta(minutes=1) if fresh else now + timedelta(hours=1)
    not_after = now + timedelta(hours=1) if fresh else now - timedelta(minutes=1)
    return {
        "contract_version": "1.0",
        "config_version": 1,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "not_after": not_after.isoformat().replace("+00:00", "Z"),
        "operations_enabled": True,
        "capabilities": {_CAPABILITY_ID: {"prepare": True, "dispatch": True, "execute": True}},
    }


def test_measured_preflight_measures_gates_drift_enrollment_not_supplied() -> None:
    """The measured preflight must OVERRIDE the static mode, E4 freshness, drift,
    and enrollment facts with provider measurements, so an operator that ASSERTS
    everything safe while the provider disagrees still refuses closed. Here every
    provider read reports UNSAFE while the operator supplied all-True."""
    # Local modules
    from operations.validation.e5_shakedown import E5Preflight, _measure_preflight

    supplied = E5Preflight(
        profile="p",
        region="us-west-2",
        fleet_id="fleet-x",
        enrolled_profile="p",
        enrolled_region="us-west-2",
        enrolled_fleet_id="fleet-x",
        starting_desired=0,
        starting_minimum=0,
        starting_maximum=1,
        static_gate_fresh_enabled=True,
        e4_gate_fresh_enabled=True,
        autonomy_switch_fresh_enabled=True,
        alarms_safe=True,
        drift_safe=True,
    )

    cfg = _measure_config()

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return CommandResult(
                0, json.dumps({"FleetCapacity": [{"InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}]}), ""
            )
        if key == "cloudwatch describe-alarms":
            return CommandResult(0, json.dumps({"MetricAlarms": [{"StateValue": "OK"}] * 4}), "")
        if key == "appconfig get-configuration":
            client_id = argv[argv.index("--client-id") + 1]
            if "killswitch" in client_id:
                pathlib.Path(argv[-1]).write_text(json.dumps(_kill_switch_doc(fresh=False)), encoding="utf-8")
            else:
                doc = _fresh_switch_doc(autonomy_enabled=True)
                doc["not_after"] = (
                    (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
                )
                pathlib.Path(argv[-1]).write_text(json.dumps(doc), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        if key == "lambda get-function-configuration":
            # Static mode is NOT operate.
            return CommandResult(0, json.dumps({"Environment": {"Variables": {"GBAW_OPERATIONS_MODE": "advise"}}}), "")
        if key == "gamelift describe-fleet-attributes":
            # Fleet is not ACTIVE (enrollment unsafe).
            return CommandResult(0, json.dumps({"FleetAttributes": [{"Status": "ERROR"}]}), "")
        if key == "cloudformation detect-stack-drift":
            return CommandResult(0, json.dumps({"StackDriftDetectionId": "det-1"}), "")
        if key == "cloudformation describe-stack-drift-detection-status":
            return CommandResult(
                0, json.dumps({"DetectionStatus": "DETECTION_COMPLETE", "StackDriftStatus": "DRIFTED"}), ""
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(cfg, runner=runner, drift_poll_sleep=lambda *_: None)
    measured = _measure_preflight(adapter, supplied)
    # Every measured fact overrides the supplied True and the run must refuse.
    assert measured.static_gate_fresh_enabled is False
    assert measured.e4_gate_fresh_enabled is False
    assert measured.autonomy_switch_fresh_enabled is False
    assert measured.drift_safe is False
    refusals = set(measured.refusals())
    assert {
        "STATIC_GATE_NOT_FRESH_ENABLED",
        "E4_GATE_NOT_FRESH_ENABLED",
        "AUTONOMY_SWITCH_NOT_FRESH_ENABLED",
        "DRIFT_NOT_SAFE",
    } <= refusals


def test_measured_preflight_static_e4_drift_enrollment_all_safe_passes() -> None:
    """When every provider read is SAFE, the measured facts do not add refusals."""
    # Local modules
    from operations.validation.e5_shakedown import E5Preflight, _measure_preflight

    supplied = E5Preflight(
        profile="p",
        region="us-west-2",
        fleet_id="fleet-x",
        enrolled_profile="p",
        enrolled_region="us-west-2",
        enrolled_fleet_id="fleet-x",
        starting_desired=0,
        starting_minimum=0,
        starting_maximum=1,
        static_gate_fresh_enabled=True,
        e4_gate_fresh_enabled=True,
        autonomy_switch_fresh_enabled=True,
        alarms_safe=True,
        drift_safe=True,
    )

    cfg = _measure_config()

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return CommandResult(
                0, json.dumps({"FleetCapacity": [{"InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}]}), ""
            )
        if key == "cloudwatch describe-alarms":
            return CommandResult(0, json.dumps({"MetricAlarms": [{"StateValue": "OK"}] * 4}), "")
        if key == "appconfig get-configuration":
            client_id = argv[argv.index("--client-id") + 1]
            if "killswitch" in client_id:
                pathlib.Path(argv[-1]).write_text(json.dumps(_kill_switch_doc(fresh=True)), encoding="utf-8")
            else:
                pathlib.Path(argv[-1]).write_text(
                    json.dumps(_fresh_switch_doc(autonomy_enabled=True)), encoding="utf-8"
                )
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        if key == "lambda get-function-configuration":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "Environment": {
                            "Variables": {"GBAW_OPERATIONS_MODE": "operate", "GBAW_OPERATIONS_AUTONOMY_ENABLED": "true"}
                        }
                    }
                ),
                "",
            )
        if key == "gamelift describe-fleet-attributes":
            return CommandResult(0, json.dumps({"FleetAttributes": [{"Status": "ACTIVE"}]}), "")
        if key == "cloudformation detect-stack-drift":
            return CommandResult(0, json.dumps({"StackDriftDetectionId": "det-1"}), "")
        if key == "cloudformation describe-stack-drift-detection-status":
            return CommandResult(
                0, json.dumps({"DetectionStatus": "DETECTION_COMPLETE", "StackDriftStatus": "IN_SYNC"}), ""
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(cfg, runner=runner, drift_poll_sleep=lambda *_: None)
    measured = _measure_preflight(adapter, supplied)
    assert measured.static_gate_fresh_enabled is True
    assert measured.e4_gate_fresh_enabled is True
    assert measured.autonomy_switch_fresh_enabled is True
    assert measured.drift_safe is True
    assert measured.refusals() == []


# --------------------------------------------------------------------------- #
# Follow-up: dispatched evidence requires a durable per-operation receipt.
# --------------------------------------------------------------------------- #


def test_dispatch_evidence_does_not_credit_a_matching_role_without_its_receipt() -> None:
    """Role/fleet/time alone prove only an executor write, not this operation."""
    op = "op_receipt_required"

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            return _write_invoke_payload(argv, {"outcome": "dispatched", "operation_id": op})
        if key == "stepfunctions describe-execution":
            return CommandResult(0, json.dumps({"status": "SUCCEEDED"}), "")
        if key == "dynamodb get-item":
            key_data = json.loads(argv[argv.index("--key") + 1])
            if key_data["SK"]["S"] == "AUTZDISPATCH#dispatched":
                return CommandResult(0, json.dumps(_dispatched_audit_item(op)), "")
            if key_data["SK"]["S"] == "AUTZRSV":
                return CommandResult(0, json.dumps(_reservation_item(op)), "")
            # No EXECPROVIDER record exists for this operation.
            return CommandResult(0, json.dumps({}), "")
        if key == "cloudtrail lookup-events":
            event = _correlated_cloudtrail_event()
            detail = json.loads(event["CloudTrailEvent"])
            detail["requestID"] = "request-for-another-operation"
            detail["requestParameters"].update({"minSize": 0, "maxSize": 1})
            event["CloudTrailEvent"] = json.dumps(detail)
            return CommandResult(0, json.dumps({"Events": [event]}), "")
        return CommandResult(0, "{}", "")

    cfg = _config()
    cfg = __import__("dataclasses").replace(cfg, executor_role_arn=_EXECUTOR_ROLE_ARN)
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok_caller()))
    _seed_observation(transport)

    body = transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers={"authorization": "Bearer x"}, body=b"{}"
    ).json()

    assert body["cloudtrail_write_actor"] != "executor"


def test_ambiguous_dispatch_refusal_retains_the_operation_for_teardown() -> None:
    """A StartExecution-uncertain result must not discard its known operation id."""
    op = "op_ambiguous_dispatch"
    runner = RecordingRunner(
        {"lambda invoke": {"outcome": "refused", "reason": "start_execution_uncertain", "operation_id": op}}
    )
    transport = CommandTransport(
        CommandAdapter(_adapter_config_with_event(), runner=runner, http_caller=_observe_ok_caller())
    )
    _seed_observation(transport)

    body = transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers={"authorization": "Bearer x"}, body=b"{}"
    ).json()

    assert body["operation_id"] == op
    assert body["dispatch_state"] == "unknown"
    assert body["wrote"] is False


def test_unreadable_evaluator_dispatch_is_unknown_not_a_no_write_refusal() -> None:
    """An invocation error has no operation id to quiesce, so it remains unresolved."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "invoke"]:
            return CommandResult(255, "", "unavailable")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(
        CommandAdapter(_adapter_config_with_event(), runner=runner, http_caller=_observe_ok_caller())
    )
    _seed_observation(transport)

    body = transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers={"authorization": "Bearer x"}, body=b"{}"
    ).json()

    assert body["outcome"] == "unknown"
    assert body["dispatch_state"] == "unknown"
    assert body["wrote"] is None


def test_malformed_dispatched_operation_id_remains_unresolved_for_teardown() -> None:
    """A malformed dispatch identifier cannot be dropped as a safe refusal."""
    runner = RecordingRunner({"lambda invoke": {"outcome": "dispatched", "operation_id": "bad-operation-id"}})
    transport = CommandTransport(
        CommandAdapter(_adapter_config_with_event(), runner=runner, http_caller=_observe_ok_caller())
    )
    _seed_observation(transport)

    body = transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers={"authorization": "Bearer x"}, body=b"{}"
    ).json()

    assert body["outcome"] == "unknown"
    assert body["dispatch_state"] == "unknown"
    assert body["wrote"] is None
