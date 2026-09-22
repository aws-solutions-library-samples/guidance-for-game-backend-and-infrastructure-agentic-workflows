"""E3 shakedown live-dispatch confirmation-gate tests (#415).

The E3 shakedown's admin-dispatch check is **write-capable**: POSTing an
admin-authenticated request to the deployed ``/dispatch`` route starts the E3
Step Functions execution and may perform a real ``UpdateFleetCapacity`` against
the enrolled fleet. The harness therefore MUST NOT make that authenticated,
write-capable call unless the operator has supplied an explicit, exact,
out-of-band confirmation value (a flag/env input that is never persisted and can
never be sourced from a response body).

These red-green tests pin the safety contract:

* Without the exact confirmation the admin-dispatch check **refuses** and makes
  **no authenticated POST** (nothing reaches the deployed handler).
* A wrong / partial / case-shifted confirmation value is **rejected** the same
  way — only the exact frozen token unlocks the write-capable check.
* The unauthenticated denial check still runs (non-mutating) with no
  confirmation, so an operator can safely verify the gateway denies anon.
* Confirmation cannot come from the HTTP response/body: even a response that
  echoes the exact token back does not unlock the check.
* The confirmation value never leaks into the sanitized summary.
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


class _RecordingTransport:
    """A transport that records every call and whether it carried a bearer."""

    def __init__(self, response: HttpResponse) -> None:
        self._response = response
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
        return self._response

    @property
    def authenticated_calls(self) -> list[tuple[str, str, bool]]:
        return [c for c in self.calls if c[2]]


def _config(confirmation: str = "") -> E3ShakedownConfig:
    return E3ShakedownConfig(
        endpoint=_ENDPOINT,
        operation_id=_OP,
        admin_bearer=_ADMIN,
        fleet_id=_FLEET,
        confirmation=confirmation,
    )


def test_admin_dispatch_refuses_without_confirmation_and_makes_no_authed_call() -> None:
    transport = _RecordingTransport(_resp(202, {"state": "dispatched"}))
    harness = E3ShakedownHarness(_config(confirmation=""), transport)

    result = harness.check_admin_dispatch_accepted()

    assert result.passed is False
    assert result.observed_error_code == "CONFIRMATION_REQUIRED"
    # The write-capable authenticated POST must never have been made.
    assert transport.authenticated_calls == []


@pytest.mark.parametrize(
    "wrong",
    [
        "execute-live-dispatch",  # case-shifted
        "EXECUTE-LIVE-DISPATC",  # truncated
        "EXECUTE-LIVE-DISPATCH ",  # trailing space
        " EXECUTE-LIVE-DISPATCH",  # leading space
        "yes",
        REQUIRED_CONFIRMATION + "X",
    ],
)
def test_admin_dispatch_refuses_on_wrong_confirmation(wrong: str) -> None:
    transport = _RecordingTransport(_resp(202, {"state": "dispatched"}))
    harness = E3ShakedownHarness(_config(confirmation=wrong), transport)

    result = harness.check_admin_dispatch_accepted()

    assert result.passed is False
    assert result.observed_error_code == "CONFIRMATION_REQUIRED"
    assert transport.authenticated_calls == []


def test_admin_dispatch_makes_authed_call_only_with_exact_confirmation() -> None:
    transport = _RecordingTransport(_resp(202, {"state": "dispatched"}))
    harness = E3ShakedownHarness(_config(confirmation=REQUIRED_CONFIRMATION), transport)

    result = harness.check_admin_dispatch_accepted()

    assert result.passed is True
    # Exactly one authenticated (write-capable) POST was made, and only after
    # the exact confirmation unlocked it.
    assert len(transport.authenticated_calls) == 1


def test_unauthenticated_check_runs_without_confirmation_and_is_non_mutating() -> None:
    transport = _RecordingTransport(_resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"}))
    harness = E3ShakedownHarness(_config(confirmation=""), transport)

    result = harness.check_unauthenticated_dispatch_denied()

    assert result.passed is True
    # The anon check makes a request but never an authenticated one.
    assert transport.authenticated_calls == []


def test_confirmation_cannot_come_from_response_body() -> None:
    # A hostile/echoing deployment returns the exact confirmation token in the
    # body. This must NOT unlock the write-capable check: confirmation is an
    # out-of-band operator input only.
    transport = _RecordingTransport(_resp(202, {"state": "dispatched", "confirmation": REQUIRED_CONFIRMATION}))
    harness = E3ShakedownHarness(_config(confirmation=""), transport)

    result = harness.check_admin_dispatch_accepted()

    assert result.passed is False
    assert result.observed_error_code == "CONFIRMATION_REQUIRED"
    assert transport.authenticated_calls == []


def test_run_without_confirmation_makes_no_authed_call_and_is_not_accepted() -> None:
    transport = _RecordingTransport(_resp(401, {"error_code": "IDENTITY_CONTEXT_INVALID"}))
    harness = E3ShakedownHarness(_config(confirmation=""), transport)

    summary = harness.run()

    assert transport.authenticated_calls == []
    assert summary["accepted"] is False
    admin_check = next(c for c in summary["checks"] if c["name"] == "admin_dispatch_accepted")
    assert admin_check["passed"] is False
    assert admin_check["observed_error_code"] == "CONFIRMATION_REQUIRED"


def test_confirmation_value_never_leaks_into_summary() -> None:
    transport = _RecordingTransport(_resp(202, {"state": "dispatched"}))
    harness = E3ShakedownHarness(_config(confirmation=REQUIRED_CONFIRMATION), transport)

    summary = harness.run()

    blob = json.dumps(summary)
    assert REQUIRED_CONFIRMATION not in blob
    assert _ADMIN not in blob
    assert summary_is_public_safe(summary)
