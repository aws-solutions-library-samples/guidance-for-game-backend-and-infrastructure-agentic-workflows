"""Provider-neutral registry for the Source Control Connector.

Owns the mapping from a configured provider *name* to the adapter factory that builds the
concrete :class:`~connector.provider.SourceControlReader` for it. This module is the
dependency-inversion seam that lets the provider-neutral core (``connector.service`` and
``connector.config``) select an adapter WITHOUT importing any concrete adapter module
(Req 4.1, 6.1, 6.2, 6.3):

- Adapters depend on the registry — each adapter self-registers at import time (for
  example ``registry.register("github", GitHubProvider)``) — never the reverse.
- The core depends only on this registry plus the :class:`SourceControlReader`
  abstraction. It never names ``connector.github_provider`` (or any other adapter).
- ``ConnectorConfig.load()`` consults :func:`is_supported` so enablement fails closed when
  the configured provider has no registered adapter (Req 7.1, 7.2).

To stay provider-neutral this module references only the read abstraction.
``SourceControlConfig`` and ``SourceControlReader`` are imported solely for typing under
``TYPE_CHECKING`` to avoid import cycles; the only runtime import is the typed
:class:`~connector.provider.UnsupportedProviderError` that :func:`get_provider` raises.
"""

from __future__ import annotations

# Standard library
from typing import TYPE_CHECKING, Callable

# Local modules
from connector.provider import UnsupportedProviderError

if TYPE_CHECKING:
    # Local modules
    from connector.config import SourceControlConfig
    from connector.provider import SourceControlReader

__all__ = ["ProviderFactory", "register", "is_supported", "get_provider"]

# An adapter factory builds a concrete reader from the composed, validated config. The
# type references only the abstraction/composed config (both TYPE_CHECKING-only).
ProviderFactory = Callable[["SourceControlConfig"], "SourceControlReader"]

# Module-level registry mapping provider name -> adapter factory. Populated by adapters
# self-registering at import time and resolved by is_supported/get_provider.
_REGISTRY: dict[str, ProviderFactory] = {}


def register(provider_name: str, factory: ProviderFactory) -> None:
    """Register ``factory`` as the adapter builder for ``provider_name``.

    Idempotent and last-wins: re-registering the same name replaces the prior factory, so
    importing an adapter module more than once is harmless (Req 6.1).
    """
    _REGISTRY[provider_name] = factory


def is_supported(provider_name: str | None) -> bool:
    """Return ``True`` iff an adapter factory is registered for ``provider_name`` (Req 6.4).

    A ``None`` or unregistered name returns ``False`` so config enablement fails closed when
    the configured provider has no adapter (Req 7.1, 7.2).
    """
    if provider_name is None:
        return False
    return provider_name in _REGISTRY


def get_provider(config: "SourceControlConfig") -> "SourceControlReader":
    """Resolve and build the read adapter for ``config.connector.provider`` via the registry.

    Returns a :class:`~connector.provider.SourceControlReader`. Raises
    :class:`~connector.provider.UnsupportedProviderError` when no adapter factory is
    registered for the configured provider, so a misconfigured provider fails closed rather
    than reaching a concrete adapter (Req 7.2). The composed :class:`SourceControlConfig` is
    passed to the factory so the adapter can read both its own :class:`AdapterConfig` fields
    and the neutral :class:`ConnectorConfig` tuning (e.g. the provider timeout).
    """
    provider_name = config.connector.provider
    factory = _REGISTRY.get(provider_name) if provider_name is not None else None
    if factory is None:
        raise UnsupportedProviderError(provider_name)
    return factory(config)
