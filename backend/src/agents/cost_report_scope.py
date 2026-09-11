"""Request-scoped authorization binding for cost report snapshots.

A validated cost report snapshot is reusable only within the trusted context
that produced it. To keep a report ID from crossing an authorization boundary
when snapshots are shared across runtime workers, every snapshot is stored under
a one-way SHA-256 hash of its scope. The raw tenant, workspace, and actor
identifiers are never persisted.

The scope is composed of:

* the trusted deployment tenant and workspace (configured at deploy time), and
* the request actor established at the AgentCore boundary.

It is published through a :class:`contextvars.ContextVar` set at the AgentCore
entrypoint so nested specialist tool calls inherit it exactly the way the
existing cost-report capture context is inherited. Code paths that never set a
scope (local development, unit tests) observe a single deterministic "unscoped"
value, so a snapshot created and reused within the same untrusted context still
resolves.
"""

from __future__ import annotations

# Standard library
import hashlib
import json
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class ReportScope:
    """Immutable trusted context a report snapshot is bound to.

    The fields are deliberately limited to the values needed to enforce the
    authorization boundary. No prompt, credential, or conversation content is
    included, and only a hash of these values is ever persisted.
    """

    tenant: str = ""
    workspace: str = ""
    actor: str = ""


# The canonical value observed when no request scope has been established. Both
# creation and reuse in an unscoped context hash to the same value, so local and
# test flows remain deterministic without special-casing.
UNSCOPED = ReportScope()

# Sentinel actor produced when no authenticated identity is supplied. It must
# never be treated as a trusted actor by a shared store.
ANONYMOUS_ACTOR = "anonymous"

_request_scope: ContextVar[ReportScope] = ContextVar("cost_report_request_scope", default=UNSCOPED)


class ScopeAuthorizationError(RuntimeError):
    """The request could not establish a trusted actor for a shared report scope.

    Raised at the AgentCore boundary when shared mode is active and the request
    either lacks a trusted actor or presents a body identity that conflicts with
    the trusted actor. The message is deliberately non-specific and never carries
    a raw identifier so it is safe to surface and to log.
    """


def build_request_scope(actor: str | None, *, tenant: str | None, workspace: str | None) -> ReportScope:
    """Build a scope from the trusted deployment binding and request actor.

    Values are normalized to stripped strings so cosmetic differences (padding,
    ``None``) cannot silently widen or narrow the authorization boundary.
    """
    return ReportScope(
        tenant=(tenant or "").strip(),
        workspace=(workspace or "").strip(),
        actor=(actor or "").strip(),
    )


def set_request_scope(scope: ReportScope) -> Token[ReportScope]:
    """Publish the active request scope; returns a token for :func:`reset_request_scope`."""
    return _request_scope.set(scope)


def reset_request_scope(token: Token[ReportScope]) -> None:
    """Restore the previous request scope using a token from :func:`set_request_scope`."""
    _request_scope.reset(token)


def current_scope() -> ReportScope:
    """Return the scope for the current context, or :data:`UNSCOPED` if unset."""
    return _request_scope.get()


def scope_hash(scope: ReportScope) -> str:
    """Return the stable SHA-256 hex digest identifying a scope.

    The digest is computed over a canonical JSON encoding with sorted keys so it
    is stable across processes and Python versions. Only this digest is stored
    with a snapshot; the raw identifiers never leave the runtime.
    """
    canonical = json.dumps(
        {"tenant": scope.tenant, "workspace": scope.workspace, "actor": scope.actor},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_scope_hash() -> str:
    """Convenience helper returning the hash of the current request scope."""
    return scope_hash(current_scope())


def is_trusted_scope(scope: ReportScope) -> bool:
    """Return whether ``scope`` identifies a concrete, authenticated actor.

    A shared/DynamoDB store must reject the unscoped sentinel, an empty actor,
    and the anonymous actor: none of these bind a report ID to a real
    authorization boundary, so persisting or resolving under them would let a
    report cross tenants or users.
    """
    if scope == UNSCOPED:
        return False
    actor = scope.actor.strip()
    if not actor or actor.casefold() == ANONYMOUS_ACTOR:
        return False
    return True


def resolve_scope_actor(*, trusted_actor: str | None, body_user_id: str | None, shared_mode: bool) -> str:
    """Resolve the actor a report scope binds to at the AgentCore boundary.

    In shared mode the actor is taken *only* from the trusted transport identity
    (the platform-established AgentCore ``runtimeUserId`` header) — never from the
    request body or a caller-supplied custom passthrough header. A trusted actor
    is required, and a body-supplied identity that conflicts with it is rejected
    as an identity-confusion signal.

    In non-shared mode (local development, tests) the trusted actor is preferred
    but the body identity is accepted as a fallback so single-process flows stay
    deterministic.

    Raises:
        ScopeAuthorizationError: in shared mode when no trusted actor is present
            or when the body identity conflicts with the trusted actor.
    """
    normalized_trusted = (trusted_actor or "").strip()
    normalized_body = (body_user_id or "").strip()

    if shared_mode:
        if not normalized_trusted or normalized_trusted.casefold() == ANONYMOUS_ACTOR:
            raise ScopeAuthorizationError("a trusted request actor is required for shared cost report reuse")
        if normalized_body and normalized_body.casefold() != ANONYMOUS_ACTOR and normalized_body != normalized_trusted:
            raise ScopeAuthorizationError("the request actor does not match the authenticated identity")
        return normalized_trusted

    return normalized_trusted or normalized_body
