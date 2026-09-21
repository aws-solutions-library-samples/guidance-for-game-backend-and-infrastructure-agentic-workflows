#!/usr/bin/env python3
"""Property-based test: the read credential is acquired only from the read ARN and applied only to reads.

# Feature: source-control-connector-readonly-split, Property 11: the read credential is acquired only from the read ARN and applied only to reads

This is a non-optional MR security-posture property. For all provider operations the read
path issues, the credential is acquired from the configured read-credential ARN and attached
only to a read request; no write-credential ARN is ever referenced (there is no
provider-mutation request in the shipped package).

The provider's credential-acquisition seam (:class:`GitHubReadTokenAuth`) is exercised with
a recording double for ``get_secret`` that captures exactly which secret id / source each
acquisition used, and the outbound HTTP layer is stubbed so no network call occurs. The test
proves: (a) every credential acquisition targets the configured read ARN with source
``"secretsmanager"``; (b) the acquired token is attached as the ``Authorization`` header on
the read request; (c) a distinct decoy *write* ARN is never referenced; and (d) the shipped
adapter exposes only read operations, so the credential can only ever be attached to a read.

Validates: Requirements 6.4, 6.5, 6.6
"""

# Standard library
import base64
from types import SimpleNamespace
from unittest import mock

# Third-party packages
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Local modules
from connector.github_provider import GitHubProvider, GitHubReadTokenAuth
from connector.provider import OutboundRequest, SourceControlReader

pytestmark = pytest.mark.unit

# Provider-mutation operations that must not exist on the shipped read adapter, so the read
# credential cannot be attached to anything but a read.
_MUTATION_OPS = ("create_branch", "commit_files", "open_change_proposal", "latest_commit_sha", "branch_exists")

_arns = st.from_regex(
    r"arn:aws:secretsmanager:us-west-2:123456789012:secret:[A-Za-z0-9/_-]{4,20}-[A-Za-z0-9]{6}",
    fullmatch=True,
)
_repos = st.from_regex(r"[A-Za-z0-9._-]{1,12}/[A-Za-z0-9._-]{1,12}", fullmatch=True)
_branches = st.from_regex(r"[A-Za-z0-9._-]{1,12}", fullmatch=True)
_paths = st.from_regex(r"[A-Za-z0-9_-]{1,8}(/[A-Za-z0-9_-]{1,8}){0,2}\.(yaml|tf)", fullmatch=True)
_contents = st.text(min_size=0, max_size=64)


class _SecretRecorder:
    """Records every ``get_secret`` acquisition and returns a fixed read token."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.calls: list[tuple[str, str]] = []

    def __call__(self, secret_name, source="auto", **kwargs):
        self.calls.append((secret_name, source))
        return self.token


class _FakeResponse:
    """A minimal httpx-like response carrying a base64 Contents payload."""

    def __init__(self, content: str) -> None:
        self.status_code = 200
        self._payload = {
            "encoding": "base64",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }

    def json(self):
        return self._payload


class _FakeClient:
    """Context-manager httpx.Client stand-in that records outbound request headers."""

    def __init__(self, captured, content):
        self._captured = captured
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, headers=None, params=None, json=None):
        self._captured.append({"method": method, "url": url, "headers": headers, "params": params})
        return _FakeResponse(self._content)


def _adapter_config(read_arn: str) -> SimpleNamespace:
    """A SourceControlConfig-shaped stub exposing only the fields the adapter reads."""
    return SimpleNamespace(
        connector=SimpleNamespace(provider="github", provider_timeout_seconds=30),
        adapter=SimpleNamespace(read_credential_secret_arn=read_arn, provider_base_url=None),
    )


def test_read_adapter_exposes_no_mutation_operation():
    """The shipped read adapter exposes only read ops, so the credential can only reach reads."""
    provider = GitHubProvider(_adapter_config("arn:aws:secretsmanager:us-west-2:123456789012:secret:scm/read-AbCdEf"))
    assert isinstance(provider, SourceControlReader)
    for op in ("get_file", "get_files"):
        assert callable(getattr(provider, op))
    for op in _MUTATION_OPS:
        assert not hasattr(provider, op), f"read adapter unexpectedly exposes a mutation op: {op}"


# Feature: source-control-connector-readonly-split, Property 11: the read credential is acquired only from the read ARN and applied only to reads
@settings(max_examples=100)
@given(read_arn=_arns, decoy_write_arn=_arns, repo=_repos, branch=_branches, path=_paths, content=_contents)
def test_property11_read_credential_from_read_arn_only(read_arn, decoy_write_arn, repo, branch, path, content):
    """The read credential is acquired from the read ARN and attached only to read requests.

    Drives a full read (``get_file``) through the adapter with the credential seam and the
    HTTP layer stubbed; asserts the acquisition targeted the configured read ARN
    (source ``secretsmanager``), the token was attached as the read request's
    ``Authorization`` header, and the decoy write ARN was never referenced (Req 6.4, 6.5, 6.6).
    """
    if decoy_write_arn == read_arn:
        decoy_write_arn = decoy_write_arn + "-decoy"

    token = "read-token-value"
    recorder = _SecretRecorder(token)
    captured: list[dict] = []

    provider = GitHubProvider(_adapter_config(read_arn))

    with (
        mock.patch("connector.github_provider.get_secret", recorder),
        mock.patch(
            "connector.github_provider.httpx.Client",
            lambda *a, **k: _FakeClient(captured, content),
        ),
    ):
        result = provider.get_file(repo, branch, path)

    # The read returned content (a real read op ran through the credential seam).
    assert result is not None
    assert result.content == content

    # (a) At least one credential acquisition occurred, and EVERY acquisition targeted the
    #     configured read ARN with the Secrets Manager source.
    assert recorder.calls, "expected the read to acquire the read credential"
    for secret_name, source in recorder.calls:
        assert secret_name == read_arn
        assert source == "secretsmanager"

    # (b) No write-credential ARN was ever referenced during the read.
    assert all(secret_name != decoy_write_arn for secret_name, _ in recorder.calls)

    # (c) The acquired token was attached as the Authorization header of the read request.
    assert captured, "expected an outbound read request"
    for request in captured:
        assert request["headers"].get("Authorization") == f"Bearer {token}"


# Feature: source-control-connector-readonly-split, Property 11: the read credential is acquired only from the read ARN and applied only to reads
@settings(max_examples=100)
@given(read_arn=_arns, decoy_write_arn=_arns)
def test_property11_auth_apply_uses_read_arn_only(read_arn, decoy_write_arn):
    """``GitHubReadTokenAuth.apply`` acquires from the read ARN and attaches it to the request.

    Exercises the credential-acquisition contract directly: the auth double records the
    secret id used; it must be the read ARN (never the decoy write ARN), and the token must
    land on the request's ``Authorization`` header (Req 6.4, 6.5, 6.6).
    """
    if decoy_write_arn == read_arn:
        decoy_write_arn = decoy_write_arn + "-decoy"

    recorder = _SecretRecorder("read-token-value")
    auth = GitHubReadTokenAuth(_adapter_config(read_arn).adapter)
    request = OutboundRequest()

    with mock.patch("connector.github_provider.get_secret", recorder):
        auth.apply(request)

    assert [secret_name for secret_name, _ in recorder.calls] == [read_arn]
    assert all(source == "secretsmanager" for _, source in recorder.calls)
    assert decoy_write_arn not in {secret_name for secret_name, _ in recorder.calls}
    assert request.headers["Authorization"] == "Bearer read-token-value"
