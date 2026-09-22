"""Repeatable, public-safe E3 *deployed* execute-dispatch shakedown harness (#415).

A disposable command-line harness that exercises an **already-deployed** E3
dispatch boundary end-to-end over HTTPS against its real API Gateway endpoint,
proving the deployed boundary behaves the way the frozen E3 contract and the
on-host dispatcher (:mod:`operations.execute.dispatcher_handler`) promise —
without mutating any AWS resource. It asserts only the *dispatch* boundary; it
never starts a real capacity write itself.

What it asserts (each is one discriminating check):

1. **Unauthenticated dispatch is denied at the gateway.** A POST with no bearer
   token never reaches the handler (401/403).
2. **A valid admin dispatch is accepted** (202/200) with a bounded, typed
   ``dispatched`` acknowledgement.
3. **A non-admin token is denied** (403) — execution is admin-only. This check
   runs only when a non-admin (plain ``users``) bearer token is supplied
   (``--non-admin-bearer`` / ``GBAW_E3_NON_ADMIN_BEARER``); it is skipped when no
   such token is available, since a deployment may not mint one.

Secret hygiene
--------------

The endpoint, the short-lived Cognito **admin access** token, the fleet id, and
the operation id are read **only** from arguments or the environment and are
**never** logged, echoed, or written to the emitted summary. The emitted summary
is sanitized: identifiers appear only as stable, non-reversible short hashes, and
responses are reduced to booleans / observed error codes, never their raw
contents. This harness never deploys, enables, disables, or mutates any AWS or
GitHub infrastructure and reuses the hardened HTTPS-only transport from the E1
shakedown (no redirects, bounded streaming, explicit timeouts).
"""

from __future__ import annotations

# Standard library
import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

# Local modules
from operations.validation.e1_shakedown import (
    HttpResponse,
    Transport,
    _requests_transport,
    _short_ref,
    response_is_bounded_and_clean,
)

# Markers that must never appear in the sanitized summary.
_FORBIDDEN_MARKERS = ("arn:aws:", "fleet-", "execute-api", "Bearer ", "eyJ")


@dataclass(frozen=True, slots=True)
class E3ShakedownConfig:
    """Validated, secret-free-at-rest configuration for one shakedown run."""

    endpoint: str
    operation_id: str
    admin_bearer: str
    fleet_id: str
    # Optional short-lived non-admin (plain ``users`` group) access token. When
    # provided, the harness runs the admin-only denial check against it; when
    # absent the check is skipped (a deployment may not mint a non-admin token).
    non_admin_bearer: str = ""

    def __post_init__(self) -> None:
        if not self.endpoint or not self.endpoint.lower().startswith("https://"):
            raise ValueError("endpoint must be a non-empty https:// URL")
        if not self.operation_id.startswith("op_"):
            raise ValueError("operation_id must be an op_ identifier")
        if not self.admin_bearer or not self.admin_bearer.strip():
            raise ValueError("admin_bearer must be a non-empty token")
        if not self.fleet_id or not self.fleet_id.strip():
            raise ValueError("fleet_id must be a non-empty identifier")


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


class E3ShakedownHarness:
    """Drive the discriminating E3 dispatch checks against a deployed endpoint."""

    def __init__(self, config: E3ShakedownConfig, transport: Transport) -> None:
        self._config = config
        self._transport = transport

    def _dispatch_url(self) -> str:
        base = self._config.endpoint.rstrip("/")
        return f"{base}/operations/{self._config.operation_id}/execute"

    def _post(self, *, bearer: Optional[str]) -> HttpResponse:
        headers = {"content-type": "application/json"}
        if bearer is not None:
            headers["authorization"] = f"Bearer {bearer}"
        return self._transport("POST", self._dispatch_url(), headers, b"{}")

    @staticmethod
    def _error_code(response: HttpResponse) -> Optional[str]:
        body = response.json_or_none()
        if isinstance(body, dict):
            code = body.get("error_code")
            return code if isinstance(code, str) else None
        return None

    def check_unauthenticated_dispatch_denied(self) -> CheckResult:
        response = self._post(bearer=None)
        passed = response.status in (401, 403)
        return CheckResult(
            name="unauthenticated_dispatch_denied",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def check_admin_dispatch_accepted(self) -> CheckResult:
        response = self._post(bearer=self._config.admin_bearer)
        clean, _ = response_is_bounded_and_clean(response)
        passed = response.status in (200, 202) and clean
        return CheckResult(
            name="admin_dispatch_accepted",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def check_non_admin_dispatch_denied(self) -> CheckResult:
        """Assert a plain non-admin (users) token is denied (403): admin-only."""
        response = self._post(bearer=self._config.non_admin_bearer)
        passed = response.status == 403
        return CheckResult(
            name="non_admin_dispatch_denied",
            passed=passed,
            observed_status=response.status,
            observed_error_code=self._error_code(response),
        )

    def run(self) -> dict[str, Any]:
        """Run every check and return a fully sanitized summary document."""
        checks = [
            self.check_unauthenticated_dispatch_denied(),
            self.check_admin_dispatch_accepted(),
        ]
        if self._config.non_admin_bearer.strip():
            checks.append(self.check_non_admin_dispatch_denied())
        return {
            "harness": "e3-execute-dispatch",
            "endpoint_ref": _short_ref("ep", self._config.endpoint),
            "operation_ref": _short_ref("op", self._config.operation_id),
            "fleet_ref": _short_ref("flt", self._config.fleet_id),
            "checks": [check.as_summary() for check in checks],
            "accepted": all(check.passed for check in checks),
        }


def summary_is_public_safe(summary: dict[str, Any]) -> bool:
    """Return whether a rendered summary is free of any forbidden secret marker."""
    blob = json.dumps(summary)
    return not any(marker in blob for marker in _FORBIDDEN_MARKERS)


def _build_config(args: argparse.Namespace) -> E3ShakedownConfig:
    return E3ShakedownConfig(
        endpoint=args.endpoint or os.environ.get("GBAW_E3_ENDPOINT", ""),
        operation_id=args.operation_id or os.environ.get("GBAW_E3_OPERATION_ID", ""),
        admin_bearer=args.admin_bearer or os.environ.get("GBAW_E3_ADMIN_BEARER", ""),
        fleet_id=args.fleet_id or os.environ.get("GBAW_E3_FLEET_ID", ""),
        non_admin_bearer=args.non_admin_bearer or os.environ.get("GBAW_E3_NON_ADMIN_BEARER", ""),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Public-safe E3 execute-dispatch shakedown")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--operation-id", dest="operation_id", default="")
    parser.add_argument("--admin-bearer", dest="admin_bearer", default="")
    parser.add_argument("--fleet-id", dest="fleet_id", default="")
    parser.add_argument("--non-admin-bearer", dest="non_admin_bearer", default="")
    args = parser.parse_args(argv)

    config = _build_config(args)
    harness = E3ShakedownHarness(config, _requests_transport())
    summary = harness.run()
    if not summary_is_public_safe(summary):  # defensive: never emit a leaky summary
        print(json.dumps({"error": "summary failed public-safety check"}))
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
