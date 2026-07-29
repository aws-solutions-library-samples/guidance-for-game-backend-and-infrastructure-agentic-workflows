#!/usr/bin/env python3
"""Example test: provider factory selection and full-interface implementation.

This is an example (non-property) test covering the provider-factory contract and the
completeness of the GitHub adapter:

- :func:`get_provider` returns a :class:`GitHubProvider` when the configured provider is
  ``"github"`` (Req 9.4), and raises :class:`UnsupportedProviderError` for any other
  provider so the connector stays disabled/read-only (Req 9.6).
- :class:`GitHubProvider` implements *every* abstract operation of the
  :class:`SourceControlProvider` contract — no abstract method is left over, so the class
  is concrete and instantiable (Req 9.2, 9.3).

``get_secret`` is patched so no real Secrets Manager call can occur. Instantiating the
adapter and selecting it via the factory does not touch the credential (that happens
per-operation in ``_auth_headers``), but the patch makes the guarantee explicit.

Validates: Requirements 9.2, 9.3, 9.4
"""

# Standard library
import inspect
from types import SimpleNamespace
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
from connector.github_provider import GitHubProvider
from connector.provider import SourceControlProvider, UnsupportedProviderError
from connector.registry import get_provider

pytestmark = pytest.mark.unit


def _make_config(provider: str) -> SimpleNamespace:
    """Build a lightweight ConnectorConfig-shaped stub for the fields the adapter reads.

    ``GitHubProvider`` only reads ``credential_secret_id``, ``provider_timeout_seconds``,
    and ``provider_base_url``; ``get_provider`` reads ``provider``. A stub keeps the test
    focused on factory selection and interface completeness.
    """
    return SimpleNamespace(
        provider=provider,
        credential_secret_id="scm/github-token",
        provider_timeout_seconds=30,
        provider_base_url=None,
    )


@patch("connector.github_provider.get_secret", return_value="unused-token")
def test_get_provider_returns_github_adapter_for_github(mock_get_secret):
    """A ``github`` config selects a concrete GitHubProvider / SourceControlProvider."""
    provider = get_provider(_make_config("github"))

    assert isinstance(provider, GitHubProvider)
    assert isinstance(provider, SourceControlProvider)
    # Selecting the adapter must not touch Secrets Manager.
    mock_get_secret.assert_not_called()


@pytest.mark.parametrize("unsupported", ["gitlab", "codecommit", "bitbucket", "", "GitHub"])
@patch("connector.github_provider.get_secret", return_value="unused-token")
def test_get_provider_raises_for_unsupported_provider(mock_get_secret, unsupported):
    """Any provider other than the exact string ``github`` is unsupported (Req 9.6)."""
    with pytest.raises(UnsupportedProviderError):
        get_provider(_make_config(unsupported))
    mock_get_secret.assert_not_called()


@patch("connector.github_provider.get_secret", return_value="unused-token")
def test_github_provider_implements_every_abstract_operation(mock_get_secret):
    """GitHubProvider leaves no abstract method unimplemented and is instantiable."""
    # No remaining abstract methods -> the class is concrete.
    assert getattr(GitHubProvider, "__abstractmethods__", frozenset()) == frozenset()

    # Concrete + instantiable without any real Secrets Manager access.
    provider = GitHubProvider(_make_config("github"))
    assert isinstance(provider, SourceControlProvider)
    mock_get_secret.assert_not_called()

    # Every abstract operation declared by the contract is present and callable.
    abstract_ops = {
        name
        for name, _ in inspect.getmembers(SourceControlProvider, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    for op in abstract_ops:
        assert callable(getattr(provider, op)), f"missing operation: {op}"


@patch("connector.github_provider.get_secret", return_value="unused-token")
def test_get_provider_result_exposes_the_full_operation_set(mock_get_secret):
    """The adapter returned by the factory exposes the full read/propose operation set."""
    provider = get_provider(_make_config("github"))
    for op in (
        "get_file",
        "get_files",
        "branch_exists",
        "latest_commit_sha",
        "create_branch",
        "commit_files",
        "open_change_proposal",
    ):
        assert callable(getattr(provider, op)), f"missing operation: {op}"
