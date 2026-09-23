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
    runner = FakeRunner(
        {
            "lambda invoke": {"decision": "authorized", "audit_recorded": True},
            "gamelift describe-fleet-capacity": {
                "FleetCapacity": [{"InstanceCounts": {"DESIRED": 1, "MINIMUM": 0, "MAXIMUM": 1}}]
            },
            "appconfig start-deployment": {"DeploymentNumber": 1},
        }
    )
    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    hdr = {"authorization": "Bearer x"}
    ev = transport("POST", "https://x/operations/autonomy/op/evaluate", headers=hdr, body=b"{}")
    assert ev.json()["decision"] == "authorized"
    cap = transport("GET", "https://x/operations/autonomy/op/capacity", headers=hdr, body=None)
    assert cap.json()["desired"] == 1
    dis = transport("POST", "https://x/operations/autonomy/disable", headers=hdr, body=b"{}")
    assert dis.json()["disabled"] is True
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
