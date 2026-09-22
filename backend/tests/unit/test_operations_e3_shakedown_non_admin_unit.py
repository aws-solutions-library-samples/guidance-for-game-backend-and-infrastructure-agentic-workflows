"""E3 shakedown non-admin denial tests (#415).

The E3 shakedown docstring promises three discriminating checks, the third being
"a non-admin token is denied (403) — execution is admin-only". The independent
review flagged that ``run()`` executed only the first two checks, so the
non-admin claim was undocumented-by-test. These red-green tests pin the third
check: when a non-admin bearer is configured, the harness actually POSTs it and
asserts a 403 denial, and ``run()`` includes that check in its summary.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any, Optional

# Local modules
from operations.validation.e1_shakedown import HttpResponse
from operations.validation.e3_shakedown import (
    REQUIRED_CONFIRMATION,
    E3ShakedownConfig,
    E3ShakedownHarness,
    summary_is_public_safe,
)

_ENDPOINT = "https://abc123.execute-api.us-west-2.amazonaws.com"
_OP = "op_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_ADMIN = "admin-token-SECRET"
_NON_ADMIN = "users-token-SECRET"
_FLEET = "fleet-1234abcd-5678-90ef"


def _resp(status: int, body: Optional[dict[str, Any]] = None) -> HttpResponse:
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    return HttpResponse(status=status, content_type="application/json", body_bytes=raw)


class _BearerAwareTransport:
    """A transport that returns a response keyed on the exact bearer token."""

    def __init__(self, by_bearer: dict[Optional[str], HttpResponse]) -> None:
        self._by_bearer = by_bearer
        self.seen_bearers: list[Optional[str]] = []

    def __call__(self, method: str, url: str, headers: Optional[dict[str, str]], body: Optional[bytes]) -> HttpResponse:
        bearer: Optional[str] = None
        if headers:
            for key, value in headers.items():
                if key.lower() == "authorization" and value.lower().startswith("bearer "):
                    bearer = value.split(" ", 1)[1]
        self.seen_bearers.append(bearer)
        return self._by_bearer[bearer]


def _config() -> E3ShakedownConfig:
    return E3ShakedownConfig(
        endpoint=_ENDPOINT,
        operation_id=_OP,
        admin_bearer=_ADMIN,
        fleet_id=_FLEET,
        non_admin_bearer=_NON_ADMIN,
        # The write-capable admin check needs explicit confirmation; without
        # it run() would refuse that check and accepted would be False.
        confirmation=REQUIRED_CONFIRMATION,
    )


def _transport() -> _BearerAwareTransport:
    return _BearerAwareTransport(
        {
            None: _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"}),
            _ADMIN: _resp(202, {"state": "dispatched"}),
            _NON_ADMIN: _resp(403, {"error_code": "AUTHORIZATION_DENIED"}),
        }
    )


def test_non_admin_dispatch_denied_check_passes_on_403() -> None:
    harness = E3ShakedownHarness(_config(), _transport())
    result = harness.check_non_admin_dispatch_denied()
    assert result.passed
    assert result.observed_status == 403


def test_run_includes_non_admin_check_when_token_configured() -> None:
    transport = _transport()
    harness = E3ShakedownHarness(_config(), transport)
    summary = harness.run()
    names = {check["name"] for check in summary["checks"]}
    assert "non_admin_dispatch_denied" in names
    assert summary["accepted"] is True
    # The non-admin token was actually exercised over the wire.
    assert _NON_ADMIN in transport.seen_bearers
    # Secrets never leak into the summary.
    blob = repr(summary)
    assert _NON_ADMIN not in blob
    assert _ADMIN not in blob
    assert summary_is_public_safe(summary)


def test_non_admin_check_absent_when_no_token_configured() -> None:
    config = E3ShakedownConfig(
        endpoint=_ENDPOINT,
        operation_id=_OP,
        admin_bearer=_ADMIN,
        fleet_id=_FLEET,
        confirmation=REQUIRED_CONFIRMATION,
    )
    transport = _BearerAwareTransport(
        {None: _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"}), _ADMIN: _resp(202, {"state": "dispatched"})}
    )
    summary = E3ShakedownHarness(config, transport).run()
    names = {check["name"] for check in summary["checks"]}
    assert "non_admin_dispatch_denied" not in names
