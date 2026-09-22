"""Deployable AWS Lambda entry point for the E3 dispatcher (issue #415).

This module is the real, deployable handler behind the API Gateway HTTP API
route that starts one execution workflow. It bootstraps the protocol-neutral
:class:`~operations.execute.dispatcher_handler.DispatcherRequestHandler` with its
runtime dependencies:

* a read-only :class:`EvidenceDispatchStore` over the E2 approval store's
  ``load_operation_evidence`` (the durable, workspace-scoped prepared-operation
  view) — the dispatcher never writes a durable record; and
* a bounded ``boto3`` Step Functions client used ONLY to ``start_execution`` the
  Standard workflow with an input of exactly ``{operation_id}``.

The frozen environment contract is resolved and validated once at import time of
the handler (fail closed), not at module import, so importing this module is
side-effect free and AWS-free. The module exposes ``handler(event, context)``.
No provider write, source-control call, generic API/shell/credential access, or
PassRole occurs here.
"""

from __future__ import annotations

# Standard library
import os
import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

# Local modules
from operations.evidence import OperationEvidence


class EvidenceDispatchStore:
    """Project one E2 OperationEvidence into the minimal dispatch view."""

    def __init__(self, evidence_store: Any) -> None:
        self._evidence_store = evidence_store

    def load_dispatch_view(self, operation_id: str) -> dict[str, Any] | None:
        evidence: OperationEvidence | None = self._evidence_store.load_operation_evidence(operation_id)
        if evidence is None:
            return None
        operation = evidence.operation
        requester = operation.get("requester") if isinstance(operation, dict) else None
        if not isinstance(requester, dict):
            return None
        return {
            "operation_id": operation_id,
            "state": evidence.state,
            "tenant_id": requester.get("tenant_id"),
            "workspace_id": requester.get("workspace_id"),
        }


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def _build_handler() -> Any:
    # Third-party packages
    import boto3
    from botocore.config import Config as BotocoreConfig

    # Local modules
    from operations.approval_store import DynamoDbApprovalStore
    from operations.control.gate_bootstrap import build_kill_switch_gate
    from operations.execute.dispatcher_handler import DispatcherRequestHandler
    from operations.settings import (
        resolve_executor_deployment_settings,
        resolve_kill_switch_extension_settings,
    )

    settings = resolve_executor_deployment_settings()
    obs = settings.observation
    # Build the deployment-wide kill-switch gate (issue #416) when the AppConfig
    # read target is configured. Dispatch is an advise-authority act; the gate's
    # static floor is the deployment mode and it can only de-escalate.
    kill_switch_gate = build_kill_switch_gate(
        extension_settings=resolve_kill_switch_extension_settings(),
        static_authority=obs.mode,
    )
    config = BotocoreConfig(connect_timeout=2.0, read_timeout=5.0, retries={"mode": "adaptive", "max_attempts": 2})
    session = boto3.Session(region_name=_region())
    dynamodb_client = session.client("dynamodb", config=config)
    sfn_client = session.client("stepfunctions", config=config)

    approval_store = DynamoDbApprovalStore(client=dynamodb_client, table_name=obs.table_name)
    return DispatcherRequestHandler(
        store=EvidenceDispatchStore(approval_store),
        step_functions=sfn_client,
        state_machine_arn=settings.state_machine_arn,
        tenant_id=obs.tenant_id,
        workspace_id=obs.workspace_id,
        trusted_audience=obs.trusted_audience,
        admin_group=settings.admin_group,
        kill_switch_gate=kill_switch_gate,
    )


@lru_cache(maxsize=1)
def _handler() -> Any:
    """Resolve settings and build the handler once per container (fail closed)."""
    return _build_handler()


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry: dispatch one execution workflow start."""
    request_handler = _handler()
    started = time.monotonic()
    try:
        response: dict[str, Any] = request_handler.handle(event)
        return response
    finally:
        _ = (time.monotonic() - started) * 1000.0
