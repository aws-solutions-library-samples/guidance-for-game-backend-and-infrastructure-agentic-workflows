"""Real-shape tests for the FOUR ultimate-fix semantic blockers of #440 (at cc77cda).

The prior live-adapter/binding work closed the observe-over-HTTPS, capacity ABI,
measured preflight, tri-state switch, evidence-gated force-write, and the
activation binding. This suite pins the FOUR remaining semantic defects to the
EXACT deployed contracts:

1. **Evaluator must use the NEWLY-SUCCEEDED observation id, not the config one.**
   The trusted observation the evaluator resolves from must be the exact
   ``operation_id`` returned by the authenticated 06 observe+poll, captured by the
   observe step — never a static ``observation_operation_id`` carried on the
   adapter config (which may be stale / never-observed). Evaluate with no
   succeeded observation fails closed (no fabricated id).

2. **Cleanup must use an operator-owned bounded inverse, not the evaluator.** The
   autonomous evaluator inverse (``down``) is denied by the 300/600s cooldown /
   frequency limits (the same limits the immediate-retry check proves), so a
   cleanup that reconciles through the evaluator can leave the fleet at
   ``desired=1``. Teardown must instead issue a valid GameLift
   ``update-fleet-capacity`` for the EXACT enrolled fleet/location at ``0/0/1``
   and poll-verify. This cleanup adapter is TEST TOOLING (not an autonomy
   component) and must require the operator's inverse confirmation.

3. **Deploy wrapper must read/compare the 07 table/KMS/tenant/workspace to 06.**
   The 07 execution stack carries the SAME OperationsTableName / OperationsKmsKeyArn
   / TenantId / WorkspaceId as 06; the wrapper must read them and refuse unless
   they byte-equal the 06 values, preserving only after equality.

4. **CloudTrail attribution must be correlated, not the newest Username literal.**
   The write proof must bind: ``eventName == UpdateFleetCapacity``, event time
   AFTER dispatch, request params for the EXACT fleet/location, and the exact 07
   ``ExecutorRoleArn`` / session issuer — never ``Events[0].Username``. An
   unreadable event or any mismatch is unknown/fail, never the executor actor.

Every command/HTTP call goes through an injected fake; no test here calls AWS.
"""

from __future__ import annotations

# Standard library
import dataclasses
import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.validation.e5_command_adapter import (
    CommandAdapter,
    CommandAdapterConfig,
    CommandResult,
    CommandTransport,
    HttpCall,
)

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
WRAPPER = PROJECT_ROOT / "scripts/infrastructure/deploy-operations-autonomy.sh"

_STATE_MACHINE_ARN = "arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-autonomy"
_EXECUTOR_ROLE_ARN = "arn:aws:iam::000000000000:role/game-agent-operations-executor"
_ENROLLED_FLEET = "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555"
_ENROLLED_LOCATION = "us-west-2"
_CAPABILITY_ID = "gamelift.capacity-adjustment"


def _config(**overrides: object) -> CommandAdapterConfig:
    base = dict(
        region="us-west-2",
        profile="unit",
        evaluator_function_name="game-agent-operations-autonomy-evaluator",
        observe_function_name="game-agent-operations-observation",
        operations_table_name="game-agent-operations",
        enrolled_fleet_id=_ENROLLED_FLEET,
        autonomy_application_id="app-1",
        autonomy_environment_id="env-1",
        autonomy_switch_profile_id="prof-1",
        errors_alarm_name="game-agent-operations-AutonomyEvaluatorErrors",
        throttles_alarm_name="game-agent-operations-AutonomyEvaluatorThrottles",
        delivery_alarm_name="game-agent-operations-AutonomyEvaluationDeliveryFailures",
        dead_letter_alarm_name="game-agent-operations-AutonomyDeadLetter",
        observation_operation_id="op_obs_STALE_config_value",
        autonomy_state_machine_arn=_STATE_MACHINE_ARN,
        enrolled_location=_ENROLLED_LOCATION,
        observe_api_endpoint="https://obs.example.aws.dev",
        observe_bearer_env_var="GBAW_E5_OBSERVE_BEARER_UNITTEST",
        executor_role_arn=_EXECUTOR_ROLE_ARN,
    )
    base.update(overrides)
    return CommandAdapterConfig(**base)  # type: ignore[arg-type]


def _write_invoke_payload(argv: list[str], payload: dict) -> CommandResult:
    """Emulate ``aws lambda invoke``: write the function PAYLOAD to the positional
    OutputFile and return only invoke METADATA on stdout."""
    outfile = argv[-1]
    pathlib.Path(outfile).write_text(json.dumps(payload), encoding="utf-8")
    return CommandResult(returncode=0, stdout=json.dumps({"StatusCode": 200, "ExecutedVersion": "$LATEST"}), stderr="")


def _dispatched_audit_item(operation_id: str) -> dict:
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
    return {
        "Item": {
            "PK": {"S": f"AUTZRSV#{operation_id}"},
            "SK": {"S": "AUTZRSV"},
            "operation_id": {"S": operation_id},
            "settled": {"BOOL": False},
        }
    }


def _cloudtrail_update_fleet_event(
    *,
    fleet_id: str = _ENROLLED_FLEET,
    location: str = _ENROLLED_LOCATION,
    role_arn: str = _EXECUTOR_ROLE_ARN,
    event_name: str = "UpdateFleetCapacity",
    event_time_offset_seconds: int = 30,
    dispatch_time: datetime,
) -> dict:
    """One CloudTrail LookupEvents record whose embedded ``CloudTrailEvent`` JSON
    carries the exact bindings: eventName, eventTime after dispatch, the fleet/
    location request parameters, and the executor role session issuer."""
    event_time = dispatch_time + timedelta(seconds=event_time_offset_seconds)
    detail = {
        "eventName": event_name,
        "eventSource": "gamelift.amazonaws.com",
        "eventTime": event_time.isoformat().replace("+00:00", "Z"),
        "requestParameters": {"fleetId": fleet_id, "location": location, "desiredInstances": 1},
        "userIdentity": {
            "type": "AssumedRole",
            "arn": f"{role_arn}/executor-session",
            "sessionContext": {"sessionIssuer": {"arn": role_arn, "type": "Role"}},
        },
    }
    return {
        "EventName": event_name,
        "EventTime": event_time.timestamp(),
        "Username": "executor-session",
        "CloudTrailEvent": json.dumps(detail),
    }


# --------------------------------------------------------------------------- #
# Blocker 1: the evaluator event must carry the NEWLY-SUCCEEDED observation id.
# --------------------------------------------------------------------------- #


class _ScriptedRunner:
    def __init__(self, table: dict[str, object]) -> None:
        self._table = table
        self.calls: list[list[str]] = []
        self.evaluate_payloads: list[dict] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            if "--payload" in argv:
                self.evaluate_payloads.append(json.loads(argv[argv.index("--payload") + 1]))
            payload = self._table.get("lambda invoke", {"outcome": "refused", "reason": "decision_denied"})
            return _write_invoke_payload(argv, dict(payload))  # type: ignore[arg-type]
        value = self._table.get(key, {})
        return CommandResult(0, json.dumps(value), "")


def test_evaluate_uses_newly_succeeded_observation_id_not_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a succeeded observe, the evaluate step must send the observation id
    returned by the 06 poll — NOT the (stale) adapter-config observation id."""
    succeeded_id = "op_obs_FRESHLY_SUCCEEDED"
    monkeypatch.setenv("GBAW_E5_OBSERVE_BEARER_UNITTEST", "short-lived")

    def observe_http(call: HttpCall):
        return 200, {}, json.dumps({"operation_id": succeeded_id, "state": "succeeded"})

    runner = _ScriptedRunner({"lambda invoke": {"outcome": "refused", "reason": "decision_denied"}})
    cfg = _config()
    assert cfg.observation_operation_id != succeeded_id
    transport = CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=observe_http))
    hdr = {"authorization": "Bearer x"}
    # Observe first (as the harness does), then evaluate.
    transport("POST", "https://x/operations/observe", headers=hdr, body=b"{}")
    transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=json.dumps({"direction": "up"}).encode()
    )
    assert runner.evaluate_payloads, "evaluate must invoke the evaluator"
    event = runner.evaluate_payloads[-1]
    assert (
        event["observation_operation_id"] == succeeded_id
    ), "the evaluator event must carry the newly-succeeded observation id, not the stale config value"
    assert event["observation_operation_id"] != cfg.observation_operation_id


def test_evaluate_without_a_succeeded_observation_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evaluate before any succeeded observation must NOT fall back to the config
    observation id: it fails closed (denied, no write, no evaluator invoke)."""
    runner = _ScriptedRunner({"lambda invoke": {"outcome": "dispatched", "operation_id": "op_should_not_happen"}})
    cfg = _config()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner))
    hdr = {"authorization": "Bearer x"}
    resp = transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=json.dumps({"direction": "up"}).encode()
    )
    body = resp.json()
    assert body.get("decision") == "denied", "evaluate with no succeeded observation must be denied"
    assert body.get("wrote") is False
    assert not runner.evaluate_payloads, "the evaluator must NOT be invoked without a trusted observation id"


# --------------------------------------------------------------------------- #
# Blocker 2: cleanup uses an operator-owned bounded update-fleet-capacity inverse.
# --------------------------------------------------------------------------- #


def test_adapter_operator_inverse_issues_valid_update_fleet_capacity() -> None:
    """The cleanup adapter must issue a VALID GameLift update-fleet-capacity for
    the EXACT enrolled fleet/location at desired=0/min=0/max=1 — the valid ABI,
    not an evaluator invoke."""
    runner = _ScriptedRunner({"gamelift update-fleet-capacity": {"FleetId": _ENROLLED_FLEET}})
    adapter = CommandAdapter(_config(), runner=runner)
    assert hasattr(
        adapter, "operator_inverse_update_fleet_capacity"
    ), "adapter must expose an operator-owned bounded inverse cleanup command"
    adapter.operator_inverse_update_fleet_capacity()
    argv = next(a for a in runner.calls if a[1:3] == ["gamelift", "update-fleet-capacity"])
    assert "--fleet-id" in argv and argv[argv.index("--fleet-id") + 1] == _ENROLLED_FLEET
    assert "--location" in argv and argv[argv.index("--location") + 1] == _ENROLLED_LOCATION
    assert "--desired-instances" in argv and argv[argv.index("--desired-instances") + 1] == "0"
    assert "--min-size" in argv and argv[argv.index("--min-size") + 1] == "0"
    assert "--max-size" in argv and argv[argv.index("--max-size") + 1] == "1"


def test_cleanup_reconciles_via_operator_inverse_when_evaluator_cooldown_denies() -> None:
    """When the evaluator inverse is denied by the 300/600s limits, the guaranteed
    teardown must still restore the fleet to zero via the operator-owned
    update-fleet-capacity — never leaving the fleet at desired=1."""
    # Local modules
    from operations.validation.e5_shakedown import (
        REQUIRED_CONFIRMATION,
        REQUIRED_INVERSE_CONFIRMATION,
        E5Preflight,
        E5ShakedownConfig,
        E5ShakedownHarness,
    )

    class CooldownThenOperatorRunner:
        """Fleet starts at 1 (post scale-up). The EVALUATOR inverse is always
        cooldown-denied; only the operator update-fleet-capacity moves it to 0."""

        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.desired = 1
            self.enabled = True
            self.operator_inverse_calls = 0

        def __call__(self, argv: list[str]) -> CommandResult:
            self.calls.append(argv)
            key = f"{argv[1]} {argv[2]}"
            if key == "lambda invoke":
                event = json.loads(argv[argv.index("--payload") + 1]) if "--payload" in argv else {}
                fn = argv[argv.index("--function-name") + 1] if "--function-name" in argv else ""
                if "observation" in fn:
                    body = json.dumps({"operation_id": "op_obs_e2e", "state": "succeeded"})
                    return _write_invoke_payload(argv, {"statusCode": 200, "headers": {}, "body": body})
                # The evaluator ALWAYS refuses the inverse with a cooldown.
                return _write_invoke_payload(argv, {"outcome": "refused", "reason": "COOLDOWN_ACTIVE"})
            if key == "gamelift update-fleet-capacity":
                self.operator_inverse_calls += 1
                self.desired = 0
                return CommandResult(0, json.dumps({"FleetId": _ENROLLED_FLEET}), "")
            if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "FleetCapacity": [
                                {
                                    "Location": _ENROLLED_LOCATION,
                                    "InstanceCounts": {"DESIRED": self.desired, "MINIMUM": 0, "MAXIMUM": 1},
                                }
                            ]
                        }
                    ),
                    "",
                )
            if key == "appconfig get-configuration":
                pathlib.Path(argv[-1]).write_text(
                    json.dumps(_fresh_switch_doc(autonomy_enabled=self.enabled)), encoding="utf-8"
                )
                return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
            if key == "appconfig start-deployment":
                self.enabled = False
                return CommandResult(0, json.dumps({"DeploymentNumber": 1}), "")
            return CommandResult(0, "{}", "")

    runner = CooldownThenOperatorRunner()
    adapter = CommandAdapter(_config(), runner=runner)
    transport = CommandTransport(adapter)
    preflight = E5Preflight(
        profile="unit",
        region="us-west-2",
        fleet_id=_ENROLLED_FLEET,
        enrolled_profile="unit",
        enrolled_region="us-west-2",
        enrolled_fleet_id=_ENROLLED_FLEET,
        starting_desired=0,
        starting_minimum=0,
        starting_maximum=1,
        static_gate_fresh_enabled=True,
        e4_gate_fresh_enabled=True,
        autonomy_switch_fresh_enabled=True,
        alarms_safe=True,
        drift_safe=True,
    )
    cfg = E5ShakedownConfig(
        endpoint="https://api.example.execute-api.us-west-2.amazonaws.com",
        admin_bearer="admin-token",
        fleet_id=_ENROLLED_FLEET,
        observation_id="obs_e2e",
        operation_id="op_e2e",
        preflight=preflight,
        confirmation=REQUIRED_CONFIRMATION,
        inverse_confirmation=REQUIRED_INVERSE_CONFIRMATION,
    )
    # The harness must be given the cleanup adapter so teardown can issue the
    # operator-owned inverse rather than the (cooldown-denied) evaluator inverse.
    harness = E5ShakedownHarness(cfg, transport, sleep=lambda *_: None, cleanup_adapter=adapter)
    summary = harness.run()
    restore = next(c for c in summary["checks"] if c["name"] == "guaranteed_restore")
    assert restore["passed"] is True, f"teardown must restore to zero, got: {restore}"
    assert runner.operator_inverse_calls >= 1, "teardown must use the operator update-fleet-capacity inverse"
    assert runner.desired == 0, "the fleet must not be left at desired=1"


def test_cleanup_operator_inverse_requires_inverse_confirmation() -> None:
    """The operator-owned cleanup adapter is TEST TOOLING and must refuse to issue
    the update-fleet-capacity inverse unless the operator's inverse confirmation
    was supplied."""
    # Local modules
    from operations.validation.e5_shakedown import (
        REQUIRED_CONFIRMATION,
        E5Preflight,
        E5ShakedownConfig,
        E5ShakedownHarness,
    )

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, argv: list[str]) -> CommandResult:
            self.calls.append(argv)
            key = f"{argv[1]} {argv[2]}"
            if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "FleetCapacity": [
                                {
                                    "Location": _ENROLLED_LOCATION,
                                    "InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1},
                                }
                            ]
                        }
                    ),
                    "",
                )
            return CommandResult(0, "{}", "")

    runner = Recorder()
    adapter = CommandAdapter(_config(), runner=runner)
    # Missing the inverse confirmation: the whole run is refused up front, but the
    # cleanup adapter itself must ALSO refuse to issue update-fleet-capacity.
    preflight = E5Preflight(
        profile="unit",
        region="us-west-2",
        fleet_id=_ENROLLED_FLEET,
        enrolled_profile="unit",
        enrolled_region="us-west-2",
        enrolled_fleet_id=_ENROLLED_FLEET,
        starting_desired=0,
        starting_minimum=0,
        starting_maximum=1,
        static_gate_fresh_enabled=True,
        e4_gate_fresh_enabled=True,
        autonomy_switch_fresh_enabled=True,
        alarms_safe=True,
        drift_safe=True,
    )
    cfg = E5ShakedownConfig(
        endpoint="https://api.example.execute-api.us-west-2.amazonaws.com",
        admin_bearer="admin-token",
        fleet_id=_ENROLLED_FLEET,
        observation_id="obs_e2e",
        operation_id="op_e2e",
        preflight=preflight,
        confirmation=REQUIRED_CONFIRMATION,
        inverse_confirmation="",  # missing
    )
    harness = E5ShakedownHarness(cfg, CommandTransport(adapter), sleep=lambda *_: None, cleanup_adapter=adapter)
    summary = harness.run()
    assert summary["refused"] is True
    assert "INVERSE_CONFIRMATION_REQUIRED" in summary["refusal_codes"]
    # No update-fleet-capacity write may be issued without the inverse confirmation.
    assert not any(a[1:3] == ["gamelift", "update-fleet-capacity"] for a in runner.calls)


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


# --------------------------------------------------------------------------- #
# Blocker 3: the deploy wrapper reads & compares 07 table/KMS/tenant/workspace.
# --------------------------------------------------------------------------- #


def test_wrapper_compares_07_table_kms_tenant_workspace_to_06() -> None:
    """The activation wrapper must read the 07 execution stack's OperationsTableName,
    OperationsKmsKeyArn, TenantId, and WorkspaceId and refuse unless each byte-equals
    the 06 value (preserve only after equality)."""
    text = WRAPPER.read_text(encoding="utf-8")
    # It must query the 07 stack (EXECUTION_STACK_NAME) for these four coordinates.
    for key in ("OperationsTableName", "OperationsKmsKeyArn", "TenantId", "WorkspaceId"):
        assert re.search(
            rf"EXECUTION_STACK_NAME.*{key}|{key}.*EXECUTION_STACK_NAME", text, re.DOTALL
        ), f"wrapper must read the 07 {key}"
    # It must compare the 07 values to the 06-derived values and refuse on mismatch.
    assert "STACK_07_TABLE" in text and "STACK_06_TABLE" in text
    assert re.search(
        r'\[\s*"\$STACK_07_TABLE"\s*!=\s*"\$STACK_06_TABLE"\s*\]', text
    ), "wrapper must refuse when the 07 table does not equal the 06 table"
    assert re.search(
        r'\[\s*"\$STACK_07_KMS"\s*!=\s*"\$STACK_06_KMS"\s*\]', text
    ), "wrapper must refuse when the 07 CMK does not equal the 06 CMK"
    assert re.search(
        r'\[\s*"\$STACK_07_TENANT"\s*!=\s*"\$STACK_06_TENANT"\s*\]', text
    ), "wrapper must refuse when the 07 tenant does not equal the 06 tenant"
    assert re.search(
        r'\[\s*"\$STACK_07_WORKSPACE"\s*!=\s*"\$STACK_06_WORKSPACE"\s*\]', text
    ), "wrapper must refuse when the 07 workspace does not equal the 06 workspace"


# --------------------------------------------------------------------------- #
# Blocker 4: CloudTrail write attribution is correlated, not a Username literal.
# --------------------------------------------------------------------------- #


def _observe_ok(call: HttpCall):
    return 200, {}, json.dumps({"operation_id": "op_obs_for_attr", "state": "succeeded"})


def _dispatch_transport(runner: object, cfg: CommandAdapterConfig) -> CommandTransport:
    return CommandTransport(CommandAdapter(cfg, runner=runner, http_caller=_observe_ok))  # type: ignore[arg-type]


class _AttribRunner:
    """A dispatched-outcome runner whose CloudTrail lookup returns a scripted list
    of events; the write actor is derived from correlation, not Events[0]."""

    def __init__(self, cloudtrail_events: list[dict]) -> None:
        self._events = cloudtrail_events
        self.calls: list[list[str]] = []
        self.op = "op_attr"

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            return _write_invoke_payload(argv, {"outcome": "dispatched", "operation_id": self.op})
        if key == "stepfunctions describe-execution":
            return CommandResult(0, json.dumps({"status": "SUCCEEDED"}), "")
        if key == "dynamodb get-item":
            k = json.loads(argv[argv.index("--key") + 1])
            if k["SK"]["S"] == "AUTZDISPATCH#dispatched":
                return CommandResult(0, json.dumps(_dispatched_audit_item(self.op)), "")
            return CommandResult(0, json.dumps(_reservation_item(self.op)), "")
        if key == "cloudtrail lookup-events":
            return CommandResult(0, json.dumps({"Events": self._events}), "")
        if key in ("gamelift describe-fleet-capacity", "gamelift describe-fleet-location-capacity"):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "FleetCapacity": [
                            {
                                "Location": _ENROLLED_LOCATION,
                                "InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1},
                            }
                        ]
                    }
                ),
                "",
            )
        return CommandResult(0, "{}", "")


def _dispatch(transport: CommandTransport, monkeypatch: object = None) -> dict:
    hdr = {"authorization": "Bearer x"}
    # Establish a trusted observation first (the harness always observes before it
    # evaluates); the observe bearer is read from the env var the config names.
    # Standard library
    import os

    os.environ["GBAW_E5_OBSERVE_BEARER_UNITTEST"] = "short-lived"
    transport("POST", "https://x/operations/observe", headers=hdr, body=b"{}")
    resp = transport("POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=b'{"direction":"up"}')
    return resp.json()


def test_attribution_binds_eventname_time_fleet_location_and_executor_role() -> None:
    """A fully-correlated UpdateFleetCapacity event by the exact executor role is
    the ONLY thing that reports the executor write actor."""
    dispatch_time = datetime.now(timezone.utc)
    events = [_cloudtrail_update_fleet_event(dispatch_time=dispatch_time)]
    runner = _AttribRunner(events)
    # Provide the dispatch time so 'after dispatch' can be evaluated.
    cfg = _config()
    transport = _dispatch_transport(runner, cfg)
    body = _dispatch(transport)
    assert body["cloudtrail_write_actor"] == "executor", "a fully-correlated executor write must attribute to executor"


def test_attribution_rejects_newest_username_without_binding() -> None:
    """A newest event that is NOT UpdateFleetCapacity by the executor role must
    NOT be attributed to the executor, even if its Username literally says so."""
    dispatch_time = datetime.now(timezone.utc)
    # Newest event is an unrelated DescribeFleetCapacity, whose Username is
    # 'executor' — the old code would have trusted this literal.
    unrelated = {
        "EventName": "DescribeFleetCapacity",
        "Username": "executor",
        "CloudTrailEvent": json.dumps(
            {
                "eventName": "DescribeFleetCapacity",
                "eventTime": (dispatch_time + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
                "requestParameters": {"fleetId": _ENROLLED_FLEET},
                "userIdentity": {"sessionContext": {"sessionIssuer": {"arn": _EXECUTOR_ROLE_ARN}}},
            }
        ),
    }
    runner = _AttribRunner([unrelated])
    body = _dispatch(_dispatch_transport(runner, _config()))
    assert (
        body["cloudtrail_write_actor"] != "executor"
    ), "a non-UpdateFleetCapacity newest Username must not be attributed to the executor"


def test_attribution_rejects_foreign_role_even_on_update_fleet_capacity() -> None:
    """An UpdateFleetCapacity for the exact fleet but by a FOREIGN role must not be
    attributed to the executor."""
    dispatch_time = datetime.now(timezone.utc)
    foreign = _cloudtrail_update_fleet_event(
        dispatch_time=dispatch_time, role_arn="arn:aws:iam::000000000000:role/some-other-role"
    )
    runner = _AttribRunner([foreign])
    body = _dispatch(_dispatch_transport(runner, _config()))
    assert body["cloudtrail_write_actor"] != "executor"


def test_attribution_rejects_wrong_fleet_or_location() -> None:
    """An UpdateFleetCapacity by the executor role but for the WRONG fleet/location
    must not attribute to the executor."""
    dispatch_time = datetime.now(timezone.utc)
    wrong_fleet = _cloudtrail_update_fleet_event(dispatch_time=dispatch_time, fleet_id="fleet-DIFFERENT")
    assert (
        _dispatch(_dispatch_transport(_AttribRunner([wrong_fleet]), _config()))["cloudtrail_write_actor"] != "executor"
    )
    wrong_loc = _cloudtrail_update_fleet_event(dispatch_time=dispatch_time, location="eu-west-1")
    assert _dispatch(_dispatch_transport(_AttribRunner([wrong_loc]), _config()))["cloudtrail_write_actor"] != "executor"


def test_attribution_rejects_event_before_dispatch() -> None:
    """An UpdateFleetCapacity event that predates the dispatch cannot be the write
    from THIS dispatch and must not attribute to the executor."""
    dispatch_time = datetime.now(timezone.utc)
    stale = _cloudtrail_update_fleet_event(dispatch_time=dispatch_time, event_time_offset_seconds=-120)
    body = _dispatch(_dispatch_transport(_AttribRunner([stale]), _config()))
    assert body["cloudtrail_write_actor"] != "executor"


def test_attribution_unreadable_event_is_unknown_not_executor() -> None:
    """An unreadable/absent CloudTrail event is UNKNOWN — never the executor."""
    runner = _AttribRunner([])  # no events at all
    body = _dispatch(_dispatch_transport(runner, _config()))
    assert body["cloudtrail_write_actor"] != "executor"
