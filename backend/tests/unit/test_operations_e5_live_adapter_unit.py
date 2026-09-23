"""Real-deployed-ABI tests for the E5 live command adapter (#440 semantic review).

These pin the SIX live adapter/binding blockers found in the semantic review of
#440 at 09bbc06 to the EXACT deployed contracts of the 06 observation Lambda, the
#439 evaluator, the #439 reservation/bundle store, the 07 Step Functions
execution, and the E4/E5 AppConfig documents:

1. ``aws lambda invoke`` writes its RESPONSE PAYLOAD to a tempfile ``OutputFile``
   and the CLI INVOKE METADATA to stdout; the two must be parsed separately (the
   old ``/dev/stdout`` + single-JSON parse concatenated them). The observe step
   invokes the real 06 observation Lambda with its exact API-Gateway-proxy event
   ABI and parses ``observation_id`` from the succeeded response body — never a
   fabricated ``{"trusted": true}``.
2. The dispatched Step Functions execution is described by an ``executionArn``
   built from the REAL deterministic execution name (``operation_id[:80]``) and
   the configured state-machine ARN — not the raw ``operation_id``. The 06/#439
   store items are read with the REAL uppercase ``PK``/``SK`` keys
   (``AUTZBUNDLE#``/``AUTZRSV#``/``AUTZDISPATCH#``) and the assertions come from
   items exactly as stored — never lowercase ``pk`` or synthetic attributes.
3. The preflight reads the REAL evaluator env var ``GBAW_OPERATIONS_MODE`` and
   the REAL E4/E5 AppConfig document keys (``issued_at``/``not_after``/
   ``operations_enabled``/``autonomy_enabled``) via the required OutputFile —
   never nonexistent ``enabled``/``expired`` keys.
4. Location/drift/enrollment are FRESH/CURRENT: capacity is read for the enrolled
   location; drift is a fresh ``detect-stack-drift`` polled by detection id, not
   the last-cached ``DriftInformation``.
5. Cleanup treats an UNREADABLE switch as UNKNOWN (not disabled) and only
   forced-write-denies when the evidence lookup SUCCEEDS.

Every command goes through a fake runner; no test here calls AWS.
"""

from __future__ import annotations

# Standard library
import dataclasses
import json
import pathlib
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# Local modules
from operations.validation.e5_command_adapter import (
    CommandAdapter,
    CommandAdapterConfig,
    CommandResult,
    CommandTransport,
)

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]

# The real deterministic Step Functions state-machine ARN prefix (07 stack).
_STATE_MACHINE_ARN = "arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-autonomy"


def _config(**overrides: object) -> CommandAdapterConfig:
    base = dict(
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
        observation_operation_id="op_obs_trusted",
        autonomy_state_machine_arn=_STATE_MACHINE_ARN,
        enrolled_location="us-west-2",
    )
    base.update(overrides)
    return CommandAdapterConfig(**base)  # type: ignore[arg-type]


def _fresh_switch_doc(*, autonomy_enabled: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "autonomy_switch_version": "1",
        "config_version": 1,
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "not_after": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "autonomy_enabled": autonomy_enabled,
        "capabilities": {},
    }


def _fresh_kill_switch_doc(*, operations_enabled: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "control_switch_version": "1",
        "config_version": 1,
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "not_after": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "operations_enabled": operations_enabled,
    }


# --------------------------------------------------------------------------- #
# Blocker 1: tempfile OutputFile for aws lambda invoke; payload parsed apart
# from the CLI invoke metadata; real observe ABI; never fabricate trusted.
# --------------------------------------------------------------------------- #


def test_lambda_invoke_uses_tempfile_outfile_and_parses_payload_not_metadata() -> None:
    """The evaluator invoke must write the payload to a tempfile OutputFile and
    parse THAT file, not the concatenated CLI metadata on stdout."""
    captured: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "invoke"]:
            captured["argv"] = argv
            # The CLI writes the function response payload to the positional
            # OutputFile and prints ONLY invoke metadata to stdout.
            outfile = argv[-1]
            assert outfile != "/dev/stdout", "must use a real tempfile OutputFile, not /dev/stdout"
            pathlib.Path(outfile).write_text(
                json.dumps({"outcome": "dispatched", "operation_id": "op_abc"}), encoding="utf-8"
            )
            return CommandResult(
                returncode=0,
                stdout=json.dumps({"StatusCode": 200, "ExecutedVersion": "$LATEST"}),
                stderr="",
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(), runner=runner)
    result = adapter.invoke_evaluate({"observation_operation_id": "op", "desired": 1, "minimum": 0, "maximum": 1})
    argv = captured["argv"]
    # A real OutputFile positional (not /dev/stdout, not a flag).
    assert not argv[-1].startswith("--")
    assert argv[-1] != "/dev/stdout"
    # The parsed result is the FUNCTION PAYLOAD, not the CLI metadata.
    assert result == {"outcome": "dispatched", "operation_id": "op_abc"}
    assert "StatusCode" not in result, "must not return the CLI invoke metadata as the payload"


def test_lambda_invoke_detects_function_error_from_metadata() -> None:
    """A FunctionError in the CLI metadata must be treated as a failed invoke
    (fail closed), not parsed as a successful payload."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "invoke"]:
            outfile = argv[-1]
            pathlib.Path(outfile).write_text(json.dumps({"errorMessage": "boom"}), encoding="utf-8")
            return CommandResult(
                returncode=0,
                stdout=json.dumps({"StatusCode": 200, "FunctionError": "Unhandled"}),
                stderr="",
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(), runner=runner)
    # Local modules
    from operations.validation.e5_command_adapter import CommandError

    with pytest.raises(CommandError):
        adapter.invoke_evaluate({"observation_operation_id": "op", "desired": 1, "minimum": 0, "maximum": 1})


def test_observe_step_invokes_real_06_abi_and_parses_observation_id() -> None:
    """The observe step must invoke the 06 observation Lambda with its exact
    API-Gateway-proxy event ABI (POST + requestContext.authorizer.jwt.claims +
    JSON body) and parse ``observation_id`` from the succeeded response body —
    never fabricate ``{"trusted": true}``."""
    captured: dict[str, object] = {}

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "invoke"]:
            event = json.loads(argv[argv.index("--payload") + 1])
            captured["event"] = event
            outfile = argv[-1]
            # The observe handler returns an API-Gateway proxy response whose body
            # is a JSON STRING carrying observation_id for a succeeded observation.
            body = json.dumps(
                {
                    "observation_contract_version": "1",
                    "observation_id": "op_obs_succeeded",
                    "phase": "observe",
                }
            )
            pathlib.Path(outfile).write_text(
                json.dumps({"statusCode": 200, "headers": {}, "body": body}), encoding="utf-8"
            )
            return CommandResult(0, json.dumps({"StatusCode": 200}), "")
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(), runner=runner)
    observation_id = adapter.observe_succeeded_observation_id()
    assert observation_id == "op_obs_succeeded", "must parse the real observation_id from the response body"
    event = captured["event"]
    assert isinstance(event, dict)
    # The event is the real proxy ABI, not a bare {observation_operation_id}.
    assert "requestContext" in event, "observe invoke must carry the API Gateway proxy requestContext"
    assert (
        event.get("httpMethod") == "POST" or event.get("requestContext", {}).get("http", {}).get("method") == "POST"
    ), "observe is a POST"
    assert isinstance(event.get("body"), str), "the proxy event body must be a JSON string"


def test_transport_observe_reports_trusted_only_on_real_succeeded_observation() -> None:
    """The observe transport step must report trusted=True ONLY when the real 06
    Lambda returns a succeeded observation; an error/ambiguous body is not
    trusted."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "invoke"]:
            outfile = argv[-1]
            # Non-200 proxy response -> not a succeeded observation.
            pathlib.Path(outfile).write_text(
                json.dumps({"statusCode": 500, "headers": {}, "body": json.dumps({"error_code": "INTERNAL_ERROR"})}),
                encoding="utf-8",
            )
            return CommandResult(0, json.dumps({"StatusCode": 200}), "")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    resp = transport("POST", "https://x/operations/observe", headers={"authorization": "Bearer x"}, body=b"{}")
    assert resp.json().get("trusted") is not True, "an unsucceeded observation must not be reported trusted"


# --------------------------------------------------------------------------- #
# Blocker 2: executionArn from real execution name; real uppercase PK/SK keys.
# --------------------------------------------------------------------------- #


def test_describe_execution_uses_arn_built_from_execution_name_not_operation_id() -> None:
    """The dispatched execution is described by an ``executionArn`` built from the
    state-machine ARN and the REAL deterministic execution name
    (``operation_id[:80]``) — never the raw operation id as the ARN."""
    captured: dict[str, str] = {}
    operation_id = "op_" + ("x" * 100)  # longer than 80 to prove truncation

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            outfile = argv[-1]
            pathlib.Path(outfile).write_text(
                json.dumps({"outcome": "dispatched", "operation_id": operation_id}), encoding="utf-8"
            )
            return CommandResult(0, json.dumps({"StatusCode": 200}), "")
        if key == "stepfunctions describe-execution":
            captured["execution_arn"] = argv[argv.index("--execution-arn") + 1]
            return CommandResult(0, json.dumps({"status": "SUCCEEDED"}), "")
        if key == "dynamodb get-item":
            return CommandResult(0, json.dumps({}), "")
        if key == "cloudtrail lookup-events":
            return CommandResult(0, json.dumps({"Events": []}), "")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    transport("POST", "https://x/operations/autonomy/op/evaluate", headers={"authorization": "Bearer x"}, body=b"{}")
    arn = captured.get("execution_arn", "")
    assert arn.startswith("arn:aws:states:"), f"must describe by a real execution ARN, got {arn!r}"
    assert ":execution:" in arn, "the ARN must be an execution ARN"
    assert arn != operation_id, "must not pass the raw operation id as the execution ARN"
    # The deterministic execution name is operation_id[:80].
    assert arn.endswith(operation_id[:80]), "execution name must be operation_id[:80]"


def test_audit_reservation_reads_use_real_uppercase_pk_sk_keys() -> None:
    """The evidence reads must key the 06/#439 items with the REAL uppercase
    ``PK``/``SK`` (``AUTZBUNDLE#<op>``/``AUTZDISPATCH#dispatched`` and
    ``AUTZRSV#<op>``/``AUTZRSV``) — never lowercase ``pk`` and never a keyless
    read."""
    keys_seen: list[dict] = []
    operation_id = "op_abc"

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            pathlib.Path(argv[-1]).write_text(
                json.dumps({"outcome": "dispatched", "operation_id": operation_id}), encoding="utf-8"
            )
            return CommandResult(0, json.dumps({"StatusCode": 200}), "")
        if key == "dynamodb get-item":
            k = json.loads(argv[argv.index("--key") + 1])
            keys_seen.append(k)
            # Return the dispatched audit / reservation items exactly as stored.
            pk = k.get("PK", {}).get("S", "")
            sk = k.get("SK", {}).get("S", "")
            if pk == f"AUTZBUNDLE#{operation_id}" and sk == "AUTZDISPATCH#dispatched":
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "Item": {
                                "PK": {"S": pk},
                                "SK": {"S": sk},
                                "operation_id": {"S": operation_id},
                                "phase": {"S": "dispatched"},
                                "execution_name": {"S": operation_id[:80]},
                                "audit": {"S": "{}"},
                            }
                        }
                    ),
                    "",
                )
            if pk == f"AUTZRSV#{operation_id}" and sk == "AUTZRSV":
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "Item": {
                                "PK": {"S": pk},
                                "SK": {"S": sk},
                                "operation_id": {"S": operation_id},
                                "settled": {"BOOL": False},
                            }
                        }
                    ),
                    "",
                )
            return CommandResult(0, json.dumps({}), "")
        if key == "stepfunctions describe-execution":
            return CommandResult(0, json.dumps({"status": "SUCCEEDED"}), "")
        if key == "cloudtrail lookup-events":
            return CommandResult(0, json.dumps({"Events": [{"Username": "executor"}]}), "")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    resp = transport(
        "POST", "https://x/operations/autonomy/op/evaluate", headers={"authorization": "Bearer x"}, body=b"{}"
    )
    body = resp.json()
    # No lowercase pk anywhere; at least one real uppercase PK/SK read happened.
    assert keys_seen, "must issue a dynamodb get-item for evidence"
    for k in keys_seen:
        assert "pk" not in k, f"must use uppercase PK, not lowercase pk: {k}"
        assert "PK" in k and "SK" in k, f"the 06/#439 store is a PK/SK table: {k}"
    real_keys = {(k["PK"]["S"], k["SK"]["S"]) for k in keys_seen}
    assert (f"AUTZBUNDLE#{operation_id}", "AUTZDISPATCH#dispatched") in real_keys
    assert (f"AUTZRSV#{operation_id}", "AUTZRSV") in real_keys
    # The assertions are derived from the items exactly as stored.
    assert body["audit_recorded"] is True
    assert body["reservation_granted"] is True
    assert body["step_functions_started"] is True


# --------------------------------------------------------------------------- #
# Blocker 3: real AppConfig document keys + GBAW_OPERATIONS_MODE.
# --------------------------------------------------------------------------- #


def test_autonomy_switch_read_uses_real_document_keys() -> None:
    """The autonomy-switch freshness read must use the REAL document keys
    (``issued_at``/``not_after``/``autonomy_enabled``), not ``enabled``/
    ``expired``."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            pathlib.Path(argv[-1]).write_text(json.dumps(_fresh_switch_doc(autonomy_enabled=True)), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(), runner=runner)
    assert adapter.observed_autonomy_switch_fresh_enabled() is True

    def runner_disabled(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            pathlib.Path(argv[-1]).write_text(json.dumps(_fresh_switch_doc(autonomy_enabled=False)), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    adapter2 = CommandAdapter(_config(), runner=runner_disabled)
    assert adapter2.observed_autonomy_switch_fresh_enabled() is False, "autonomy_enabled=False must not be enabled"

    # An expired (past not_after) document is stale -> fail closed.
    def runner_stale(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            now = datetime.now(timezone.utc)
            doc = _fresh_switch_doc(autonomy_enabled=True)
            doc["not_after"] = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            pathlib.Path(argv[-1]).write_text(json.dumps(doc), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    adapter3 = CommandAdapter(_config(), runner=runner_stale)
    assert adapter3.observed_autonomy_switch_fresh_enabled() is False, "a past-not_after doc is stale"


def test_e4_kill_switch_read_uses_real_document_keys() -> None:
    """The E4 kill-switch freshness read must use the REAL document keys
    (``issued_at``/``not_after``/``operations_enabled``)."""
    cfg = _config(
        kill_switch_application_id="ks-app",
        kill_switch_environment_id="ks-env",
        kill_switch_profile_id="ks-prof",
    )

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            pathlib.Path(argv[-1]).write_text(json.dumps(_fresh_kill_switch_doc()), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(cfg, runner=runner)
    assert adapter.observed_e4_kill_switch_fresh() is True

    def runner_stale(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            now = datetime.now(timezone.utc)
            doc = _fresh_kill_switch_doc()
            doc["issued_at"] = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")  # not yet valid
            pathlib.Path(argv[-1]).write_text(json.dumps(doc), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    adapter2 = CommandAdapter(cfg, runner=runner_stale)
    assert adapter2.observed_e4_kill_switch_fresh() is False, "a not-yet-valid doc is not fresh"


def test_static_mode_reads_real_gbaw_operations_mode_env_var() -> None:
    """The static-mode preflight must read the REAL deployed evaluator env var
    ``GBAW_OPERATIONS_MODE`` (== 'operate') and require
    ``GBAW_OPERATIONS_AUTONOMY_ENABLED`` to be a true token — not the nonexistent
    ``GBAW_OPERATIONS_STATIC_DEPLOYMENT_MODE``."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "get-function-configuration"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "Environment": {
                            "Variables": {
                                "GBAW_OPERATIONS_MODE": "operate",
                                "GBAW_OPERATIONS_AUTONOMY_ENABLED": "true",
                            }
                        }
                    }
                ),
                "",
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(), runner=runner)
    assert adapter.observed_static_mode_operate() is True

    # The old (nonexistent) env var must NOT be what the adapter keys off.
    def runner_only_old(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "get-function-configuration"]:
            return CommandResult(
                0,
                json.dumps({"Environment": {"Variables": {"GBAW_OPERATIONS_STATIC_DEPLOYMENT_MODE": "operate"}}}),
                "",
            )
        return CommandResult(0, "{}", "")

    adapter2 = CommandAdapter(_config(), runner=runner_only_old)
    assert adapter2.observed_static_mode_operate() is False, "must not key off the nonexistent old env var"

    # operate mode but autonomy NOT enabled -> not operate-ready.
    def runner_mode_only(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "get-function-configuration"]:
            return CommandResult(
                0,
                json.dumps({"Environment": {"Variables": {"GBAW_OPERATIONS_MODE": "operate"}}}),
                "",
            )
        return CommandResult(0, "{}", "")

    adapter3 = CommandAdapter(_config(), runner=runner_mode_only)
    assert adapter3.observed_static_mode_operate() is False, "operate mode requires autonomy explicitly enabled"


# --------------------------------------------------------------------------- #
# Blocker 4: location-specific capacity + fresh detect-stack-drift by id +
# current enrollment.
# --------------------------------------------------------------------------- #


def test_fleet_capacity_read_is_location_specific() -> None:
    """The capacity read must be scoped to the enrolled fleet's LOCATION so a
    multi-location fleet does not read a foreign location's capacity."""
    captured: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["gamelift", "describe-fleet-capacity"]:
            captured["argv"] = argv
            return CommandResult(
                0,
                json.dumps(
                    {
                        "FleetCapacity": [
                            {"Location": "us-west-2", "InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}},
                            {"Location": "eu-west-1", "InstanceCounts": {"DESIRED": 9, "MINIMUM": 9, "MAXIMUM": 9}},
                        ]
                    }
                ),
                "",
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(enrolled_location="us-west-2"), runner=runner)
    caps = adapter.observed_fleet_capacity()
    argv = captured["argv"]
    assert "--location" in argv or "--locations" in argv, "the capacity read must scope to the enrolled location"
    assert caps is not None
    # The enrolled-location capacity (0/0/1) is what is returned, not eu-west-1.
    assert caps["desired"] == 0 and caps["maximum"] == 1


def test_drift_is_fresh_detect_stack_drift_polled_by_detection_id() -> None:
    """Drift must be a FRESH ``detect-stack-drift`` (start + poll by detection id
    to a terminal ``DETECTION_COMPLETE`` and read ``StackDriftStatus``), not the
    last-cached ``describe-stacks`` DriftInformation."""
    cfg = _config(autonomy_stack_name="game-agent-operations-autonomy")
    services_called: list[str] = []
    poll_count = {"n": 0}

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        services_called.append(key)
        if key == "cloudformation detect-stack-drift":
            return CommandResult(0, json.dumps({"StackDriftDetectionId": "det-123"}), "")
        if key == "cloudformation describe-stack-drift-detection-status":
            # The detection id must be threaded through.
            assert argv[argv.index("--stack-drift-detection-id") + 1] == "det-123"
            poll_count["n"] += 1
            if poll_count["n"] < 2:
                return CommandResult(0, json.dumps({"DetectionStatus": "DETECTION_IN_PROGRESS"}), "")
            return CommandResult(
                0,
                json.dumps({"DetectionStatus": "DETECTION_COMPLETE", "StackDriftStatus": "IN_SYNC"}),
                "",
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(cfg, runner=runner)
    assert adapter.observed_no_stack_drift() is True
    assert "cloudformation detect-stack-drift" in services_called, "must START a fresh drift detection"
    assert (
        "cloudformation describe-stack-drift-detection-status" in services_called
    ), "must POLL the detection status by id"
    assert "cloudformation describe-stacks" not in services_called, "must not use the cached DriftInformation"


def test_drift_detection_reports_drifted() -> None:
    cfg = _config(autonomy_stack_name="game-agent-operations-autonomy")

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "cloudformation detect-stack-drift":
            return CommandResult(0, json.dumps({"StackDriftDetectionId": "det-9"}), "")
        if key == "cloudformation describe-stack-drift-detection-status":
            return CommandResult(
                0, json.dumps({"DetectionStatus": "DETECTION_COMPLETE", "StackDriftStatus": "DRIFTED"}), ""
            )
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(cfg, runner=runner, drift_poll_sleep=lambda *_: None)
    assert adapter.observed_no_stack_drift() is False


# --------------------------------------------------------------------------- #
# Blocker 5: cleanup treats unreadable switch as UNKNOWN (not disabled), and
# forced-write denial requires an evidence lookup success.
# --------------------------------------------------------------------------- #


def test_disable_get_reports_unknown_when_switch_unreadable() -> None:
    """A GET on the disable route with an UNREADABLE switch must report UNKNOWN
    (not disabled): the body must NOT carry ``autonomy_enabled: false`` (which the
    harness would read as a confirmed disable)."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            # The read FAILS (unreadable switch).
            return CommandResult(returncode=255, stdout="", stderr="denied")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    resp = transport("GET", "https://x/operations/autonomy/disable", headers={"authorization": "Bearer x"}, body=None)
    body = resp.json()
    # Unknown must NOT be reported as autonomy_enabled=False.
    assert body.get("autonomy_enabled") is not False, "an unreadable switch must not be reported as disabled"


def test_disable_get_reports_disabled_only_on_readable_disabled_switch() -> None:
    """A readable, fresh, DISABLED switch reports autonomy_enabled=False; a
    readable enabled switch reports True."""

    def runner_disabled(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            pathlib.Path(argv[-1]).write_text(json.dumps(_fresh_switch_doc(autonomy_enabled=False)), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner_disabled))
    resp = transport("GET", "https://x/operations/autonomy/disable", headers={"authorization": "Bearer x"}, body=None)
    assert resp.json().get("autonomy_enabled") is False

    def runner_enabled(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            pathlib.Path(argv[-1]).write_text(json.dumps(_fresh_switch_doc(autonomy_enabled=True)), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    transport2 = CommandTransport(CommandAdapter(_config(), runner=runner_enabled))
    resp2 = transport2("GET", "https://x/operations/autonomy/disable", headers={"authorization": "Bearer x"}, body=None)
    assert resp2.json().get("autonomy_enabled") is True


def test_force_write_denial_requires_evidence_lookup_success() -> None:
    """After disable, a forced attempt whose evidence lookup FAILS must NOT be
    reported as a confirmed no-write denial: an unreadable execution/evidence is
    UNKNOWN, so ``wrote`` must not be a false ``False`` and the response must
    signal the ambiguity rather than a clean 409 denial."""

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        if key == "lambda invoke":
            # The evaluator DISPATCHED (a real failure of the disable).
            pathlib.Path(argv[-1]).write_text(
                json.dumps({"outcome": "dispatched", "operation_id": "op_forced"}), encoding="utf-8"
            )
            return CommandResult(0, json.dumps({"StatusCode": 200}), "")
        if key == "stepfunctions describe-execution":
            # Evidence lookup FAILS (unreadable) -> unknown, not "no write".
            return CommandResult(returncode=255, stdout="", stderr="throttled")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    resp = transport(
        "POST", "https://x/operations/autonomy/op/force-write", headers={"authorization": "Bearer x"}, body=b"{}"
    )
    body = resp.json()
    # A dispatched-outcome forced attempt whose write-evidence is UNREADABLE must
    # NOT be reported as a clean confirmed no-write denial.
    assert body.get("wrote") is not False, "an unreadable write-evidence lookup must not confirm no-write"


# --------------------------------------------------------------------------- #
# Blocker 6: activation binds 07 table/KMS/tenant/workspace + workflow/fleet/
# audience to the exact 06 values and 08 AppConfig.
# --------------------------------------------------------------------------- #


def test_activation_binding_requires_07_matches_06_and_08() -> None:
    """The activation binding must compare the 07 execution stack's
    table/KMS/tenant/workspace (plus workflow/fleet/audience) to the EXACT 06
    observation values and the 08 AppConfig coordinates, refusing on any
    mismatch."""
    # Local modules
    from operations.validation.e5_command_adapter import verify_activation_binding

    observation_06 = {
        "table_name": "game-agent-operations",
        "kms_key_arn": "arn:aws:kms:us-west-2:000000000000:key/06-cmk",
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "workflow_arn": _STATE_MACHINE_ARN,
        "fleet_id": "fleet-0000aaaa-11bb-22cc-33dd-4444eeee5555",
        "trusted_audience": "aud-1",
    }
    execution_07 = dict(observation_06)  # exact match
    control_08 = {"kill_switch_application_id": "ks-app", "operations_enabled": True}

    result = verify_activation_binding(observation_06=observation_06, execution_07=execution_07, control_08=control_08)
    assert result.bound is True
    assert result.mismatches == []

    # A KMS mismatch must refuse and name the field.
    bad_kms = dict(execution_07, kms_key_arn="arn:aws:kms:us-west-2:000000000000:key/OTHER")
    result2 = verify_activation_binding(observation_06=observation_06, execution_07=bad_kms, control_08=control_08)
    assert result2.bound is False
    assert "kms_key_arn" in result2.mismatches

    # A tenant/workspace mismatch must refuse.
    bad_tenant = dict(execution_07, tenant_id="tenant-OTHER")
    result3 = verify_activation_binding(observation_06=observation_06, execution_07=bad_tenant, control_08=control_08)
    assert result3.bound is False
    assert "tenant_id" in result3.mismatches

    # Missing 08 AppConfig coordinate must refuse.
    result4 = verify_activation_binding(observation_06=observation_06, execution_07=execution_07, control_08={})
    assert result4.bound is False
