"""Disposable validation spikes for the optional operations control plane.

This package holds *disposable* validation harnesses that produce measured
evidence for architecture decisions. Nothing here deploys production
infrastructure, grants provider write permissions, or enables operations.

The E0 latency validation (issue #412) lives in :mod:`e0_latency`. It measures
whether the intended E1 synchronous GameLift observation — three bounded,
read-only provider reads plus representative persistence and canonical
serialization — completes with explicit headroom below the API Gateway HTTP API
integration timeout. See ``docs/adr/0005-persist-operations-and-recover-workflows.md``.

Persistence modes for the harness live in :mod:`e0_persistence`: a deterministic
in-memory sink for unit tests only, and an opt-in real DynamoDB transactional
sink (the only mode acceptable for live ADR evidence).
"""

from __future__ import annotations

# Local modules
from operations.validation.e0_latency import (
    DEFAULT_BUDGET,
    GATEWAY_INTEGRATION_TIMEOUT_S,
    DeadlineExceededError,
    LatencyBudget,
    LatencySummary,
    ObservationOutcome,
    ObservationRunner,
    PartialObservationError,
    percentile_nearest_rank,
    summarize_latencies,
)
from operations.validation.e0_persistence import (
    MODE_DYNAMODB_TRANSACT,
    MODE_IN_MEMORY,
    DynamoDbTransactionalSink,
    InMemoryPersistenceSink,
)

__all__ = [
    "DEFAULT_BUDGET",
    "GATEWAY_INTEGRATION_TIMEOUT_S",
    "MODE_DYNAMODB_TRANSACT",
    "MODE_IN_MEMORY",
    "DeadlineExceededError",
    "DynamoDbTransactionalSink",
    "InMemoryPersistenceSink",
    "LatencyBudget",
    "LatencySummary",
    "ObservationOutcome",
    "ObservationRunner",
    "PartialObservationError",
    "percentile_nearest_rank",
    "summarize_latencies",
]
