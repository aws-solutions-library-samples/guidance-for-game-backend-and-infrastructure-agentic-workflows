"""E5 bounded-autonomy runtime (issue #439, track A).

A deterministic, default-disabled runtime that consumes server-owned trusted
inputs and produces the frozen issue #438 v2 autonomous decision and prepared
operation, atomically reserves the write's footprint against the policy-bound
rolling window state, and hands a durable Step Functions dispatcher an
identifier-only envelope. It holds no provider-write permission and no executor
credential, accepts no model/request identity/policy/limits/executor input, and
fails closed on any store conflict or unavailability.

This package is additive and self-contained: it adds no AWS infrastructure, no
settings, no AppConfig gates, no executor changes, no provider calls, and no
human-approval fields. The chat runtime remains provider-read-only.
"""

from __future__ import annotations

# Local modules
from operations.autonomy_runtime.dispatch import (
    DispatchEnvelope,
    DispatchOutcome,
    DispatchRefused,
    DispatchResult,
    EmergencyDisablement,
    build_dispatch_envelope,
)
from operations.autonomy_runtime.service import (
    AutonomyRuntimeError,
    AutonomyRuntimeInputs,
    AutonomyRuntimeService,
    PreparedAutonomousOperation,
)
from operations.autonomy_runtime.store import (
    DynamoDbReservationStore,
    InMemoryReservationStore,
    ReservationOutcome,
    ReservationRequest,
    ReservationResult,
    ReservationStore,
    ReservationStoreError,
)

__all__ = [
    # service
    "AutonomyRuntimeError",
    "AutonomyRuntimeInputs",
    "AutonomyRuntimeService",
    "PreparedAutonomousOperation",
    # store / reservation lifecycle
    "DynamoDbReservationStore",
    "InMemoryReservationStore",
    "ReservationOutcome",
    "ReservationRequest",
    "ReservationResult",
    "ReservationStore",
    "ReservationStoreError",
    # dispatch boundary
    "DispatchEnvelope",
    "DispatchOutcome",
    "DispatchRefused",
    "DispatchResult",
    "EmergencyDisablement",
    "build_dispatch_envelope",
]
