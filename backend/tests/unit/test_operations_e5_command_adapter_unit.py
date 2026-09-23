"""Tests for the concrete E5 command adapter and measured preflight (#440, Findings 7 & 8).

These prove the ``--adapter command`` path issues concrete, structured ``aws``
CLI JSON commands through an injectable runner (never HTTP, never an imaginary
``/operations/autonomy/*`` route), and that the preflight facts are MEASURED from
the provider rather than trusted from operator booleans.

No test here calls AWS: every command goes through a fake runner that records the
argv and returns canned JSON.
"""

from __future__ import annotations

# Standard library
import json
import pathlib

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
    )


class FakeRunner:
    """Records every argv and returns queued JSON results; never touches AWS."""

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses or {}

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        # Key by "<service> <operation>".
        key = f"{argv[1]} {argv[2]}"
        payload = self._responses.get(key, {})
        return CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")


def test_every_command_is_a_structured_aws_json_argv() -> None:
    """Each lifecycle operation issues an `aws <svc> <op> --output json` argv."""
    runner = FakeRunner(
        {
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
        }
    )
    adapter = CommandAdapter(_config(), runner=runner)
    adapter.invoke_evaluate({"observation_operation_id": "op", "desired": 1, "minimum": 0, "maximum": 1})
    adapter.invoke_observe({"kind": "capacity"})
    adapter.describe_execution("arn:aws:states:us-west-2:000000000000:execution:sm:exec")
    adapter.get_audit_reservation_item({"pk": {"S": "op"}})
    adapter.lookup_write_attribution("fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555")
    adapter.describe_fleet_capacity()
    adapter.deploy_disabled_autonomy_switch("1")
    assert runner.calls, "adapter must issue commands"
    for argv in runner.calls:
        assert argv[0] == "aws", f"every command must be an aws CLI invocation: {argv}"
        assert "--output" in argv and argv[argv.index("--output") + 1] == "json"
        assert "--region" in argv
        # No HTTP artifacts anywhere in the argv.
        assert not any("http" in tok for tok in argv), f"no HTTP in a command adapter argv: {argv}"
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


def test_transport_maps_known_paths_to_commands() -> None:
    """Known paths map to real commands and consume the REAL evaluator contract.

    The evaluator returns {outcome, operation_id}; a dispatched outcome plus the
    supporting evidence reads yield an authorized decision. Capacity is reported
    under the nested body['capacity']['desired'] shape the harness reads, and
    disable reports autonomy_enabled=False."""
    runner = FakeRunner(
        {
            "lambda invoke": {"outcome": "dispatched", "operation_id": "op_abc"},
            "stepfunctions describe-execution": {"status": "SUCCEEDED"},
            "dynamodb get-item": {"Item": {"audit": {"BOOL": True}, "reservation": {"BOOL": True}}},
            "cloudtrail lookup-events": {"Events": [{"Username": "executor"}]},
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
            "appconfig start-deployment": {"DeploymentNumber": 1},
        }
    )
    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    hdr = {"authorization": "Bearer x"}
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
    runner = FakeRunner(
        {
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
            "cloudwatch describe-alarms": {
                "MetricAlarms": [
                    {"StateValue": "ALARM"},
                    {"StateValue": "OK"},
                    {"StateValue": "OK"},
                    {"StateValue": "OK"},
                ]
            },
            "appconfig get-configuration": {"enabled": True, "expired": False},
        }
    )
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
    """Records argv and returns per-command JSON, capturing the evaluator payload."""

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.evaluate_payloads: list[dict] = []
        self._responses = responses or {}

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            # Capture the --payload of the evaluator invoke.
            if "--payload" in argv:
                raw = argv[argv.index("--payload") + 1]
                self.evaluate_payloads.append(json.loads(raw))
        payload = self._responses.get(key, {})
        return CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")


def _adapter_config_with_event() -> CommandAdapterConfig:
    """The config MUST carry the trusted observation operation id so the transport
    can build the exact closed event; a direction-only body is not the contract."""
    cfg = _config()
    # The observation_operation_id is a required server-owned coordinate.
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
            "stepfunctions describe-execution": {"status": "RUNNING", "executionArn": "arn:sfn:exec"},
            "dynamodb get-item": {"Item": {"audit": {"BOOL": True}}},
            "cloudtrail lookup-events": {"Events": [{"Username": "executor"}]},
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
        }
    )
    cfg = _adapter_config_with_event()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner))
    hdr = {"authorization": "Bearer x"}
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
    assert event["observation_operation_id"] == cfg.observation_operation_id
    assert isinstance(event["desired"], int) and isinstance(event["minimum"], int) and isinstance(event["maximum"], int)


def test_transport_evaluate_up_and_down_map_to_capacity_triples() -> None:
    """up -> desired 1, down -> desired 0; minimum/maximum stay the 0/1 window."""
    runner = RecordingRunner({"lambda invoke": {"outcome": "refused", "reason": "decision_denied"}})
    cfg = _adapter_config_with_event()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner))
    hdr = {"authorization": "Bearer x"}
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
    transport = CommandTransport(CommandAdapter(cfg, runner=runner))
    hdr = {"authorization": "Bearer x"}
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
    # CloudTrail shows a NON-executor actor -> the executor-only assertion fails.
    runner = RecordingRunner(
        {
            "lambda invoke": {"outcome": "dispatched", "operation_id": "op_abc"},
            "stepfunctions describe-execution": {"status": "SUCCEEDED", "executionArn": "arn:sfn:exec"},
            "dynamodb get-item": {"Item": {"audit": {"BOOL": True}, "reservation": {"BOOL": True}}},
            "cloudtrail lookup-events": {"Events": [{"Username": "some-other-principal"}]},
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
        }
    )
    cfg = _adapter_config_with_event()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner))
    hdr = {"authorization": "Bearer x"}
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
    runner = RecordingRunner(
        {
            "lambda invoke": {"outcome": "refused", "reason": "autonomy_disabled"},
            "stepfunctions describe-execution": {"status": "ABSENT"},
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
            "cloudtrail lookup-events": {"Events": []},
        }
    )
    cfg = _adapter_config_with_event()
    transport = CommandTransport(CommandAdapter(cfg, runner=runner))
    hdr = {"authorization": "Bearer x"}
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

    - lambda invoke (evaluate): 'up' -> desired 1 & dispatched; 'down' -> desired
      0 & dispatched; a second immediate 'up' at desired 1 -> refused COOLDOWN.
    - stepfunctions/dynamodb/cloudtrail: report evidence for the last dispatch.
    - gamelift describe-fleet-capacity: the current modeled desired.
    - appconfig start-deployment (disable): flips autonomy off; a later evaluate
      then refuses AUTONOMY_DISABLED and starts no execution.
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
            # observe probe carries only the observation id.
            if set(event) == {"observation_operation_id"}:
                return self._json({"trusted": True})
            want = event.get("desired")
            if not self.enabled:
                self.dispatched = False
                return self._json({"outcome": "refused", "reason": "AUTONOMY_DISABLED"})
            if want == 1 and self.desired == 1:
                # Immediate retry at the top of the window is cooldown-denied.
                self.dispatched = False
                return self._json({"outcome": "refused", "reason": "COOLDOWN_ACTIVE"})
            self.desired = int(want)
            self.dispatched = True
            self.last_op = "op_e2e"
            return self._json({"outcome": "dispatched", "operation_id": self.last_op})
        if key == "stepfunctions describe-execution":
            return self._json({"status": "SUCCEEDED"} if self.dispatched else {"status": "ABSENT"})
        if key == "dynamodb get-item":
            if self.dispatched:
                return self._json(
                    {"Item": {"audit": {"BOOL": True}, "reservation": {"BOOL": True}, "prepared_hash": {"S": "h"}}}
                )
            return self._json({})
        if key == "cloudtrail lookup-events":
            return self._json({"Events": [{"Username": "executor"}]} if self.dispatched else {"Events": []})
        if key == "gamelift describe-fleet-capacity":
            return self._json(
                {"FleetCapacity": [{"InstanceCounts": {"DESIRED": self.desired, "MINIMUM": 0, "MAXIMUM": 1}}]}
            )
        if key == "cloudwatch describe-alarms":
            return self._json({"MetricAlarms": [{"StateValue": "OK"}] * 4})
        if key == "appconfig get-configuration":
            return self._json({"enabled": True, "expired": False})
        if key == "appconfig start-deployment":
            self.enabled = False
            return self._json({"DeploymentNumber": 1})
        return self._json({})

    @staticmethod
    def _json(payload: dict) -> CommandResult:
        return CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")


def test_command_transport_drives_full_harness_to_accepted() -> None:
    """The real CommandTransport, driven through the full E5 harness with good
    evidence, is ACCEPTED and every check passes."""
    # Local modules
    from operations.validation.e5_shakedown import (
        REQUIRED_CONFIRMATION,
        REQUIRED_INVERSE_CONFIRMATION,
        E5Preflight,
        E5ShakedownConfig,
        E5ShakedownHarness,
    )

    runner = ScriptedLifecycleRunner()
    cfg = _config()
    # Bind the trusted observation id the closed event carries.
    # Standard library
    import dataclasses

    cfg = dataclasses.replace(cfg, observation_operation_id="op_obs_e2e")
    transport = CommandTransport(CommandAdapter(cfg, runner=runner))

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
    summary = E5ShakedownHarness(shakedown_cfg, transport).run()
    failed = [c for c in summary["checks"] if not c["passed"]]
    assert summary["accepted"] is True, f"harness not accepted; failing checks: {[c['name'] for c in failed]}"
    assert summary["refused"] is False
    # The evaluate step never forwarded a 'direction' to the evaluator.
    for argv in runner.calls:
        if argv[1:3] == ["lambda", "invoke"] and "--payload" in argv:
            ev = json.loads(argv[argv.index("--payload") + 1])
            assert "direction" not in ev
