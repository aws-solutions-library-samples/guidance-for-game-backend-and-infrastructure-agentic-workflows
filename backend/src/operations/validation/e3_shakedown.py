"""Repeatable, public-safe E3 *deployed* dispatch shakedown harness (#415).

A disposable command-line harness that exercises an **already-deployed** E3
dispatch boundary end-to-end over HTTPS against its real API Gateway endpoint,
proving the deployed boundary behaves the way the frozen E3 contract and the
on-host dispatcher (:mod:`operations.execute.dispatcher_handler`) promise.

.. danger::

   **The admin-dispatch check is write-capable — it is not a dry run.** POSTing
   an admin-authenticated request to the deployed ``/dispatch`` route **starts
   the real E3 Step Functions execution**, which may perform an actual
   ``UpdateFleetCapacity`` against the **enrolled fleet**. Running this harness
   with a live admin token therefore mutates AWS state. It does not mutate
   anything else — only the single enrolled fleet, and only within the bounds
   already prepared for the operation — but that one write is real.

   Because of this, the harness is **safe to run** only when **both** hold:

   1. The operation being dispatched has **already been directly approved by a
      human** through the normal E3 approval workflow (this harness performs no
      approval and is not itself an approval), and
   2. The operator supplies an **explicit, exact confirmation input**
      (``--confirm-live-dispatch`` / ``GBAW_E3_CONFIRM_LIVE_DISPATCH`` set to the
      exact value :data:`REQUIRED_CONFIRMATION`).

   Without that confirmation the harness **refuses** the admin-dispatch check
   and makes **no authenticated request at all** — nothing reaches the deployed
   handler, so no execution can start. The confirmation is an out-of-band
   operator input only: it is never persisted and **can never be sourced from a
   response body**, so a hostile or echoing deployment cannot unlock the write.

Frozen dispatch route
---------------------

The harness POSTs to exactly one frozen route, :data:`DISPATCH_ROUTE_TEMPLATE`
(``/operations/{operationId}/dispatch``). This is the single source of truth the
07 execution CloudFormation template's API Gateway ``RouteKey`` and the
dispatcher handler must all agree on: a mismatch makes a live shakedown 404 for
every check. ``test_operations_e3_route_contract_unit.py`` reads the 07 template
as data (when present) and fails on any harness/template route drift *before*
deployment.

What it asserts (each is one discriminating check):

1. **Unauthenticated dispatch is denied at the gateway.** A POST with no bearer
   token never reaches the handler (401/403). This check is **non-mutating** and
   runs with no confirmation.
2. **A valid admin dispatch is accepted** (202/200) with a bounded, typed
   ``dispatched`` acknowledgement. This check is **write-capable** (see the
   danger note above) and runs **only** when the exact confirmation input is
   supplied; without it the check refuses and makes no authenticated call.
3. **A non-admin token is denied** (403) — execution is admin-only. This check
   runs only when a non-admin (plain ``users``) bearer token is supplied
   (``--non-admin-bearer`` / ``GBAW_E3_NON_ADMIN_BEARER``); it is skipped when no
   such token is available, since a deployment may not mint one. A non-admin
   token cannot start an execution, so this check is treated as non-mutating.

Secret hygiene
--------------

The endpoint, the short-lived Cognito **admin access** token, the fleet id, the
operation id, and the confirmation value are read **only** from arguments or the
environment and are **never** logged, echoed, or written to the emitted summary.
The emitted summary is sanitized: identifiers appear only as stable,
non-reversible short hashes, and responses are reduced to booleans / observed
error codes, never their raw contents. This harness never deploys, enables,
disables, or mutates any AWS or GitHub infrastructure other than the single
already-approved, in-bounds capacity write described above, and reuses the
hardened HTTPS-only transport from the E1 shakedown (no redirects, bounded
streaming, explicit timeouts).
"""

from __future__ import annotations

# Standard library
import argparse
import hmac
import json
import os
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

# The single frozen dispatch route the harness POSTs to, in API Gateway
# ``{operationId}`` path-variable form. The handler, this harness, and the 07
# execution template's ``RouteKey`` must all agree on this exact path; the
# cross-contract test guards against drift before deployment.
DISPATCH_ROUTE_TEMPLATE = "/operations/{operationId}/dispatch"

# The exact, frozen out-of-band confirmation an operator must supply before the
# harness will make the write-capable admin-dispatch call. It is compared
# byte-for-byte (no trimming, no case folding); any other value is refused. It is
# never read from a response body — only from the CLI flag / environment.
REQUIRED_CONFIRMATION = "EXECUTE-LIVE-DISPATCH"

# The stable error code the admin-dispatch check reports when confirmation is
# missing or wrong. Chosen so the sanitized summary can distinguish "we refused
# to make a write-capable call" from a real deployed-boundary failure.
_CONFIRMATION_REQUIRED_CODE = "CONFIRMATION_REQUIRED"

# Markers that must never appear in the sanitized summary.
_FORBIDDEN_MARKERS = ("arn:aws:", "fleet-", "execute-api", "Bearer ", "eyJ")


def dispatch_path(operation_id: str) -> str:
    """Return the frozen dispatch path for a concrete operation id."""
    return DISPATCH_ROUTE_TEMPLATE.replace("{operationId}", operation_id)


def confirmation_is_valid(confirmation: str) -> bool:
    """Return whether an operator-supplied confirmation exactly unlocks a write.

    The comparison is constant-time and byte-exact: the value must equal
    :data:`REQUIRED_CONFIRMATION` with no surrounding whitespace and no case
    difference. Any missing / partial / case-shifted / padded value is rejected.
    """
    return hmac.compare_digest(confirmation, REQUIRED_CONFIRMATION)


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
    # Explicit, out-of-band operator confirmation that unlocks the write-capable
    # admin-dispatch check. Must equal :data:`REQUIRED_CONFIRMATION` exactly.
    # Never persisted and never sourced from a response body. Empty by default
    # so the harness fails closed: no confirmation, no authenticated dispatch.
    confirmation: str = ""

    def __post_init__(self) -> None:
        if not self.endpoint or not self.endpoint.lower().startswith("https://"):
            raise ValueError("endpoint must be a non-empty https:// URL")
        if not self.operation_id.startswith("op_"):
            raise ValueError("operation_id must be an op_ identifier")
        if not self.admin_bearer or not self.admin_bearer.strip():
            raise ValueError("admin_bearer must be a non-empty token")
        if not self.fleet_id or not self.fleet_id.strip():
            raise ValueError("fleet_id must be a non-empty identifier")

    @property
    def live_dispatch_confirmed(self) -> bool:
        """Whether the exact write-unlocking confirmation was supplied."""
        return confirmation_is_valid(self.confirmation)


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
        return f"{base}{dispatch_path(self._config.operation_id)}"

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
        """Assert an admin dispatch is accepted — **only after** confirmation.

        This is the harness's single write-capable check: a successful POST here
        starts the real E3 execution and may mutate the enrolled fleet's
        capacity. It therefore refuses to make **any** authenticated request
        unless the operator supplied the exact out-of-band confirmation. The
        refusal happens *before* the transport is touched, so a missing or wrong
        confirmation never reaches the deployed handler and can never start an
        execution. Confirmation is read from config (CLI/env) only — never from a
        response body.
        """
        if not self._config.live_dispatch_confirmed:
            return CheckResult(
                name="admin_dispatch_accepted",
                passed=False,
                observed_status=None,
                observed_error_code=_CONFIRMATION_REQUIRED_CODE,
                detail=(
                    "refused: write-capable admin dispatch requires the exact "
                    "out-of-band confirmation input; no authenticated request "
                    "was made"
                ),
            )
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
        """Run every check and return a fully sanitized summary document.

        The unauthenticated check is always non-mutating. The admin-dispatch
        check is write-capable and self-gates on confirmation, so ``run()`` makes
        no authenticated request unless confirmation was supplied. The non-admin
        check runs only when a non-admin token is configured.
        """
        checks = [
            self.check_unauthenticated_dispatch_denied(),
            self.check_admin_dispatch_accepted(),
        ]
        if self._config.non_admin_bearer.strip():
            checks.append(self.check_non_admin_dispatch_denied())
        return {
            "harness": "e3-dispatch",
            "endpoint_ref": _short_ref("ep", self._config.endpoint),
            "operation_ref": _short_ref("op", self._config.operation_id),
            "fleet_ref": _short_ref("flt", self._config.fleet_id),
            "live_dispatch_confirmed": self._config.live_dispatch_confirmed,
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
        # Confirmation is an out-of-band operator input only (flag/env). It is
        # never read from any response and is not persisted anywhere.
        confirmation=args.confirm_live_dispatch or os.environ.get("GBAW_E3_CONFIRM_LIVE_DISPATCH", ""),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Public-safe E3 dispatch shakedown")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--operation-id", dest="operation_id", default="")
    parser.add_argument("--admin-bearer", dest="admin_bearer", default="")
    parser.add_argument("--fleet-id", dest="fleet_id", default="")
    parser.add_argument("--non-admin-bearer", dest="non_admin_bearer", default="")
    parser.add_argument(
        "--confirm-live-dispatch",
        dest="confirm_live_dispatch",
        default="",
        help=(
            "Exact out-of-band confirmation that authorizes the write-capable "
            "admin dispatch. Must equal the frozen REQUIRED_CONFIRMATION value. "
            "The dispatched operation must already be human-approved; running "
            "with this set can start a real execution and mutate the enrolled "
            "fleet's capacity within its prepared bounds. Without it the admin "
            "dispatch check refuses and makes no authenticated request."
        ),
    )
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
