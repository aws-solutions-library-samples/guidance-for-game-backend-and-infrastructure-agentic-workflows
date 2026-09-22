"""Deployable AWS Lambda entry point for the E4 control plane (issue #416).

This module is the real, deployable handler behind the API Gateway HTTP API
routes of the operations control plane:

* ``GET  /operations/capabilities``  — capability discovery
* ``GET  /operations``               — bounded, workspace-scoped list
* ``GET  /operations/{operationId}`` — bounded, workspace-scoped detail
* ``POST /operations/control``       — admin kill-switch control (compare-and-set)

It bootstraps the protocol-neutral :class:`ControlPlaneRouter` with its runtime
dependencies:

* the fail-closed :class:`KillSwitchGate` over the AWS AppConfig Lambda extension
  (localhost transport) — read fresh on every request, no cache/fallback;
* the read-only :class:`OperationsProjectionService` over the E2 approval store's
  workspace catalog Query and evidence load, with an HMAC cursor key;
* the :class:`CapabilityDiscoveryService`;
* the admin :class:`KillSwitchControlService` over the CAS
  :class:`DynamoDbControlAuditStore` and the :class:`AppConfigKillSwitchPublisher`;
* the JWT-bound :class:`ControlReadHandler` and :class:`ControlRequestHandler`.

The frozen environment contract is resolved and validated once at first
invocation (fail closed), not at module import, so importing this module is
side-effect free and AWS-free. It exposes ``handler(event, context)``.
"""

from __future__ import annotations

# Standard library
import os
from collections.abc import Mapping
from functools import lru_cache
from typing import Any


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


class _ControlRuntime:
    def __init__(self, *, router: Any) -> None:
        self.router = router


def _build_runtime() -> _ControlRuntime:
    # Third-party packages
    import boto3
    from botocore.config import Config as BotocoreConfig

    # Local modules
    from operations.approval_store import DynamoDbApprovalStore
    from operations.contracts.control_plane import CAPABILITY_ID
    from operations.control.appconfig_extension import AppConfigExtensionClient
    from operations.control.appconfig_publisher import AppConfigKillSwitchPublisher
    from operations.control.capability_discovery import CapabilityDiscoveryService
    from operations.control.control_audit_store import DynamoDbControlAuditStore
    from operations.control.control_handler import ControlRequestHandler
    from operations.control.control_service import KillSwitchControlService
    from operations.control.kill_switch_gate import KillSwitchGate
    from operations.control.metrics import CloudWatchControlMetrics
    from operations.control.projections import OperationsProjectionService
    from operations.control.read_handler import ControlReadHandler
    from operations.control.router import ControlPlaneRouter
    from operations.settings import load_cursor_signing_key, resolve_control_plane_deployment_settings

    settings = resolve_control_plane_deployment_settings()
    obs = settings.observation
    if not settings.control_enabled:
        # Fail closed: the control plane serves only when GBAW_OPERATIONS_CONTROL_MODE
        # is enabled. This is independent of GBAW_OPERATIONS_MODE, so an operator can
        # keep admin controls available to recover operations while static
        # execution is disabled.
        raise RuntimeError("operations control plane is not enabled (GBAW_OPERATIONS_CONTROL_MODE)")

    config = BotocoreConfig(connect_timeout=2.0, read_timeout=5.0, retries={"mode": "adaptive", "max_attempts": 2})
    session = boto3.Session(region_name=_region())
    dynamodb_client = session.client("dynamodb", config=config)
    appconfig_control = session.client("appconfig", config=config)
    cloudwatch_client = session.client("cloudwatch", config=config)
    metrics = CloudWatchControlMetrics(client=cloudwatch_client, namespace=obs.metric_namespace)

    approval_store = DynamoDbApprovalStore(client=dynamodb_client, table_name=obs.table_name)
    control_audit_store = DynamoDbControlAuditStore(client=dynamodb_client, table_name=obs.table_name)

    extension = AppConfigExtensionClient(
        application=settings.appconfig_application,
        environment=settings.appconfig_environment,
        profile=settings.appconfig_profile,
        port=settings.appconfig_extension_port,
    )
    gate = KillSwitchGate(extension=extension, capability_id=CAPABILITY_ID, static_authority=obs.mode)

    secretsmanager_client = (
        session.client("secretsmanager", config=config) if settings.cursor_signing_key_secret_arn is not None else None
    )
    cursor_signing_key = load_cursor_signing_key(settings, secretsmanager_client=secretsmanager_client)
    projection_service = OperationsProjectionService(
        catalog_store=approval_store,
        evidence_store=approval_store,
        cursor_key=cursor_signing_key.encode("utf-8"),
    )
    discovery_service = CapabilityDiscoveryService(
        kill_switch_gate=gate,
        deployment_mode=obs.mode,
        provisioned=settings.provisioned,
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
        audit_store=control_audit_store,
        publisher=publisher,
        admin_group=settings.admin_group,
        metrics=metrics,
    )

    read_handler = ControlReadHandler(
        projection_service=projection_service,
        discovery_service=discovery_service,
        tenant_id=obs.tenant_id,
        workspace_id=obs.workspace_id,
        trusted_audience=obs.trusted_audience,
        admin_group=settings.admin_group,
        kill_switch_gate=gate,
        metrics=metrics,
    )
    control_handler = ControlRequestHandler(
        control_service=control_service,
        tenant_id=obs.tenant_id,
        workspace_id=obs.workspace_id,
        trusted_audience=obs.trusted_audience,
        admin_group=settings.admin_group,
        kill_switch_gate=gate,
        metrics=metrics,
    )
    router = ControlPlaneRouter(read_handler=read_handler, control_handler=control_handler)
    return _ControlRuntime(router=router)


@lru_cache(maxsize=1)
def _runtime() -> _ControlRuntime:
    """Resolve settings and build the runtime once per container (fail closed)."""
    return _build_runtime()


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry: route one E4 control-plane request."""
    runtime = _runtime()
    response: dict[str, Any] = runtime.router.handle(event)
    return response
