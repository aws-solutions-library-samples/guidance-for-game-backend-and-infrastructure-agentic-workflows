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
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

# Third-party packages
import pytest

# Local modules
from operations.validation.e1_shakedown import (
    CONTRACT_VERSION,
    MAX_RESPONSE_BYTES,
    HttpResponse,
    ShakedownConfig,
    ShakedownRunner,
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
        body = {
            "status_contract_version": CONTRACT_VERSION,
            "operation_id": operation_id,
            "state": "succeeded",
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


def _run(behavior: str, *, id_token: Optional[str] = None):
    app = FakeE1App(behavior=behavior)
    with _RunningServer(app) as server:
        config = _config_for(server, id_token=id_token)
        runner = ShakedownRunner(config, _local_transport(server))
        report = runner.run()
    return report


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
    ],
)
def test_broken_deployment_fails_expected_check(behavior: str, expected_failed_check: str):
    report = _run(behavior, id_token=ID_TOKEN)
    target = _check(report, expected_failed_check)
    assert target.passed is False, f"{behavior} should have failed {expected_failed_check}"
    assert report.accepted is False


def test_leaky_body_is_detected_as_leak_specifically():
    # The leaky_body server injects an ARN + provider payload markers into the
    # success body; the bounded/clean assertion must reject it.
    report = _run("leaky_body", id_token=ID_TOKEN)
    success = _check(report, "authenticated_valid_post_succeeds_bounded_typed")
    assert success.passed is False


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
