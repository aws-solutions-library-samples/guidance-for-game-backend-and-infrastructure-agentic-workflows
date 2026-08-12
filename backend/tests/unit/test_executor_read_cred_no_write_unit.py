#!/usr/bin/env python3
"""Adapter-level test: the read credential cannot perform a provider write (SECURITY GATE).

Task 9.8 (design → Testing Strategy → "Read credential cannot write"). On this branch the
executor's :class:`ExecutorWriter` is the *sole* write surface and it is only constructed by
:func:`connector.executor.writer.build_writer`, which first acquires the **write** credential
via the executor role's ``GetSecretValue``. A read-scoped credential (one that cannot yield the
write secret) therefore fails closed: :func:`build_writer` raises ``ProviderAuthError`` and no
``ExecutorWriter`` — and thus no write path — is ever produced. Separately, the writer surface
structurally exposes no merge/approve/close/delete/force-push operation, so even a constructed
writer cannot mutate beyond the branch/commit/unmerged-proposal subset.

Validates: Requirements 9.7, 14.3
"""

# Standard library
import inspect

# Third-party packages
import pytest

# Local modules
from connector.executor.writer import ExecutorWriter, acquire_write_credential, build_writer
from connector.provider import ProviderAuthError
from support.fake_provider import FakeProvider

pytestmark = pytest.mark.unit

_WRITE_SECRET = "arn:aws:secretsmanager:us-west-2:123456789012:secret:scm-write"

# Operations that would let a caller mutate/finalize beyond the executor's allowed subset. The
# write surface must expose NONE of these (Req 11.1, 11.2), so a read path likewise cannot.
_FORBIDDEN_WRITE_OPS = (
    "merge",
    "merge_change_proposal",
    "approve",
    "approve_change_proposal",
    "close",
    "close_change_proposal",
    "delete_branch",
    "delete",
    "force_push",
    "push_force",
)


def _read_scoped_acquirer(secret_arn: str, *, source: str) -> str | None:
    """A read-scoped credential source: it cannot yield the write secret (returns None).

    Models a credential/adapter provisioned for the read path only — it has no access to the
    write secret ARN, so asking for it yields no token.
    """
    return None


def test_build_writer_fails_closed_without_the_write_secret() -> None:
    """A read-scoped credential cannot construct the writer: build_writer raises ProviderAuthError
    and no ExecutorWriter (no write path) is produced (Req 9.7, 14.3)."""
    provider = FakeProvider()
    with pytest.raises(ProviderAuthError):
        build_writer(provider, secret_arn=_WRITE_SECRET, acquirer=_read_scoped_acquirer)
    # No provider mutation was attempted while failing closed.
    assert not provider.created_branches
    assert not provider.commits
    assert not provider.pull_requests


def test_acquire_write_credential_requires_a_non_empty_write_token() -> None:
    """The write path requires the write secret; an empty/absent token fails closed (Req 9.7)."""
    with pytest.raises(ProviderAuthError):
        acquire_write_credential(_WRITE_SECRET, acquirer=lambda arn, *, source: None)
    with pytest.raises(ProviderAuthError):
        acquire_write_credential(_WRITE_SECRET, acquirer=lambda arn, *, source: "")


def test_executor_writer_is_the_sole_write_surface_and_exposes_no_finalizing_ops() -> None:
    """The writer exposes ONLY the branch/commit/unmerged-proposal subset — no merge, approve,
    close, delete, or force-push — so no write path can finalize or destroy state (Req 14.3)."""
    for forbidden in _FORBIDDEN_WRITE_OPS:
        assert not hasattr(ExecutorWriter, forbidden), f"ExecutorWriter unexpectedly exposes {forbidden!r}"

    public_methods = {
        name for name, _ in inspect.getmembers(ExecutorWriter, predicate=inspect.isfunction) if not name.startswith("_")
    }
    assert public_methods == {
        "branch_exists",
        "latest_commit_sha",
        "create_branch",
        "commit_files",
        "open_change_proposal",
        "find_open_change_proposal",
    }, f"unexpected writer surface: {sorted(public_methods)}"


def test_build_writer_produces_a_writer_only_with_a_valid_write_token() -> None:
    """With a valid write token the sole write surface is produced; the credential is required
    to obtain it, confirming ExecutorWriter is gated behind the write secret (Req 14.3)."""
    provider = FakeProvider()
    writer = build_writer(provider, secret_arn=_WRITE_SECRET, acquirer=lambda arn, *, source: "write-token")
    assert isinstance(writer, ExecutorWriter)
