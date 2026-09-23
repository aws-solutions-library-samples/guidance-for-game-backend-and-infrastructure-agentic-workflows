"""Repeatable, public-safe E5 bounded-autonomy *deployed* shakedown harness (#440).

A disposable command-line harness that exercises an **optional, reviewed,
already-deployed** E5 bounded-autonomy boundary end-to-end over HTTPS against its
real API Gateway endpoint, proving the deployed boundary behaves the way the
frozen E5 contracts and runtime promise.

E5 autonomy is **default disabled and default-zero**: the canonical
``deploy-all.sh`` deployment creates **no** autonomy resources, grants **no**
provider-write permission, and mints **no** executor credential. Bounded autonomy
lives only in an *optional* stack an operator stands up out of band after review.
This harness therefore only ever targets such an optional stack, and it is a
validation spike — it deploys nothing, enables nothing by itself, and holds no
credential of its own.

.. danger::

   **The lifecycle run is write-capable — it is not a dry run.** A successful
   forward evaluation drives the real E5 evaluator, reservation, Step Functions
   execution, and the single narrow executor, which performs an actual
   ``UpdateFleetCapacity`` (``0 -> 1``) against the **authorized demo fleet**.
   The harness then drives a separately-confirmed inverse (``1 -> 0``) to restore
   zero. It mutates only that one enrolled demo fleet and only within the frozen
   ``0/1/1`` window, but those writes are real.

   Because of this the harness performs a **hard preflight** and refuses the
   entire run — making **no authenticated request at all** — unless *every* one
   of these holds:

   1. the operator supplies the exact out-of-band forward confirmation
      (:data:`REQUIRED_CONFIRMATION`) **and** the distinct inverse confirmation
      (:data:`REQUIRED_INVERSE_CONFIRMATION`); neither can be sourced from a
      response body;
   2. the operator profile, region, and fleet exactly match the server's
      enrollment;
   3. the starting capacity is exactly ``desired=0 / minimum=0 / maximum=1``;
   4. the static deployment gate, the E4 control gate, and the **separate**
      AppConfig autonomy switch are each fresh and explicitly enabled;
   5. alarms and drift are both in a safe state.

Modeled lifecycle (each step is a discriminating check)
-------------------------------------------------------

1. **Unauthenticated denied** — a request with no bearer never reaches the
   handler (401/403). Non-mutating; always runs.
2. **Trusted E1 observation** — one trusted, bounded read establishes the
   fresh observation the evaluator requires.
3. **Evaluator authorizes 0->1** — the deterministic evaluator authorizes a
   single in-bounds write.
4. **Write audited / reserved / SFN / executor-only** — the resulting write is
   recorded in the audit ledger, was atomically reserved, ran through Step
   Functions, and — critically — the CloudTrail write is attributed to the
   **executor principal only**, never the evaluator or a model path.
5. **Capacity is one** — the enrolled fleet's desired capacity is now ``1``.
6. **Immediate retry denied by limits** — an immediate second forward
   evaluation is denied by cooldown / frequency / concurrency, not by a
   transient error.
7. **Inverse 1->0 confirmed** — a **separately confirmed**, human-approved or
   explicit test-owned inverse write restores the fleet.
8. **Capacity is zero** — the enrolled fleet's desired capacity is back to ``0``.
9. **Autonomy disabled** — the emergency/normal disable turns autonomy off.
10. **Forced evaluator/executor cannot write** — after disable, a forced
    evaluator/executor attempt is refused and performs **no** write.
11-15. **Negative controls** — IAM-negative, alarms-safe, drift-safe,
    exact-artifact, and audit-complete assertions.

Any ambiguous result (e.g. a capacity read missing ``desired``) fails **closed**.

Secret hygiene
--------------

The endpoint, tokens, profile, fleet, observation id, and operation id are read
**only** from arguments or the environment and are **never** logged, echoed, or
written to the emitted summary. The emitted summary is sanitized: identifiers
appear only as stable, non-reversible short hashes, and responses are reduced to
booleans / observed error codes, never their raw contents. This harness reuses
the hardened HTTPS-only transport from the E1 shakedown (no redirects, bounded
streaming, explicit timeouts).
"""

from __future__ import annotations

# Standard library
import argparse
import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

# Local modules
from operations.validation.e1_shakedown import (
    HttpResponse,
    Transport,
    _short_ref,
    response_is_bounded_and_clean,
)
from operations.validation.e5_command_adapter import (
    CommandAdapter,
    CommandAdapterConfig,
    CommandTransport,
    subprocess_runner,
)

# The exact, frozen out-of-band confirmations the operator must supply. They are
# compared byte-for-byte (no trimming, no case folding) and are **distinct**: the
# forward (``0 -> 1``) write and the inverse (``1 -> 0``) restore each require
# their own confirmation, so neither can unlock the other. Neither is ever read
# from a response body — only from the CLI flag / environment.
REQUIRED_CONFIRMATION = "EXECUTE-LIVE-AUTONOMY-0-TO-1"
REQUIRED_INVERSE_CONFIRMATION = "EXECUTE-LIVE-AUTONOMY-INVERSE-1-TO-0"

# The frozen starting capacity triple the preflight requires. Anything else
# refuses the run: the demo must begin at rest with headroom for exactly one
# in-bounds step.
REQUIRED_START_DESIRED = 0
REQUIRED_START_MINIMUM = 0
REQUIRED_START_MAXIMUM = 1

# Bounded teardown reconcile/disable poll. The guaranteed teardown re-reads and
# reconciles up to this many attempts, sleeping between attempts, before it
# gives up and records a FAILED teardown. Kept small and injectable (via the
# harness ``sleep``) so it is bounded and test-fast.
_TEARDOWN_MAX_ATTEMPTS = 3
_TEARDOWN_POLL_SECONDS = 2.0

# The evaluator reason codes that legitimately deny an *immediate retry*. The
# retry check passes only when the deny reason is one of these (a limit), not a
# transient/ambiguous error. These mirror the frozen #438 contract's
# rate-limiting reason codes.
LIMIT_REASON_CODES = frozenset({"COOLDOWN_ACTIVE", "FREQUENCY_EXCEEDED", "CONCURRENCY_LIMIT"})

# The principal the single provider write must be attributed to in CloudTrail.
# Anything else (evaluator, model path, admin) fails the executor-only check.
_EXECUTOR_WRITE_ACTOR = "executor"

# Markers that must never appear in the sanitized summary.
_FORBIDDEN_MARKERS = ("arn:aws:", "fleet-", "execute-api", "Bearer ", "eyJ", "AKIA")


# The concrete COMMAND CONTRACT for a real run. The E5 evaluator exposes NO HTTP
# API: it is a Lambda invoked by EventBridge / Step Functions StartExecution, and
# its effects are verified through provider-native reads. Each logical lifecycle
# step below maps to a concrete AWS operation, NOT an HTTP route. A real adapter
# wires these; the HTTP-shaped ``path`` strings in this module are only the
# in-memory test seam and must never be issued against a live plane.
ADAPTER_COMMAND_CONTRACT = {
    "observe": "lambda:InvokeFunction on the E1/E2 observe function (trusted observation)",
    "evaluate": "lambda:InvokeFunction on the E5 evaluator with the closed #439 event",
    "dispatch": "stepfunctions:DescribeExecution on the started E3 STANDARD execution",
    "audit_reservation": "dynamodb:GetItem on the 06 operations table (audit + reservation items)",
    "write_attribution": "cloudtrail:LookupEvents to confirm the executor (not the evaluator) wrote",
    "capacity": "gamelift:DescribeFleetCapacity / DescribeFleetLocationCapacity on the enrolled fleet",
    "disable": "appconfig deploy of the disabled autonomy switch document",
}


def confirmation_is_valid(confirmation: str) -> bool:
    """Return whether the operator-supplied forward confirmation exactly unlocks 0->1."""
    return hmac.compare_digest(confirmation, REQUIRED_CONFIRMATION)


def inverse_confirmation_is_valid(confirmation: str) -> bool:
    """Return whether the operator-supplied inverse confirmation exactly unlocks 1->0."""
    return hmac.compare_digest(confirmation, REQUIRED_INVERSE_CONFIRMATION)


@dataclass(frozen=True, slots=True)
class E5Preflight:
    """The hard preflight state that must all hold before any authenticated call.

    Every field is a server-owned / operator-verified fact supplied out of band.
    :meth:`refusals` returns the stable refusal codes for whichever conditions do
    not hold; an empty list means the preflight is safe.
    """

    profile: str
    region: str
    fleet_id: str
    enrolled_profile: str
    enrolled_region: str
    enrolled_fleet_id: str
    starting_desired: int
    starting_minimum: int
    starting_maximum: int
    static_gate_fresh_enabled: bool
    e4_gate_fresh_enabled: bool
    autonomy_switch_fresh_enabled: bool
    alarms_safe: bool
    drift_safe: bool

    def refusals(self) -> list[str]:
        codes: list[str] = []
        if self.profile != self.enrolled_profile:
            codes.append("PROFILE_MISMATCH")
        if self.region != self.enrolled_region:
            codes.append("REGION_MISMATCH")
        if self.fleet_id != self.enrolled_fleet_id:
            codes.append("FLEET_MISMATCH")
        if (
            self.starting_desired != REQUIRED_START_DESIRED
            or self.starting_minimum != REQUIRED_START_MINIMUM
            or self.starting_maximum != REQUIRED_START_MAXIMUM
        ):
            codes.append("STARTING_CAPACITY_NOT_0_0_1")
        if not self.static_gate_fresh_enabled:
            codes.append("STATIC_GATE_NOT_FRESH_ENABLED")
        if not self.e4_gate_fresh_enabled:
            codes.append("E4_GATE_NOT_FRESH_ENABLED")
        if not self.autonomy_switch_fresh_enabled:
            codes.append("AUTONOMY_SWITCH_NOT_FRESH_ENABLED")
        if not self.alarms_safe:
            codes.append("ALARMS_NOT_SAFE")
        if not self.drift_safe:
            codes.append("DRIFT_NOT_SAFE")
        return codes

    @property
    def is_safe(self) -> bool:
        return not self.refusals()


@dataclass(frozen=True, slots=True)
class E5ShakedownConfig:
    """Validated, secret-free-at-rest configuration for one shakedown run."""

    endpoint: str
    admin_bearer: str
    fleet_id: str
    observation_id: str
    operation_id: str
    preflight: E5Preflight
    confirmation: str = ""
    inverse_confirmation: str = ""
    # Optional distinct token used only for the post-disable forced-write attempt.
    # Falls back to the admin bearer when not supplied.
    forced_bearer: str = ""

    def __post_init__(self) -> None:
        if not self.endpoint or not self.endpoint.lower().startswith("https://"):
            raise ValueError("endpoint must be a non-empty https:// URL")
        if not self.admin_bearer or not self.admin_bearer.strip():
            raise ValueError("admin_bearer must be a non-empty token")
        if not self.fleet_id or not self.fleet_id.strip():
            raise ValueError("fleet_id must be a non-empty identifier")
        if not self.operation_id.startswith("op_"):
            raise ValueError("operation_id must be an op_ identifier")
        if not self.observation_id.startswith("obs_"):
            raise ValueError("observation_id must be an obs_ identifier")

    def refusal_codes(self) -> list[str]:
        """Return every reason the whole run must be refused, or an empty list.

        Preflight facts first, then the two mandatory out-of-band confirmations.
        A run proceeds only when this is empty.
        """
        codes = list(self.preflight.refusals())
        if not confirmation_is_valid(self.confirmation):
            codes.append("CONFIRMATION_REQUIRED")
        if not inverse_confirmation_is_valid(self.inverse_confirmation):
            codes.append("INVERSE_CONFIRMATION_REQUIRED")
        return codes

    @property
    def effective_forced_bearer(self) -> str:
        return self.forced_bearer.strip() or self.admin_bearer


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One sanitized, discriminating check outcome."""

    name: str
    passed: bool
    observed_status: Optional[int] = None
    observed_error_code: Optional[str] = None
    detail: str = ""

    def as_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "observed_status": self.observed_status,
            "observed_error_code": self.observed_error_code,
            "detail": self.detail,
        }


class E5ShakedownHarness:
    """Drive the discriminating E5 autonomy-lifecycle checks against a deployed stack."""

    def __init__(
        self,
        config: E5ShakedownConfig,
        transport: Transport,
        sleep: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._transport = transport
        # Injectable so tests drive the bounded reconcile/disable poll without a
        # real wall-clock wait. Production uses time.sleep.
        if sleep is None:
            # Standard library
            import time

            sleep = time.sleep
        self._sleep = sleep

    # -- transport helpers -------------------------------------------------- #

    def _url(self, path: str) -> str:
        return f"{self._config.endpoint.rstrip('/')}{path}"

    def _request(
        self, method: str, path: str, *, bearer: Optional[str], payload: Optional[dict[str, Any]] = None
    ) -> HttpResponse:
        headers = {"content-type": "application/json"}
        if bearer is not None:
            headers["authorization"] = f"Bearer {bearer}"
        body = json.dumps(payload or {}).encode("utf-8")
        return self._transport(method, self._url(path), headers, body)

    @staticmethod
    def _error_code(response: HttpResponse) -> Optional[str]:
        body = response.json_or_none()
        if isinstance(body, dict):
            code = body.get("error_code")
            return code if isinstance(code, str) else None
        return None

    @staticmethod
    def _body(response: HttpResponse) -> dict[str, Any]:
        body = response.json_or_none()
        return body if isinstance(body, dict) else {}

    def _desired_or_none(self, response: HttpResponse) -> Optional[int]:
        """Return the observed desired capacity, or ``None`` when ambiguous."""
        capacity = self._body(response).get("capacity")
        if not isinstance(capacity, dict):
            return None
        value = capacity.get("desired")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    # -- individual checks -------------------------------------------------- #

    def check_unauthenticated_denied(self) -> CheckResult:
        response = self._request("POST", "/operations/observe", bearer=None)
        return CheckResult(
            name="unauthenticated_denied",
            passed=response.status in (401, 403),
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def check_trusted_e1_observation(self) -> CheckResult:
        response = self._request("POST", "/operations/observe", bearer=self._config.admin_bearer)
        clean, _ = response_is_bounded_and_clean(response)
        body = self._body(response)
        passed = response.status in (200, 201) and clean and body.get("trusted") is True
        return CheckResult(
            name="trusted_e1_observation",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def _evaluate(self, direction: str, *, bearer: str) -> HttpResponse:
        return self._request(
            "POST",
            f"/operations/autonomy/{self._config.operation_id}/evaluate",
            bearer=bearer,
            payload={"direction": direction},
        )

    def check_evaluator_authorizes_0_to_1(self, response: HttpResponse) -> CheckResult:
        clean, _ = response_is_bounded_and_clean(response)
        body = self._body(response)
        passed = (
            response.status == 200
            and clean
            and body.get("decision") == "authorized"
            and body.get("reason_codes") == ["APPROVED_AUTONOMOUS"]
        )
        return CheckResult(
            name="evaluator_authorizes_0_to_1",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def check_write_audited_reserved_sfn_executor_only(self, response: HttpResponse) -> CheckResult:
        body = self._body(response)
        passed = (
            body.get("audit_recorded") is True
            and body.get("reservation_granted") is True
            and body.get("step_functions_started") is True
            and body.get("cloudtrail_write_actor") == _EXECUTOR_WRITE_ACTOR
        )
        return CheckResult(
            name="write_audited_reserved_sfn_executor_only",
            passed=passed,
            observed_status=response.status,
            detail="" if passed else "write not fully audited/reserved/SFN/executor-attributed",
        )

    def _check_capacity(self, name: str, expected: int) -> CheckResult:
        response = self._request(
            "GET", f"/operations/autonomy/{self._config.operation_id}/capacity", bearer=self._config.admin_bearer
        )
        desired = self._desired_or_none(response)
        if desired is None:
            return CheckResult(
                name=name,
                passed=False,
                observed_status=response.status,
                observed_error_code="AMBIGUOUS_RESULT",
                detail="capacity read did not carry an unambiguous integer desired",
            )
        return CheckResult(
            name=name,
            passed=desired == expected,
            observed_status=response.status,
            detail=f"observed desired={desired}, expected={expected}",
        )

    def check_immediate_retry_denied_by_limits(self) -> CheckResult:
        response = self._evaluate("up", bearer=self._config.admin_bearer)
        code = self._error_code(response)
        body = self._body(response)
        passed = response.status == 429 and code in LIMIT_REASON_CODES and body.get("wrote") is False
        return CheckResult(
            name="immediate_retry_denied_by_limits",
            passed=passed,
            observed_status=response.status,
            observed_error_code=code,
        )

    def check_inverse_1_to_0(self, response: HttpResponse) -> CheckResult:
        clean, _ = response_is_bounded_and_clean(response)
        body = self._body(response)
        passed = response.status == 200 and clean and body.get("decision") == "authorized"
        return CheckResult(
            name="inverse_1_to_0_confirmed",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def check_autonomy_disabled(self) -> CheckResult:
        response = self._request("POST", "/operations/autonomy/disable", bearer=self._config.admin_bearer)
        body = self._body(response)
        passed = response.status == 200 and body.get("autonomy_enabled") is False
        return CheckResult(
            name="autonomy_disabled",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def check_forced_write_denied(self) -> CheckResult:
        """A forced evaluator/executor attempt after disable must not write."""
        response = self._request(
            "POST",
            f"/operations/autonomy/{self._config.operation_id}/force-write",
            bearer=self._config.effective_forced_bearer,
            payload={"direction": "up", "force": True},
        )
        body = self._body(response)
        # Fail closed: the check passes only when the deployment both refused
        # (>=400) and explicitly reports it performed no write.
        passed = response.status >= 400 and body.get("wrote") is False
        return CheckResult(
            name="forced_evaluator_executor_cannot_write",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
            detail="" if passed else "forced attempt was not fully refused / may have written",
        )

    def check_iam_negative(self) -> CheckResult:
        """No autonomy component holds provider-write permission of its own.

        Proven by the forced attempt above producing no write; here we assert the
        forced principal is denied at the boundary rather than silently no-op'ing
        a write it lacks permission for. Reuses the disabled-state force-write
        response semantics: a non-executor forced principal cannot write.
        """
        response = self._request(
            "POST",
            f"/operations/autonomy/{self._config.operation_id}/force-write",
            bearer=self._config.effective_forced_bearer,
            payload={"direction": "up", "force": True},
        )
        body = self._body(response)
        passed = response.status in (403, 409) and body.get("wrote") is False
        return CheckResult(
            name="iam_negative",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    # -- orchestration ------------------------------------------------------ #

    def run(self) -> dict[str, Any]:
        """Run the whole lifecycle, or refuse before any authenticated request.

        Fails closed: if any preflight fact or either confirmation is missing,
        ``run()`` makes **no** authenticated request and returns a refusal
        summary. Otherwise it runs the discriminating checks in lifecycle order
        and returns a fully sanitized summary.
        """
        refusal_codes = self._config.refusal_codes()
        if refusal_codes:
            return self._sanitized_summary(checks=[], refused=True, refusal_codes=refusal_codes)

        checks: list[CheckResult] = []

        # 1. Unauthenticated denied (non-mutating).
        checks.append(self.check_unauthenticated_denied())

        # 2. One trusted E1 observation.
        checks.append(self.check_trusted_e1_observation())

        # Everything from the forward 0 -> 1 write onward is a SCALED state that
        # MUST be torn back down no matter what happens. We track whether a
        # scale-up was attempted and guarantee, in a finally block, a
        # separately-confirmed inverse 1 -> 0 restore and an autonomy disable —
        # even if a check in between raises. This is the safety-critical property:
        # the harness never leaves the enrolled fleet scaled up.
        scaled_up = False
        forward: Optional[HttpResponse] = None
        try:
            # 3-4. Forward evaluation 0 -> 1 (write-capable) and its write assertions.
            forward = self._evaluate("up", bearer=self._config.admin_bearer)
            scaled_up = True
            checks.append(self.check_evaluator_authorizes_0_to_1(forward))
            checks.append(self.check_write_audited_reserved_sfn_executor_only(forward))

            # 5. Capacity is one.
            checks.append(self._check_capacity("capacity_is_one", 1))

            # 6. Immediate retry denied by cooldown/frequency/concurrency.
            checks.append(self.check_immediate_retry_denied_by_limits())

            # 7. Separately-confirmed inverse 1 -> 0 (restore).
            inverse = self._evaluate("down", bearer=self._config.admin_bearer)
            checks.append(self.check_inverse_1_to_0(inverse))
            scaled_up = False  # the planned restore succeeded

            # 8. Capacity is zero (restored).
            checks.append(self._check_capacity("capacity_is_zero", 0))

            # 9. Disable autonomy.
            checks.append(self.check_autonomy_disabled())

            # 10. Forced evaluator/executor attempt after disable cannot write.
            checks.append(self.check_forced_write_denied())

            # 11-15. Negative controls.
            checks.append(self.check_iam_negative())
            checks.append(self._preflight_echo_check("alarms_safe", self._config.preflight.alarms_safe))
            checks.append(self._preflight_echo_check("drift_safe", self._config.preflight.drift_safe))
            checks.append(self._exact_artifact_check(forward))
            checks.append(self._audit_complete_check(forward))
        except Exception as exc:  # noqa: BLE001 - record and still tear down
            # A mid-lifecycle failure must NOT abort teardown or the summary. It is
            # recorded as a failed check; the finally block still restores+disables.
            checks.append(
                CheckResult(
                    name="lifecycle_exception",
                    passed=False,
                    detail=f"mid-lifecycle failure: {type(exc).__name__}",
                )
            )
        finally:
            # GUARANTEED teardown: if the fleet was scaled up and the planned
            # inverse did not already restore it, force the separately-confirmed
            # inverse 1 -> 0 and disable autonomy, recording the outcome. This runs
            # on the normal path (a no-op when already restored+disabled) and on
            # every post-scale exception path.
            checks.append(self._guaranteed_restore_and_disable(scaled_up))

        return self._sanitized_summary(checks=checks, refused=False, refusal_codes=[])

    def _read_desired_or_ambiguous(self) -> tuple[Optional[int], bool]:
        """Return (desired, read_ok). A read that RAISES or is ambiguous returns
        (None, False) WITHOUT skipping the caller's reconcile — an ambiguous read
        is treated as 'not confirmed zero', never as 'already restored'."""
        try:
            desired = self._desired_or_none(
                self._request(
                    "GET",
                    f"/operations/autonomy/{self._config.operation_id}/capacity",
                    bearer=self._config.admin_bearer,
                )
            )
            return desired, True
        except Exception:  # noqa: BLE001 - an erroring read is ambiguous, not fatal
            return None, False

    def _confirm_capacity_zero(self) -> bool:
        """Poll the capacity, reconciling with a bounded inverse until the fleet
        is CONFIRMED at desired=0 or the attempts are exhausted.

        The inverse is attempted whenever the capacity is not confirmed zero —
        INCLUDING when the read raised or was ambiguous. The read never being
        clean does not skip the inverse; it only means we keep trying within the
        bounded budget. Any exception is swallowed so teardown never re-raises."""
        for attempt in range(_TEARDOWN_MAX_ATTEMPTS):
            desired, _read_ok = self._read_desired_or_ambiguous()
            if desired == 0:
                return True
            # Not confirmed zero (ambiguous OR > 0): attempt the bounded inverse
            # through the trusted adapter, then loop to re-read and reconcile.
            try:
                self._evaluate("down", bearer=self._config.admin_bearer)
            except Exception:  # noqa: BLE001 - teardown must never re-raise
                pass
            if attempt + 1 < _TEARDOWN_MAX_ATTEMPTS:
                self._sleep(_TEARDOWN_POLL_SECONDS)
        # Final confirming read after the last inverse.
        desired, _ = self._read_desired_or_ambiguous()
        return desired == 0

    def _read_autonomy_disabled(self) -> Optional[bool]:
        """Re-read the live disable state (07/09/AppConfig-backed). Returns True
        when confirmed disabled, False when still enabled, None when ambiguous."""
        try:
            response = self._request("GET", "/operations/autonomy/disable", bearer=self._config.admin_bearer)
        except Exception:  # noqa: BLE001 - an erroring read is ambiguous
            return None
        body = self._body(response)
        value = body.get("autonomy_enabled")
        if value is True:
            return False
        if value is False:
            return True
        return None

    def _confirm_autonomy_disabled(self) -> bool:
        """Issue disable, then WAIT and RE-READ the disable state until it is
        CONFIRMED disabled or the bounded attempts are exhausted. A POST that
        optimistically returns 200 is NOT trusted; only a re-read that reports
        ``autonomy_enabled is False`` confirms the disable."""
        try:
            self._request("POST", "/operations/autonomy/disable", bearer=self._config.admin_bearer)
        except Exception:  # noqa: BLE001 - teardown must never re-raise
            pass
        for attempt in range(_TEARDOWN_MAX_ATTEMPTS):
            state = self._read_autonomy_disabled()
            if state is True:
                return True
            # Still enabled or ambiguous: wait and re-read (and re-issue disable
            # on a still-enabled read) within the bounded budget.
            if state is False:
                try:
                    self._request("POST", "/operations/autonomy/disable", bearer=self._config.admin_bearer)
                except Exception:  # noqa: BLE001
                    pass
            if attempt + 1 < _TEARDOWN_MAX_ATTEMPTS:
                self._sleep(_TEARDOWN_POLL_SECONDS)
        return self._read_autonomy_disabled() is True

    def _guaranteed_restore_and_disable(self, scaled_up: bool) -> CheckResult:
        """Exception-safe restore of the fleet to 0 and confirmed disable.

        Reconciles capacity to a CONFIRMED zero via a bounded poll+inverse loop
        (never skipping the inverse just because a read raised or was ambiguous),
        then issues disable and WAITS/RE-READS the disable state until confirmed.
        Any failure is recorded as a FAILED check rather than raised, so the
        summary always surfaces whether the guaranteed teardown succeeded."""
        restored = self._confirm_capacity_zero()
        disabled = self._confirm_autonomy_disabled()
        detail = ""
        if not restored:
            detail = "guaranteed reconcile did not confirm the fleet restored to zero"
        if not disabled:
            detail = (detail + "; " if detail else "") + "disable was not confirmed on re-read"
        if restored and disabled and not scaled_up:
            detail = "no scale-up occurred; fleet confirmed at zero and autonomy confirmed disabled"
        return CheckResult(name="guaranteed_restore", passed=restored and disabled, detail=detail)

    def _preflight_echo_check(self, name: str, value: bool) -> CheckResult:
        return CheckResult(name=name, passed=bool(value))

    def _exact_artifact_check(self, forward: HttpResponse) -> CheckResult:
        passed = self._body(forward).get("artifact_matches_expected") is True
        return CheckResult(
            name="exact_artifact",
            passed=passed,
            detail="" if passed else "produced artifact did not match the expected prepared operation",
        )

    def _audit_complete_check(self, forward: HttpResponse) -> CheckResult:
        passed = self._body(forward).get("audit_recorded") is True
        return CheckResult(
            name="audit_complete",
            passed=passed,
            detail="" if passed else "no complete audit record was written for the autonomous write",
        )

    def _sanitized_summary(
        self, *, checks: list[CheckResult], refused: bool, refusal_codes: list[str]
    ) -> dict[str, Any]:
        return {
            "harness": "e5-autonomy",
            "endpoint_ref": _short_ref("ep", self._config.endpoint),
            "operation_ref": _short_ref("op", self._config.operation_id),
            "observation_ref": _short_ref("obs", self._config.observation_id),
            "fleet_ref": _short_ref("flt", self._config.fleet_id),
            "profile_ref": _short_ref("prf", self._config.preflight.profile),
            "region": self._config.preflight.region,
            "refused": refused,
            "refusal_codes": refusal_codes,
            "checks": [check.as_summary() for check in checks],
            "accepted": (not refused) and bool(checks) and all(check.passed for check in checks),
        }


def summary_is_public_safe(summary: dict[str, Any]) -> bool:
    """Return whether a rendered summary is free of any forbidden secret marker."""
    blob = json.dumps(summary)
    return not any(marker in blob for marker in _FORBIDDEN_MARKERS)


def _build_preflight(args: argparse.Namespace) -> E5Preflight:
    def _int(name: str, default: int) -> int:
        raw = getattr(args, name, None) or os.environ.get(f"GBAW_E5_{name.upper()}", "")
        return int(raw) if str(raw).strip() else default

    def _flag(name: str) -> bool:
        raw = getattr(args, name, None)
        if raw is None:
            raw = os.environ.get(f"GBAW_E5_{name.upper()}", "")
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    return E5Preflight(
        profile=args.profile or os.environ.get("GBAW_E5_PROFILE", ""),
        region=args.region or os.environ.get("GBAW_E5_REGION", ""),
        fleet_id=args.fleet_id or os.environ.get("GBAW_E5_FLEET_ID", ""),
        enrolled_profile=args.enrolled_profile or os.environ.get("GBAW_E5_ENROLLED_PROFILE", ""),
        enrolled_region=args.enrolled_region or os.environ.get("GBAW_E5_ENROLLED_REGION", ""),
        enrolled_fleet_id=args.enrolled_fleet_id or os.environ.get("GBAW_E5_ENROLLED_FLEET_ID", ""),
        starting_desired=_int("starting_desired", -1),
        starting_minimum=_int("starting_minimum", -1),
        starting_maximum=_int("starting_maximum", -1),
        static_gate_fresh_enabled=_flag("static_gate_fresh_enabled"),
        e4_gate_fresh_enabled=_flag("e4_gate_fresh_enabled"),
        autonomy_switch_fresh_enabled=_flag("autonomy_switch_fresh_enabled"),
        alarms_safe=_flag("alarms_safe"),
        drift_safe=_flag("drift_safe"),
    )


def _build_config(args: argparse.Namespace) -> E5ShakedownConfig:
    return E5ShakedownConfig(
        endpoint=args.endpoint or os.environ.get("GBAW_E5_ENDPOINT", ""),
        admin_bearer=args.admin_bearer or os.environ.get("GBAW_E5_ADMIN_BEARER", ""),
        fleet_id=args.fleet_id or os.environ.get("GBAW_E5_FLEET_ID", ""),
        observation_id=args.observation_id or os.environ.get("GBAW_E5_OBSERVATION_ID", ""),
        operation_id=args.operation_id or os.environ.get("GBAW_E5_OPERATION_ID", ""),
        preflight=_build_preflight(args),
        confirmation=args.confirm_forward or os.environ.get("GBAW_E5_CONFIRM_FORWARD", ""),
        inverse_confirmation=args.confirm_inverse or os.environ.get("GBAW_E5_CONFIRM_INVERSE", ""),
        forced_bearer=args.forced_bearer or os.environ.get("GBAW_E5_FORCED_BEARER", ""),
    )


def _build_command_adapter(args: argparse.Namespace, config: "E5ShakedownConfig") -> CommandAdapter:
    """Assemble the concrete command adapter from args/env. No secrets are read."""

    def _val(name: str, default: str = "") -> str:
        return getattr(args, name, None) or os.environ.get(f"GBAW_E5_{name.upper()}", "") or default

    project = _val("project_name", "game-agent")
    adapter_config = CommandAdapterConfig(
        region=config.preflight.region,
        profile=config.preflight.profile,
        evaluator_function_name=_val("evaluator_function_name", f"{project}-operations-autonomy-evaluator"),
        observe_function_name=_val("observe_function_name", f"{project}-operations-observation"),
        operations_table_name=_val("operations_table_name", f"{project}-operations"),
        enrolled_fleet_id=config.preflight.enrolled_fleet_id or config.fleet_id,
        autonomy_application_id=_val("autonomy_application_id"),
        autonomy_environment_id=_val("autonomy_environment_id"),
        autonomy_switch_profile_id=_val("autonomy_switch_profile_id"),
        errors_alarm_name=_val("errors_alarm_name", f"{project}-operations-AutonomyEvaluatorErrors"),
        throttles_alarm_name=_val("throttles_alarm_name", f"{project}-operations-AutonomyEvaluatorThrottles"),
        delivery_alarm_name=_val("delivery_alarm_name", f"{project}-operations-AutonomyEvaluationDeliveryFailures"),
        dead_letter_alarm_name=_val("dead_letter_alarm_name", f"{project}-operations-AutonomyDeadLetter"),
        # The trusted E1 observation the evaluator resolves the observation from.
        # It is the ONLY event-carried coordinate; the requested capacity triple
        # is derived from the lifecycle direction, never from the event body.
        observation_operation_id=config.observation_id,
        # The E4 (08) kill-switch coordinate, measured for freshness so the
        # preflight refuses on a stale/absent kill switch.
        kill_switch_application_id=_val("appconfig_application_id"),
        kill_switch_environment_id=_val("appconfig_environment_id"),
        kill_switch_profile_id=_val("appconfig_profile_id"),
        # The 09 autonomy stack, measured for FRESH drift (detect-stack-drift).
        autonomy_stack_name=_val("autonomy_stack_name", f"{project}-operations-autonomy"),
        # The 07 Standard state-machine ARN the evaluator starts; required to
        # build the exact dispatched executionArn from the deterministic name.
        autonomy_state_machine_arn=_val("autonomy_state_machine_arn"),
        # The enrolled fleet's LOCATION; capacity is read scoped to it.
        enrolled_location=_val("enrolled_location") or config.preflight.enrolled_region or config.preflight.region,
    )
    return CommandAdapter(adapter_config, runner=subprocess_runner)


def _measure_preflight(adapter: CommandAdapter, supplied: E5Preflight) -> E5Preflight:
    """Return a preflight whose measurable facts come from the provider.

    Capacity, alarm safety, and autonomy-switch freshness are READ from the
    provider and OVERRIDE the operator-supplied values. If an observed value
    conflicts with a supplied "safe" flag, the measured (unsafe) value wins so
    the run refuses. Profile/region/fleet identity remain operator-declared and
    are still checked by E5Preflight.refusals().
    """
    caps = adapter.observed_fleet_capacity()
    if caps is None:
        # Missing/ambiguous capacity read -> fail closed with an out-of-window
        # sentinel so STARTING_CAPACITY_NOT_0_0_1 fires.
        starting_desired, starting_minimum, starting_maximum = -1, -1, -1
    else:
        starting_desired = caps["desired"]
        starting_minimum = caps["minimum"]
        starting_maximum = caps["maximum"]
    alarms_safe = adapter.observed_alarms_safe()
    autonomy_switch_fresh_enabled = adapter.observed_autonomy_switch_fresh_enabled()
    # #440 Finding 8: every measurable preflight fact is READ from the provider
    # and AND-ed with the supplied value, so a measurement can only make the
    # preflight MORE conservative, never less. No fact below is trusted from the
    # operator boolean alone: the static deployment mode, the E4 kill-switch
    # freshness, the E5 autonomy-switch freshness, drift, and enrollment are all
    # measured. Identity (profile/region/fleet) stays operator-declared and is
    # still cross-checked by E5Preflight.refusals(); the enrollment measurement
    # additionally requires the fleet to be observed ACTIVE.
    static_mode_operate = adapter.observed_static_mode_operate()
    e4_kill_switch_fresh = adapter.observed_e4_kill_switch_fresh()
    no_stack_drift = adapter.observed_no_stack_drift()
    fleet_enrolled_active = adapter.observed_fleet_enrolled_active()
    return E5Preflight(
        profile=supplied.profile,
        region=supplied.region,
        fleet_id=supplied.fleet_id,
        enrolled_profile=supplied.enrolled_profile,
        enrolled_region=supplied.enrolled_region,
        enrolled_fleet_id=supplied.enrolled_fleet_id,
        starting_desired=starting_desired,
        starting_minimum=starting_minimum,
        starting_maximum=starting_maximum,
        static_gate_fresh_enabled=static_mode_operate and supplied.static_gate_fresh_enabled,
        e4_gate_fresh_enabled=e4_kill_switch_fresh and supplied.e4_gate_fresh_enabled,
        autonomy_switch_fresh_enabled=autonomy_switch_fresh_enabled and supplied.autonomy_switch_fresh_enabled,
        alarms_safe=alarms_safe,
        drift_safe=no_stack_drift and fleet_enrolled_active and supplied.drift_safe,
    )


def _replace_preflight(config: "E5ShakedownConfig", preflight: E5Preflight) -> "E5ShakedownConfig":
    """Return a copy of config with a new (measured) preflight."""
    # Standard library
    import dataclasses

    return dataclasses.replace(config, preflight=preflight)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Public-safe E5 bounded-autonomy shakedown")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--admin-bearer", dest="admin_bearer", default="")
    parser.add_argument("--forced-bearer", dest="forced_bearer", default="")
    parser.add_argument("--fleet-id", dest="fleet_id", default="")
    parser.add_argument("--observation-id", dest="observation_id", default="")
    parser.add_argument("--operation-id", dest="operation_id", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--enrolled-profile", dest="enrolled_profile", default="")
    parser.add_argument("--enrolled-region", dest="enrolled_region", default="")
    parser.add_argument("--enrolled-fleet-id", dest="enrolled_fleet_id", default="")
    parser.add_argument("--starting-desired", dest="starting_desired", default="")
    parser.add_argument("--starting-minimum", dest="starting_minimum", default="")
    parser.add_argument("--starting-maximum", dest="starting_maximum", default="")
    parser.add_argument("--static-gate-fresh-enabled", dest="static_gate_fresh_enabled", default=None)
    parser.add_argument("--e4-gate-fresh-enabled", dest="e4_gate_fresh_enabled", default=None)
    parser.add_argument("--autonomy-switch-fresh-enabled", dest="autonomy_switch_fresh_enabled", default=None)
    parser.add_argument("--alarms-safe", dest="alarms_safe", default=None)
    parser.add_argument("--drift-safe", dest="drift_safe", default=None)
    parser.add_argument(
        "--confirm-forward",
        dest="confirm_forward",
        default="",
        help=(
            "Exact out-of-band confirmation authorizing the write-capable "
            "forward (0->1) autonomous write against the authorized demo fleet. "
            "Must equal the frozen REQUIRED_CONFIRMATION value."
        ),
    )
    parser.add_argument(
        "--confirm-inverse",
        dest="confirm_inverse",
        default="",
        help=(
            "Exact out-of-band confirmation authorizing the separately-confirmed "
            "inverse (1->0) restore. Must equal REQUIRED_INVERSE_CONFIRMATION and "
            "is distinct from the forward confirmation."
        ),
    )
    parser.add_argument(
        "--adapter",
        default="",
        choices=["", "command"],
        help=(
            "REQUIRED to run: the E5 evaluator has no HTTP API. Pass "
            "'--adapter command' to acknowledge the concrete command contract "
            "(Lambda/StepFunctions/DynamoDB/CloudTrail/GameLift). Without it the "
            "harness refuses rather than issue HTTP to nonexistent routes."
        ),
    )
    args = parser.parse_args(argv)

    if args.adapter != "command":
        # Fail closed: never issue HTTP to imaginary routes. A real run must wire
        # the concrete command adapter and pass --adapter command.
        print(
            json.dumps(
                {
                    "error": "refused: the E5 evaluator has no HTTP API; wire a concrete command adapter",
                    "command_contract": ADAPTER_COMMAND_CONTRACT,
                }
            )
        )
        return 3

    config = _build_config(args)

    # Finding 7: build the CONCRETE command adapter and drive the lifecycle
    # through it — NEVER _requests_transport / an HTTP route. The adapter issues
    # structured `aws` CLI JSON commands only.
    adapter = _build_command_adapter(args, config)

    # Finding 8: MEASURE the preflight facts from the provider via the adapter
    # (fleet capacity, the four AWS-native alarms, and the live autonomy switch
    # freshness) and OVERLAY them over the operator-supplied values. A conflict
    # between an observed state and a supplied boolean fails closed BEFORE any
    # authenticated mutation.
    try:
        measured = _measure_preflight(adapter, config.preflight)
    except Exception as exc:  # noqa: BLE001 - a failed measurement fails closed
        print(json.dumps({"error": f"refused: preflight measurement failed ({type(exc).__name__})"}))
        return 3
    config = _replace_preflight(config, measured)
    transport = CommandTransport(adapter)
    harness = E5ShakedownHarness(config, transport)
    summary = harness.run()
    if not summary_is_public_safe(summary):  # defensive: never emit a leaky summary
        print(json.dumps({"error": "summary failed public-safety check"}))
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["refused"]:
        return 3
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
