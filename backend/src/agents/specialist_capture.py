"""Request-scoped capture of specialist tool outputs.

The orchestrator must be able to deterministically re-assemble a multi-specialist
answer from the exact text each specialist returned, rather than trusting the
orchestrator model's own prose (which may fabricate financial values). This
module provides a request-local capture — mirroring the cost-report capture in
``agents.cost_report`` — that records each specialist's finalized output keyed by
a per-request id.

Determinism: capture is a per-request mapping keyed by (normalized) service
name, not a completion-ordered list. ``finish_specialist_capture`` always
returns sections in a **fixed service order** (GameLift, then EKS, then Cost,
then any other services alphabetically), so the composed answer is stable
regardless of which specialist's concurrent tool call finishes first. Duplicate
calls for the same service are resolved **last-write-wins**: the most recent
recorded output for a service replaces any earlier one, and only one section per
service is ever emitted.

Concurrency: Strands runs each tool via ``asyncio.to_thread``, which copies the
active :mod:`contextvars` context into the worker thread. The active capture id
therefore propagates to specialist executions (including nested specialists),
and all shared-state mutation is guarded by a lock, so parallel tool calls and
concurrent requests stay isolated by capture id.
"""

from __future__ import annotations

# Standard library
import threading
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass

# Fixed composition order for known services. Cost is captured too (so callers
# can detect that the cost specialist ran) but the orchestrator sources the
# financial section from the authoritative cost rendering, never this capture.
_COMPOSE_ORDER: tuple[str, ...] = ("gamelift", "eks", "cost")

_active_capture_id: ContextVar[str | None] = ContextVar("specialist_capture_id", default=None)
# capture_id -> {normalized_service_name -> (display_service_name, output)}
_captured_outputs: dict[str, dict[str, tuple[str, str]]] = {}
_capture_lock = threading.RLock()


def _normalize(service_name: str) -> str:
    return service_name.strip().lower()


def _ordered_services(services: set[str]) -> list[str]:
    """Return known services in fixed order, then any unknown services sorted."""
    known = [name for name in _COMPOSE_ORDER if name in services]
    extra = sorted(services - set(_COMPOSE_ORDER))
    return [*known, *extra]


@dataclass(frozen=True)
class SpecialistCapture:
    """Request-scoped handle for capturing specialist outputs."""

    capture_id: str
    token: Token[str | None]


def begin_specialist_capture() -> SpecialistCapture:
    """Start request-scoped capture of specialist outputs."""
    capture_id = uuid.uuid4().hex
    token = _active_capture_id.set(capture_id)
    with _capture_lock:
        _captured_outputs[capture_id] = {}
    return SpecialistCapture(capture_id=capture_id, token=token)


def finish_specialist_capture(capture: SpecialistCapture) -> list[tuple[str, str]]:
    """Finish a capture and return ``(service_name, output)`` pairs in fixed service order.

    Order is deterministic (GameLift, EKS, Cost, then any others alphabetically)
    and independent of concurrent completion timing. Idempotent: a second finish
    for the same handle returns an empty list without raising. The capture id is
    popped first; the ContextVar token is reset only on the first finish, because
    a :class:`~contextvars.Token` may be reset exactly once — resetting it again
    would raise ``RuntimeError``.
    """
    with _capture_lock:
        recorded = _captured_outputs.pop(capture.capture_id, None)
    if recorded is None:
        # Already finished (or never began): the token was reset on the first
        # finish and must not be reset again.
        return []
    _active_capture_id.reset(capture.token)
    return [recorded[name] for name in _ordered_services(set(recorded))]


def record_specialist_output(service_name: str, output: str) -> None:
    """Record one specialist's returned text for the active request, if any.

    Keyed by normalized service name with last-write-wins semantics, so a
    duplicate call for the same service overwrites the earlier output and only
    one section per service is composed. A no-op when no capture is active, so
    specialists remain safe to call outside an orchestrated request (e.g. in
    isolated unit tests).
    """
    capture_id = _active_capture_id.get()
    if not capture_id:
        return
    with _capture_lock:
        bucket = _captured_outputs.get(capture_id)
        if bucket is not None:
            bucket[_normalize(service_name)] = (service_name, output)
