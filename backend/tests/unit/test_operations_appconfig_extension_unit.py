"""AppConfig Lambda extension HTTP client tests (issue #416, E4).

:class:`~operations.control.appconfig_extension.AppConfigExtensionClient` reads
the raw kill-switch bytes from the AppConfig Lambda extension's localhost HTTP
endpoint. It is a thin, bounded transport: it builds the exact extension URL from
the frozen application/environment/profile identifiers, issues one bounded-timeout
GET, and returns the response bytes. It performs no JSON parsing, schema
validation, freshness check, cache, or fallback — that is the gate's job. Any
transport/HTTP failure propagates so the gate fails closed.
"""

from __future__ import annotations

# Standard library
from typing import Any

# Third-party packages
import pytest

# Local modules
from operations.control.appconfig_extension import AppConfigExtensionClient

pytestmark = [pytest.mark.unit, pytest.mark.fast]


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self, amt: int | None = None) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _client(opener: Any, **overrides: Any) -> AppConfigExtensionClient:
    kwargs: dict[str, Any] = {
        "application": "game-agent-operations",
        "environment": "prod",
        "profile": "operations-kill-switch",
        "port": 2772,
        "timeout_s": 0.5,
        "opener": opener,
    }
    kwargs.update(overrides)
    return AppConfigExtensionClient(**kwargs)


def test_builds_exact_extension_url_and_returns_bytes() -> None:
    seen: dict[str, Any] = {}

    def opener(url: str, timeout: float) -> _FakeResponse:
        seen["url"] = url
        seen["timeout"] = timeout
        return _FakeResponse(b'{"ok": true}')

    client = _client(opener)
    body = client.fetch_configuration()
    assert body == b'{"ok": true}'
    assert seen["url"] == (
        "http://localhost:2772/applications/game-agent-operations"
        "/environments/prod/configurations/operations-kill-switch"
    )
    assert seen["timeout"] == 0.5


def test_custom_port_is_used() -> None:
    seen: dict[str, Any] = {}

    def opener(url: str, timeout: float) -> _FakeResponse:
        seen["url"] = url
        return _FakeResponse(b"{}")

    _client(opener, port=2773).fetch_configuration()
    assert ":2773/" in seen["url"]


def test_transport_error_propagates() -> None:
    def opener(url: str, timeout: float) -> _FakeResponse:
        raise OSError("connection refused")

    with pytest.raises(OSError):
        _client(opener).fetch_configuration()


def test_non_200_status_raises() -> None:
    def opener(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse(b"missing", status=404)

    with pytest.raises(RuntimeError):
        _client(opener).fetch_configuration()


def test_oversized_body_is_refused() -> None:
    big = b"x" * (64 * 1024 + 2)

    def opener(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse(big)

    with pytest.raises(RuntimeError):
        _client(opener).fetch_configuration()


def test_invalid_identifiers_fail_closed_at_construction() -> None:
    with pytest.raises(ValueError):
        _client(lambda url, timeout: _FakeResponse(b"{}"), application="")
    with pytest.raises(ValueError):
        _client(lambda url, timeout: _FakeResponse(b"{}"), port=0)
    with pytest.raises(ValueError):
        _client(lambda url, timeout: _FakeResponse(b"{}"), timeout_s=0)
    with pytest.raises(ValueError):
        # A path-injecting identifier is rejected before any request.
        _client(lambda url, timeout: _FakeResponse(b"{}"), profile="../evil")
