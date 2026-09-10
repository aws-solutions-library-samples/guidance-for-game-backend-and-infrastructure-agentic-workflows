"""
Provider abstraction for the Source Control Connector (read-only).

This module defines the common, provider-agnostic **read** contract that every
source-control Provider_Adapter (GitHub, and future GitLab/CodeCommit) implements
identically. Per Architecture Update v1.3 the provider-WRITE authority has moved to a
separate operations control plane / isolated executor (issue #314); the chat runtime
ships only the read interface here. The abstraction therefore exposes a *fixed* set of
read operations and a set of typed exceptions that concrete adapters raise so the
connector service layer can react uniformly regardless of the underlying provider.

Design guarantees encoded here (see
``.kiro/specs/source-control-connector-readonly-split/design.md`` → Components):

- Signatures reference only provider-agnostic types (`FileContent`, `FileFetchResult`
  from ``connector.models``) and Python primitives; no provider-specific type ever
  appears in the contract.
- The abstraction defines a fixed **read** operation set (`get_file`, `get_files`) that
  adapters implement identically, so adding a provider requires no change to the
  agent-facing tools.
- There is deliberately **no** write, merge, approve, or close operation, and no
  ``SourceControlWriter`` interface. The read-only posture is a property of the type
  graph, not a runtime guard: the shipped runtime holds no importable, callable, or
  attribute-reachable provider-write operation.
- Typed exceptions map provider failure modes to connector-level handling: unavailable,
  auth/no-retry, and transient/retryable. ``UnsupportedProviderError`` covers selection
  of a provider with no adapter.

Type annotations are kept lazy (``from __future__ import annotations``) and the model
types are imported only under ``TYPE_CHECKING``.
"""

from __future__ import annotations

# Standard library
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Local modules
    from connector.models import FileContent, FileFetchResult


class ProviderError(Exception):
    """Base class for all provider-abstraction errors.

    Concrete adapters raise the specific subclasses below; the connector service layer
    catches these to decide retry behavior and to build safe, agent-visible messages.
    """


class ProviderUnavailableError(ProviderError):
    """The Provider is unreachable or did not respond within the configured timeout.

    The Connector treats the Provider as unavailable and surfaces an availability error;
    the read fails closed with no result.
    """


class ProviderAuthError(ProviderError):
    """The read credential was rejected by the Provider as invalid or unauthorized.

    The operation SHALL NOT be retried; the connector surfaces an authorization error.
    """


class ProviderTransientError(ProviderError):
    """A transient/temporary Provider failure that can be safely retried.

    Connection timeouts, network failures, provider-reported temporary unavailability
    (e.g. HTTP 5xx/429) for read operations that can be repeated safely.
    """


class UnsupportedProviderError(ProviderError):
    """The configured Provider has no available Provider_Adapter.

    Raised by the provider factory when configuration selects a provider that is not
    implemented (or none at all). Caught at config-load time so the Connector remains
    disabled and retains read-only behavior.
    """


@dataclass
class OutboundRequest:
    """A provider-neutral, mutable description of an outbound request to a Provider.

    A :class:`ProviderAuth` receives an ``OutboundRequest`` and attaches whatever
    credential material the underlying credential model requires — a bearer token in a
    header for a token-based Provider, or a set of SigV4 signature headers for an
    IAM-native Provider — by mutating :attr:`headers` (and, for signing schemes,
    reading :attr:`method`/:attr:`url`/:attr:`params`). The type references only Python
    primitives so no provider-specific vocabulary enters the neutral auth contract.
    """

    method: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None


class ProviderAuth(ABC):
    """Provider-neutral credential-acquisition contract owned by each Provider_Adapter.

    Credential acquisition is owned **entirely by the adapter** behind this neutral
    interface, so the Connector_Core issues no credential retrieval of its own and a
    token-based adapter and an IAM-native adapter satisfy the *same* contract.

    An implementation acquires its **read** credential (e.g. a token from Secrets Manager,
    or the runtime role for SigV4 signing) and attaches it to the outbound provider read
    request. On any acquisition failure it raises :class:`ProviderAuthError` so the
    operation fails closed with no retry. Implementations MUST never log or otherwise
    expose the credential value.
    """

    @abstractmethod
    def apply(self, request: OutboundRequest) -> None:
        """Acquire credentials and attach them to ``request`` (fail-closed).

        Mutates ``request`` in place to carry the credential material. Raises
        :class:`ProviderAuthError` if the credential cannot be acquired, so the calling
        operation is aborted without retry.
        """
        raise NotImplementedError


class SourceControlReader(ABC):
    """Common, provider-agnostic source-control **read** contract.

    Every Provider_Adapter implements this fixed read operation set identically. All
    parameters and return values use provider-agnostic dataclasses (see
    ``connector.models``) or primitives, so no agent-facing tool ever references a
    provider-specific type.

    The operation set is intentionally limited to reading files. It defines **no** write,
    merge, approve, or close operation and there is no companion ``SourceControlWriter``
    interface in the shipped package, structurally guaranteeing the chat runtime cannot
    mutate a provider.
    """

    @abstractmethod
    def get_file(self, repo: str, branch: str, path: str) -> FileContent | None:
        """Return the file at ``path`` on ``branch`` of ``repo``, or ``None`` if absent.

        Read-only. Used to review existing IaC content.
        """
        raise NotImplementedError

    @abstractmethod
    def get_files(self, repo: str, branch: str, paths: list[str]) -> FileFetchResult:
        """Fetch multiple files on ``branch`` of ``repo``.

        Returns a :class:`~connector.models.FileFetchResult` carrying the resolved files
        and the paths that were missing.
        """
        raise NotImplementedError
