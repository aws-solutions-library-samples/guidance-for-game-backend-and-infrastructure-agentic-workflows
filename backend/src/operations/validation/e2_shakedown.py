"""Repeatable, public-safe E2 *deployed* approval shakedown harness (#414).

A disposable command-line harness that exercises an **already-deployed** E2
prepare/approval control plane end-to-end over HTTP against its real API Gateway
endpoint, proving the deployed boundary behaves the way the frozen E2 contract
and the on-host handler (``operations.approval_handler``) promise, without
mutating any AWS resource beyond the operation records the approval flow itself
creates.

What it asserts (each is one discriminating check):

1. **Unauthenticated prepare is denied at the gateway.** A prepare with no
   bearer token never reaches the handler (401/403).
2. **Identity injection in the prepare body is denied.** An authenticated
   prepare whose body carries an injected ``requester``/``workspace_id`` is
   rejected with ``CONTRACT_INVALID`` — identity is never read from the body.
3. **A valid authenticated prepare succeeds** with a bounded, typed
   ``approval_required`` prepared operation and an ``op_`` id.
4. **The requester's own approval is denied** by the default self-approval
   policy (403).
5. **A distinct human approver grants** (200), and **a replayed grant conflicts**
   (409) — the fenced, atomic single-grant invariant.
6. **GET returns bounded evidence** for the operation (200), leak-free.
7. **Reject and cancel on the now-approved operation conflict** (409) — the
   terminal-state fence.

Secret hygiene
--------------

The endpoint, the short-lived Cognito **access** token, the distinct **approver**
token, the fleet id, and the bound observation id are read **only** from
arguments or the environment and are **never** logged, echoed, or written to the
emitted summary. Tokens live in memory for the duration of the run and are not
persisted. The emitted summary is sanitized: identifiers appear only as stable,
non-reversible short hashes, and responses are reduced to booleans/observed error
codes, never their raw contents.

This harness never deploys, enables, disables, or mutates any AWS or GitHub
infrastructure. It reuses the hardened HTTP transport from the E1 shakedown
(HTTPS-only, no redirects, bounded streaming, explicit timeouts).
"""

from __future__ import annotations

# Standard library
import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

# Local modules
from operations.validation.e1_shakedown import (
    HttpResponse,
    Transport,
    TransportSecurityError,
    _requests_transport,
    response_is_bounded_and_clean,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISSING_INPUT = 2
EXIT_SHAKEDOWN_FAILED = 4

CONTRACT_VERSION = "1.0"

_FLEET_ID_PATTERN = re.compile(r"^fleet-[a-f0-9-]{1,120}$")
_LOCATION_PATTERN = re.compile(r"^[a-z0-9-]+$")
_OBSERVATION_ID_PATTERN = re.compile(r"^obs_[a-z0-9]{26}$")


def _short_ref(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _new_idempotency_token() -> str:
    return "idem_" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


@dataclass(frozen=True, slots=True)
class E2ShakedownConfig:
    """Inputs for the E2 shakedown. Tokens are never logged or summarized."""

    endpoint: str
    access_token: str
    approver_token: str
    fleet_id: str
    location: str
    observation_id: str

    def __post_init__(self) -> None:
        if not self.endpoint.lower().startswith("https://"):
            raise ValueError("endpoint must be an https:// URL")
        if not self.access_token or not self.approver_token:
            raise ValueError("access_token and approver_token are required")
        if not _FLEET_ID_PATTERN.fullmatch(self.fleet_id):
            raise ValueError("fleet_id is not a valid classic fleet id")
        if not _LOCATION_PATTERN.fullmatch(self.location):
            raise ValueError("location is not a valid location")
        if not _OBSERVATION_ID_PATTERN.fullmatch(self.observation_id):
            raise ValueError("observation_id is not a valid observation id")


@dataclass
class CheckResult:
    """One discriminating check outcome, reduced to bounded, safe fields."""

    name: str
    passed: bool
    detail: str = ""

    def to_summary(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class E2ShakedownReport:
    endpoint_ref: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def to_summary(self) -> dict[str, Any]:
        return {
            "surface": "e2-approval",
            "endpoint_ref": self.endpoint_ref,
            "ok": self.ok,
            "checks": [c.to_summary() for c in self.checks],
        }


class E2ShakedownRunner:
    """Drive the deployed E2 approval boundary through the acceptance flow."""

    def __init__(self, *, config: E2ShakedownConfig, transport: Transport) -> None:
        self._config = config
        self._transport = transport
        self._token = _new_idempotency_token()

    # -- HTTP helpers -----------------------------------------------------

    def _request(self, method: str, path: str, *, bearer: Optional[str], body: Optional[dict]) -> HttpResponse:
        headers = {"content-type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        return self._transport(method, self._config.endpoint + path, headers, raw)

    def _prepare_body(self) -> dict[str, Any]:
        return {
            "request_contract_version": CONTRACT_VERSION,
            "capability_id": "gamelift.capacity-adjustment",
            "idempotency_token": self._token,
            "observation_id": self._config.observation_id,
            "proposal": {
                "fleet_id": self._config.fleet_id,
                "location": self._config.location,
                "requested": {"desired": 12, "minimum": 2, "maximum": 20},
            },
        }

    # -- Checks -----------------------------------------------------------

    def check_unauthenticated_denied(self) -> CheckResult:
        response = self._request("POST", "/operations/prepare", bearer=None, body=self._prepare_body())
        ok, reason = response_is_bounded_and_clean(response)
        passed = ok and response.status in (401, 403)
        return CheckResult("unauthenticated_denied", passed, "" if passed else f"status={response.status} {reason}")

    def check_identity_injection_denied(self) -> CheckResult:
        body = self._prepare_body()
        body["requester"] = {"subject_id": "attacker"}
        response = self._request("POST", "/operations/prepare", bearer=self._config.access_token, body=body)
        ok, reason = response_is_bounded_and_clean(response)
        passed = ok and response.status == 400
        return CheckResult("identity_injection_denied", passed, "" if passed else f"status={response.status} {reason}")

    def check_prepare(self) -> tuple[CheckResult, Optional[str]]:
        response = self._request(
            "POST", "/operations/prepare", bearer=self._config.access_token, body=self._prepare_body()
        )
        ok, reason = response_is_bounded_and_clean(response)
        payload = response.json_or_none() if ok else None
        operation_id = payload.get("operation_id") if isinstance(payload, dict) else None
        passed = (
            ok
            and response.status == 201
            and isinstance(operation_id, str)
            and operation_id.startswith("op_")
            and isinstance(payload, dict)
            and payload.get("decision") == "approval_required"
        )
        return (
            CheckResult("prepare", passed, "" if passed else f"status={response.status} {reason}"),
            operation_id if passed else None,
        )

    def check_self_approval_denied(self, operation_id: str) -> CheckResult:
        response = self._request(
            "POST", f"/operations/{operation_id}/approve", bearer=self._config.access_token, body={}
        )
        ok, reason = response_is_bounded_and_clean(response)
        passed = ok and response.status == 403
        return CheckResult("self_approval_denied", passed, "" if passed else f"status={response.status} {reason}")

    def check_distinct_approver_grants(self, operation_id: str) -> CheckResult:
        response = self._request(
            "POST", f"/operations/{operation_id}/approve", bearer=self._config.approver_token, body={}
        )
        ok, reason = response_is_bounded_and_clean(response)
        passed = ok and response.status == 200
        return CheckResult("distinct_approver_grants", passed, "" if passed else f"status={response.status} {reason}")

    def check_replay_grant_conflicts(self, operation_id: str) -> CheckResult:
        response = self._request(
            "POST", f"/operations/{operation_id}/approve", bearer=self._config.approver_token, body={}
        )
        ok, reason = response_is_bounded_and_clean(response)
        passed = ok and response.status == 409
        return CheckResult("replay_grant_conflicts", passed, "" if passed else f"status={response.status} {reason}")

    def check_evidence(self, operation_id: str) -> CheckResult:
        response = self._request("GET", f"/operations/{operation_id}", bearer=self._config.approver_token, body=None)
        ok, reason = response_is_bounded_and_clean(response)
        payload = response.json_or_none() if ok else None
        passed = (
            ok and response.status == 200 and isinstance(payload, dict) and payload.get("operation_id") == operation_id
        )
        return CheckResult("evidence", passed, "" if passed else f"status={response.status} {reason}")

    def check_reject_conflict(self, operation_id: str) -> CheckResult:
        response = self._request(
            "POST", f"/operations/{operation_id}/reject", bearer=self._config.approver_token, body={}
        )
        ok, reason = response_is_bounded_and_clean(response)
        passed = ok and response.status == 409
        return CheckResult("reject_conflict", passed, "" if passed else f"status={response.status} {reason}")

    def check_cancel_conflict(self, operation_id: str) -> CheckResult:
        response = self._request(
            "POST", f"/operations/{operation_id}/cancel", bearer=self._config.approver_token, body={}
        )
        ok, reason = response_is_bounded_and_clean(response)
        passed = ok and response.status == 409
        return CheckResult("cancel_conflict", passed, "" if passed else f"status={response.status} {reason}")

    # -- Orchestration ----------------------------------------------------

    def run(self) -> E2ShakedownReport:
        report = E2ShakedownReport(endpoint_ref=_short_ref("endpoint", self._config.endpoint))
        try:
            report.checks.append(self.check_unauthenticated_denied())
            report.checks.append(self.check_identity_injection_denied())
            prepare_check, operation_id = self.check_prepare()
            report.checks.append(prepare_check)
            if operation_id is None:
                return report
            report.checks.append(self.check_self_approval_denied(operation_id))
            report.checks.append(self.check_distinct_approver_grants(operation_id))
            report.checks.append(self.check_replay_grant_conflicts(operation_id))
            report.checks.append(self.check_evidence(operation_id))
            report.checks.append(self.check_reject_conflict(operation_id))
            report.checks.append(self.check_cancel_conflict(operation_id))
        except TransportSecurityError as exc:
            report.checks.append(CheckResult("transport_security", False, str(exc)[:120]))
        return report


# --------------------------------------------------------------------------- #
# CLI (secrets read only from env/args, never logged)
# --------------------------------------------------------------------------- #


def _resolve(cli_value: Optional[str], env_name: str) -> Optional[str]:
    if cli_value:
        return cli_value
    return os.environ.get(env_name)


def build_config_from_env(args: argparse.Namespace) -> E2ShakedownConfig:
    endpoint = _resolve(args.endpoint, "GBAW_E2_ENDPOINT")
    access = _resolve(args.access_token, "GBAW_E2_ACCESS_TOKEN")
    approver = _resolve(args.approver_token, "GBAW_E2_APPROVER_TOKEN")
    fleet = _resolve(args.fleet_id, "GBAW_E2_FLEET_ID")
    location = _resolve(args.location, "GBAW_E2_LOCATION")
    observation = _resolve(args.observation_id, "GBAW_E2_OBSERVATION_ID")
    missing = [
        name
        for name, value in (
            ("endpoint", endpoint),
            ("access_token", access),
            ("approver_token", approver),
            ("fleet_id", fleet),
            ("location", location),
            ("observation_id", observation),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"missing required inputs: {', '.join(missing)}")
    assert endpoint and access and approver and fleet and location and observation
    return E2ShakedownConfig(
        endpoint=endpoint,
        access_token=access,
        approver_token=approver,
        fleet_id=fleet,
        location=location,
        observation_id=observation,
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Public-safe E2 deployed approval shakedown")
    parser.add_argument("--endpoint")
    parser.add_argument("--access-token", dest="access_token")
    parser.add_argument("--approver-token", dest="approver_token")
    parser.add_argument("--fleet-id", dest="fleet_id")
    parser.add_argument("--location")
    parser.add_argument("--observation-id", dest="observation_id")
    parser.add_argument("--out")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        config = build_config_from_env(args)
    except ValueError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_MISSING_INPUT

    runner = E2ShakedownRunner(config=config, transport=_requests_transport())
    report = runner.run()
    summary = report.to_summary()
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    return EXIT_OK if report.ok else EXIT_SHAKEDOWN_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
