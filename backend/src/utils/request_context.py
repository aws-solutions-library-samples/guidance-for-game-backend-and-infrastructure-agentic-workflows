"""
Request-scoped context propagation.

The specialist tools produced by ``agents.base_specialist.create_specialist_agent``
only receive a ``query: str`` argument, so the authenticated ``Requesting_User``
identity cannot be threaded through the tool-call arguments. Deriving identity
from model/tool arguments would also be spoofable by a prompt-injected model.

This module exposes a request-scoped ``contextvars.ContextVar`` holding the
already-validated ``user_context`` (``user_id``, ``groups``, ``session_id``, ...).
It is set in ``agentcore_main.invoke_agent`` immediately before the orchestrator
runs and reset in a ``finally`` block, so identity is isolated per invocation and
never leaks across requests. Downstream components (for example the Source Control
Connector service layer) read the identity via ``get_request_context`` rather than
from agent/model-supplied input.

``contextvars.ContextVar`` is the correct primitive here: it is isolated
per-invocation and safe under the async/threaded AgentCore runtime, unlike a
module-level global.

Usage:
    from utils.request_context import (
        set_request_context,
        get_request_context,
        reset_request_context,
    )

    token = set_request_context(user_context)
    try:
        ...  # user_context is readable via get_request_context() downstream
    finally:
        reset_request_context(token)
"""

from __future__ import annotations

# Standard library
from contextvars import ContextVar, Token
from typing import Any

# Holds the validated user_context for the current request. Defaults to an empty
# dict so downstream reads are always safe even when no context was set.
_request_context: ContextVar[dict[str, Any]] = ContextVar("gbaw_request_context", default={})


def set_request_context(context: dict[str, Any]) -> Token:
    """
    Set the request-scoped user context.

    Args:
        context: The validated user context (``user_id``, ``groups``,
            ``session_id``, ...). A non-dict value is coerced to an empty dict
            so downstream reads remain safe.

    Returns:
        A ``Token`` that must be passed to ``reset_request_context`` to restore
        the previous value once the request completes.
    """
    if not isinstance(context, dict):
        context = {}
    return _request_context.set(context)


def get_request_context() -> dict[str, Any]:
    """
    Get the request-scoped user context for the current invocation.

    Returns:
        The user context set for this request, or an empty dict when no context
        has been set.
    """
    return _request_context.get()


def reset_request_context(token: Token) -> None:
    """
    Reset the request-scoped user context to its previous value.

    Args:
        token: The ``Token`` returned by ``set_request_context``.
    """
    _request_context.reset(token)


__all__ = [
    "set_request_context",
    "get_request_context",
    "reset_request_context",
]
