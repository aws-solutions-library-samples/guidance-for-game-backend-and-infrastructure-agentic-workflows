"""Deployable AWS Lambda entry point for E4 scheduled maintenance (#416).

The EventBridge invocation performs two bounded duties:

* expire due prepared operations through the E2 approval store's workspace
  ``Query`` and fenced :class:`LifecycleDecisionService`; and
* refresh a due AppConfig kill-switch deadman window by replaying the exact
  validated authority booleans through the audited CAS control service.

The refresh never invents authority. It records a fixed system actor, advances
one immutable config version, and uses the normal gradual deployment strategy.
An unavailable/invalid document emits the AppConfig rollback-monitor metric and
fails the invocation so EventBridge can retry. The frozen environment contract
is resolved once at first invocation; module import is side-effect free.
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
    def __init__(self, *, sweeper: Any, freshness_service: Any, metrics: Any) -> None:
        self.sweeper = sweeper
        self.freshness_service = freshness_service
        self.metrics = metrics


def _build_runtime() -> _SweeperRuntime:
    # Third-party packages
    import boto3
    from botocore.config import Config as BotocoreConfig

    # Local modules
    from operations.approval import ApprovalPolicy
    from operations.approval_store import DynamoDbApprovalStore
    from operations.control.appconfig_extension import AppConfigExtensionClient
    from operations.control.appconfig_publisher import AppConfigKillSwitchPublisher
    from operations.control.control_audit_store import DynamoDbControlAuditStore
    from operations.control.control_service import KillSwitchControlService
    from operations.control.expiry_sweeper import ExpirySweeper
    from operations.control.freshness_sweeper import KillSwitchFreshnessService
    from operations.control.kill_switch_gate import KillSwitchGate
    from operations.control.metrics import CloudWatchControlMetrics
    from operations.decisions import LifecycleDecisionService
    from operations.identity import ApprovalIdentityBoundary
    from operations.settings import resolve_control_plane_deployment_settings

    settings = resolve_control_plane_deployment_settings()
    obs = settings.observation
    if not settings.control_enabled:
        raise RuntimeError("operations control-plane sweeper is not enabled")

    config = BotocoreConfig(connect_timeout=2.0, read_timeout=5.0, retries={"mode": "adaptive", "max_attempts": 2})
    session = boto3.Session(region_name=_region())
    dynamodb_client = session.client("dynamodb", config=config)
    appconfig_control = session.client("appconfig", config=config)
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

    extension = AppConfigExtensionClient(
        application=settings.appconfig_application,
        environment=settings.appconfig_environment,
        profile=settings.appconfig_profile,
        port=settings.appconfig_extension_port,
    )
    gate = KillSwitchGate(
        extension=extension,
        capability_id="gamelift.capacity-adjustment",
        static_authority=obs.mode,
    )
    publisher = AppConfigKillSwitchPublisher(
        client=appconfig_control,
        application_id=settings.appconfig_application,
        environment_id=settings.appconfig_environment,
        configuration_profile_id=settings.appconfig_profile,
        gradual_strategy_id=settings.appconfig_gradual_strategy_id,
        immediate_strategy_id=settings.appconfig_immediate_strategy_id,
    )
    control_service = KillSwitchControlService(
        audit_store=DynamoDbControlAuditStore(client=dynamodb_client, table_name=obs.table_name),
        publisher=publisher,
        admin_group=settings.admin_group,
        freshness_seconds=settings.kill_switch_freshness_seconds,
        metrics=metrics,
    )
    freshness_service = KillSwitchFreshnessService(
        gate=gate,
        control_service=control_service,
        admin_group=settings.admin_group,
        refresh_before_seconds=settings.kill_switch_refresh_before_seconds,
        clock=_utcnow,
    )
    return _SweeperRuntime(sweeper=sweeper, freshness_service=freshness_service, metrics=metrics)


@lru_cache(maxsize=1)
def _runtime() -> _SweeperRuntime:
    """Resolve settings and build the runtime once per container (fail closed)."""
    return _build_runtime()


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Expire due operations, then refresh the kill-switch deadman window."""
    runtime = _runtime()
    result = runtime.sweeper.run()
    try:
        runtime.metrics.put_expiry_sweep_expired(result.expired)
    except Exception:  # noqa: BLE001 - metrics must never break a sweep
        pass

    try:
        freshness = runtime.freshness_service.run()
    except Exception:
        # This exact metric is the AppConfig environment monitor. A refresh
        # failure must be visible and must fail the invocation so EventBridge
        # retries; no stale document is ever treated as enabled.
        try:
            runtime.metrics.record("kill_switch.unavailable")
        except Exception:  # noqa: BLE001 - preserve the original refresh failure
            pass
        raise

    return {
        "considered": result.considered,
        "expired": result.expired,
        "errors": result.errors,
        "pages": result.pages,
        "freshness_attempted": freshness.attempted,
        "freshness_applied": freshness.applied,
        "freshness_outcome": freshness.outcome,
        "freshness_config_version": freshness.config_version,
    }
