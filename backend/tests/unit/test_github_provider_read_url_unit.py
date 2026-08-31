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
