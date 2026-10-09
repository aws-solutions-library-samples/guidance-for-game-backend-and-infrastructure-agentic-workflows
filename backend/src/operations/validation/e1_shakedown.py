"""Repeatable, public-safe E1 *deployed* observation shakedown harness (#413).

This is a disposable command-line harness that exercises an **already-deployed**
E1 observation control plane end-to-end over HTTP against its real API Gateway
endpoint. It proves the deployed boundary behaves the way the frozen core
contract (ADR 0005 / ADR 0002 / ADR 0003) and the on-host handler
(``operations.observation_handler``) promise, without mutating any AWS resource.

What it asserts (each is one discriminating check):

1. **Unauthenticated POST is denied at the gateway.** A POST with no bearer
   token never reaches the handler — API Gateway's JWT authorizer rejects it
   (401/403). A body is still sent so a mis-wired route cannot pass by ignoring
   input.
2. **Malformed / identity-injection payload is denied.** An authenticated POST
   whose body carries extra keys (an injected ``requester``/``workspace_id`` or a
   malformed fleet id) is rejected with ``CONTRACT_INVALID`` — identity is never
   read from the body.
3. **A valid authenticated POST succeeds with a bounded, typed observation.**
   The response is 200, JSON, within the size ceiling, carries the frozen
   observation contract fields, and contains no raw AWS ARN / account id /
   provider payload.
4. **An identical retry replays byte-for-byte.** Re-sending the same token +
   target returns a byte-equivalent stored observation and the *same*
   ``operation_id`` — the idempotent replay path, no second provider read.
5. **The same token with a changed valid-looking target conflicts.** Re-using
   the idempotency token against a *different but still valid* fleet id returns
   ``IDEMPOTENCY_CONFLICT`` (409) and never reaches the provider.
6. **The status GET round-trips.** ``GET /operations/{operationId}`` for the
   operation created in check 3 returns a matching ``operation_id`` and a
   terminal ``succeeded`` state whose embedded observation matches the POST.
7. **An ID token cannot gain access.** Presenting a Cognito *id* token (when one
   is supplied) is denied — either at the gateway (audience/authorizer) or at
   the handler (``token_use != "access"``). Skipped, not passed, when no id
   token is supplied.
8. **An unknown operation returns a bounded NOT_FOUND.** A status GET for a
   syntactically valid but never-created operation id returns 404 /
   ``NOT_FOUND`` with a bounded body.
9. **Every response is bounded and leak-free.** Across all checks: content type
   is JSON, body size is under the ceiling, and no response body contains a raw
   AWS ARN, 12-digit account id, or GameLift provider payload.

Secret hygiene
--------------

The endpoint, the short-lived Cognito **access** token, the classic fleet id,
and the optional **id** token are read **only** from arguments or the
environment and are **never** logged, echoed, or written to the emitted summary.
Tokens live in memory for the duration of the run and are not persisted. The
emitted summary is sanitized: identifiers appear only as stable, non-reversible
short hashes, and response bodies are reduced to booleans/sizes/observed error
codes, never their raw contents.

Read-only postchecks
--------------------

If an AWS ``--profile``/``--region`` (or ``AWS_PROFILE``/``AWS_REGION``) is
supplied, the harness may additionally perform **read-only** CloudWatch
``ListMetrics`` calls to confirm the E1 metric names are present in the
``GameAgent/Operations`` namespace. This performs **no** AWS mutation and is
best-effort: a missing metric is reported, never fatal, and the absence of
credentials simply skips the postcheck.

Usage (requires a live deployed endpoint + short-lived access token)::

    GBAW_E1_ENDPOINT=https://<api-id>.execute-api.<region>.amazonaws.com \\
    GBAW_E1_ACCESS_TOKEN=<short-lived-cognito-access-token> \\
    GBAW_E1_FLEET_ID=<classic-fleet-id> \\
    GBAW_E1_ALT_FLEET_ID=<second-classic-fleet-id> \\
        python -m operations.validation.e1_shakedown \\
            --profile demo --region us-west-2 \\
            --out docs/evidence/e1-shakedown-<date>.json

The access/id tokens and fleet ids may also be passed as flags, but the
environment form is preferred so they never appear in shell history or a process
listing. This harness never deploys, enables, disables, or mutates any AWS or
GitHub resource.
"""

from __future__ import annotations

# Standard library
import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISSING_INPUT = 2
EXIT_SHAKEDOWN_FAILED = 4

# The frozen observation/error/status contract version the deployed handler
# stamps on every response (operations.contracts.versions.CONTRACT_VERSION).
CONTRACT_VERSION = "1.0"

# Bounded-response ceiling. The E1 observation is a small, fixed-shape JSON
# document; any response larger than this indicates an unbounded/leaky body.
MAX_RESPONSE_BYTES = 64 * 1024

# Explicit, separate connect/read timeouts for the real HTTP transport. Their
# sum stays *below* the 30s API Gateway integration ceiling
# (operations.settings) so the client gives up before the gateway would, and a
# slow-loris peer can never hold the connection open past the ceiling.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 20.0

# The number of bytes we are ever willing to pull off the wire for one response:
# one byte past the accepted ceiling, so an over-ceiling body is detected as
# oversized without ever being buffered unboundedly. A hostile server that
# streams forever is aborted here.
MAX_READ_BYTES = MAX_RESPONSE_BYTES + 1

# The E1 CloudWatch metric namespace and the frozen metric names, for the
# optional read-only postcheck.
METRIC_NAMESPACE = "GameAgent/Operations"
EXPECTED_METRICS = (
    "ObservationFailures",
    "ObservationTimeouts",
    "StuckOperations",
    "ObservationRequestLatency",
)

# Frozen request-shape patterns, mirrored from the core contract
# (operations.observation). Used only to build *valid-looking* inputs and to
# assert the harness never sends a malformed value by accident.
_FLEET_ID_PATTERN = re.compile(r"^fleet-[a-f0-9-]{1,120}$")
_IDEMPOTENCY_PATTERN = re.compile(r"^idem_[A-Za-z0-9_-]{20,128}$")

# Public-safe leak detectors applied to every response body. These match the
# spirit of scripts/check_public_content.py but run at request time against the
# live bytes so a leaky deployed response fails the shakedown.
_ARN_PATTERN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:")
_ACCOUNT_ID_PATTERN = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
# GameLift provider payload markers that must never cross the sanitized
# boundary (raw botocore/describe_* response envelopes).
_PROVIDER_PAYLOAD_MARKERS = (
    "ResponseMetadata",
    "FleetAttributes",
    "HTTPStatusCode",
    "RequestId",
)


def _short_ref(prefix: str, value: str) -> str:
    """Return a stable, non-reversible short reference for an identifier.

    A truncated SHA-256 digest cannot be reversed to the source value but lets a
    reader confirm two runs used the same target/endpoint without disclosing it.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _new_idempotency_token() -> str:
    """Return a fresh, valid idempotency token (matches the frozen pattern)."""
    token = "idem_" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    # 5 ("idem_") + 32 + 8 = 45 chars, inside the 20..128 window.
    assert _IDEMPOTENCY_PATTERN.fullmatch(token), "internal: bad idempotency token"
    return token


def _valid_unknown_operation_id() -> str:
    """Return a syntactically valid operation id that was never created."""
    return "obs_" + uuid.uuid4().hex[:26]


# --------------------------------------------------------------------------- #
# HTTP transport abstraction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A minimal, transport-neutral HTTP response the checks reason over."""

    status: int
    content_type: str
    body_bytes: bytes

    @property
    def size(self) -> int:
        return len(self.body_bytes)

    def json(self) -> Any:
        return json.loads(self.body_bytes.decode("utf-8"))

    def json_or_none(self) -> Optional[Any]:
        try:
            return self.json()
        except (ValueError, UnicodeDecodeError):
            return None


# A transport is a callable that performs one HTTP request and returns an
# HttpResponse. Injecting it lets the unit tests drive the runner against a
# local fake HTTP server, and lets the CLI use ``requests`` in production.
Transport = Callable[[str, str, Optional[Mapping[str, str]], Optional[bytes]], HttpResponse]


class TransportSecurityError(RuntimeError):
    """A request violated a hard transport-security invariant.

    Raised *instead of* returning an :class:`HttpResponse` when the peer does
    something the harness must never tolerate: a non-HTTPS URL, a redirect
    (any 3xx), or an over-ceiling / unbounded response body. Raising rather
    than returning guarantees the offending exchange (a redirect that would
    otherwise resubmit the bearer token, or a hostile streamed body) is never
    followed, resubmitted, or buffered.
    """


def _read_bounded(response: Any) -> bytes:
    """Stream at most ``MAX_READ_BYTES`` off the wire, then close the response.

    We read ``iter_content`` up to one byte past the accepted ceiling and stop.
    If the peer had *more* to send, the body is over the ceiling: we raise
    (never buffering the rest) so an unbounded/hostile body cannot exhaust
    memory. The response is always released.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_READ_BYTES:
                # We have pulled one byte past the ceiling (or exactly the
                # cap). Stop immediately without reading the remainder.
                raise TransportSecurityError("response body exceeded the byte ceiling")
    finally:
        response.close()
    return b"".join(chunks)


def _requests_transport() -> Transport:
    """Build a hardened ``requests``-backed transport (deferred import).

    Security invariants enforced on every request:

    * **HTTPS only.** A non-``https://`` URL is refused before any socket opens.
    * **Certificate verification on** (``verify=True``).
    * **No redirects.** ``allow_redirects=False`` and any 3xx status is a hard
      failure, so a ``Location`` redirect can never resubmit the ``Authorization``
      header to another origin.
    * **Streamed + bounded.** ``stream=True`` plus an up-front ``Content-Length``
      rejection and a byte-capped read mean a hostile or oversized body is never
      buffered unboundedly.
    * **Explicit connect/read timeouts** whose sum is below the API ceiling.
    """
    # Third-party packages
    import requests

    session = requests.Session()

    def _do(
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]],
        body: Optional[bytes],
    ) -> HttpResponse:
        if not url.lower().startswith("https://"):
            raise TransportSecurityError("refusing to send a request over a non-HTTPS URL")

        response = session.request(
            method,
            url,
            headers=dict(headers or {}),
            data=body,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            verify=True,
            allow_redirects=False,
            stream=True,
        )
        try:
            # Any 3xx is refused: never follow a redirect (it would resubmit the
            # bearer token to the redirect target).
            if 300 <= response.status_code < 400:
                raise TransportSecurityError("refusing to follow an HTTP redirect (3xx)")

            # Reject an over-ceiling body up front when the peer declares one,
            # before reading any of it.
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    declared_len = int(declared)
                except ValueError:
                    raise TransportSecurityError("response declared a malformed Content-Length")
                if declared_len > MAX_RESPONSE_BYTES:
                    raise TransportSecurityError("response Content-Length exceeds the size ceiling")

            body_bytes = _read_bounded(response)
            return HttpResponse(
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                body_bytes=body_bytes,
            )
        finally:
            # Idempotent; ensures the connection is released even on the raise
            # paths above (before _read_bounded took ownership).
            response.close()

    return _do


# --------------------------------------------------------------------------- #
# Config (secrets never logged)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ShakedownConfig:
    """Runtime inputs. Tokens are held only in memory and never serialized."""

    endpoint: str
    access_token: str
    fleet_id: str
    alt_fleet_id: str
    id_token: Optional[str] = None
    profile: Optional[str] = None
    region: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.endpoint or not self.endpoint.lower().startswith("https://"):
            raise ValueError("endpoint must be a non-empty https:// URL")
        if not self.access_token:
            raise ValueError("access token is required")
        if not _FLEET_ID_PATTERN.fullmatch(self.fleet_id):
            raise ValueError("fleet id is not a valid classic fleet id")
        if not _FLEET_ID_PATTERN.fullmatch(self.alt_fleet_id):
            raise ValueError("alternate fleet id is not a valid classic fleet id")
        if self.alt_fleet_id == self.fleet_id:
            raise ValueError("alternate fleet id must differ from the primary fleet id")


# --------------------------------------------------------------------------- #
# Check results (sanitized)
# --------------------------------------------------------------------------- #


@dataclass
class CheckResult:
    """Sanitized outcome of one check. Carries no token, ARN, or raw body."""

    name: str
    passed: bool
    detail: str
    observed_status: Optional[int] = None
    observed_error_code: Optional[str] = None
    response_size: Optional[int] = None
    skipped: bool = False

    def as_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "skipped": self.skipped,
            "detail": self.detail,
            "observed_status": self.observed_status,
            "observed_error_code": self.observed_error_code,
            "response_size": self.response_size,
        }


@dataclass
class ShakedownReport:
    """The full sanitized evidence document. Contains no secret material."""

    endpoint_ref: str
    fleet_ref: str
    alt_fleet_ref: str
    started_at: str
    checks: list[CheckResult] = field(default_factory=list)
    postchecks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return all(c.passed or c.skipped for c in self.checks) and any(c.passed and not c.skipped for c in self.checks)

    def as_document(self) -> dict[str, Any]:
        return {
            "kind": "e1-deployed-shakedown",
            "contract_version": CONTRACT_VERSION,
            "endpoint_ref": self.endpoint_ref,
            "fleet_ref": self.fleet_ref,
            "alt_fleet_ref": self.alt_fleet_ref,
            "started_at": self.started_at,
            "accepted": self.accepted,
            "checks": [c.as_summary() for c in self.checks],
            "postchecks": self.postchecks,
        }


# --------------------------------------------------------------------------- #
# Leak / bound assertions
# --------------------------------------------------------------------------- #


def response_is_bounded_and_clean(response: HttpResponse) -> tuple[bool, str]:
    """Return (ok, reason) for the universal bounded/leak-free contract."""
    if "application/json" not in response.content_type.lower():
        return False, "content type is not application/json"
    if response.size > MAX_RESPONSE_BYTES:
        return False, "response exceeds the size ceiling"
    text = response.body_bytes.decode("utf-8", errors="replace")
    if _ARN_PATTERN.search(text):
        return False, "response contains a raw AWS ARN"
    if _ACCOUNT_ID_PATTERN.search(text):
        return False, "response contains a 12-digit account id"
    for marker in _PROVIDER_PAYLOAD_MARKERS:
        if marker in text:
            return False, "response contains a raw provider payload marker"
    return True, "bounded and leak-free"


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #


class ShakedownRunner:
    """Drives the nine deployed-boundary checks over an injected transport."""

    def __init__(self, config: ShakedownConfig, transport: Transport) -> None:
        self._config = config
        self._transport = transport
        self._observe_url = self._join(config.endpoint, "/operations/observe")

    @staticmethod
    def _join(endpoint: str, path: str) -> str:
        return endpoint.rstrip("/") + path

    def _status_url(self, operation_id: str) -> str:
        return self._join(self._config.endpoint, f"/operations/{operation_id}")

    def _post_observe(
        self,
        *,
        body: bytes,
        bearer: Optional[str],
    ) -> HttpResponse:
        headers = {"content-type": "application/json"}
        if bearer is not None:
            headers["authorization"] = f"Bearer {bearer}"
        return self._transport("POST", self._observe_url, headers, body)

    def _get_status(self, operation_id: str, *, bearer: Optional[str]) -> HttpResponse:
        headers: dict[str, str] = {}
        if bearer is not None:
            headers["authorization"] = f"Bearer {bearer}"
        return self._transport("GET", self._status_url(operation_id), headers, None)

    @staticmethod
    def _observe_body(fleet_id: str, token: str) -> bytes:
        return json.dumps(
            {"fleet_id": fleet_id, "idempotency_token": token},
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _error_code(response: HttpResponse) -> Optional[str]:
        payload = response.json_or_none()
        if isinstance(payload, dict):
            code = payload.get("error_code")
            if isinstance(code, str):
                return code
        return None

    # -- individual checks -------------------------------------------------- #

    def check_unauthenticated_denied(self) -> CheckResult:
        body = self._observe_body(self._config.fleet_id, _new_idempotency_token())
        response = self._post_observe(body=body, bearer=None)
        ok, reason = response_is_bounded_and_clean(response)
        denied = response.status in (401, 403)
        return CheckResult(
            name="unauthenticated_post_denied_at_gateway",
            passed=denied and ok,
            detail="gateway denied unauthenticated POST" if denied else f"expected 401/403; {reason}",
            observed_status=response.status,
            response_size=response.size,
        )

    def check_identity_injection_denied(self) -> CheckResult:
        # A valid-looking observe body poisoned with injected identity + an
        # unexpected key. The handler must reject on strict shape, never trust
        # the injected identity.
        poisoned = json.dumps(
            {
                "fleet_id": self._config.fleet_id,
                "idempotency_token": _new_idempotency_token(),
                "requester": {"workspace_id": "injected", "sub": "attacker"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._post_observe(body=poisoned, bearer=self._config.access_token)
        ok, reason = response_is_bounded_and_clean(response)
        code = self._error_code(response)
        passed = response.status == 400 and code == "CONTRACT_INVALID" and ok
        return CheckResult(
            name="identity_injection_payload_denied",
            passed=passed,
            detail="handler rejected identity-injection body" if passed else f"expected 400 CONTRACT_INVALID; {reason}",
            observed_status=response.status,
            observed_error_code=code,
            response_size=response.size,
        )

    def check_valid_post_succeeds(self, token: str) -> tuple[CheckResult, Optional[str], Optional[bytes]]:
        body = self._observe_body(self._config.fleet_id, token)
        response = self._post_observe(body=body, bearer=self._config.access_token)
        ok, reason = response_is_bounded_and_clean(response)
        payload = response.json_or_none()
        operation_id: Optional[str] = None
        typed = False
        if response.status == 200 and isinstance(payload, dict):
            operation_id = payload.get("observation_id")
            typed = (
                payload.get("observation_contract_version") == CONTRACT_VERSION
                and payload.get("phase") == "observe"
                and payload.get("provider") == "gamelift"
                and isinstance(payload.get("results"), dict)
                and isinstance(operation_id, str)
            )
        passed = response.status == 200 and typed and ok
        return (
            CheckResult(
                name="authenticated_valid_post_succeeds_bounded_typed",
                passed=passed,
                detail=(
                    "valid POST returned bounded typed observation"
                    if passed
                    else f"expected 200 typed observation; {reason}"
                ),
                observed_status=response.status,
                response_size=response.size,
            ),
            operation_id if isinstance(operation_id, str) else None,
            response.body_bytes if passed else None,
        )

    def check_identical_retry_replays(self, token: str, first_operation_id: str, first_body: bytes) -> CheckResult:
        body = self._observe_body(self._config.fleet_id, token)
        response = self._post_observe(body=body, bearer=self._config.access_token)
        ok, _ = response_is_bounded_and_clean(response)
        payload = response.json_or_none()
        same_op = isinstance(payload, dict) and payload.get("observation_id") == first_operation_id
        byte_equal = response.body_bytes == first_body
        passed = response.status == 200 and same_op and byte_equal and ok
        return CheckResult(
            name="identical_retry_replays_byte_equivalent_same_operation_id",
            passed=passed,
            detail=(
                "retry replayed byte-equivalent result with same operation id"
                if passed
                else "retry did not replay byte-equivalent result / same operation id"
            ),
            observed_status=response.status,
            response_size=response.size,
        )

    def check_changed_target_conflicts(self, token: str) -> CheckResult:
        # Same idempotency token, different but still-valid fleet id.
        body = self._observe_body(self._config.alt_fleet_id, token)
        response = self._post_observe(body=body, bearer=self._config.access_token)
        ok, reason = response_is_bounded_and_clean(response)
        code = self._error_code(response)
        passed = response.status == 409 and code == "IDEMPOTENCY_CONFLICT" and ok
        return CheckResult(
            name="changed_target_same_token_idempotency_conflict",
            passed=passed,
            detail=(
                "changed target under same token conflicted before provider access"
                if passed
                else f"expected 409 IDEMPOTENCY_CONFLICT; {reason}"
            ),
            observed_status=response.status,
            observed_error_code=code,
            response_size=response.size,
        )

    def check_status_roundtrip(self, operation_id: str, first_body: bytes) -> CheckResult:
        response = self._get_status(operation_id, bearer=self._config.access_token)
        ok, reason = response_is_bounded_and_clean(response)
        payload = response.json_or_none()
        matched = False
        if response.status == 200 and isinstance(payload, dict):
            same_op = payload.get("operation_id") == operation_id
            succeeded = payload.get("state") == "succeeded"
            observation = payload.get("observation")
            first = json.loads(first_body.decode("utf-8"))
            obs_matches = isinstance(observation, dict) and observation.get("observation_id") == first.get(
                "observation_id"
            )
            matched = bool(same_op and succeeded and obs_matches)
        passed = response.status == 200 and matched and ok
        return CheckResult(
            name="status_get_returns_matching_status_result",
            passed=passed,
            detail="status GET matched the created operation" if passed else f"status GET did not match; {reason}",
            observed_status=response.status,
            response_size=response.size,
        )

    def check_id_token_denied(self) -> CheckResult:
        if not self._config.id_token:
            return CheckResult(
                name="id_token_cannot_gain_access",
                passed=False,
                skipped=True,
                detail="no id token supplied; check skipped (supply GBAW_E1_ID_TOKEN to run)",
            )
        body = self._observe_body(self._config.fleet_id, _new_idempotency_token())
        response = self._post_observe(body=body, bearer=self._config.id_token)
        ok, reason = response_is_bounded_and_clean(response)
        code = self._error_code(response)
        # Gateway denial (401/403 with no typed code) OR handler denial
        # (401 IDENTITY_CONTEXT_INVALID because token_use != "access").
        gateway_denied = response.status in (401, 403) and code is None
        handler_denied = response.status == 401 and code == "IDENTITY_CONTEXT_INVALID"
        passed = (gateway_denied or handler_denied) and ok
        return CheckResult(
            name="id_token_cannot_gain_access",
            passed=passed,
            detail="id token denied (gateway or handler)" if passed else f"id token was not denied; {reason}",
            observed_status=response.status,
            observed_error_code=code,
            response_size=response.size,
        )

    def check_unknown_operation_not_found(self) -> CheckResult:
        response = self._get_status(_valid_unknown_operation_id(), bearer=self._config.access_token)
        ok, reason = response_is_bounded_and_clean(response)
        code = self._error_code(response)
        passed = response.status == 404 and code == "NOT_FOUND" and ok
        return CheckResult(
            name="unknown_operation_returns_bounded_not_found",
            passed=passed,
            detail="unknown operation returned bounded NOT_FOUND" if passed else f"expected 404 NOT_FOUND; {reason}",
            observed_status=response.status,
            observed_error_code=code,
            response_size=response.size,
        )

    def run(self) -> ShakedownReport:
        report = ShakedownReport(
            endpoint_ref=_short_ref("endpoint", self._config.endpoint),
            fleet_ref=_short_ref("fleet", self._config.fleet_id),
            alt_fleet_ref=_short_ref("fleet", self._config.alt_fleet_id),
            started_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        )

        report.checks.append(self.check_unauthenticated_denied())
        report.checks.append(self.check_identity_injection_denied())

        token = _new_idempotency_token()
        success, operation_id, first_body = self.check_valid_post_succeeds(token)
        report.checks.append(success)

        if success.passed and operation_id and first_body is not None:
            report.checks.append(self.check_identical_retry_replays(token, operation_id, first_body))
            report.checks.append(self.check_status_roundtrip(operation_id, first_body))
        else:
            for dependent in (
                "identical_retry_replays_byte_equivalent_same_operation_id",
                "status_get_returns_matching_status_result",
            ):
                report.checks.append(
                    CheckResult(
                        name=dependent,
                        passed=False,
                        detail="skipped: valid POST did not succeed, cannot verify dependent behavior",
                    )
                )

        report.checks.append(self.check_changed_target_conflicts(token))
        report.checks.append(self.check_id_token_denied())
        report.checks.append(self.check_unknown_operation_not_found())
        return report


# --------------------------------------------------------------------------- #
# Optional read-only CloudWatch postcheck
# --------------------------------------------------------------------------- #


def run_metric_postcheck(
    *,
    profile: Optional[str],
    region: Optional[str],
    client_factory: Optional[Callable[..., Any]] = None,
) -> list[dict[str, Any]]:
    """Best-effort, READ-ONLY CloudWatch ListMetrics presence check.

    Returns a sanitized per-metric presence list. Performs no AWS mutation.
    Missing credentials or a missing metric is reported, never fatal.
    """
    if not region:
        return [{"postcheck": "cloudwatch_metric_presence", "skipped": True, "reason": "no region supplied"}]

    def _default_factory(**kwargs: Any) -> Any:
        # Third-party packages
        import boto3

        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        return session.client("cloudwatch", region_name=region)

    factory = client_factory or _default_factory
    try:
        client = factory(profile=profile, region=region)
    except Exception:  # noqa: BLE001 - credentials/config problems are non-fatal
        return [
            {
                "postcheck": "cloudwatch_metric_presence",
                "skipped": True,
                "reason": "could not create a read-only CloudWatch client",
            }
        ]

    results: list[dict[str, Any]] = []
    for metric in EXPECTED_METRICS:
        try:
            response = client.list_metrics(Namespace=METRIC_NAMESPACE, MetricName=metric)
            present = bool(response.get("Metrics"))
        except Exception:  # noqa: BLE001 - a failed read is reported, never fatal
            results.append({"metric": metric, "present": None, "error": "list_metrics failed"})
            continue
        results.append({"metric": metric, "present": present})
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _resolve(cli_value: Optional[str], env_name: str) -> Optional[str]:
    if cli_value:
        return cli_value
    env_value = os.environ.get(env_name)
    return env_value or None


def build_config_from_env(args: argparse.Namespace) -> ShakedownConfig:
    """Resolve config from args then environment. Never logs any secret."""
    endpoint = _resolve(args.endpoint, "GBAW_E1_ENDPOINT")
    access_token = _resolve(args.access_token, "GBAW_E1_ACCESS_TOKEN")
    fleet_id = _resolve(args.fleet_id, "GBAW_E1_FLEET_ID")
    alt_fleet_id = _resolve(args.alt_fleet_id, "GBAW_E1_ALT_FLEET_ID")
    id_token = _resolve(args.id_token, "GBAW_E1_ID_TOKEN")
    profile = _resolve(args.profile, "AWS_PROFILE")
    region = _resolve(args.region, "AWS_REGION")

    missing = [
        name
        for name, value in (
            ("endpoint", endpoint),
            ("access token", access_token),
            ("fleet id", fleet_id),
            ("alternate fleet id", alt_fleet_id),
        )
        if not value
    ]
    if missing:
        raise ValueError("missing required input(s): " + ", ".join(missing))

    return ShakedownConfig(
        endpoint=endpoint,  # type: ignore[arg-type]
        access_token=access_token,  # type: ignore[arg-type]
        fleet_id=fleet_id,  # type: ignore[arg-type]
        alt_fleet_id=alt_fleet_id,  # type: ignore[arg-type]
        id_token=id_token,
        profile=profile,
        region=region,
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="E1 deployed observation shakedown (read-only; public-safe summary only)"
    )
    parser.add_argument("--endpoint", default=None, help="Deployed API base URL (or GBAW_E1_ENDPOINT)")
    parser.add_argument(
        "--access-token",
        default=None,
        help="Short-lived Cognito ACCESS token (prefer GBAW_E1_ACCESS_TOKEN; never logged)",
    )
    parser.add_argument("--fleet-id", default=None, help="Classic GameLift fleet id (or GBAW_E1_FLEET_ID)")
    parser.add_argument(
        "--alt-fleet-id",
        default=None,
        help="A second, different classic fleet id for the conflict check (or GBAW_E1_ALT_FLEET_ID)",
    )
    parser.add_argument(
        "--id-token",
        default=None,
        help="Optional Cognito ID token to prove it cannot gain access (prefer GBAW_E1_ID_TOKEN; never logged)",
    )
    parser.add_argument("--profile", default=None, help="AWS profile for the optional read-only postcheck")
    parser.add_argument("--region", default=None, help="AWS region for the optional read-only postcheck")
    parser.add_argument("--out", default=None, help="Write the sanitized summary JSON here (default: stdout)")
    parser.add_argument(
        "--skip-postcheck",
        action="store_true",
        help="Skip the optional read-only CloudWatch metric-presence postcheck",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = build_config_from_env(args)
    except ValueError as exc:
        # The message names only which inputs are missing/invalid — never a value.
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return EXIT_MISSING_INPUT

    runner = ShakedownRunner(config, _requests_transport())
    try:
        report = runner.run()
    except Exception:  # noqa: BLE001 - never leak a stack trace with a token in a frame
        print(json.dumps({"error": "shakedown run failed before completion"}), file=sys.stderr)
        return EXIT_ERROR

    if not args.skip_postcheck:
        report.postchecks = run_metric_postcheck(profile=config.profile, region=config.region)

    document = report.as_document()
    rendered = json.dumps(document, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)

    return EXIT_OK if report.accepted else EXIT_SHAKEDOWN_FAILED


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
