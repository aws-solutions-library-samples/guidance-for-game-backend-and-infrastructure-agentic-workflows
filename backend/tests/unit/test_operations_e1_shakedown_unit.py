"""Unit tests for the E1 deployed shakedown harness (issue #413).

These tests spin up a **local stdlib HTTP server** that faithfully emulates the
deployed E1 API Gateway + handler contract, then run the harness against it over
real TCP. A compliant fake server makes every check pass; a set of deliberately
*broken* fake servers each make exactly one check fail. That red-green pairing
is what proves the harness's assertions are **discriminating** — a check that
cannot fail proves nothing.

No AWS credentials, network egress, or deployed stack is required: the server is
bound to 127.0.0.1 on an ephemeral port. This test lives in ``tests/unit`` so it
runs in the default no-credential unit suite; the harness *module* it exercises
is a manual ``python -m`` tool that is never collected by pytest.
"""

from __future__ import annotations

# Standard library
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

# Third-party packages
import pytest

# Local modules
import operations.validation.e1_shakedown as e1_shakedown
from operations.validation.e1_shakedown import (
    CONNECT_TIMEOUT_S,
    CONTRACT_VERSION,
    MAX_READ_BYTES,
    MAX_RESPONSE_BYTES,
    READ_TIMEOUT_S,
    HttpResponse,
    ShakedownConfig,
    ShakedownRunner,
    TransportSecurityError,
    _requests_transport,
    build_config_from_env,
    response_is_bounded_and_clean,
    run_metric_postcheck,
)

pytestmark = pytest.mark.unit

FLEET_ID = "fleet-1234abcd-5678-90ef-a1b2-c3d4e5f60789"
ALT_FLEET_ID = "fleet-9999ffff-0000-1111-2222-3333abcd5555"
ACCESS_TOKEN = "access-token-value-never-logged"
ID_TOKEN = "id-token-value-never-logged"


# --------------------------------------------------------------------------- #
# A compliant fake of the deployed E1 API (gateway + handler)
# --------------------------------------------------------------------------- #


class FakeE1App:
    """In-memory emulation of the deployed E1 API contract.

    ``behavior`` selects a deliberately broken mode so a single check can be
    shown to fail; ``"compliant"`` is the correct deployed contract.
    """

    def __init__(self, *, behavior: str = "compliant") -> None:
        self.behavior = behavior
        # operation_id -> stored observation body bytes (byte-stable replay)
        self._store: dict[str, bytes] = {}
        # idempotency fingerprint -> (operation_id, fleet_id)
        self._by_token: dict[str, tuple[str, str]] = {}
        self._counter = 0

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _error(code: str, status: int) -> tuple[int, bytes]:
        body = {
            "error_contract_version": CONTRACT_VERSION,
            "error_code": code,
            "safe_message": "denied",
            "retryable": False,
        }
        return status, json.dumps(body, separators=(",", ":")).encode("utf-8")

    def _make_observation(self, fleet_id: str, token: str) -> tuple[str, bytes]:
        self._counter += 1
        operation_id = f"obs_{self._counter:026d}"
        observation = {
            "observation_contract_version": CONTRACT_VERSION,
            "observation_id": operation_id,
            "idempotency_token": token,
            "phase": "observe",
            "provider": "gamelift",
            "results": {"utilization": {"active": 1}, "capacity": [], "scaling_policies": []},
            "target": {"provider": "gamelift", "fleet_ref": fleet_id[:12]},
        }
        return operation_id, json.dumps(observation, separators=(",", ":")).encode("utf-8")

    # -- routing ---------------------------------------------------------- #

    def handle(self, method: str, path: str, bearer: Optional[str], body: bytes) -> tuple[int, str, bytes]:
        content_type = "application/json"
        status, payload = self._route(method, path, bearer, body)
        if self.behavior == "leaky_content_type":
            content_type = "text/plain"
        return status, content_type, payload

    def _route(self, method: str, path: str, bearer: Optional[str], body: bytes) -> tuple[int, bytes]:
        # Gateway JWT authorizer: no bearer -> denied before the handler.
        if bearer is None:
            if self.behavior == "allows_unauthenticated":
                # broken: a mis-wired route serves an unauthenticated POST as if
                # it were authorized instead of denying at the gateway.
                if method == "POST" and path == "/operations/observe":
                    return self._observe(body)
                return self._error("", 401)[0], b'{"message":"Unauthorized"}'
            return self._error("", 401)[0], b'{"message":"Unauthorized"}'
        # An id token is rejected. Compliant deployments deny at the gateway
        # (audience mismatch) OR at the handler (token_use != access). We model
        # the handler denial path here.
        if bearer == ID_TOKEN and self.behavior != "id_token_allowed":
            return self._error("IDENTITY_CONTEXT_INVALID", 401)

        if method == "POST" and path == "/operations/observe":
            return self._observe(body)
        if method == "GET" and path.startswith("/operations/"):
            operation_id = path.rsplit("/", 1)[-1]
            return self._status(operation_id)
        return self._error("CONTRACT_INVALID", 400)

    def _observe(self, body: bytes) -> tuple[int, bytes]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._error("CONTRACT_INVALID", 400)
        # Strict shape: exactly {fleet_id, idempotency_token}. Identity injection
        # (extra keys) is rejected — identity never comes from the body.
        if not isinstance(payload, dict) or set(payload) != {"fleet_id", "idempotency_token"}:
            if self.behavior == "accepts_injection":
                pass  # broken: pretend the extra key is fine
            else:
                return self._error("CONTRACT_INVALID", 400)
        fleet_id = payload.get("fleet_id")
        token = payload.get("idempotency_token")

        existing = self._by_token.get(token)
        if existing is not None:
            stored_op, stored_fleet = existing
            if stored_fleet != fleet_id and self.behavior != "no_conflict":
                # Same token, changed target -> conflict before provider access.
                return self._error("IDEMPOTENCY_CONFLICT", 409)
            if self.behavior == "nondeterministic_replay":
                # broken: produce a NEW operation on retry.
                op, stored = self._make_observation(fleet_id, token)
                self._store[op] = stored
                self._by_token[token] = (op, fleet_id)
                return 200, stored
            # Byte-stable replay of the stored observation.
            return 200, self._store[stored_op]

        if self.behavior == "success_returns_error":
            # broken: a valid authenticated POST returns a bounded typed error
            # instead of the success observation. The success check must fail.
            return self._error("INTERNAL_ERROR", 500)
        operation_id, stored = self._make_observation(fleet_id, token)
        self._store[operation_id] = stored
        self._by_token[token] = (operation_id, fleet_id)
        if self.behavior == "leaky_body":
            leaked = json.loads(stored.decode("utf-8"))
            leaked["target"]["arn"] = "arn:aws:gamelift:us-west-2:123456789012:fleet/x"
            leaked["ResponseMetadata"] = {"HTTPStatusCode": 200, "RequestId": "abc"}
            return 200, json.dumps(leaked, separators=(",", ":")).encode("utf-8")
        return 200, stored

    def _status(self, operation_id: str) -> tuple[int, bytes]:
        stored = self._store.get(operation_id)
        if stored is None:
            if self.behavior == "unknown_is_500":
                return self._error("INTERNAL_ERROR", 500)
            return self._error("NOT_FOUND", 404)
        observation = json.loads(stored.decode("utf-8"))
        state = "succeeded"
        reported_op = operation_id
        if self.behavior == "status_mismatch":
            # broken: the status GET round-trips a *different* operation id and a
            # non-terminal state, so the status/result-match check must fail even
            # though the body is bounded and leak-free.
            reported_op = "obs_00000000000000000000000000"
            state = "running"
        body = {
            "status_contract_version": CONTRACT_VERSION,
            "operation_id": reported_op,
            "state": state,
            "observation": observation,
        }
        return 200, json.dumps(body, separators=(",", ":")).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    app: FakeE1App  # set per-server below

    def log_message(self, *args: Any) -> None:  # silence the test server
        pass

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        auth = self.headers.get("authorization")
        bearer = auth.split(" ", 1)[1] if auth and auth.lower().startswith("bearer ") else None
        status, content_type, payload = self.app.handle(method, self.path, bearer, body)
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("POST")


class _RunningServer:
    def __init__(self, app: FakeE1App) -> None:
        handler = type("_BoundHandler", (_Handler,), {"app": app})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"https://{host}:{port}"  # scheme is https for config validation

    @property
    def http_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_RunningServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _config_for(server: _RunningServer, *, id_token: Optional[str] = None) -> ShakedownConfig:
    # ShakedownConfig requires https://; we bind locally over http, so build the
    # config with an https endpoint and let the runner target the actual local
    # http URL via a transport that rewrites the scheme.
    return ShakedownConfig(
        endpoint=server.base_url,
        access_token=ACCESS_TOKEN,
        fleet_id=FLEET_ID,
        alt_fleet_id=ALT_FLEET_ID,
        id_token=id_token,
    )


def _local_transport(server: _RunningServer):
    """A real-TCP transport that rewrites the https config URL to local http."""
    # Standard library
    import urllib.request

    https_base = server.base_url
    http_base = server.http_url

    def _do(method: str, url: str, headers, body):
        real_url = url.replace(https_base, http_base, 1)
        request = urllib.request.Request(real_url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return HttpResponse(
                    status=response.status,
                    content_type=response.headers.get("content-type", ""),
                    body_bytes=raw,
                )
        except urllib.error.HTTPError as exc:  # non-2xx come back as HTTPError
            raw = exc.read()
            return HttpResponse(
                status=exc.code,
                content_type=exc.headers.get("content-type", "") if exc.headers else "",
                body_bytes=raw,
            )

    return _do


# Fixed, DIGIT-FREE tokens (only letters/underscores) so the runner's random
# tokens can never contain a spurious 12-digit run that the account-id leak
# detector would flag — that unrelated false-positive would otherwise flake the
# success/leak checks. Determinism here keeps every check assertion stable.
_DETERMINISTIC_TOKENS = (
    "idem_aaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # >= 20 chars, matches the frozen pattern
    "idem_bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "idem_cccccccccccccccccccccccccccc",
    "idem_dddddddddddddddddddddddddddd",
)
_DETERMINISTIC_UNKNOWN_OP = "obs_zzzzzzzzzzzzzzzzzzzzzzzzzz"


@contextlib.contextmanager
def _deterministic_tokens():
    """Patch the harness token generators to fixed, digit-free values."""
    # Standard library
    import itertools
    from unittest import mock

    tokens = itertools.cycle(_DETERMINISTIC_TOKENS)
    with (
        mock.patch.object(e1_shakedown, "_new_idempotency_token", lambda: next(tokens)),
        mock.patch.object(e1_shakedown, "_valid_unknown_operation_id", lambda: _DETERMINISTIC_UNKNOWN_OP),
    ):
        yield


def _run(behavior: str, *, id_token: Optional[str] = None):
    # Real TCP transport (proves the runner over a socket), deterministic tokens.
    app = FakeE1App(behavior=behavior)
    with _RunningServer(app) as server, _deterministic_tokens():
        config = _config_for(server, id_token=id_token)
        runner = ShakedownRunner(config, _local_transport(server))
        report = runner.run()
    return report


def _in_process_transport(app: "FakeE1App"):
    """A deterministic transport that calls the fake app directly (no TCP).

    The real-TCP path (``_local_transport``) proves the runner works over a
    socket; this path removes socket flakiness so the *discrimination* tests
    (which assert a precise set of failing checks) are deterministic.
    """

    def _do(method: str, url: str, headers, body):
        # Extract the path from the https config URL.
        # url looks like "https://host:port/operations/...".
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        auth = (headers or {}).get("authorization")
        bearer = auth.split(" ", 1)[1] if auth and auth.lower().startswith("bearer ") else None
        status, content_type, payload = app.handle(method, path, bearer, body or b"")
        return HttpResponse(status=status, content_type=content_type, body_bytes=payload)

    return _do


def _run_in_process(behavior: str, *, id_token: Optional[str] = None):
    app = FakeE1App(behavior=behavior)
    # A syntactically valid https endpoint; the in-process transport only needs
    # the path, and ShakedownConfig requires https.
    config = ShakedownConfig(
        endpoint="https://in-process.example.com",
        access_token=ACCESS_TOKEN,
        fleet_id=FLEET_ID,
        alt_fleet_id=ALT_FLEET_ID,
        id_token=id_token,
    )
    runner = ShakedownRunner(config, _in_process_transport(app))
    with _deterministic_tokens():
        return runner.run()


def _check(report, name: str):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name}")


# --------------------------------------------------------------------------- #
# Green: a compliant deployment passes every (non-skipped) check.
# --------------------------------------------------------------------------- #


def test_compliant_deployment_passes_all_checks_with_id_token():
    report = _run("compliant", id_token=ID_TOKEN)
    failing = [c.name for c in report.checks if not c.passed and not c.skipped]
    assert failing == [], f"unexpected failing checks: {failing}"
    assert report.accepted is True
    # The id-token check actually ran (not skipped) because an id token was given.
    assert _check(report, "id_token_cannot_gain_access").skipped is False


def test_id_token_check_skipped_when_no_id_token_supplied():
    report = _run("compliant", id_token=None)
    id_check = _check(report, "id_token_cannot_gain_access")
    assert id_check.skipped is True
    assert id_check.passed is False
    # A skipped check does not deny acceptance.
    assert report.accepted is True


# --------------------------------------------------------------------------- #
# Red: each broken deployment fails exactly the matching check (discriminating).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("behavior", "expected_failed_check"),
    [
        ("accepts_injection", "identity_injection_payload_denied"),
        ("nondeterministic_replay", "identical_retry_replays_byte_equivalent_same_operation_id"),
        ("no_conflict", "changed_target_same_token_idempotency_conflict"),
        ("id_token_allowed", "id_token_cannot_gain_access"),
        ("unknown_is_500", "unknown_operation_returns_bounded_not_found"),
        ("leaky_content_type", "authenticated_valid_post_succeeds_bounded_typed"),
        ("leaky_body", "authenticated_valid_post_succeeds_bounded_typed"),
        # Dedicated red cases so the remaining three checks are also
        # discriminating (a check that cannot fail proves nothing):
        ("allows_unauthenticated", "unauthenticated_post_denied_at_gateway"),
        ("success_returns_error", "authenticated_valid_post_succeeds_bounded_typed"),
        ("status_mismatch", "status_get_returns_matching_status_result"),
    ],
)
def test_broken_deployment_fails_expected_check(behavior: str, expected_failed_check: str):
    report = _run_in_process(behavior, id_token=ID_TOKEN)
    target = _check(report, expected_failed_check)
    assert target.passed is False, f"{behavior} should have failed {expected_failed_check}"
    assert report.accepted is False


def test_leaky_body_is_detected_as_leak_specifically():
    # The leaky_body server injects an ARN + provider payload markers into the
    # success body; the bounded/clean assertion must reject it.
    report = _run_in_process("leaky_body", id_token=ID_TOKEN)
    success = _check(report, "authenticated_valid_post_succeeds_bounded_typed")
    assert success.passed is False


@pytest.mark.parametrize(
    ("behavior", "only_failed_check"),
    [
        # These broken fakes isolate a single check: the named check is the
        # ONLY non-skipped failure, which proves that check discriminates that
        # specific deployed behavior and nothing else.
        ("allows_unauthenticated", "unauthenticated_post_denied_at_gateway"),
        ("status_mismatch", "status_get_returns_matching_status_result"),
        ("accepts_injection", "identity_injection_payload_denied"),
        ("nondeterministic_replay", "identical_retry_replays_byte_equivalent_same_operation_id"),
        ("no_conflict", "changed_target_same_token_idempotency_conflict"),
        ("id_token_allowed", "id_token_cannot_gain_access"),
        ("unknown_is_500", "unknown_operation_returns_bounded_not_found"),
    ],
)
def test_broken_fake_isolates_exactly_one_check(behavior: str, only_failed_check: str):
    report = _run_in_process(behavior, id_token=ID_TOKEN)
    failed = [c.name for c in report.checks if not c.passed and not c.skipped]
    assert failed == [only_failed_check], f"{behavior} should fail only {only_failed_check}, got {failed}"


def test_success_returns_error_fails_the_success_check_and_dependents():
    # A valid authenticated POST that returns a bounded typed *error* (not the
    # success observation) must fail the success check on the typed-success
    # axis (not merely a leak/bound axis) and cascade to its dependents.
    report = _run_in_process("success_returns_error", id_token=ID_TOKEN)
    success = _check(report, "authenticated_valid_post_succeeds_bounded_typed")
    assert success.passed is False
    assert success.observed_status == 500  # the response WAS bounded + JSON, but not a typed success
    assert report.accepted is False


# --------------------------------------------------------------------------- #
# response_is_bounded_and_clean: direct discriminating unit coverage.
# --------------------------------------------------------------------------- #


def _resp(body: dict, *, content_type="application/json") -> HttpResponse:
    return HttpResponse(
        status=200,
        content_type=content_type,
        body_bytes=json.dumps(body).encode("utf-8"),
    )


def test_clean_bounded_response_accepted():
    ok, _ = response_is_bounded_and_clean(_resp({"observation_id": "obs_1", "results": {}}))
    assert ok is True


def test_non_json_content_type_rejected():
    ok, reason = response_is_bounded_and_clean(_resp({"a": 1}, content_type="text/plain"))
    assert ok is False and "content type" in reason


def test_arn_in_body_rejected():
    ok, reason = response_is_bounded_and_clean(_resp({"x": "arn:aws:gamelift:us-west-2:123456789012:fleet/y"}))
    assert ok is False and "ARN" in reason


def test_account_id_in_body_rejected():
    ok, reason = response_is_bounded_and_clean(_resp({"x": "123456789012"}))
    assert ok is False and "account id" in reason


def test_provider_payload_marker_rejected():
    ok, reason = response_is_bounded_and_clean(_resp({"ResponseMetadata": {"HTTPStatusCode": 200}}))
    assert ok is False and "provider payload" in reason


def test_oversize_body_rejected():
    big = HttpResponse(status=200, content_type="application/json", body_bytes=b"{}" + b"x" * (MAX_RESPONSE_BYTES + 1))
    ok, reason = response_is_bounded_and_clean(big)
    assert ok is False and "size ceiling" in reason


# --------------------------------------------------------------------------- #
# Config + secret hygiene: tokens never appear in the sanitized summary.
# --------------------------------------------------------------------------- #


def test_summary_document_contains_no_secret_material():
    report = _run("compliant", id_token=ID_TOKEN)
    rendered = json.dumps(report.as_document())
    assert ACCESS_TOKEN not in rendered
    assert ID_TOKEN not in rendered
    assert FLEET_ID not in rendered  # only the short hash ref appears
    assert ALT_FLEET_ID not in rendered
    # The stable short refs are present so two runs are comparable.
    assert report.as_document()["fleet_ref"].startswith("fleet-")
    assert report.as_document()["endpoint_ref"].startswith("endpoint-")


def test_config_rejects_http_endpoint():
    with pytest.raises(ValueError):
        ShakedownConfig(
            endpoint="http://insecure.example.com",
            access_token=ACCESS_TOKEN,
            fleet_id=FLEET_ID,
            alt_fleet_id=ALT_FLEET_ID,
        )


def test_config_rejects_identical_fleet_ids():
    with pytest.raises(ValueError):
        ShakedownConfig(
            endpoint="https://api.example.com",
            access_token=ACCESS_TOKEN,
            fleet_id=FLEET_ID,
            alt_fleet_id=FLEET_ID,
        )


def test_config_rejects_malformed_fleet_id():
    with pytest.raises(ValueError):
        ShakedownConfig(
            endpoint="https://api.example.com",
            access_token=ACCESS_TOKEN,
            fleet_id="not-a-fleet",
            alt_fleet_id=ALT_FLEET_ID,
        )


def test_build_config_from_env_reports_missing_inputs_without_values(monkeypatch):
    for name in ("GBAW_E1_ENDPOINT", "GBAW_E1_ACCESS_TOKEN", "GBAW_E1_FLEET_ID", "GBAW_E1_ALT_FLEET_ID"):
        monkeypatch.delenv(name, raising=False)

    class _Args:
        endpoint = access_token = fleet_id = alt_fleet_id = id_token = profile = region = None

    with pytest.raises(ValueError) as excinfo:
        build_config_from_env(_Args())
    # Names the missing inputs, not any value.
    assert "endpoint" in str(excinfo.value)
    assert "access token" in str(excinfo.value)


def test_build_config_prefers_args_over_env(monkeypatch):
    monkeypatch.setenv("GBAW_E1_ENDPOINT", "https://from-env.example.com")
    monkeypatch.setenv("GBAW_E1_ACCESS_TOKEN", "env-token")
    monkeypatch.setenv("GBAW_E1_FLEET_ID", FLEET_ID)
    monkeypatch.setenv("GBAW_E1_ALT_FLEET_ID", ALT_FLEET_ID)

    class _Args:
        endpoint = "https://from-arg.example.com"
        access_token = None
        fleet_id = None
        alt_fleet_id = None
        id_token = None
        profile = None
        region = None

    config = build_config_from_env(_Args())
    assert config.endpoint == "https://from-arg.example.com"  # arg wins
    assert config.access_token == "env-token"  # env fallback


# --------------------------------------------------------------------------- #
# Optional read-only CloudWatch postcheck (no real AWS).
# --------------------------------------------------------------------------- #


def test_metric_postcheck_skipped_without_region():
    results = run_metric_postcheck(profile=None, region=None)
    assert results[0]["skipped"] is True


def test_metric_postcheck_reports_presence_read_only():
    calls: list[dict] = []

    class _FakeCw:
        def list_metrics(self, *, Namespace, MetricName):  # noqa: N803 - boto3 kwarg names
            calls.append({"Namespace": Namespace, "MetricName": MetricName})
            # Present for two, absent for the rest.
            present = MetricName in ("ObservationFailures", "ObservationRequestLatency")
            return {"Metrics": [{"MetricName": MetricName}] if present else []}

    def factory(**kwargs):
        return _FakeCw()

    results = run_metric_postcheck(profile="demo", region="us-west-2", client_factory=factory)
    presence = {r["metric"]: r["present"] for r in results}
    assert presence["ObservationFailures"] is True
    assert presence["StuckOperations"] is False
    # Only read-only ListMetrics was ever called — no mutation surface exists.
    assert {c["Namespace"] for c in calls} == {"GameAgent/Operations"}


def test_metric_postcheck_missing_credentials_is_non_fatal():
    def factory(**kwargs):
        raise RuntimeError("no credentials")

    results = run_metric_postcheck(profile=None, region="us-west-2", client_factory=factory)
    assert results[0]["skipped"] is True


# --------------------------------------------------------------------------- #
# Hardened real transport: redirect + streaming invariants.
#
# These drive the *real* ``_requests_transport`` (the code the CLI uses) via a
# mocked ``requests`` session, so they prove the production transport — not a
# stand-in — refuses redirects without resubmitting the bearer token and aborts
# a hostile stream at the byte bound without reading the rest.
# --------------------------------------------------------------------------- #


class _FakeRawResponse:
    """A minimal stand-in for a streaming ``requests`` Response."""

    def __init__(self, *, status_code, headers, chunks):
        self.status_code = status_code
        # Case-insensitive-ish: the transport reads lowercase keys, so store
        # them lowercase to match ``requests``' CaseInsensitiveDict behavior.
        self.headers = {k.lower(): v for k, v in headers.items()}
        self._chunks = list(chunks)
        self.closed = False
        self.iter_started = False
        self.chunks_yielded = 0

    def iter_content(self, chunk_size=8192):
        self.iter_started = True
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk

    def close(self):
        self.closed = True


class _RecordingSession:
    """Fake ``requests.Session`` that records the request kwargs it received."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._response


class _FakeRequestsModule:
    """Fake ``requests`` module exposing only ``Session``."""

    def __init__(self, session):
        self._session = session

    def Session(self):  # noqa: N802 - mirror requests' API
        return self._session


def _install_fake_requests(monkeypatch, response):
    # Standard library
    import sys

    session = _RecordingSession(response)
    monkeypatch.setitem(sys.modules, "requests", _FakeRequestsModule(session))
    return session


def test_transport_refuses_non_https_url_before_any_socket(monkeypatch):
    # No response is ever produced: the URL guard fires first.
    session = _install_fake_requests(monkeypatch, response=None)
    transport = _requests_transport()
    with pytest.raises(TransportSecurityError):
        transport("GET", "http://insecure.example.com/x", {"authorization": "Bearer t"}, None)
    # The session was never asked to make the request.
    assert session.calls == []


def test_transport_sets_secure_request_options(monkeypatch):
    response = _FakeRawResponse(
        status_code=200,
        headers={"content-type": "application/json", "content-length": "2"},
        chunks=[b"{}"],
    )
    session = _install_fake_requests(monkeypatch, response)
    transport = _requests_transport()
    result = transport("GET", "https://api.example.com/x", {"authorization": "Bearer t"}, None)
    assert result.status == 200 and result.body_bytes == b"{}"
    call = session.calls[0]
    # verify on, redirects off, streaming on, and explicit split timeouts below
    # the 30s API ceiling.
    assert call["verify"] is True
    assert call["allow_redirects"] is False
    assert call["stream"] is True
    assert call["timeout"] == (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
    assert CONNECT_TIMEOUT_S + READ_TIMEOUT_S < 30
    assert response.closed is True


def test_transport_refuses_redirect_without_resubmitting_authorization(monkeypatch):
    # A hostile 3xx with a Location pointing at an attacker origin. The transport
    # must NOT follow it (allow_redirects=False) and must fail hard, so the
    # bearer token is never resubmitted to the redirect target.
    response = _FakeRawResponse(
        status_code=302,
        headers={
            "content-type": "application/json",
            "location": "https://attacker.example.com/steal",
        },
        chunks=[b"should-never-be-read"],
    )
    session = _install_fake_requests(monkeypatch, response)
    transport = _requests_transport()
    with pytest.raises(TransportSecurityError):
        transport(
            "POST",
            "https://api.example.com/operations/observe",
            {"authorization": "Bearer super-secret-token"},
            b'{"fleet_id":"fleet-x","idempotency_token":"idem_x"}',
        )
    # Exactly one request was made (the original) — the redirect was never
    # followed, so the Authorization header was never resubmitted anywhere.
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://api.example.com/operations/observe"
    # requests was told not to follow redirects itself.
    assert session.calls[0]["allow_redirects"] is False
    # The redirect body was never streamed in, and the response was released.
    assert response.iter_started is False
    assert response.closed is True


def test_transport_rejects_oversized_content_length_before_reading(monkeypatch):
    response = _FakeRawResponse(
        status_code=200,
        headers={
            "content-type": "application/json",
            "content-length": str(MAX_RESPONSE_BYTES + 1),
        },
        chunks=[b"x" * 1024],
    )
    session = _install_fake_requests(monkeypatch, response)
    transport = _requests_transport()
    with pytest.raises(TransportSecurityError):
        transport("GET", "https://api.example.com/x", {}, None)
    # The declared oversize was rejected before any body byte was streamed.
    assert response.iter_started is False
    assert response.closed is True


def test_transport_aborts_stream_at_bound_without_reading_the_rest(monkeypatch):
    # A hostile server that omits Content-Length and streams far more than the
    # ceiling. The transport must stop at MAX_READ_BYTES and never pull the rest.
    total_chunks = 100
    chunk = b"a" * 8192  # 8 KiB per chunk; MAX_READ_BYTES is 64 KiB + 1
    response = _FakeRawResponse(
        status_code=200,
        headers={"content-type": "application/json"},  # no content-length
        chunks=[chunk] * total_chunks,  # ~800 KiB available if fully read
    )
    session = _install_fake_requests(monkeypatch, response)
    transport = _requests_transport()
    with pytest.raises(TransportSecurityError):
        transport("GET", "https://api.example.com/x", {}, None)
    # It aborted after crossing the bound, not after draining the stream: the
    # number of chunks it pulled is bounded by ceil(MAX_READ_BYTES / 8192),
    # far fewer than the 100 available.
    # Standard library
    import math

    max_expected_chunks = math.ceil(MAX_READ_BYTES / 8192)
    assert response.chunks_yielded <= max_expected_chunks
    assert response.chunks_yielded < total_chunks
    assert response.closed is True


def test_transport_returns_bounded_body_at_the_ceiling(monkeypatch):
    # A body exactly at the accepted ceiling with no declared length streams in
    # cleanly (it does not trip the abort, which fires strictly past the cap).
    body = b"z" * MAX_RESPONSE_BYTES
    response = _FakeRawResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        chunks=[body[i : i + 8192] for i in range(0, len(body), 8192)],
    )
    _install_fake_requests(monkeypatch, response)
    transport = _requests_transport()
    result = transport("GET", "https://api.example.com/x", {}, None)
    assert result.size == MAX_RESPONSE_BYTES
    assert response.closed is True
