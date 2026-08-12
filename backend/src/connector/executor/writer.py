"""Executor write adapter — the reused ``SourceControlProvider`` write subset (Component 7.1).

The executor is the **sole holder of the write credential** (design → Architecture → resource
topology; Req 4.1, 9.6). This module models that boundary in application code:

- :class:`ExecutorWriter` wraps a concrete :class:`connector.provider.SourceControlProvider`
  and exposes **only** the write subset the baseline propose pipeline uses — ``create_branch``,
  ``commit_files``, ``open_change_proposal``, ``find_open_change_proposal``,
  ``latest_commit_sha``, and ``branch_exists``. It deliberately exposes **no**
  merge/approve/close/delete/force-push operation; that limitation is *structural* (the methods
  do not exist), mirroring the provider abstraction's own structural guarantee (Req 11.1,
  11.2).
- :func:`acquire_write_credential` obtains the provider write credential **only** via the
  executor role's ``GetSecretValue`` (modeled by :func:`utils.secrets.get_secret`). A
  missing/empty credential fails closed as a :class:`ProviderAuthError` with no retry. In the
  real deployment only the executor role holds this grant, and it is not attached at all until
  the #280 security gate passes (gated task 9.3) — nothing in this module attaches IAM.
- :func:`build_writer` is the executor's gate-6 factory: it acquires the write credential
  (fail-closed) and returns an :class:`ExecutorWriter` around the provider.
"""

from __future__ import annotations

# Standard library
from collections.abc import Callable
from typing import TYPE_CHECKING

# Local modules
from connector.provider import ProviderAuthError
from utils.secrets import get_secret

if TYPE_CHECKING:
    # Local modules
    from connector.models import ChangeProposalResult, ProposedFile
    from connector.provider import SourceControlProvider

__all__ = ["ExecutorWriter", "acquire_write_credential", "build_writer", "WRITE_CREDENTIAL_SOURCE"]

# The credential source the executor role reads the write secret from (Secrets Manager).
WRITE_CREDENTIAL_SOURCE = "secretsmanager"

# The credential acquirer signature: ``(secret_id, *, source) -> str | None`` — matches
# :func:`utils.secrets.get_secret`. Injectable so tests can supply a fake acquirer.
CredentialAcquirer = Callable[..., "str | None"]


def acquire_write_credential(secret_arn: str, *, acquirer: CredentialAcquirer = get_secret) -> str:
    """Acquire the provider write credential via the executor role's ``GetSecretValue``.

    Reads the write secret from Secrets Manager (the sole grant belongs to the executor role).
    A missing/empty credential fails closed as :class:`ProviderAuthError` with no retry, so the
    write is aborted before any provider mutation. The returned token is never logged.
    """
    token = acquirer(secret_arn, source=WRITE_CREDENTIAL_SOURCE)
    if not token:
        raise ProviderAuthError("Executor write credential could not be retrieved from Secrets Manager")
    return token


class ExecutorWriter:
    """A write-only facade over a :class:`SourceControlProvider` (no merge/approve/close/...).

    Only the six operations the executor needs are delegated to the wrapped provider. Because
    the destructive/finalizing operations are simply **not defined** here, the executor cannot
    merge, approve, close, delete, or force-push — a structural guarantee, not a runtime check
    (Req 11.1, 11.2).
    """

    def __init__(self, provider: "SourceControlProvider") -> None:
        self._provider = provider

    def branch_exists(self, repo: str, branch: str) -> bool:
        """Return ``True`` if ``branch`` already exists in ``repo`` (reconcile read)."""
        return self._provider.branch_exists(repo, branch)

    def latest_commit_sha(self, repo: str, branch: str) -> str:
        """Return the current head SHA of ``branch`` (base-revision re-verify / reconcile)."""
        return self._provider.latest_commit_sha(repo, branch)

    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        """Create ``new_branch`` at ``from_sha`` (the deterministic ``gbaw/<short-op-id>``)."""
        return self._provider.create_branch(repo, new_branch, from_sha)

    def commit_files(self, repo: str, branch: str, files: "list[ProposedFile]", message: str) -> str:
        """Commit the exact stored file set to ``branch``; return the commit SHA."""
        return self._provider.commit_files(repo, branch, files, message)

    def open_change_proposal(self, repo: str, head: str, base: str, title: str, body: str) -> "ChangeProposalResult":
        """Open exactly one **unmerged** change proposal from ``head`` into ``base``."""
        return self._provider.open_change_proposal(repo, head, base, title, body)

    def find_open_change_proposal(self, repo: str, head: str, base: str) -> "ChangeProposalResult | None":
        """Return an existing open proposal for ``head`` → ``base`` (reconcile query)."""
        return self._provider.find_open_change_proposal(repo, head, base)


def build_writer(
    provider: "SourceControlProvider",
    *,
    secret_arn: str,
    acquirer: CredentialAcquirer = get_secret,
) -> ExecutorWriter:
    """Executor gate-6 factory: acquire the write credential (fail-closed) then wrap ``provider``.

    Acquiring the credential here models the executor role as the sole write-credential holder:
    if the secret cannot be read the call fails closed with :class:`ProviderAuthError` and no
    :class:`ExecutorWriter` (and therefore no write path) is produced.
    """
    acquire_write_credential(secret_arn, acquirer=acquirer)
    return ExecutorWriter(provider)
