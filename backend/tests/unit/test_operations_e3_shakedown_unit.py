"""Public-safe E3 shakedown harness tests (#415).

The E3 shakedown exercises the deployed dispatch boundary over HTTPS. These
tests use a fake transport to assert the discriminating checks and — critically
for a public sample — that the emitted summary is sanitized: it never contains
the endpoint host, a bearer token, an ARN, a fleet id, or a raw operation id.

The admin-dispatch check is write-capable (it can start a real execution), so it
runs only when the exact out-of-band confirmation is supplied. Tests that
exercise that check therefore construct the config with the confirmation set;
the dedicated confirmation-gate suite
(``test_operations_e3_shakedown_confirmation_unit.py``) proves that a missing or
wrong confirmation refuses the check and makes no authenticated call.
"""

from __future__ import annotations

# Standard library
import json
from typing import Any, Optional

# Third-party packages
import pytest

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
_FLEET = "fleet-1234abcd-5678-90ef"


def _resp(status: int, body: Optional[dict[str, Any]] = None) -> HttpResponse:
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    return HttpResponse(status=status, content_type="application/json", body_bytes=raw)


class _FakeTransport:
    """A callable transport keyed on method + presence of an auth header."""

    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Optional[dict[str, str]],
        body: Optional[bytes],
    ) -> HttpResponse:
        authed = bool(headers and any(k.lower() == "authorization" for k in headers))
        self.calls.append((method, url, authed))
        return self._responses["auth" if authed else "anon"]


def _config(confirmation: str = REQUIRED_CONFIRMATION) -> E3ShakedownConfig:
    return E3ShakedownConfig(
        endpoint=_ENDPOINT,
        operation_id=_OP,
        admin_bearer=_ADMIN,
        fleet_id=_FLEET,
        confirmation=confirmation,
    )


def test_config_requires_https() -> None:
    with pytest.raises(ValueError):
        E3ShakedownConfig(endpoint="http://insecure", operation_id=_OP, admin_bearer=_ADMIN, fleet_id=_FLEET)


def test_unauthenticated_dispatch_denied_check() -> None:
    transport = _FakeTransport(
        {"anon": _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"}), "auth": _resp(202, {"state": "dispatched"})}
    )
    harness = E3ShakedownHarness(_config(), transport)
    assert harness.check_unauthenticated_dispatch_denied().passed


def test_valid_admin_dispatch_accepted_check() -> None:
    transport = _FakeTransport(
        {"anon": _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"}), "auth": _resp(202, {"state": "dispatched"})}
    )
    # The admin-dispatch check is write-capable, so it requires the explicit
    # out-of-band confirmation before it will make the authenticated call.
    harness = E3ShakedownHarness(_config(confirmation=REQUIRED_CONFIRMATION), transport)
    assert harness.check_admin_dispatch_accepted().passed


def test_summary_is_public_safe_and_hides_secrets() -> None:
    transport = _FakeTransport(
        {"anon": _resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"}), "auth": _resp(202, {"state": "dispatched"})}
    )
    harness = E3ShakedownHarness(_config(confirmation=REQUIRED_CONFIRMATION), transport)
    summary = harness.run()
    blob = repr(summary)
    assert _ADMIN not in blob
    assert _FLEET not in blob
    assert "abc123.execute-api" not in blob
    assert _OP not in blob  # operation id appears only as a short hash
    assert REQUIRED_CONFIRMATION not in blob  # confirmation never leaks
    assert summary_is_public_safe(summary)
