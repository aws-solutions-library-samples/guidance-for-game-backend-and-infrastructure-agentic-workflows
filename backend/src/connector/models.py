"""Provider-agnostic data models for the Source Control Connector (read-only).

These frozen dataclasses form the common vocabulary shared by every layer of the
read-only Connector — the agent-facing tools, the connector service, the provider
abstraction, and each concrete provider adapter. They reference only Python primitives so
that no provider-specific type ever leaks across the abstraction boundary.

Immutability (``frozen=True``) makes each value safe to pass through the read pipeline
without a caller mutating it after a gate has inspected it, and lets the provider layer
return results the service can trust unchanged.

Contents:

- :class:`FileContent` / :class:`FileFetchResult` — the read path: a single file and a
  batch fetch result carrying missing paths and a limit-exceeded flag.

The write-path models (``ProposedFile``, ``ChangeProposalResult``, ``ProposalResult``)
have been removed with the provider-write path (preserved in branch history for the #314
executor).
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass


@dataclass(frozen=True)
class FileContent:
    """A single file read from the source repository."""

    path: str
    content: str


@dataclass(frozen=True)
class FileFetchResult:
    """Result of fetching one or more files from the repository/branch.

    ``missing`` lists paths that could not be found; ``limit_exceeded`` is ``True`` when
    the requested path count exceeded the configured maximum and no provider fetch was
    performed.

    Note (advisory-only contract): this result deliberately carries **no** write-usable
    ``revision``/base-revision field. Head-revision resolution was a read-before-write
    affordance for the removed write path. Should a future read consumer need a freshness
    or audit hint, it may be added as an optional ``observed_revision`` that is advisory
    only and is never accepted by any base-revision/write contract (there is none in
    scope).
    """

    files: tuple[FileContent, ...]
    missing: tuple[str, ...]
    limit_exceeded: bool
