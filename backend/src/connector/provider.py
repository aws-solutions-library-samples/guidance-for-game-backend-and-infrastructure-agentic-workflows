"""
Provider abstraction for the Source Control Connector.

This module defines the common, provider-agnostic contract that every source-control
Provider_Adapter (GitHub, and future GitLab/CodeCommit) implements identically. The
abstraction exposes a *fixed* set of read/propose operations and a set of typed
exceptions that concrete adapters raise so the connector service layer can react
uniformly regardless of the underlying provider.

Design guarantees encoded here:

- Signatures reference only provider-agnostic types (`FileContent`, `FileFetchResult`,
  `ProposedFile`, `ChangeProposalResult` from ``connector.models``) and Python primitives;
  no provider-specific type ever appears in the contract (Req 9.1).
- The abstraction defines a fixed operation set that adapters implement identically, so
  adding a provider requires no change to the agent-facing tools (Req 9.2).
- There is deliberately **no** merge, approve, or close operation. The Connector cannot
  merge or close a Change_Proposal; this is a structural guarantee, not a runtime check
  (Req 2.5, 6.1, 6.2).
- Typed exceptions map provider failure modes to connector-level handling: unavailable
  (Req 10.1), auth/no-retry (Req 10.2), conflict (Req 10.4), and transient/retryable
  (Req 10.5). ``UnsupportedProviderError`` covers selection of a provider with no adapter.

Type annotations are kept lazy (``from __future__ import annotations``) and the model
types are imported only under ``TYPE_CHECKING`` so this module imports cleanly even
before ``connector/models.py`` exists.
"""

from __future__ import annotations

# Standard library
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Local modules
    from connector.models import (
        ChangeProposalResult,
        FileContent,
        FileFetchResult,
        ProposedFile,
    )


class ProviderError(Exception):
    """Base class for all provider-abstraction errors.

    Concrete adapters raise the specific subclasses below; the connector service layer
    catches these to decide retry behavior and to build safe, agent-visible messages.
    """


class ProviderUnavailableError(ProviderError):
    """The Provider is unreachable or did not respond within the configured timeout.

    Maps Requirement 10.1: the Connector treats the Provider as unavailable, surfaces an
    availability error, and makes no branch/PR changes.
    """


class ProviderAuthError(ProviderError):
    """The SCM_Credential was rejected by the Provider as invalid or unauthorized.

    Maps Requirement 10.2: the operation SHALL NOT be retried; the connector surfaces an
    authorization error.
    """


class ProviderConflictError(ProviderError):
    """A branch/ref or merge conflict prevented the operation from completing cleanly.

    Maps Requirement 10.4: the conflict is reported and existing Target_Branch content is
    preserved; no destructive automatic resolution is attempted.
    """


class ProviderTransientError(ProviderError):
    """A transient/temporary Provider failure that can be safely retried.

    Maps Requirement 10.5: connection timeouts, network failures, provider-reported
    temporary unavailability (e.g. HTTP 5xx/429) for operations that can be repeated
    without creating a duplicate Change_Proposal.
    """


class UnsupportedProviderError(ProviderError):
    """The configured Provider has no available Provider_Adapter.

    Maps Requirements 9.6 / 9.5: raised by the provider factory when configuration selects
    a provider that is not implemented (or none at all). Caught at config-load time so the
    Connector remains disabled and retains read-only behavior.
    """


@dataclass
class OutboundRequest:
    """A provider-neutral, mutable description of an outbound request to a Provider.

    A :class:`ProviderAuth` receives an ``OutboundRequest`` and attaches whatever
    credential material the underlying credential model requires — a bearer token in a
    header for a token-based Provider, or a set of SigV4 signature headers for an
    IAM-native Provider — by mutating :attr:`headers` (and, for signing schemes,
    reading :attr:`method`/:attr:`url`/:attr:`params`). The type references only Python
    primitives so no provider-specific vocabulary enters the neutral auth contract
    (Req 9.1, 11.1).
    """

    method: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None


class ProviderAuth(ABC):
    """Provider-neutral credential-acquisition contract owned by each Provider_Adapter.

    Credential acquisition is owned **entirely by the adapter** behind this neutral
    interface, so the Connector_Core issues no credential retrieval of its own and a
    token-based adapter and an IAM-native adapter satisfy the *same* contract (Req 11.1).

    An implementation acquires its credential (e.g. a token from Secrets Manager, or the
    runtime role for SigV4 signing) and attaches it to the outbound provider request. On
    any acquisition failure it raises :class:`ProviderAuthError` so the operation fails
    closed with no retry (the service maps ``ProviderAuthError`` to a safe, no-retry error
    result). Implementations MUST never log or otherwise expose the credential value.
    """

    @abstractmethod
    def apply(self, request: OutboundRequest) -> None:
        """Acquire credentials and attach them to ``request`` (fail-closed).

        Mutates ``request`` in place to carry the credential material. Raises
        :class:`ProviderAuthError` if the credential cannot be acquired, so the calling
        operation is aborted without retry (Req 11.1).
        """
        raise NotImplementedError


class SourceControlProvider(ABC):
    """Common, provider-agnostic source-control contract.

    Every Provider_Adapter implements this fixed operation set identically. All parameters
    and return values use provider-agnostic dataclasses (see ``connector.models``) or
    primitives, so no agent-facing tool ever references a provider-specific type
    (Req 9.1, 9.2).

    The operation set is intentionally limited to reading files and proposing changes
    (create branch, commit files, open change proposal). It defines **no** merge, approve,
    or close operation, structurally guaranteeing the Connector cannot merge or close a
    Change_Proposal (Req 2.5, 6.1, 6.2).
    """

    @abstractmethod
    def get_file(self, repo: str, branch: str, path: str) -> FileContent | None:
        """Return the file at ``path`` on ``branch`` of ``repo``, or ``None`` if absent.

        Read-only. Used to review existing IaC before proposing changes (Req 3.1).
        """
        raise NotImplementedError

    @abstractmethod
    def get_files(self, repo: str, branch: str, paths: list[str]) -> FileFetchResult:
        """Fetch multiple files on ``branch`` of ``repo``.

        Returns a :class:`~connector.models.FileFetchResult` carrying the resolved files
        and the paths that were missing, without creating any proposal (Req 3.1, 3.4).
        """
        raise NotImplementedError

    @abstractmethod
    def branch_exists(self, repo: str, branch: str) -> bool:
        """Return ``True`` if ``branch`` already exists in ``repo``.

        Used to guarantee a unique Proposal_Branch name so an existing branch is never
        overwritten (Req 2.8).
        """
        raise NotImplementedError

    @abstractmethod
    def latest_commit_sha(self, repo: str, branch: str) -> str:
        """Return the SHA of the current head commit on ``branch`` of ``repo``.

        This is the provider-neutral **head-revision resolver**: it returns an opaque
        revision string for the current head of ``branch``. The connector uses it for two
        purposes (Req 3.3, 7.1):

        - on a read, to capture the :class:`~connector.models.FileFetchResult.revision`
          (the Verified_Source_Snapshot the caller actually read), and
        - on a propose, to re-read the target head and verify the caller's ``base_revision``
          still matches it (rejecting a stale snapshot) before basing the Proposal_Branch on
          that verified revision.
        """
        raise NotImplementedError

    @abstractmethod
    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        """Create ``new_branch`` in ``repo`` pointing at ``from_sha``.

        Creates the Proposal_Branch based on the current state of the Target_Branch
        (Req 2.1). Never overwrites an existing branch (Req 2.8).
        """
        raise NotImplementedError

    @abstractmethod
    def commit_files(
        self,
        repo: str,
        branch: str,
        files: list[ProposedFile],
        message: str,
    ) -> str:
        """Commit ``files`` to ``branch`` of ``repo`` with ``message``; return commit SHA.

        Carries the complete set of modified IaC files onto the Proposal_Branch
        (Req 2.3).
        """
        raise NotImplementedError

    @abstractmethod
    def open_change_proposal(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> ChangeProposalResult:
        """Open exactly one Change_Proposal from ``head`` into ``base`` on ``repo``.

        Returns a :class:`~connector.models.ChangeProposalResult` with the proposal
        identifier and URL (Req 2.2, 2.6). The proposal is created unmerged and requires
        human review (Req 6.1).
        """
        raise NotImplementedError

    def find_open_change_proposal(
        self,
        repo: str,
        head: str,
        base: str,
    ) -> ChangeProposalResult | None:
        """Return an existing OPEN Change_Proposal for ``head`` → ``base``, or ``None``.

        This is an **optional, provider-neutral reconciliation query** used by the service
        layer's reconcile-before-retry logic (Req 12.4): after an ambiguous transient
        failure while opening a change proposal, the service consults this operation to
        discover whether the provider already opened one for the same head→base pair, so it
        can return the existing proposal instead of opening a duplicate.

        Unlike the other operations this method is **not abstract**: it has a default
        implementation that returns ``None`` ("cannot determine / none found"). Adapters
        that can query their native change-proposal listing (e.g. the GitHub adapter)
        override it; adapters that cannot degrade safely to "none found", which makes the
        service fall back to a plain retry rather than a duplicate-avoiding short-circuit.
        The method is read-only and safe to repeat.
        """
        return None
