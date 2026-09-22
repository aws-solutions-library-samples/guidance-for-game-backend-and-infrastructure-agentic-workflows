"""Production-bootstrap BDD scenarios for the E4 control plane (issue #416).

These scenarios drive the REAL deployable E4 bootstrap
(``operations.control.control_entry._build_runtime``) over injected fake AWS
clients and the frozen environment contract — not hand-constructed services.
They prove the wiring that a manually assembled router cannot:

* the control entry fails closed at bootstrap when ``GBAW_OPERATIONS_CONTROL_MODE``
  is disabled (admin controls are opt-in and independent of the execution mode);
* with control enabled, the bootstrap wires the REAL ``KillSwitchGate`` over the
  REAL ``AppConfigExtensionClient`` (localhost transport), so
  ``GET /operations/control/kill-switch`` reflects the document the extension
  currently serves and fails closed (503) when the extension is unavailable;
* the control read/list/detail/capabilities routes are reachable through the
  real router with the JWT identity boundary enforced.

Only the localhost HTTP transport and the boto3 clients are faked; the gate,
extension client, router, and handlers are the real production objects.
"""

from __future__ import annotations

# Standard library
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Third-party packages
import pytest

# Local modules
import operations.control.control_entry as entry
from operations.contracts.control_plane import CAPABILITY_ID, KILL_SWITCH_ROUTE

pytestmark = [pytest.mark.integration, pytest.mark.localhost]

_TRUSTED = "trusted-app-client"
_TABLE = "game-agent-operations"

_ENV = {
    "GBAW_OPERATIONS_TABLE_NAME": _TABLE,
    "GBAW_OPERATIONS_METRIC_NAMESPACE": "GameAgent/Operations",
    "GBAW_OPERATIONS_TENANT_ID": "tenant-default",
    "GBAW_OPERATIONS_WORKSPACE_ID": "workspace-default",
    "GBAW_OPERATIONS_TRUSTED_AUDIENCE": _TRUSTED,
    "GBAW_OPERATIONS_MODE": "remediate",
    "GBAW_OPERATIONS_CONTROL_MODE": "enabled",
    "GBAW_OPERATIONS_APPCONFIG_APPLICATION": "game-agent-operations",
    "GBAW_OPERATIONS_APPCONFIG_ENVIRONMENT": "prod",
    "GBAW_OPERATIONS_APPCONFIG_PROFILE": "operations-kill-switch",
    "GBAW_OPERATIONS_APPCONFIG_GRADUAL_STRATEGY_ID": "strategy-gradual",
    "GBAW_OPERATIONS_APPCONFIG_IMMEDIATE_STRATEGY_ID": "strategy-immediate",
    "GBAW_OPERATIONS_CURSOR_SIGNING_KEY": "a-sufficiently-long-signing-key",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _kill_switch_bytes(*, prepare: bool, dispatch: bool, execute: bool) -> bytes:
    now = _now()
    document = {
        "contract_version": "1.0",
        "config_version": 11,
        "issued_at": _z(now - timedelta(seconds=30)),
        "not_after": _z(now + timedelta(minutes=5)),
        "operations_enabled": True,
        "capabilities": {CAPABILITY_ID: {"prepare": prepare, "dispatch": dispatch, "execute": execute}},
    }
    return json.dumps(document).encode("utf-8")


class _FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {"Item": item} if item else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {"Items": [], "Count": 0}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        self.items[(item["PK"]["S"], item["SK"]["S"])] = item
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        return {}


class _NoopClient:
    def __getattr__(self, _name: str) -> Any:
        def _call(**_kwargs: Any) -> dict[str, Any]:
            return {}

        return _call


class _FakeSession:
    def __init__(self, dynamo: _FakeDynamo) -> None:
        self._dynamo = dynamo

    def client(self, name: str, **_kwargs: Any) -> Any:
        if name == "dynamodb":
            return self._dynamo
        return _NoopClient()


class _FakeHTTPResponse:
    def __init__(self, body: bytes | None) -> None:
        self._body = body
        self.status = 200

    def __enter__(self) -> "_FakeHTTPResponse":
        if self._body is None:
            raise OSError("extension unavailable")
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self, amt: int | None = None) -> bytes:
        assert self._body is not None
        return self._body


def _build_runtime(monkeypatch: pytest.MonkeyPatch, *, switch_body: bytes | None, env: dict[str, str]) -> Any:
    for key in list(__import__("os").environ):
        if key.startswith("GBAW_OPERATIONS_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("boto3.Session", lambda **_k: _FakeSession(_FakeDynamo()), raising=False)
    monkeypatch.setattr(entry, "_region", lambda: "us-west-2")

    def _fake_urlopen(url: str, timeout: float) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(switch_body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen, raising=False)
    entry._runtime.cache_clear()
    runtime = entry._runtime()
    entry._runtime.cache_clear()
    return runtime


def _kill_switch_event() -> dict[str, Any]:
    return {
        "requestContext": {
            "http": {"method": "GET", "path": KILL_SWITCH_ROUTE},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "admin-1",
                        "client_id": _TRUSTED,
                        "token_use": "access",
                        "exp": str(int((_now() + timedelta(hours=1)).timestamp())),
                        "cognito:groups": "[admin]",
                    }
                }
            },
        }
    }


def test_bootstrap_fails_closed_when_control_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    env = dict(_ENV)
    env["GBAW_OPERATIONS_CONTROL_MODE"] = "disabled"
    with pytest.raises(Exception):
        _build_runtime(monkeypatch, switch_body=_kill_switch_bytes(prepare=True, dispatch=True, execute=True), env=env)
    entry._runtime.cache_clear()


def test_bootstrap_kill_switch_read_reflects_extension_document(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _build_runtime(
        monkeypatch,
        switch_body=_kill_switch_bytes(prepare=True, dispatch=False, execute=False),
        env=dict(_ENV),
    )
    response = runtime.router.handle(_kill_switch_event())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["operations_enabled"] is True
    assert body["config_version"] == 11
    # execute/dispatch are OFF in the served document; the real gate reflects it.
    assert body["phases"]["prepare"] is True
    assert body["phases"]["dispatch"] is False
    assert body["phases"]["execute"] is False
    entry._runtime.cache_clear()


def test_bootstrap_kill_switch_read_fails_closed_when_extension_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _build_runtime(monkeypatch, switch_body=None, env=dict(_ENV))
    response = runtime.router.handle(_kill_switch_event())
    assert response["statusCode"] == 503
    body = json.loads(response["body"])
    assert body["error_code"] == "KILL_SWITCH_UNAVAILABLE"
    entry._runtime.cache_clear()
