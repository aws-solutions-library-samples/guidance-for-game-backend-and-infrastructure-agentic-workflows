#!/usr/bin/env python3
"""GitHub Contents responses fail through the typed, sanitized provider boundary.

A successful HTTP status does not make the response body trusted. Invalid JSON, non-object
payloads, malformed content fields, invalid base64, and non-UTF-8 bytes must become a
non-retryable :class:`ProviderError`, never a raw decoder exception. The error must not
include provider-controlled payload or file content.

Validates: PR #319 re-review malformed-provider-payload finding.
"""

# Standard library
import base64
from types import SimpleNamespace
from typing import Callable

# Third-party packages
import httpx
import pytest

# Local modules
from connector import github_provider
from connector.github_provider import GitHubProvider
from connector.provider import ProviderError

pytestmark = pytest.mark.unit

_REPO = "org/iac"
_BRANCH = "main"
_PATH = "infra/main.tf"
_GENERIC_ERROR = "Provider returned an invalid Contents API response"


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        connector=SimpleNamespace(provider="github", provider_timeout_seconds=30),
        adapter=SimpleNamespace(
            read_credential_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf",
            provider_base_url=None,
        ),
    )


def _install_transport(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Install a recording MockTransport and a synthetic credential."""
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)
    real_client = httpx.Client

    def client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(github_provider.httpx, "Client", client_with_mock_transport)
    monkeypatch.setattr(github_provider, "get_secret", lambda *args, **kwargs: "unused-token")
    return requests


def _assert_sanitized_permanent_error(exc: pytest.ExceptionInfo[ProviderError], marker: str = "") -> None:
    assert type(exc.value) is ProviderError
    assert str(exc.value) == _GENERIC_ERROR
    if marker:
        assert marker not in str(exc.value)


def test_invalid_json_raises_sanitized_provider_error(monkeypatch):
    marker = "DO_NOT_LOG_INVALID_JSON_7f31"
    requests = _install_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, content=f'{{"content":"{marker}"'.encode()),
    )

    provider = GitHubProvider(_make_config())
    with pytest.raises(ProviderError) as exc:
        provider.get_files(_REPO, _BRANCH, [_PATH])

    _assert_sanitized_permanent_error(exc, marker)
    assert len(requests) == 1


@pytest.mark.parametrize("payload", [None, "DO_NOT_LOG_SCALAR_7f31", 42, True])
def test_non_object_json_raises_sanitized_provider_error(monkeypatch, payload):
    requests = _install_transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))

    provider = GitHubProvider(_make_config())
    with pytest.raises(ProviderError) as exc:
        provider.get_files(_REPO, _BRANCH, [_PATH])

    _assert_sanitized_permanent_error(exc, "DO_NOT_LOG_SCALAR_7f31")
    assert len(requests) == 1


def test_directory_array_retains_missing_file_behavior(monkeypatch):
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json=[]))

    result = GitHubProvider(_make_config()).get_files(_REPO, _BRANCH, [_PATH])

    assert result.files == ()
    assert result.missing == (_PATH,)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"encoding": "base64"},
        {"encoding": "base64", "content": None},
        {"encoding": "base64", "content": 123},
        {"encoding": "none", "content": "DO_NOT_LOG_UNSUPPORTED_7f31"},
    ],
)
def test_malformed_content_fields_raise_sanitized_provider_error(monkeypatch, payload):
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))

    provider = GitHubProvider(_make_config())
    with pytest.raises(ProviderError) as exc:
        provider.get_files(_REPO, _BRANCH, [_PATH])

    _assert_sanitized_permanent_error(exc, "DO_NOT_LOG_UNSUPPORTED_7f31")


def test_empty_base64_content_is_a_valid_empty_file(monkeypatch):
    payload = {"encoding": "base64", "content": ""}
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))

    result = GitHubProvider(_make_config()).get_files(_REPO, _BRANCH, [_PATH])

    assert len(result.files) == 1
    assert result.files[0].content == ""


@pytest.mark.parametrize(
    "content",
    [
        "!!!!DO_NOT_LOG_BASE64_7f31!!!!",
        base64.b64encode(b"\xff\xfe").decode("ascii"),
    ],
)
def test_invalid_base64_or_non_utf8_raises_sanitized_provider_error(monkeypatch, content):
    payload = {"encoding": "base64", "content": content}
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))

    provider = GitHubProvider(_make_config())
    with pytest.raises(ProviderError) as exc:
        provider.get_files(_REPO, _BRANCH, [_PATH])

    _assert_sanitized_permanent_error(exc, "DO_NOT_LOG_BASE64_7f31")
    assert content not in str(exc.value)


def test_line_wrapped_base64_remains_supported(monkeypatch):
    encoded = base64.b64encode(b"resource {}\n").decode("ascii")
    line_wrapped = f"  {encoded[:4]}\r\n{encoded[4:]}\t"
    payload = {"encoding": "base64", "content": line_wrapped}
    _install_transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))

    result = GitHubProvider(_make_config()).get_files(_REPO, _BRANCH, [_PATH])

    assert result.files[0].content == "resource {}\n"
