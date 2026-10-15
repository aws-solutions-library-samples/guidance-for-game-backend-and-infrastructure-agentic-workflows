#!/usr/bin/env python3
"""Unit test: the GitHub adapter sends the exact Contents URL for the authorized path.

Regression for the PR #319 path-hardening finding. The connector authorizes a single
canonical repo-relative path and the adapter must fetch *that same* path — proving the
authorized path equals the path in the provider URL. This captures the outgoing request via
an httpx ``MockTransport`` (respx is not a dependency; ``httpx`` is) and asserts the final
URL is exactly::

    https://api.github.com/repos/{repo}/contents/{path}?ref={branch}

``get_secret`` is patched (as the other GitHub adapter tests do) so no real Secrets Manager
call occurs and no credential is required.
"""

# Standard library
import base64
from types import SimpleNamespace

# Third-party packages
import httpx
import pytest

# Local modules
from connector import github_provider
from connector.github_provider import GitHubProvider

pytestmark = pytest.mark.unit

_REPO = "org/iac-repo"
_BRANCH = "main"
_PATH = "infra/nested/main.tf"


def _make_config() -> SimpleNamespace:
    """Build a minimal config stub with the fields GitHubProvider reads (public host)."""
    return SimpleNamespace(
        connector=SimpleNamespace(provider="github", provider_timeout_seconds=30),
        adapter=SimpleNamespace(
            read_credential_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:x-AbCdEf",
            provider_base_url=None,
        ),
    )


def test_get_files_sends_exact_contents_url_for_authorized_path(monkeypatch):
    """The adapter's outgoing URL equals the canonical authorized path (repo/contents/path?ref)."""
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        payload = {
            "encoding": "base64",
            "content": base64.b64encode(b"resource {}").decode("ascii"),
            "path": _PATH,
        }
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    # Inject the MockTransport into the client the adapter builds inside _request, without
    # changing the adapter: wrap httpx.Client so every instantiation uses the mock transport.
    real_client = httpx.Client

    def client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(github_provider.httpx, "Client", client_with_mock_transport)
    # Patch the credential fetch so no Secrets Manager call occurs (as existing tests do).
    monkeypatch.setattr(github_provider, "get_secret", lambda *args, **kwargs: "unused-token")

    provider = GitHubProvider(_make_config())
    result = provider.get_files(_REPO, _BRANCH, [_PATH])

    # The file was fetched from the exact authorized path.
    assert [f.path for f in result.files] == [_PATH]
    assert result.missing == ()

    assert captured_urls == [f"https://api.github.com/repos/{_REPO}/contents/{_PATH}?ref={_BRANCH}"]


def _install_capturing_transport(monkeypatch) -> list[httpx.Request]:
    """Route the adapter's httpx.Client through a MockTransport that records each request.

    Returns the list the handler appends every outgoing :class:`httpx.Request` to, and
    patches ``get_secret`` so no Secrets Manager call occurs.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload = {
            "encoding": "base64",
            "content": base64.b64encode(b"resource {}").decode("ascii"),
            "path": str(request.url.path),
        }
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(github_provider.httpx, "Client", client_with_mock_transport)
    monkeypatch.setattr(github_provider, "get_secret", lambda *args, **kwargs: "unused-token")
    return captured


def test_get_files_transmits_nested_path_exactly_and_encodes_ref(monkeypatch):
    """A normal nested path is sent verbatim and the branch is a properly-encoded ref query."""
    captured = _install_capturing_transport(monkeypatch)

    # A branch value carrying URL-significant characters must not inject into the path/query;
    # httpx encodes it as a query parameter that round-trips back to the exact branch value.
    branch = "feature/x y"
    path = "infra/sub/app.yaml"

    provider = GitHubProvider(_make_config())
    result = provider.get_files(_REPO, branch, [path])

    assert [f.path for f in result.files] == [path]
    assert len(captured) == 1
    request = captured[0]

    # The authorized canonical path equals the path in the outgoing URL, byte for byte.
    assert request.url.path == f"/repos/{_REPO}/contents/{path}"
    # The branch is carried only as an encoded query param that decodes back to the original;
    # it never leaks a raw space into the URL and cannot inject into the path.
    assert request.url.params.get("ref") == branch
    assert " " not in str(request.url)


def test_get_files_percent_encodes_reserved_chars_within_a_segment(monkeypatch):
    """A character requiring encoding inside a segment is percent-encoded; separators kept.

    A space is used because it is a valid path character not rejected by the service layer's
    Layer 1; it must be percent-encoded inside its segment while the ``/`` separators between
    segments remain literal, so the requested URL path still resolves to the authorized path.
    """
    captured = _install_capturing_transport(monkeypatch)

    path = "infra/my config/app.yaml"  # space inside the middle segment

    provider = GitHubProvider(_make_config())
    provider.get_files(_REPO, _BRANCH, [path])

    assert len(captured) == 1
    request = captured[0]

    # The space is encoded within the segment; the "/" separators are preserved.
    assert str(request.url) == (
        f"https://api.github.com/repos/{_REPO}/contents/infra/my%20config/app.yaml?ref={_BRANCH}"
    )
    # And the decoded path is exactly the authorized path (no divergence, no truncation).
    assert request.url.path == f"/repos/{_REPO}/contents/{path}"
