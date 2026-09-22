"""Deployable AWS Lambda entry point for the E4 expiry sweeper (issue #416).

This module is the real, deployable handler a scheduled EventBridge rule invokes
to periodically expire due prepared operations. It bootstraps the
:class:`ExpirySweeper` over:

* the E2 approval store's bounded workspace catalog ``Query`` (never a ``Scan``)
  to enumerate candidates; and
* the fenced, atomic E2 :class:`LifecycleDecisionService` (``expire_if_due``) so
  each transition keeps the one-terminal-transition conditional/transactional
  guarantee.

The frozen environment contract is resolved and validated once at first
invocation (fail closed), not at module import, so importing this module is
side-effect free and AWS-free. It exposes ``handler(event, context)`` and emits a
bounded, dimensionless expiry-count metric.
"""

from __future__ import annotations

# Standard library
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _SweeperRuntime:
    def __init__(self, *, sweeper: Any, metrics: Any) -> None:
        self.sweeper = sweeper
        self.metrics = metrics


def _build_runtime() -> _SweeperRuntime:
    # Third-party packages
    import boto3
    from botocore.config import Config as BotocoreConfig

    # Local modules
    from operations.approval import ApprovalPolicy
    from operations.approval_store import DynamoDbApprovalStore
    from operations.control.expiry_sweeper import ExpirySweeper
    from operations.control.metrics import CloudWatchControlMetrics
    from operations.decisions import LifecycleDecisionService
    from operations.identity import ApprovalIdentityBoundary
    from operations.settings import resolve_control_plane_deployment_settings

    settings = resolve_control_plane_deployment_settings()
    obs = settings.observation

    config = BotocoreConfig(connect_timeout=2.0, read_timeout=5.0, retries={"mode": "adaptive", "max_attempts": 2})
    session = boto3.Session(region_name=_region())
    dynamodb_client = session.client("dynamodb", config=config)
    cloudwatch_client = session.client("cloudwatch", config=config)

    approval_store = DynamoDbApprovalStore(client=dynamodb_client, table_name=obs.table_name)
    boundary = ApprovalIdentityBoundary(
        tenant_id=obs.tenant_id,
        workspace_id=obs.workspace_id,
        requester_client_ids=frozenset({obs.trusted_audience}),
        approver_client_ids=frozenset({obs.trusted_audience}),
        trusted_audiences=frozenset({obs.trusted_audience}),
    )
    policy = ApprovalPolicy(
        policy_id="operations.capacity-adjustment",
        policy_version="1.0",
        approver_groups=frozenset({settings.admin_group}),
        low_risk_self_approval_actions=frozenset(),
    )
    decision_service = LifecycleDecisionService(
        identity_boundary=boundary, policy=policy, store=approval_store, clock=_utcnow
    )
    metrics = CloudWatchControlMetrics(client=cloudwatch_client, namespace=obs.metric_namespace)
    sweeper = ExpirySweeper(
        catalog_store=approval_store,
        decision_service=decision_service,
        workspaces=[obs.workspace_id],
    )
    return _SweeperRuntime(sweeper=sweeper, metrics=metrics)


@lru_cache(maxsize=1)
def _runtime() -> _SweeperRuntime:
    """Resolve settings and build the runtime once per container (fail closed)."""
    return _build_runtime()


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry: run one bounded expiry sweep and report counts."""
    runtime = _runtime()
    result = runtime.sweeper.run()
    try:
        runtime.metrics.put_expiry_sweep_expired(result.expired)
    except Exception:  # noqa: BLE001 - metrics must never break a sweep
        pass
    return {
        "considered": result.considered,
        "expired": result.expired,
        "errors": result.errors,
        "pages": result.pages,
    }
