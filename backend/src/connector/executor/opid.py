"""Operation-identity derivation — branch, canonical hash, and idempotency (consumes #277).

This module is the single place the executor write path derives the three *distinct*
operation-identity artifacts the design keeps deliberately separate (Component 6, Data
Models → "Branch naming" / "Canonical-hash input"):

- The **provider branch** ``gbaw/<short-operation-id>`` — a bounded, provider-safe projection
  of the ``Operation_ID`` **alone**, obtained through the ``OperationContracts277.branch_name``
  seam. It is *not* content-addressed, so a retry of the same operation derives the same
  branch while identical content under a different id derives a different branch (Req 6.5,
  6.6, 10.5).
- The **canonical hash** — the *binding* hash over the operation's exact content **and**
  context, obtained through the ``OperationContracts277.canonical_hash`` seam. This is what an
  approval binds to; it is intentionally distinct from the retry idempotency token (Req 6.2).
- The **idempotency token** — stable across retries of the *same* operation yet distinct per
  operation, and the **duplicate-content key** — identical for two operations with identical
  content regardless of their operation identity. Both reuse the order-independent,
  content-addressed serialization of :func:`connector.service._idempotency_key` (the baseline
  construction), and the two are kept separate so retry idempotency and duplicate-content
  detection never collapse into one another (Req 6.7, 6.8).

The contract version supported-set check (Req 4.6, 6.9) is also funnelled through the #277
seam here so the preparation service and the executor verify it identically.
"""

from __future__ import annotations

# Standard library
import hashlib
from typing import TYPE_CHECKING, Sequence

# Local modules
from connector.service import _idempotency_key

if TYPE_CHECKING:
    # Local modules
    from connector.executor.models import PreparedOperation
    from connector.executor.seams import OperationContracts277
    from connector.models import ProposedFile

__all__ = [
    "branch_name",
    "canonical_hash",
    "verify_contract_version",
    "duplicate_content_key",
    "idempotency_token",
    "idempotency_token_for",
]


def branch_name(contracts: "OperationContracts277", operation_id: str) -> str:
    """Return the deterministic provider branch ``gbaw/<short-operation-id>`` via the seam.

    Delegates to :meth:`OperationContracts277.branch_name` (owned by #277). The result depends
    only on ``operation_id`` — never on the operation's content — so it is bounded,
    provider-safe, and stable for a retry of the same operation (Req 6.5, 6.6, 10.5).
    """
    return contracts.branch_name(operation_id)


def canonical_hash(contracts: "OperationContracts277", operation: "PreparedOperation") -> str:
    """Return the binding canonical hash for ``operation`` via the #277 seam (Req 6.2).

    Delegates to :meth:`OperationContracts277.canonical_hash`. The hash binds an approval to
    the operation's exact content **and** context; it is deliberately distinct from the retry
    :func:`idempotency_token` below.
    """
    return contracts.canonical_hash(operation)


def verify_contract_version(contracts: "OperationContracts277", version: str) -> bool:
    """Return ``True`` iff ``version`` is in the supported operation-contract set (Req 4.6, 6.9).

    Both the preparation service (when stamping a version) and the executor (before any write)
    funnel through this single check so a version the executor cannot interpret is rejected
    consistently.
    """
    return version in contracts.supported_contract_versions()


def duplicate_content_key(
    *,
    repo: str,
    target_branch: str,
    base_revision: str,
    files: Sequence["ProposedFile"],
) -> str:
    """Return the content-addressed duplicate-content key (Req 6.8).

    Reuses the baseline :func:`connector.service._idempotency_key` order-independent,
    content-addressed serialization with an **empty principal**, so the key is a pure function
    of the effective target and the proposed file bodies: two operations that carry identical
    content against the same target/base share this key regardless of who requested them or
    which ``Operation_ID`` they were assigned. It is used only for duplicate-content detection
    and is intentionally independent of the per-operation retry :func:`idempotency_token`.
    """
    return _idempotency_key(
        repo=repo,
        target_branch=target_branch,
        base_revision=base_revision,
        files=files,
        user_id="",
    )


def idempotency_token(
    *,
    operation_id: str,
    repo: str,
    target_branch: str,
    base_revision: str,
    files: Sequence["ProposedFile"],
) -> str:
    """Return the retry idempotency token for an operation (Req 6.7).

    Derived as ``sha256(operation_id | duplicate_content_key)`` so it is **stable across
    retries** of the same operation (the ``Operation_ID`` and content are fixed) yet
    **distinct per operation** (two operations with byte-identical content but different
    ``Operation_ID`` values yield different tokens). This is the token that correlates the
    ledger attempts of one logical operation; it is kept separate from
    :func:`duplicate_content_key` so retry idempotency and duplicate-content detection never
    conflate (Req 6.7, 6.8).
    """
    content_key = duplicate_content_key(
        repo=repo,
        target_branch=target_branch,
        base_revision=base_revision,
        files=files,
    )
    return hashlib.sha256(f"{operation_id}|{content_key}".encode("utf-8")).hexdigest()


def idempotency_token_for(operation: "PreparedOperation") -> str:
    """Return :func:`idempotency_token` computed from a stored :class:`PreparedOperation`."""
    return idempotency_token(
        operation_id=operation.operation_id,
        repo=operation.target_repo,
        target_branch=operation.target_branch,
        base_revision=operation.base_revision,
        files=operation.files,
    )
