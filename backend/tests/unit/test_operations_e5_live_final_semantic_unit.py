"""Real-shape tests for the FIVE final semantic blockers of #440 (at 7794bcd).

The prior live-adapter work (09bbc06) introduced the tempfile OutputFile invoke,
the executionArn build, the real PK/SK reads, tri-state switch, and the
activation binding. This suite pins the FIVE remaining semantic defects to the
EXACT deployed contracts of the 06 observation HTTP API, the 08 kill-switch
document, the 09 autonomy-switch document, and the GameLift capacity ABI:

1. **Observe cannot be a direct empty-claims Lambda invoke.** The deployed 06
   handler's ``_verified_principal`` raises ``IDENTITY_CONTEXT_INVALID`` on empty
   ``requestContext.authorizer.jwt.claims``, and ``ObservationRequest.from_payload``
   requires the body to be EXACTLY ``{"fleet_id", "idempotency_token"}``. So the
   observe step must issue an AUTHENTICATED HTTPS API call to the configured 06
   endpoint with a short-lived bearer supplied only in the environment (never
   argv/log), send a real proposal body carrying ``idempotency_token``, and parse
   the succeeded ``observation_id`` by POLLing the real status.
2. **GameLift capacity ABI.** ``aws gamelift describe-fleet-capacity`` has NO
   ``--locations`` parameter; per-location capacity comes from
   ``describe-fleet-location-capacity --fleet-id --location``. The adapter must
   derive the enrolled location's exact capacity through the valid location API.
3. **E4/E5 preflight measures the real flags.** E4 permission is
   ``operations_enabled`` AND the capability's ``dispatch`` AND ``execute`` phase
   booleans AND freshness; E5 permission is ``autonomy_enabled`` AND the exact
   capability's ``autonomous_write`` AND freshness. A fresh-but-not-permitting
   document must fail closed.
4. **Cleanup/force-write cannot false-confirm on unavailable evidence.** A forced
   attempt whose EVALUATOR invoke is unavailable must not map to a clean
   "disabled/no-write" denial; and a dispatched forced attempt whose
   before/after write evidence is unreadable is UNKNOWN, never a confirmed
   ``wrote: false``.
5. **Deploy wrapper compares 08 tenant/workspace/source to 06.** (script-level;
   see ``test_operations_deploy_wrappers_static_unit`` / the shellcheck gate.)

Every command/HTTP call goes through an injected fake; no test here calls AWS.
"""

from __future__ import annotations

# Standard library
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

_STATE_MACHINE_ARN = "arn:aws:states:us-west-2:000000000000:stateMachine:game-agent-operations-autonomy"
_CAPABILITY_ID = "gamelift.capacity-adjustment"


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
        observe_api_endpoint="https://obs.example.aws.dev",
        observe_bearer_env_var="GBAW_E5_OBSERVE_BEARER_UNITTEST",
    )
    base.update(overrides)
    return CommandAdapterConfig(**base)  # type: ignore[arg-type]


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


def _fresh_kill_switch_doc(
    *, operations_enabled: bool = True, prepare: bool = True, dispatch: bool = True, execute: bool = True
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "contract_version": "1.0",
        "config_version": 1,
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "not_after": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "operations_enabled": operations_enabled,
        "capabilities": {_CAPABILITY_ID: {"prepare": prepare, "dispatch": dispatch, "execute": execute}},
    }


# --------------------------------------------------------------------------- #
# Blocker 1: observe via authenticated HTTPS API + idempotency_token + poll.
# --------------------------------------------------------------------------- #


def test_observe_uses_authenticated_https_api_with_idempotency_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The observe step must POST the configured 06 HTTPS endpoint with a bearer
    read from the environment (never argv), a body carrying a valid
    ``idempotency_token`` and ``fleet_id`` (exactly those two keys), and parse the
    succeeded observation id by POLLing the real status route."""
    monkeypatch.setenv("GBAW_E5_OBSERVE_BEARER_UNITTEST", "short-lived-token")
    calls: list[HttpCall] = []

    def http(call: HttpCall) -> tuple[int, dict[str, str], str]:
        calls.append(call)
        # The bearer must be carried in the Authorization header, taken from env.
        assert call.headers.get("authorization") == "Bearer short-lived-token"
        if call.method == "POST":
            body = json.loads(call.body or "{}")
            # EXACTLY the two-key observe contract, including idempotency_token.
            assert set(body) == {"fleet_id", "idempotency_token"}, body
            assert re.fullmatch(r"idem_[A-Za-z0-9_-]{20,128}", body["idempotency_token"]), body["idempotency_token"]
            # A synchronous 200 returns the operation id; state may still be pending.
            resp_body = json.dumps({"operation_id": "obs_" + "a" * 26, "observation_id": "obs_" + "a" * 26})
            return 200, {}, resp_body
        # GET status poll -> succeeded, carrying observation_id.
        assert call.method == "GET"
        assert call.url.endswith("/operations/obs_" + "a" * 26)
        return 200, {}, json.dumps({"operation_id": "obs_" + "a" * 26, "state": "succeeded"})

    adapter = CommandAdapter(_config(), runner=lambda argv: CommandResult(0, "{}", ""), http_caller=http)
    observation_id = adapter.observe_succeeded_observation_id()
    assert observation_id == "obs_" + "a" * 26
    # Must be HTTPS (never a lambda invoke), and the bearer must never appear in
    # any command argv or the HTTP URL/query.
    assert calls and all(c.url.startswith("https://") for c in calls)
    for c in calls:
        assert "short-lived-token" not in c.url, "bearer must never be placed in the URL/query"


def test_observe_not_succeeded_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status that never reaches ``succeeded`` yields None (fail closed); the
    transport must not report trusted."""
    monkeypatch.setenv("GBAW_E5_OBSERVE_BEARER_UNITTEST", "tok")

    def http(call: HttpCall) -> tuple[int, dict[str, str], str]:
        if call.method == "POST":
            return 200, {}, json.dumps({"operation_id": "obs_" + "b" * 26})
        return 200, {}, json.dumps({"operation_id": "obs_" + "b" * 26, "state": "observing"})

    adapter = CommandAdapter(
        _config(),
        runner=lambda argv: CommandResult(0, "{}", ""),
        http_caller=http,
        observe_poll_sleep=lambda *_: None,
    )
    assert adapter.observe_succeeded_observation_id() is None
    transport = CommandTransport(adapter)
    resp = transport("POST", "https://x/operations/observe", headers={"authorization": "Bearer x"}, body=b"{}")
    assert resp.json().get("trusted") is not True


def test_observe_missing_bearer_env_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no bearer in the environment the observe must fail closed (None),
    never issue an unauthenticated call that the API would reject anyway."""
    monkeypatch.delenv("GBAW_E5_OBSERVE_BEARER_UNITTEST", raising=False)
    called = {"n": 0}

    def http(call: HttpCall) -> tuple[int, dict[str, str], str]:
        called["n"] += 1
        return 401, {}, "{}"

    adapter = CommandAdapter(_config(), runner=lambda argv: CommandResult(0, "{}", ""), http_caller=http)
    assert adapter.observe_succeeded_observation_id() is None


# --------------------------------------------------------------------------- #
# Blocker 2: GameLift describe-fleet-location-capacity (valid ABI).
# --------------------------------------------------------------------------- #


def test_location_capacity_uses_describe_fleet_location_capacity_singular_location() -> None:
    """A multi-location (enrolled-location) capacity read must use the valid
    ``describe-fleet-location-capacity`` API with a SINGULAR ``--location`` — never
    ``describe-fleet-capacity --locations`` (which is not a real parameter)."""
    seen: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> CommandResult:
        key = f"{argv[1]} {argv[2]}"
        seen[key] = argv
        if key == "gamelift describe-fleet-location-capacity":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "FleetCapacity": {
                            "Location": "us-west-2",
                            "InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1},
                        }
                    }
                ),
                "",
            )
        if key == "gamelift describe-fleet-capacity":
            # If this path is taken it must NOT carry a bogus --locations flag.
            assert "--locations" not in argv, "describe-fleet-capacity has no --locations parameter"
        return CommandResult(0, "{}", "")

    adapter = CommandAdapter(_config(enrolled_location="us-west-2"), runner=runner)
    caps = adapter.observed_fleet_capacity()
    assert "gamelift describe-fleet-location-capacity" in seen, "must read location capacity via the valid API"
    argv = seen["gamelift describe-fleet-location-capacity"]
    assert "--location" in argv and "--locations" not in argv, "the location API takes a singular --location"
    assert argv[argv.index("--location") + 1] == "us-west-2"
    assert caps == {"desired": 0, "minimum": 0, "maximum": 1}


def test_describe_fleet_capacity_never_uses_invalid_locations_flag() -> None:
    """Any home-Region-only fallback to ``describe-fleet-capacity`` must never
    attach the invalid ``--locations`` flag."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["gamelift", "describe-fleet-capacity"]:
            assert "--locations" not in argv, "describe-fleet-capacity has no --locations parameter"
            return CommandResult(
                0,
                json.dumps({"FleetCapacity": [{"InstanceCounts": {"DESIRED": 0, "MINIMUM": 0, "MAXIMUM": 1}}]}),
                "",
            )
        return CommandResult(0, "{}", "")

    # No enrolled location and no region-derived location -> home-only fleet.
    adapter = CommandAdapter(_config(enrolled_location="", region=""), runner=runner)
    caps = adapter.observed_fleet_capacity()
    assert caps == {"desired": 0, "minimum": 0, "maximum": 1}


# --------------------------------------------------------------------------- #
# Blocker 3: E4 requires operations_enabled + dispatch + execute; E5 requires
# autonomous_write; both require freshness.
# --------------------------------------------------------------------------- #


def _kill_switch_runner(doc: dict):
    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["appconfig", "get-configuration"]:
            pathlib.Path(argv[-1]).write_text(json.dumps(doc), encoding="utf-8")
            return CommandResult(0, json.dumps({"ConfigurationVersion": "1"}), "")
        return CommandResult(0, "{}", "")

    return runner


def _ks_cfg() -> CommandAdapterConfig:
    return _config(
        kill_switch_application_id="ks-app",
        kill_switch_environment_id="ks-env",
        kill_switch_profile_id="ks-prof",
    )


def test_e4_permission_requires_operations_enabled_and_dispatch_and_execute() -> None:
    cfg = _ks_cfg()
    # Fresh + operations_enabled + dispatch + execute -> permitted.
    a = CommandAdapter(cfg, runner=_kill_switch_runner(_fresh_kill_switch_doc()))
    assert a.observed_e4_kill_switch_fresh() is True

    # operations_enabled False -> not permitted even if fresh + phases true.
    a2 = CommandAdapter(cfg, runner=_kill_switch_runner(_fresh_kill_switch_doc(operations_enabled=False)))
    assert a2.observed_e4_kill_switch_fresh() is False, "operations_enabled=False must not permit E4"

    # dispatch False -> not permitted.
    a3 = CommandAdapter(cfg, runner=_kill_switch_runner(_fresh_kill_switch_doc(dispatch=False)))
    assert a3.observed_e4_kill_switch_fresh() is False, "dispatch phase disabled must not permit E4"

    # execute False -> not permitted.
    a4 = CommandAdapter(cfg, runner=_kill_switch_runner(_fresh_kill_switch_doc(execute=False)))
    assert a4.observed_e4_kill_switch_fresh() is False, "execute phase disabled must not permit E4"


def test_e5_permission_requires_exact_capability_autonomous_write() -> None:
    # Fresh + autonomy_enabled + autonomous_write -> permitted.
    a = CommandAdapter(_config(), runner=_kill_switch_runner(_fresh_switch_doc()))
    assert a.observed_autonomy_switch_fresh_enabled() is True

    # autonomy_enabled True but capability autonomous_write False -> not permitted.
    a2 = CommandAdapter(_config(), runner=_kill_switch_runner(_fresh_switch_doc(autonomous_write=False)))
    assert a2.observed_autonomy_switch_fresh_enabled() is False, "autonomous_write=False must not permit E5"


# --------------------------------------------------------------------------- #
# Blocker 4: force-write cannot false-confirm on evaluator/evidence unavailable.
# --------------------------------------------------------------------------- #


def test_force_write_evaluator_unavailable_is_not_a_clean_denial() -> None:
    """When the forced-attempt EVALUATOR invoke is UNAVAILABLE (CommandError), the
    result must not be a clean confirmed no-write denial: ``wrote`` must not be a
    false ``False`` and the code must signal the evaluator was unavailable."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "invoke"]:
            # The evaluator invoke fails outright (unreadable outcome).
            return CommandResult(returncode=255, stdout="", stderr="throttled")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    transport._trusted_observation_id = "op_obs_seeded"  # corrected #440: evaluate requires a succeeded observation
    resp = transport(
        "POST", "https://x/operations/autonomy/op/force-write", headers={"authorization": "Bearer x"}, body=b"{}"
    )
    body = resp.json()
    assert body.get("wrote") is not False, "an unavailable evaluator must not confirm a clean no-write"
    assert body.get("error_code") == "EVALUATOR_UNAVAILABLE"


def test_force_write_refused_with_evidence_confirms_no_write() -> None:
    """A cleanly refused forced attempt (evaluator returns a refusal) reports a
    confirmed no-write denial (``wrote: false``)."""

    def runner(argv: list[str]) -> CommandResult:
        if argv[1:3] == ["lambda", "invoke"]:
            pathlib.Path(argv[-1]).write_text(
                json.dumps({"outcome": "refused", "reason": "AUTONOMY_DISABLED"}), encoding="utf-8"
            )
            return CommandResult(0, json.dumps({"StatusCode": 200}), "")
        return CommandResult(0, "{}", "")

    transport = CommandTransport(CommandAdapter(_config(), runner=runner))
    transport._trusted_observation_id = "op_obs_seeded"  # corrected #440: evaluate requires a succeeded observation
    resp = transport(
        "POST", "https://x/operations/autonomy/op/force-write", headers={"authorization": "Bearer x"}, body=b"{}"
    )
    body = resp.json()
    assert body.get("wrote") is False
    assert body.get("decision") == "denied"
