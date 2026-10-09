"""Deployable AWS Lambda entry point for the observe phase (issue #413).

This module is the real, deployable handler behind the API Gateway HTTP API.
It bootstraps the protocol-neutral :class:`~operations.observation.ObservationService`
with its runtime dependencies:

* a bounded ``boto3`` GameLift client wrapped by the read-only
  :class:`~operations.observe.gamelift_adapter.GameLiftObservationAdapter`
  (three ``describe_*`` reads, no write surface);
* the :class:`~operations.observation_store.DynamoDbObservationStore` on the
  frozen ``GBAW_OPERATIONS_TABLE_NAME`` table;
* the :class:`~operations.observe.metrics.CloudWatchObservationMetrics` sink
  publishing ``ObservationFailures``/``ObservationTimeouts``/``StuckOperations``/
  ``ObservationRequestLatency`` under ``GBAW_OPERATIONS_METRIC_NAMESPACE``; and
* the :class:`~operations.observation_handler.ObservationRequestHandler`, which
  dispatches ``POST`` observe and ``GET`` status on the verified JWT caller.

The frozen environment contract is resolved and validated on the first
invocation (the per-container ``_handler`` cache is populated lazily, fail
closed) and reused for the container's lifetime. The module exposes a
module-level ``handler(event, context)`` that AWS Lambda invokes; it measures
request latency and publishes it through the metrics sink after each request.
There is no provider-write surface anywhere in this module.
"""

from __future__ import annotations

# Standard library
import os
import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

# Local modules
from operations.observation import AuthorityInputs, ObservationService
from operations.observation_handler import ObservationRequestHandler
from operations.observation_store import DynamoDbObservationStore
from operations.observe.gamelift_adapter import GameLiftObservationAdapter
from operations.observe.metrics import CloudWatchObservationMetrics
from operations.settings import (
    ObservationDeploymentSettings,
    resolve_observation_deployment_settings,
    resolve_operations_settings,
)

# The observe capability (playbook) this Lambda serves.
_CAPABILITY_ID = "gamelift.observe-fleet"
_CAPABILITY_VERSION = "1.0"

# The request-side authority inputs a bare observe deployment presents, except
# principal authority, which the handler derives per request from the verified
# access-token groups against the server-owned observer allowlist. These are the
# tenant/workspace/capability/risk ceilings; the deployment mode and the derived
# principal authority still cap the effective authority at the service.
_DEFAULT_AUTHORITY_INPUTS = AuthorityInputs(
    tenant_policy="observe",
    workspace_policy="observe",
    principal_authority="observe",
    capability_maximum="observe",
    risk_policy="observe",
)


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def _bounded_config(settings: ObservationDeploymentSettings) -> Any:
    """A botocore config whose timeouts sit inside the persistence budget.

    The store and metrics clients are the persistence-phase callers, so their
    timeouts derive from the persistence budget rather than the per-read budget,
    and they make a single attempt: the service owns retry/reclaim through its
    monotonic request deadline, so a transparent SDK retry must never let one
    store call outlast the request budget.
    """
    # Third-party packages
    from botocore.config import Config as BotocoreConfig

    persistence = settings.operations.persistence_budget_s
    return BotocoreConfig(
        connect_timeout=min(2.0, max(0.5, persistence / 2.0)),
        read_timeout=persistence,
        retries={"mode": "standard", "max_attempts": 1},
    )


def _read_config(settings: ObservationDeploymentSettings) -> Any:
    """A botocore config whose timeouts sit inside the per-read budget."""
    # Third-party packages
    from botocore.config import Config as BotocoreConfig

    per_read = settings.operations.per_read_budget_s
    return BotocoreConfig(
        connect_timeout=min(2.0, max(0.5, per_read / 2.0)),
        read_timeout=per_read,
        retries={"mode": "standard", "max_attempts": 1},
    )


def _build_handler(
    settings: ObservationDeploymentSettings,
) -> tuple[ObservationRequestHandler, CloudWatchObservationMetrics]:
    # Third-party packages
    import boto3

    # Local modules
    from operations.identity import ApprovalIdentityBoundary

    read_config = _read_config(settings)
    persistence_config = _bounded_config(settings)
    session = boto3.Session(region_name=_region())
    gamelift_client = session.client("gamelift", config=read_config)
    dynamodb_client = session.client("dynamodb", config=persistence_config)
    cloudwatch_client = session.client("cloudwatch", config=persistence_config)

    metrics = CloudWatchObservationMetrics(
        client=cloudwatch_client,
        namespace=settings.metric_namespace,
        total_deadline_s=settings.operations.total_deadline_s,
        min_publish_remaining_s=settings.operations.persistence_budget_s,
    )
    reader = GameLiftObservationAdapter(gamelift_client)
    store = DynamoDbObservationStore(client=dynamodb_client, table_name=settings.table_name)

    # The deployment binds exactly one tenant/workspace/audience and trusts the
    # requester client ids configured for it. The client id is verified by API
    # Gateway before it reaches the handler; the boundary re-checks it.
    boundary = ApprovalIdentityBoundary(
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        requester_client_ids=frozenset({settings.trusted_audience}),
        approver_client_ids=frozenset({settings.trusted_audience}),
        trusted_audiences=frozenset({settings.trusted_audience}),
    )

    service = ObservationService(
        settings=settings.operations,
        identity_boundary=boundary,
        reader=reader,
        store=store,
        metrics=metrics,
    )
    handler = ObservationRequestHandler(
        service=service,
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        trusted_audience=settings.trusted_audience,
        capability_id=_CAPABILITY_ID,
        capability_version=_CAPABILITY_VERSION,
        authority_inputs=_DEFAULT_AUTHORITY_INPUTS,
        observer_groups=settings.operations.observer_groups,
    )
    return handler, metrics


@lru_cache(maxsize=1)
def _handler() -> tuple[ObservationRequestHandler, CloudWatchObservationMetrics]:
    """Resolve settings and build the handler once per container (fail closed)."""
    settings = resolve_observation_deployment_settings()
    return _build_handler(settings)


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry: dispatch one request and publish request latency."""
    request_handler, metrics_sink = _handler()
    started = time.monotonic()
    try:
        return request_handler.handle(event)
    finally:
        elapsed_s = time.monotonic() - started
        # The sink skips the publish when too little of the request budget
        # remains and swallows its own CloudWatch errors; the guard here is a
        # final backstop so publishing latency can never break a request.
        try:
            metrics_sink.publish_latency(elapsed_s=elapsed_s)
        except Exception:  # noqa: BLE001 - metrics must never break a request
            pass
