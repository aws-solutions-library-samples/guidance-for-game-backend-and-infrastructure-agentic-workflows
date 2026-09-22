"""Deployable AWS Lambda entry point for the observe phase (issue #413).

This module is the real, deployable handler behind the API Gateway HTTP API.
It bootstraps the protocol-neutral :class:`~operations.observation.ObservationService`
with its runtime dependencies:

* a bounded ``boto3`` GameLift client wrapped by the read-only
  :class:`~operations.observe.gamelift_adapter.GameLiftObservationAdapter`
  (three ``describe_*`` reads, no write surface);
* the :class:`~operations.observation_store.DynamoDbObservationStore` on the
  frozen ``GBAW_OPERATIONS_TABLE_NAME`` table (TTL attribute ``ttl``);
* the :class:`~operations.observe.metrics.CloudWatchObservationMetrics` sink
  publishing ``ObservationFailures``/``ObservationTimeouts``/``StuckOperations``/
  ``ObservationRequestLatency`` under ``GBAW_OPERATIONS_METRIC_NAMESPACE``; and
* the :class:`~operations.observation_handler.ObservationRequestHandler`, which
  dispatches ``POST`` observe and ``GET`` status on the verified JWT caller.

The frozen environment contract is resolved and validated once at import time
(fail closed). The module exposes a module-level ``handler(event, context)`` that
AWS Lambda invokes; it measures request latency and publishes it after each
request. There is no provider-write surface anywhere in this module.
"""

from __future__ import annotations

# Standard library
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
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

# The request-side authority inputs a bare observe deployment presents. Every
# value is at least ``observe``; the deployment mode and the verified principal
# still cap the effective authority at the service. These are conservative
# defaults, not a grant of higher authority.
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
    """A botocore config whose timeouts sit inside the observe budgets."""
    # Third-party packages
    from botocore.config import Config as BotocoreConfig

    per_read = settings.operations.per_read_budget_s
    return BotocoreConfig(
        connect_timeout=min(2.0, max(0.5, per_read / 2.0)),
        read_timeout=per_read,
        retries={"mode": "adaptive", "max_attempts": 2},
    )


def _build_handler(settings: ObservationDeploymentSettings) -> Any:
    # Third-party packages
    import boto3

    # Local modules
    from operations.identity import ApprovalIdentityBoundary

    config = _bounded_config(settings)
    session = boto3.Session(region_name=_region())
    gamelift_client = session.client("gamelift", config=config)
    dynamodb_client = session.client("dynamodb", config=config)
    cloudwatch_client = session.client("cloudwatch", config=config)

    metrics = CloudWatchObservationMetrics(client=cloudwatch_client, namespace=settings.metric_namespace)
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
    observation_handler = ObservationRequestHandler(
        service=service,
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        trusted_audience=settings.trusted_audience,
        capability_id=_CAPABILITY_ID,
        capability_version=_CAPABILITY_VERSION,
        authority_inputs=_DEFAULT_AUTHORITY_INPUTS,
    )

    # The E2 prepare/approval surface is additive and gated on the advise
    # ceiling. When it is disabled the deployable handler is exactly the E1
    # observation handler (byte-for-byte preserved). When it is enabled both
    # surfaces are served through one router behind the single entry point.
    if not settings.e2_enabled:
        setattr(observation_handler, "_metrics_sink", metrics)
        return observation_handler

    # Local modules
    from operations.router import OperationsRequestRouter

    approval_handler = _build_approval_handler(
        settings=settings,
        boundary=boundary,
        dynamodb_client=dynamodb_client,
        cloudwatch_client=cloudwatch_client,
        observation_store=store,
    )
    router = OperationsRequestRouter(
        observation_handler=observation_handler,
        approval_handler=approval_handler,
    )
    setattr(router, "_metrics_sink", metrics)
    return router


# The E2 approval policy identity and playbook binding are code-owned, not
# request-derived. The playbook hash is a stable, deterministic placeholder for
# the (not-yet-executed) capacity playbook; E2 performs no provider write.
_POLICY_ID = "policy.gamelift.capacity"
_POLICY_VERSION = "1"
_PLAYBOOK_HASH = "sha256:" + "0" * 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_approval_handler(
    *,
    settings: ObservationDeploymentSettings,
    boundary: Any,
    dynamodb_client: Any,
    cloudwatch_client: Any,
    observation_store: Any,
) -> Any:
    """Construct the E2 approval handler and its service dependencies."""
    # Local modules
    from operations.advice import AdviceService
    from operations.approval import ApprovalPolicy, ApprovalService
    from operations.approval_handler import ApprovalRequestHandler
    from operations.approval_store import DynamoDbApprovalStore
    from operations.capacity_bounds import DeploymentCapacityBoundsResolver
    from operations.capacity_state import E1ObservationCapacityStatePort
    from operations.contracts.capacity import ACTION, PROFILE
    from operations.decisions import LifecycleDecisionService
    from operations.evidence import E2EvidenceService
    from operations.observe.e2_metrics import CloudWatchApprovalMetrics
    from operations.prepare import CapacityPlaybook, PrepareService
    from operations.prepare_orchestrator import PrepareOrchestrator

    ops = settings.operations
    approval_store = DynamoDbApprovalStore(client=dynamodb_client, table_name=settings.table_name)
    approval_metrics = CloudWatchApprovalMetrics(client=cloudwatch_client, namespace=settings.metric_namespace)

    def _advice_service_factory(observation_id: str) -> AdviceService:
        state_port = E1ObservationCapacityStatePort(status_loader=observation_store, observation_id=observation_id)
        bounds_port = DeploymentCapacityBoundsResolver(
            state_port=state_port,
            floor=0,
            ceiling=1_000_000,
            max_step=1_000_000,
            enrollment_id="enrollment.gamelift.capacity",
            enrollment_version="1",
            policy_id=_POLICY_ID,
            policy_version=_POLICY_VERSION,
        )
        return AdviceService(
            settings=ops,
            identity_boundary=boundary,
            state_port=state_port,
            bounds_port=bounds_port,
            clock=_utcnow,
        )

    playbook = CapacityPlaybook(
        playbook_id="playbook.gamelift-capacity",
        playbook_version="1.0",
        playbook_hash=_PLAYBOOK_HASH,
        profile=PROFILE,
        retry_policy={
            "max_attempts": 3,
            "base_delay_seconds": 2,
            "max_delay_seconds": 60,
            "reconcile_before_retry": True,
        },
        future_executor_binding={
            "executor_id": "executor.gamelift-capacity",
            "executor_binding_version": "1.0",
        },
    )
    prepare_service = PrepareService(
        settings=ops,
        identity_boundary=boundary,
        playbook=playbook,
        clock=_utcnow,
        operation_ttl_seconds=ops.preparation_expiry_s,
    )
    orchestrator = PrepareOrchestrator(
        prepare_service=prepare_service,
        store=approval_store,
        clock=_utcnow,
        deployment_mode=ops.mode,
        advice_service_factory=_advice_service_factory,
        preparation_expiry_s=ops.preparation_expiry_s,
    )

    # A direct JWT-authenticated approver in the trusted audience. Self-approval
    # is denied by default; only an explicit low-risk opt-in admits the requester
    # approving their own low-risk operation.
    low_risk_actions = frozenset({ACTION}) if ops.low_risk_self_approval_enabled else frozenset()
    policy = ApprovalPolicy(
        policy_id=_POLICY_ID,
        policy_version=_POLICY_VERSION,
        approver_scopes=frozenset({settings.trusted_audience}),
        low_risk_self_approval_actions=low_risk_actions,
    )
    approval_service = ApprovalService(identity_boundary=boundary, policy=policy, store=approval_store, clock=_utcnow)
    decision_service = LifecycleDecisionService(
        identity_boundary=boundary, policy=policy, store=approval_store, clock=_utcnow
    )
    evidence_service = E2EvidenceService(store=approval_store, identity_boundary=boundary)

    return ApprovalRequestHandler(
        orchestrator=orchestrator,
        approval_service=approval_service,
        decision_service=decision_service,
        evidence_service=evidence_service,
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        trusted_audience=settings.trusted_audience,
        metrics=approval_metrics,
    )


@lru_cache(maxsize=1)
def _handler() -> Any:
    """Resolve settings and build the handler once per container (fail closed)."""
    settings = resolve_observation_deployment_settings()
    return _build_handler(settings)


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry: dispatch one request and publish request latency."""
    request_handler = _handler()
    started = time.monotonic()
    try:
        response: dict[str, Any] = request_handler.handle(event)
        return response
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        sink = getattr(request_handler, "_metrics_sink", None)
        if sink is not None:
            try:
                sink.put_latency_ms(elapsed_ms)
            except Exception:  # noqa: BLE001 - metrics must never break a request
                pass
