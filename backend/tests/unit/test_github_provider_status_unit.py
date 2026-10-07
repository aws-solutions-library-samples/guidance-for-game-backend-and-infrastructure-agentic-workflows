#!/usr/bin/env python3
"""GitHub auth denials and throttles map to distinct, bounded provider failures.

GitHub can return 403 for both permission denial and primary/secondary rate limits. Only
header-confirmed throttles are retryable; ambiguous 403 responses remain permanent auth
failures. Provider delay hints are numeric, payload-free, and bounded.

Validates: PR #319 re-review GitHub throttle-classification finding.
"""

# Third-party packages
import httpx
import pytest

# Local modules
from connector import github_provider
from connector.github_provider import GitHubProvider
from connector.provider import (
    MAX_PROVIDER_RETRY_DELAY_SECONDS,
    ProviderAuthError,
    ProviderTransientError,
)

pytestmark = pytest.mark.unit

_METHOD = "GET"
_PATH = "/repos/org/iac/contents/main.tf"
_BODY_MARKER = "DO_NOT_LOG_GITHUB_BODY_7f31"


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, content=_BODY_MARKER.encode())


def _raise(response: httpx.Response) -> None:
    GitHubProvider._raise_for_status(response, _METHOD, _PATH, allow_404=False)


@pytest.mark.parametrize("status", [401, 403])
def test_credential_denial_is_permanent_and_payload_free(status):
    with pytest.raises(ProviderAuthError) as exc:
        _raise(_response(status))

    assert _BODY_MARKER not in str(exc.value)


def test_403_with_nonzero_remaining_is_still_auth_denial():
    with pytest.raises(ProviderAuthError):
        _raise(_response(403, {"X-RateLimit-Remaining": "1"}))


def test_primary_rate_limit_403_is_transient_with_reset_hint(monkeypatch):
    monkeypatch.setattr(github_provider, "_time", lambda: 1_000.0)
    response = _response(
        403,
        {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1003",
        },
    )

    with pytest.raises(ProviderTransientError) as exc:
        _raise(response)

    assert exc.value.retry_after_seconds == 3.0
    assert _BODY_MARKER not in str(exc.value)


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        ("2.5", 2.5),
        ("invalid", None),
        ("-1", None),
        ("999999", MAX_PROVIDER_RETRY_DELAY_SECONDS),
    ],
)
def test_secondary_rate_limit_403_is_transient_even_if_hint_is_invalid(retry_after, expected_delay):
    with pytest.raises(ProviderTransientError) as exc:
        _raise(_response(403, {"Retry-After": retry_after}))

    assert exc.value.retry_after_seconds == expected_delay
    assert retry_after not in str(exc.value)
    assert _BODY_MARKER not in str(exc.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_429_and_5xx_are_transient_without_a_hint(status):
    with pytest.raises(ProviderTransientError) as exc:
        _raise(_response(status))

    assert exc.value.retry_after_seconds is None
    assert _BODY_MARKER not in str(exc.value)


def test_429_honors_bounded_retry_after():
    with pytest.raises(ProviderTransientError) as exc:
        _raise(_response(429, {"Retry-After": "4"}))

    assert exc.value.retry_after_seconds == 4.0


def test_503_honors_bounded_retry_after():
    with pytest.raises(ProviderTransientError) as exc:
        _raise(_response(503, {"Retry-After": "60"}))

    assert exc.value.retry_after_seconds == MAX_PROVIDER_RETRY_DELAY_SECONDS
