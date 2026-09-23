"""Unit tests for the public-safe E5 bounded-autonomy shakedown harness (#440).

Drives ``operations.validation.e5_shakedown`` against an in-memory fake HTTP
transport that models an **optional, reviewed, already-deployed** E5 autonomy
stack. E5 autonomy is default-disabled and creates no resources in the main
``deploy-all.sh`` deployment, so this harness only ever runs against a stack an
operator explicitly stood up out of band.

The harness is *implementation only* here: every test injects a fake transport,
and no test performs a real AWS call. The tests assert the harness:

* refuses the whole live run unless every hard preflight condition holds
  (explicit confirmation, profile/region/fleet match server enrollment,
  starting capacity exactly ``0/0/1``, all static + E4 + separate-autonomy
  gates fresh-enabled, alarms/drift safe);
* runs the full lifecycle in order (one trusted E1 observation -> evaluator
  ``0->1`` -> audit/reservation/Step Functions/executor-only CloudTrail
  write/capacity-1 verified -> immediate retry denied by
  cooldown/frequency/concurrency -> separately confirmed inverse ``1->0`` ->
  capacity-0 verified -> autonomy disabled -> forced evaluator/executor writes
  refused);
* proves the negative controls (IAM-negative, alarm, drift, exact-artifact,
  audit, rollback/ambiguous-result);
* never leaks a secret or a raw provider payload into the emitted summary.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.validation import e5_shakedown as sd

pytestmark = pytest.mark.unit

ENDPOINT = "https://api.example.execute-api.us-west-2.amazonaws.com"
ADMIN = "admin-access-token-value"
FORCED = "forced-attempt-token-value"
FLEET = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
REGION = "us-west-2"
PROFILE = "demo-operator"
OBS_ID = "obs_aaaaaaaaaaaaaaaaaaaaaaaaaa"
OP_ID = "op_bbbbbbbbbbbbbbbbbbbbbbbbbb"

# Tests never wait on the bounded teardown poll; inject a no-op sleep.
_NO_SLEEP = lambda *_: None  # noqa: E731


def _safe_preflight(**overrides: Any) -> sd.E5Preflight:
    base = dict(
        profile=PROFILE,
        region=REGION,
        fleet_id=FLEET,
        enrolled_profile=PROFILE,
        enrolled_region=REGION,
        enrolled_fleet_id=FLEET,
        starting_desired=0,
        starting_minimum=0,
        starting_maximum=1,
        static_gate_fresh_enabled=True,
        e4_gate_fresh_enabled=True,
        autonomy_switch_fresh_enabled=True,
        alarms_safe=True,
        drift_safe=True,
    )
    base.update(overrides)
    return sd.E5Preflight(**base)


def _config(**overrides: Any) -> sd.E5ShakedownConfig:
    base = dict(
        endpoint=ENDPOINT,
        admin_bearer=ADMIN,
        fleet_id=FLEET,
        observation_id=OBS_ID,
        operation_id=OP_ID,
        preflight=_safe_preflight(),
        confirmation=sd.REQUIRED_CONFIRMATION,
        inverse_confirmation=sd.REQUIRED_INVERSE_CONFIRMATION,
        forced_bearer=FORCED,
    )
    base.update(overrides)
    return sd.E5ShakedownConfig(**base)


def _resp(status: int, payload: dict[str, Any]) -> sd.HttpResponse:
    body = json.dumps(payload).encode("utf-8")
    return sd.HttpResponse(status=status, content_type="application/json", body_bytes=body)


class FakeDeployedE5:
    """A minimal in-memory model of the deployed optional E5 autonomy boundary.

    Models capacity as a single ``desired`` integer for the enrolled fleet, an
    ``autonomy_enabled`` posture, and a per-window write counter that a second
    immediate evaluation trips (cooldown/frequency/concurrency).
    """

    def __init__(self) -> None:
        self.desired = 0
        self.autonomy_enabled = True
        self.writes_this_window = 0
        self.calls: list[tuple[str, str]] = []
        # Toggles the fakes flip to exercise negative branches.
        self.executor_write_actor = "executor"  # who CloudTrail attributes the write to
        self.audit_complete = True
        self.artifact_matches = True

    def __call__(self, method, url, headers, body) -> sd.HttpResponse:
        self.calls.append((method, url))
        auth = (headers or {}).get("authorization", "") or (headers or {}).get("Authorization", "")
        path = url[len(ENDPOINT) :]
        payload = json.loads(body.decode("utf-8")) if body else {}

        if not auth:
            return _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"})

        forced = auth.endswith(FORCED)

        if path == "/operations/observe" and method == "POST":
            return _resp(201, {"observation_id": OBS_ID, "trusted": True, "capacity": {"desired": self.desired}})

        if path == f"/operations/autonomy/{OP_ID}/evaluate" and method == "POST":
            if not self.autonomy_enabled or forced:
                return _resp(409, {"error_code": "AUTONOMY_DISABLED", "wrote": False})
            direction = payload.get("direction")
            if self.writes_this_window >= 1 and direction == "up":
                # Immediate retry is denied by cooldown/frequency/concurrency.
                return _resp(
                    429,
                    {"error_code": "COOLDOWN_ACTIVE", "wrote": False, "decision": "denied"},
                )
            # Authorized deterministic decision -> reservation -> SFN -> executor write.
            if direction == "up":
                self.desired = 1
            else:
                self.desired = 0
            self.writes_this_window += 1
            return _resp(
                200,
                {
                    "decision": "authorized",
                    "reason_codes": ["APPROVED_AUTONOMOUS"],
                    "audit_recorded": self.audit_complete,
                    "reservation_granted": True,
                    "step_functions_started": True,
                    "cloudtrail_write_actor": self.executor_write_actor,
                    "artifact_matches_expected": self.artifact_matches,
                    "capacity": {"desired": self.desired},
                },
            )

        if path == f"/operations/autonomy/{OP_ID}/capacity" and method == "GET":
            return _resp(200, {"capacity": {"desired": self.desired}})

        if path == "/operations/autonomy/disable" and method == "POST":
            self.autonomy_enabled = False
            return _resp(200, {"autonomy_enabled": False})

        if path == "/operations/autonomy/disable" and method == "GET":
            # The guaranteed teardown RE-READS the disable state; report the live
            # posture so a confirmed disable is observable.
            return _resp(200, {"autonomy_enabled": self.autonomy_enabled})

        if path == f"/operations/autonomy/{OP_ID}/force-write" and method == "POST":
            # Forced evaluator/executor attempt after disable: must never write.
            if not self.autonomy_enabled:
                return _resp(409, {"error_code": "AUTONOMY_DISABLED", "wrote": False})
            return _resp(403, {"error_code": "FORBIDDEN", "wrote": False})

        return _resp(404, {"error_code": "NOT_FOUND"})


# --------------------------------------------------------------------------- #
# Confirmation / preflight refusal
# --------------------------------------------------------------------------- #


def test_confirmations_are_distinct_and_byte_exact() -> None:
    assert sd.REQUIRED_CONFIRMATION != sd.REQUIRED_INVERSE_CONFIRMATION
    assert sd.confirmation_is_valid(sd.REQUIRED_CONFIRMATION)
    assert not sd.confirmation_is_valid(sd.REQUIRED_CONFIRMATION + " ")
    assert not sd.confirmation_is_valid(sd.REQUIRED_CONFIRMATION.lower())
    assert not sd.confirmation_is_valid("")
    assert sd.inverse_confirmation_is_valid(sd.REQUIRED_INVERSE_CONFIRMATION)
    # The forward confirmation must not unlock the inverse and vice versa.
    assert not sd.inverse_confirmation_is_valid(sd.REQUIRED_CONFIRMATION)
    assert not sd.confirmation_is_valid(sd.REQUIRED_INVERSE_CONFIRMATION)


def test_safe_preflight_has_no_refusals() -> None:
    assert _safe_preflight().refusals() == []
    assert _safe_preflight().is_safe


@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        ({"enrolled_profile": "other"}, "PROFILE_MISMATCH"),
        ({"enrolled_region": "eu-west-1"}, "REGION_MISMATCH"),
        ({"enrolled_fleet_id": "fleet-zzzz"}, "FLEET_MISMATCH"),
        ({"starting_desired": 1}, "STARTING_CAPACITY_NOT_0_0_1"),
        ({"starting_minimum": 1}, "STARTING_CAPACITY_NOT_0_0_1"),
        ({"starting_maximum": 2}, "STARTING_CAPACITY_NOT_0_0_1"),
        ({"static_gate_fresh_enabled": False}, "STATIC_GATE_NOT_FRESH_ENABLED"),
        ({"e4_gate_fresh_enabled": False}, "E4_GATE_NOT_FRESH_ENABLED"),
        ({"autonomy_switch_fresh_enabled": False}, "AUTONOMY_SWITCH_NOT_FRESH_ENABLED"),
        ({"alarms_safe": False}, "ALARMS_NOT_SAFE"),
        ({"drift_safe": False}, "DRIFT_NOT_SAFE"),
    ],
)
def test_each_preflight_condition_can_refuse(overrides: dict[str, Any], expected_code: str) -> None:
    refusals = _safe_preflight(**overrides).refusals()
    assert expected_code in refusals
    assert not _safe_preflight(**overrides).is_safe


def test_harness_refuses_entire_run_without_confirmation() -> None:
    fake = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(confirmation=""), fake, sleep=_NO_SLEEP)
    summary = harness.run()
    assert summary["accepted"] is False
    assert summary["refused"] is True
    assert "CONFIRMATION_REQUIRED" in summary["refusal_codes"]
    # Fail-closed: refusal happens before any authenticated request.
    assert fake.calls == []


def test_harness_refuses_run_when_preflight_unsafe() -> None:
    fake = FakeDeployedE5()
    cfg = _config(preflight=_safe_preflight(starting_desired=1, drift_safe=False))
    harness = sd.E5ShakedownHarness(cfg, fake, sleep=_NO_SLEEP)
    summary = harness.run()
    assert summary["refused"] is True
    assert "STARTING_CAPACITY_NOT_0_0_1" in summary["refusal_codes"]
    assert "DRIFT_NOT_SAFE" in summary["refusal_codes"]
    assert fake.calls == []


def test_inverse_requires_its_own_confirmation() -> None:
    # Forward confirmation present but the inverse (1->0) confirmation missing:
    # the run is refused before any request because the inverse is mandatory.
    fake = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(inverse_confirmation=""), fake, sleep=_NO_SLEEP)
    summary = harness.run()
    assert summary["refused"] is True
    assert "INVERSE_CONFIRMATION_REQUIRED" in summary["refusal_codes"]
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Happy-path lifecycle
# --------------------------------------------------------------------------- #


def _named(summary: dict[str, Any], name: str) -> dict[str, Any]:
    for check in summary["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"missing check {name!r}")


def test_full_lifecycle_accepts_and_restores_zero() -> None:
    fake = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP)
    summary = harness.run()
    assert summary["refused"] is False
    assert summary["accepted"] is True
    # Every discriminating check passed.
    for check in summary["checks"]:
        assert check["passed"], check
    # The fleet was restored to zero desired at the end.
    assert fake.desired == 0
    assert fake.autonomy_enabled is False


def test_lifecycle_runs_expected_checks_in_order() -> None:
    fake = FakeDeployedE5()
    summary = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP).run()
    order = [c["name"] for c in summary["checks"]]
    assert order == [
        "unauthenticated_denied",
        "trusted_e1_observation",
        "evaluator_authorizes_0_to_1",
        "write_audited_reserved_sfn_executor_only",
        "capacity_is_one",
        "immediate_retry_denied_by_limits",
        "inverse_1_to_0_confirmed",
        "capacity_is_zero",
        "autonomy_disabled",
        "forced_evaluator_executor_cannot_write",
        "iam_negative",
        "alarms_safe",
        "drift_safe",
        "exact_artifact",
        "audit_complete",
        "guaranteed_restore",
    ]


def test_immediate_retry_denied_reports_limit_reason() -> None:
    fake = FakeDeployedE5()
    summary = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP).run()
    retry = _named(summary, "immediate_retry_denied_by_limits")
    assert retry["passed"]
    assert retry["observed_error_code"] in sd.LIMIT_REASON_CODES


def test_executor_only_write_attribution_checked() -> None:
    fake = FakeDeployedE5()
    summary = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP).run()
    assert _named(summary, "write_audited_reserved_sfn_executor_only")["passed"]


# --------------------------------------------------------------------------- #
# Negative controls
# --------------------------------------------------------------------------- #


def test_write_not_executor_attributed_fails_check() -> None:
    fake = FakeDeployedE5()
    fake.executor_write_actor = "evaluator"  # a non-executor principal wrote
    summary = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP).run()
    assert _named(summary, "write_audited_reserved_sfn_executor_only")["passed"] is False
    assert summary["accepted"] is False


def test_forced_write_after_disable_that_succeeds_fails_check() -> None:
    class LeakyFake(FakeDeployedE5):
        def __call__(self, method, url, headers, body):
            path = url[len(ENDPOINT) :]
            if path.endswith("/force-write"):
                # Simulate a broken deployment that writes despite being disabled.
                self.desired = 1
                return _resp(200, {"wrote": True, "capacity": {"desired": 1}})
            return super().__call__(method, url, headers, body)

    summary = sd.E5ShakedownHarness(_config(), LeakyFake(), sleep=_NO_SLEEP).run()
    assert _named(summary, "forced_evaluator_executor_cannot_write")["passed"] is False
    assert summary["accepted"] is False


def test_ambiguous_capacity_result_fails_closed() -> None:
    class AmbiguousFake(FakeDeployedE5):
        def __call__(self, method, url, headers, body):
            path = url[len(ENDPOINT) :]
            if path.endswith("/capacity"):
                return _resp(200, {"capacity": {}})  # missing desired -> ambiguous
            return super().__call__(method, url, headers, body)

    summary = sd.E5ShakedownHarness(_config(), AmbiguousFake(), sleep=_NO_SLEEP).run()
    assert summary["accepted"] is False
    ambiguous = _named(summary, "capacity_is_one")
    assert ambiguous["observed_error_code"] == "AMBIGUOUS_RESULT"


def test_audit_incomplete_fails_check() -> None:
    fake = FakeDeployedE5()
    fake.audit_complete = False
    summary = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP).run()
    assert _named(summary, "audit_complete")["passed"] is False


def test_artifact_mismatch_fails_check() -> None:
    fake = FakeDeployedE5()
    fake.artifact_matches = False
    summary = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP).run()
    assert _named(summary, "exact_artifact")["passed"] is False


# --------------------------------------------------------------------------- #
# Public-safety of the emitted summary
# --------------------------------------------------------------------------- #


def test_summary_is_public_safe_and_hides_identifiers() -> None:
    fake = FakeDeployedE5()
    summary = sd.E5ShakedownHarness(_config(), fake, sleep=_NO_SLEEP).run()
    assert sd.summary_is_public_safe(summary)
    blob = json.dumps(summary)
    for secret in (ENDPOINT, ADMIN, FORCED, FLEET, PROFILE, OBS_ID, OP_ID):
        assert secret not in blob
    # Stable references are present instead.
    assert summary["endpoint_ref"].startswith("ep-")
    assert summary["fleet_ref"].startswith("flt-")


def test_summary_public_safety_detects_injected_secret() -> None:
    assert sd.summary_is_public_safe({"ok": True})
    assert not sd.summary_is_public_safe({"leak": "arn:aws:iam::123456789012:role/x"})
    assert not sd.summary_is_public_safe({"leak": "Bearer abc"})


def test_config_rejects_non_https_endpoint() -> None:
    with pytest.raises(ValueError):
        _config(endpoint="http://insecure.example.com")


def test_config_rejects_blank_admin_bearer() -> None:
    with pytest.raises(ValueError):
        _config(admin_bearer="   ")


# --------------------------------------------------------------------------- #
# Finding 9: guaranteed finally restore + disable on any post-scale exception.
# --------------------------------------------------------------------------- #
class _RaiseAfterScaleTransport(FakeDeployedE5):
    """A transport that raises right after the 0->1 scale, to prove the harness
    still restores 1->0 and disables in a finally block."""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    def __call__(self, method, url, headers, body):
        path = url[len(ENDPOINT) :]
        # Let the forward 0->1 evaluate succeed (sets desired=1), then raise on
        # the very next capacity read to simulate a mid-lifecycle failure.
        if path == f"/operations/autonomy/{OP_ID}/capacity" and self.desired == 1 and not self._raised:
            self._raised = True
            raise RuntimeError("simulated mid-lifecycle failure after scale-up")
        return super().__call__(method, url, headers, body)


def test_post_scale_exception_still_restores_and_disables() -> None:
    transport = _RaiseAfterScaleTransport()
    harness = sd.E5ShakedownHarness(_config(), transport, sleep=_NO_SLEEP)
    summary = harness.run()
    # The fleet must have been restored to 0 despite the mid-lifecycle failure.
    assert transport.desired == 0, "the harness must restore 1->0 in a finally block"
    # Autonomy must have been disabled as part of the guaranteed teardown.
    assert transport.autonomy_enabled is False, "the harness must disable autonomy in the finally block"
    # The summary must record the failure (not silently pass) and the restore.
    names = {c["name"] for c in summary["checks"]}
    assert "guaranteed_restore" in names, "the harness must record a guaranteed_restore check"


def test_no_restore_needed_when_never_scaled() -> None:
    """If the run is refused before any scale, the finally must not attempt a
    provider write (nothing to restore)."""
    cfg = _config(confirmation="")  # refused: no confirmation
    transport = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(cfg, transport, sleep=_NO_SLEEP)
    summary = harness.run()
    assert summary["refused"] is True
    # No write ever happened, so desired stayed 0 and no evaluate call was made.
    assert transport.desired == 0
    assert all(m != "POST" or "/evaluate" not in u for (m, u) in transport.calls)


def test_main_refuses_imaginary_http_routes_without_explicit_adapter() -> None:
    """Finding 9: the E5 evaluator has NO HTTP API (it is a Lambda invoked by
    EventBridge/StartExecution, verified via DynamoDB/StepFunctions/CloudTrail/
    GameLift). main() must NOT silently issue HTTP to nonexistent routes; it must
    fail closed unless an operator explicitly selects a concrete command
    adapter."""
    rc = sd.main(["--endpoint", "https://example.invalid"])
    assert rc != 0, "main() must refuse to run against imaginary HTTP routes by default"


def test_adapter_command_contract_is_documented() -> None:
    """The concrete command contract (each logical step -> real AWS operation)
    must be documented in the module so operators wire a real adapter."""
    assert sd.ADAPTER_COMMAND_CONTRACT, "the module must expose a concrete command contract"
    contract = sd.ADAPTER_COMMAND_CONTRACT
    # Every lifecycle step maps to a concrete AWS operation, not an HTTP route.
    joined = " ".join(contract.values()).lower()
    assert "lambda" in joined
    assert "stepfunctions" in joined or "states" in joined
    assert "dynamodb" in joined
    assert "cloudtrail" in joined
    assert "gamelift" in joined


# --------------------------------------------------------------------------- #
# #440 review (cleanup): the guaranteed teardown must NEVER skip the inverse
# solely because a capacity read raised, and it must WAIT and RE-READ the
# disable state rather than fire-and-forget a single disable POST.
# --------------------------------------------------------------------------- #


class CapacityReadRaisesThenRecovers:
    """A transport whose capacity GET raises the first time it is read during
    teardown, modelling an ambiguous/erroring read. Everything else behaves like
    the healthy fake so the run reaches teardown. The inverse (evaluate down) is
    recorded so a test can prove teardown still attempted it."""

    def __init__(self) -> None:
        self.desired = 1  # left scaled up
        self.autonomy_enabled = True
        self.capacity_reads = 0
        self.inverse_attempts = 0
        self.disable_reads = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        auth = (headers or {}).get("authorization", "") or (headers or {}).get("Authorization", "")
        path = url[len(ENDPOINT) :]
        payload = json.loads(body.decode("utf-8")) if body else {}
        if not auth:
            return _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"})
        if path == "/operations/observe":
            return _resp(201, {"trusted": True, "capacity": {"desired": self.desired}})
        if path.endswith("/evaluate"):
            direction = payload.get("direction")
            if direction == "down":
                self.inverse_attempts += 1
                self.desired = 0
                return _resp(200, {"decision": "authorized", "capacity": {"desired": 0}})
            self.desired = 1
            return _resp(
                200,
                {
                    "decision": "authorized",
                    "reason_codes": ["APPROVED_AUTONOMOUS"],
                    "audit_recorded": True,
                    "reservation_granted": True,
                    "step_functions_started": True,
                    "cloudtrail_write_actor": "executor",
                    "artifact_matches_expected": True,
                    "capacity": {"desired": 1},
                },
            )
        if path.endswith("/capacity"):
            self.capacity_reads += 1
            # The FIRST teardown-time capacity read raises; a naive teardown would
            # then skip the inverse. It must not.
            if self.capacity_reads == 1:
                raise RuntimeError("ambiguous capacity read")
            return _resp(200, {"capacity": {"desired": self.desired}})
        if path == "/operations/autonomy/disable":
            self.autonomy_enabled = False
            return _resp(200, {"autonomy_enabled": False})
        if path.endswith("/force-write"):
            return _resp(409, {"error_code": "AUTONOMY_DISABLED", "wrote": False})
        return _resp(404, {"error_code": "NOT_FOUND"})


def test_teardown_does_not_skip_inverse_when_capacity_read_raises() -> None:
    """If the teardown capacity read raises, the guaranteed restore must still
    attempt the separately-confirmed inverse (1->0), not skip it."""
    fake = CapacityReadRaisesThenRecovers()
    harness = sd.E5ShakedownHarness(_config(), fake, sleep=lambda *_: None)
    result = harness._guaranteed_restore_and_disable(scaled_up=True)
    assert fake.inverse_attempts >= 1, "teardown must attempt the inverse even when a capacity read raised"
    # And it reached the disable.
    assert fake.autonomy_enabled is False, "teardown must still disable autonomy"
    assert result.name == "guaranteed_restore"


class DisableNeverConfirms:
    """A transport whose disable POST returns 200 but whose disable STATE never
    flips (autonomy_enabled stays True on re-read), modelling a disable that did
    not take effect. A fire-and-forget teardown would wrongly report success."""

    def __init__(self) -> None:
        self.desired = 0
        self.autonomy_enabled = True
        self.disable_state_reads = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        auth = (headers or {}).get("authorization", "") or (headers or {}).get("Authorization", "")
        path = url[len(ENDPOINT) :]
        if not auth:
            return _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"})
        if path.endswith("/capacity"):
            return _resp(200, {"capacity": {"desired": self.desired}})
        if path.endswith("/evaluate"):
            return _resp(200, {"decision": "authorized", "capacity": {"desired": self.desired}})
        if path == "/operations/autonomy/disable":
            if method == "POST":
                # POST claims success but the state does NOT flip.
                return _resp(200, {"autonomy_enabled": True})
            # GET re-read of the disable state.
            self.disable_state_reads += 1
            return _resp(200, {"autonomy_enabled": True})
        return _resp(404, {"error_code": "NOT_FOUND"})


def test_teardown_waits_and_rereads_disable_state() -> None:
    """The guaranteed teardown must WAIT and RE-READ the disable state; a disable
    whose state never confirms disabled must fail the teardown check, not pass on
    the POST's optimistic 200."""
    fake = DisableNeverConfirms()
    harness = sd.E5ShakedownHarness(_config(), fake, sleep=lambda *_: None)
    result = harness._guaranteed_restore_and_disable(scaled_up=False)
    # It must have re-read the disable state at least once.
    assert fake.disable_state_reads >= 1, "teardown must re-read the disable state, not fire-and-forget"
    # And because the state never confirmed disabled, the teardown must NOT pass.
    assert result.passed is False, "an unconfirmed disable must fail the guaranteed teardown check"


# --------------------------------------------------------------------------- #
# Follow-up: teardown disables and quiesces known executions before final zero.
# --------------------------------------------------------------------------- #


def test_guaranteed_teardown_quiesces_before_final_capacity_reconcile() -> None:
    """A delayed executor write after an earlier zero read is reconciled safely.

    The emergency disable must be confirmed first. The shakedown then waits for
    the operation it dispatched to become terminal before it trusts a final zero
    read or issues the bounded operator inverse.
    """

    class TraceTransport(FakeDeployedE5):
        def __init__(self) -> None:
            super().__init__()
            self.trace: list[str] = []

        def __call__(self, method, url, headers, body):
            path = url[len(ENDPOINT) :]
            if path == "/operations/autonomy/disable":
                self.trace.append(f"disable-{method.lower()}")
            elif path.endswith("/capacity"):
                self.trace.append("capacity")
            return super().__call__(method, url, headers, body)

    class QuiescingCleanup:
        def __init__(self, transport: TraceTransport) -> None:
            self.transport = transport
            self.polls = 0

        def execution_is_terminal(self, operation_id: str):
            self.transport.trace.append("execution")
            self.polls += 1
            if self.polls == 1:
                return False
            # The previously dispatched executor completes after an earlier zero
            # observation and restores desired=1. The final reconcile must see it.
            self.transport.desired = 1
            return True

        def operator_inverse_update_fleet_capacity(self) -> None:
            self.transport.trace.append("inverse")
            self.transport.desired = 0

    transport = TraceTransport()
    cleanup = QuiescingCleanup(transport)
    harness = sd.E5ShakedownHarness(_config(), transport, sleep=_NO_SLEEP, cleanup_adapter=cleanup)
    harness._dispatched_operation_ids.add(OP_ID)

    result = harness._guaranteed_restore_and_disable(scaled_up=True)

    assert result.passed is True
    assert cleanup.polls >= 2
    assert transport.trace.index("disable-post") < transport.trace.index("execution")
    assert transport.trace.index("execution") < transport.trace.index("inverse")
    assert transport.trace[-1] == "capacity"
    assert transport.desired == 0


def test_guaranteed_teardown_fails_closed_when_a_known_execution_never_quiesces() -> None:
    """A permanently running execution prevents a false restored-to-zero claim."""

    class NeverTerminalCleanup:
        def execution_is_terminal(self, operation_id: str):
            return False

        def operator_inverse_update_fleet_capacity(self) -> None:
            pass

    transport = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(), transport, sleep=_NO_SLEEP, cleanup_adapter=NeverTerminalCleanup())
    harness._dispatched_operation_ids.add(OP_ID)

    result = harness._guaranteed_restore_and_disable(scaled_up=True)

    assert result.passed is False
    assert "execution" in result.detail.lower()


def test_ambiguous_dispatch_with_operation_id_is_quiesced_before_teardown_success() -> None:
    """A refused result that retains an operation id still enters terminality polling."""
    transport = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(), transport, sleep=_NO_SLEEP)
    response = _resp(
        200,
        {
            "decision": "denied",
            "outcome": "refused",
            "operation_id": OP_ID,
            "dispatch_state": "unknown",
            "wrote": False,
        },
    )

    harness._track_dispatched_operation(response)

    assert harness._dispatched_operation_ids == {OP_ID}


def test_unresolved_dispatch_uncertainty_fails_guaranteed_teardown() -> None:
    """No operation id means the harness cannot prove a racing execution is absent."""
    transport = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(), transport, sleep=_NO_SLEEP)
    harness._unresolved_dispatch = True

    result = harness._guaranteed_restore_and_disable(scaled_up=True)

    assert result.passed is False
    assert "execution" in result.detail.lower()


def test_malformed_ambiguous_dispatch_identifier_marks_teardown_unresolved() -> None:
    """A non-contract operation id cannot be silently ignored during teardown."""
    transport = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(), transport, sleep=_NO_SLEEP)
    response = _resp(
        200,
        {"outcome": "refused", "operation_id": "bad-operation-id", "dispatch_state": "unknown", "wrote": None},
    )

    harness._track_dispatched_operation(response)

    assert harness._dispatched_operation_ids == set()
    assert harness._unresolved_dispatch is True
