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
    harness = sd.E5ShakedownHarness(_config(confirmation=""), fake)
    summary = harness.run()
    assert summary["accepted"] is False
    assert summary["refused"] is True
    assert "CONFIRMATION_REQUIRED" in summary["refusal_codes"]
    # Fail-closed: refusal happens before any authenticated request.
    assert fake.calls == []


def test_harness_refuses_run_when_preflight_unsafe() -> None:
    fake = FakeDeployedE5()
    cfg = _config(preflight=_safe_preflight(starting_desired=1, drift_safe=False))
    harness = sd.E5ShakedownHarness(cfg, fake)
    summary = harness.run()
    assert summary["refused"] is True
    assert "STARTING_CAPACITY_NOT_0_0_1" in summary["refusal_codes"]
    assert "DRIFT_NOT_SAFE" in summary["refusal_codes"]
    assert fake.calls == []


def test_inverse_requires_its_own_confirmation() -> None:
    # Forward confirmation present but the inverse (1->0) confirmation missing:
    # the run is refused before any request because the inverse is mandatory.
    fake = FakeDeployedE5()
    harness = sd.E5ShakedownHarness(_config(inverse_confirmation=""), fake)
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
    harness = sd.E5ShakedownHarness(_config(), fake)
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
    summary = sd.E5ShakedownHarness(_config(), fake).run()
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
    ]


def test_immediate_retry_denied_reports_limit_reason() -> None:
    fake = FakeDeployedE5()
    summary = sd.E5ShakedownHarness(_config(), fake).run()
    retry = _named(summary, "immediate_retry_denied_by_limits")
    assert retry["passed"]
    assert retry["observed_error_code"] in sd.LIMIT_REASON_CODES


def test_executor_only_write_attribution_checked() -> None:
    fake = FakeDeployedE5()
    summary = sd.E5ShakedownHarness(_config(), fake).run()
    assert _named(summary, "write_audited_reserved_sfn_executor_only")["passed"]


# --------------------------------------------------------------------------- #
# Negative controls
# --------------------------------------------------------------------------- #


def test_write_not_executor_attributed_fails_check() -> None:
    fake = FakeDeployedE5()
    fake.executor_write_actor = "evaluator"  # a non-executor principal wrote
    summary = sd.E5ShakedownHarness(_config(), fake).run()
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

    summary = sd.E5ShakedownHarness(_config(), LeakyFake()).run()
    assert _named(summary, "forced_evaluator_executor_cannot_write")["passed"] is False
    assert summary["accepted"] is False


def test_ambiguous_capacity_result_fails_closed() -> None:
    class AmbiguousFake(FakeDeployedE5):
        def __call__(self, method, url, headers, body):
            path = url[len(ENDPOINT) :]
            if path.endswith("/capacity"):
                return _resp(200, {"capacity": {}})  # missing desired -> ambiguous
            return super().__call__(method, url, headers, body)

    summary = sd.E5ShakedownHarness(_config(), AmbiguousFake()).run()
    assert summary["accepted"] is False
    ambiguous = _named(summary, "capacity_is_one")
    assert ambiguous["observed_error_code"] == "AMBIGUOUS_RESULT"


def test_audit_incomplete_fails_check() -> None:
    fake = FakeDeployedE5()
    fake.audit_complete = False
    summary = sd.E5ShakedownHarness(_config(), fake).run()
    assert _named(summary, "audit_complete")["passed"] is False


def test_artifact_mismatch_fails_check() -> None:
    fake = FakeDeployedE5()
    fake.artifact_matches = False
    summary = sd.E5ShakedownHarness(_config(), fake).run()
    assert _named(summary, "exact_artifact")["passed"] is False


# --------------------------------------------------------------------------- #
# Public-safety of the emitted summary
# --------------------------------------------------------------------------- #


def test_summary_is_public_safe_and_hides_identifiers() -> None:
    fake = FakeDeployedE5()
    summary = sd.E5ShakedownHarness(_config(), fake).run()
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
