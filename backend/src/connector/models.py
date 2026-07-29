"""Provider-agnostic data models for the Source Control Connector.

These frozen dataclasses form the common vocabulary shared by every layer of the
Connector — the agent-facing tools, the connector service, the provider abstraction,
and each concrete provider adapter. They reference only Python primitives so that no
provider-specific type ever leaks across the abstraction boundary (Req 9.1).

Immutability (``frozen=True``) makes each value safe to pass through the safety
pipeline without a caller mutating it after a gate has inspected it, and lets the
provider layer return results the service can trust unchanged.

Contents:

- :class:`FileContent` / :class:`FileFetchResult` — the read path: a single file and a
  batch fetch result carrying missing paths and a limit-exceeded flag (Req 3.2, 3.4).
- :class:`ProposedFile` — one agent-proposed IaC file plus its declared format.
- :class:`ChangeProposalResult` — the identifier and URL of an opened Change_Proposal
  (Req 2.6).
- :class:`ProposalResult` — the safe, agent-visible outcome of a propose operation;
  its ``message`` never contains secrets.
"""

# Standard library
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FileContent:
    """A single file read from the source repository."""

    path: str
    content: str


@dataclass(frozen=True)
class FileFetchResult:
    """Result of fetching one or more files from the repository/branch.

    ``missing`` lists paths that could not be found without creating any proposal
    (Req 3.4); ``limit_exceeded`` is ``True`` when the requested path count exceeded
    the configured maximum and no provider fetch was performed (Req 3.2).
    """

    files: tuple[FileContent, ...]
    missing: tuple[str, ...]
    limit_exceeded: bool


@dataclass(frozen=True)
class ProposedFile:
    """A single agent-proposed IaC file destined for a Change_Proposal."""

    path: str
    content: str
    iac_format: str  # "cloudformation" | "terraform"


@dataclass(frozen=True)
class ChangeProposalResult:
    """Identifier and URL of an opened Change_Proposal (Req 2.6)."""

    proposal_id: str
    proposal_url: str


@dataclass(frozen=True)
class ProposalResult:
    """Safe, agent-visible outcome of a propose operation.

    ``message`` is safe for the agent to relay and never contains secrets.
    """

    status: str  # "created" | "declined" | "rejected" | "error"
    proposal_id: str | None
    proposal_url: str | None
    message: str
