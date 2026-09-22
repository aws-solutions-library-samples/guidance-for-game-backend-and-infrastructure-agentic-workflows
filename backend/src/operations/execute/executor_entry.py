"""Deployable AWS Lambda entry point for the E3 executor (issue #415).

This module is the real, deployable handler the Step Functions Standard workflow
invokes to execute one approved capacity operation. The workflow caller is
authenticated by IAM (the executor's execution role trusts only the state
machine role); the *invocation payload* accepts ONLY ``{operation_id}`` and
carries no identity, credential, or executable content. The executor reloads the
prepared operation and its granted approval from the durable store and
re-verifies every server-owned precondition itself.

It bootstraps:

* an :class:`EvidenceExecutionReloadStore` over the E2 approval store's
  ``load_operation_evidence`` to reload the full prepared operation + approval;
* the fenced :class:`~operations.execute.execution_store.DynamoDbExecutionStore`
  for the one-logical-update audit records (TTL only on the lease);
* the normalized single-write
  :class:`~operations.execute.gamelift_adapter.GameLiftExecutionAdapter`;
* the independent :class:`~operations.execution_verifier.ExecutionVerifier` built
  from the code-owned deployment authority context (real playbook hash, executor
  binding, enrolled fleet id/ARN/location, remediate authority); and
* the :class:`~operations.execute.executor_service.ExecutorService`.

The frozen environment contract is resolved and validated once at first
invocation (fail closed). Importing this module is side-effect free and AWS-free.
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

_APPROVED_STATE = "approved"


class EvidenceExecutionReloadStore:
    """Reload the full prepared operation + granted approval + state for execute."""

    def __init__(self, evidence_store: Any) -> None:
        self._evidence_store = evidence_store

    def load_for_execution(self, operation_id: str) -> tuple[dict[str, Any], dict[str, Any], str] | None:
        evidence: OperationEvidence | None = self._evidence_store.load_operation_evidence(operation_id)
        if evidence is None:
            return None
        if not isinstance(evidence.operation, dict) or not isinstance(evidence.approval, dict):
            # Without a stored granted approval the executor cannot proceed.
            return None
        return evidence.operation, evidence.approval, evidence.state


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


class _ExecutorRuntime:
    """The resolved, code-owned executor runtime (services + reload store)."""

    def __init__(self, *, service: Any, reload_store: EvidenceExecutionReloadStore, metrics: Any) -> None:
        self.service = service
        self.reload_store = reload_store
        self.metrics = metrics


def _build_runtime() -> _ExecutorRuntime:
    # Third-party packages
    import boto3
    from botocore.config import Config as BotocoreConfig

    # Local modules
    from operations.approval_store import DynamoDbApprovalStore
    from operations.control.gate_bootstrap import build_kill_switch_gate
    from operations.control.metrics import CloudWatchControlMetrics
    from operations.execute.execution_store import DynamoDbExecutionStore
    from operations.execute.executor_service import ExecutorService
    from operations.execute.gamelift_adapter import GameLiftExecutionAdapter
    from operations.execute.metrics import CloudWatchExecutionMetrics
    from operations.execution_verifier import ExecutionAuthorityContext, ExecutionVerifier
    from operations.playbook_definition import (
        EXECUTOR_BINDING_VERSION,
        EXECUTOR_ID,
        capacity_playbook_hash,
    )
    from operations.settings import (
        resolve_executor_deployment_settings,
        resolve_kill_switch_extension_settings,
    )

    settings = resolve_executor_deployment_settings()
    obs = settings.observation
    if not settings.execute_enabled:
        # Fail closed: a deployment below remediate never builds a writer.
        raise RuntimeError("executor deployment is not enabled for remediate execution")

    config = BotocoreConfig(connect_timeout=2.0, read_timeout=5.0, retries={"mode": "adaptive", "max_attempts": 2})
    session = boto3.Session(region_name=_region())
    dynamodb_client = session.client("dynamodb", config=config)
    gamelift_client = session.client("gamelift", config=config)
    cloudwatch_client = session.client("cloudwatch", config=config)

    approval_store = DynamoDbApprovalStore(client=dynamodb_client, table_name=obs.table_name)
    execution_store = DynamoDbExecutionStore(client=dynamodb_client, table_name=obs.table_name)
    adapter = GameLiftExecutionAdapter(gamelift_client)
    metrics = CloudWatchExecutionMetrics(client=cloudwatch_client, namespace=obs.metric_namespace)
    kill_switch_metrics = CloudWatchControlMetrics(client=cloudwatch_client, namespace=obs.metric_namespace)

    context = ExecutionAuthorityContext(
        deployment_mode=obs.mode,
        capability_maximum=settings.capability_maximum,
        tenant_id=obs.tenant_id,
        workspace_id=obs.workspace_id,
        expected_playbook_hash=capacity_playbook_hash(),
        expected_executor_id=EXECUTOR_ID,
        expected_executor_binding_version=EXECUTOR_BINDING_VERSION,
        enrolled_fleet_id=settings.enrolled_fleet_id,
        enrolled_fleet_arn=settings.enrolled_fleet_arn,
        enrolled_location=settings.enrolled_location,
    )
    verifier = ExecutionVerifier(context=context)
    # Build the deployment-wide kill-switch gate (issue #416) when the AppConfig
    # read target is configured. The executor is the last line before a provider
    # write and re-checks the execute phase immediately before UpdateFleetCapacity,
    # so a switch flipped mid-flight still blocks the write. The gate's static
    # floor is the deployment mode; it can only de-escalate.
    kill_switch_gate = build_kill_switch_gate(
        extension_settings=resolve_kill_switch_extension_settings(),
        static_authority=obs.mode,
        unavailable_callback=lambda: kill_switch_metrics.record("kill_switch.unavailable"),
    )
    service = ExecutorService(
        verifier=verifier,
        adapter=adapter,
        store=execution_store,
        kill_switch_gate=kill_switch_gate,
    )
    return _ExecutorRuntime(
        service=service,
        reload_store=EvidenceExecutionReloadStore(approval_store),
        metrics=metrics,
    )


@lru_cache(maxsize=1)
def _runtime() -> _ExecutorRuntime:
    return _build_runtime()


def _emit(metrics: Any, event: str) -> None:
    try:
        metrics.record(event)
    except Exception:  # noqa: BLE001 - metrics must never break a request
        pass


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Step Functions entry: execute one approved operation (identifier only)."""
    # Local modules
    from operations.execute.executor_service import ExecutionInvocation, ExecutorServiceError

    runtime = _runtime()
    started = time.monotonic()
    try:
        invocation = ExecutionInvocation.from_payload(dict(event) if isinstance(event, Mapping) else event)
        reloaded = runtime.reload_store.load_for_execution(invocation.operation_id)
        if reloaded is None:
            raise ExecutorServiceError("operation is unavailable for execution")
        prepared_operation, approval, state = reloaded
        if state != _APPROVED_STATE:
            raise ExecutorServiceError("operation is not approved for execution")
        request_id = _request_id(context)
        result: dict[str, Any] = runtime.service.execute(
            invocation,
            prepared_operation=prepared_operation,
            approval=approval,
            lease_holder=request_id,
        )
        _emit_outcome(runtime.metrics, result)
        return result
    except ExecutorServiceError:
        _emit(runtime.metrics, "execution.failed")
        # Re-raise so the Step Functions workflow records a failed execution and
        # a human can inspect it; the message is safe (no raw internals).
        raise
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        try:
            runtime.metrics.put_latency_ms(elapsed_ms)
        except Exception:  # noqa: BLE001
            pass


def _emit_outcome(metrics: Any, result: Mapping[str, Any]) -> None:
    outcome = result.get("outcome")
    if outcome == "RECONCILED":
        _emit(metrics, "execution.reconciled")
    elif outcome == "HUMAN_RECONCILIATION_REQUIRED":
        _emit(metrics, "execution.human_reconciliation_required")
    elif outcome == "FAILED":
        _emit(metrics, "execution.failed")
    if result.get("provider_write_issued"):
        _emit(metrics, "execution.provider_write_issued")


def _request_id(context: Any) -> str:
    candidate = getattr(context, "aws_request_id", None)
    if isinstance(candidate, str) and candidate:
        safe = "".join(ch for ch in candidate if ch.isalnum() or ch in "._:-")
        if len(safe) >= 3:
            return f"executor.{safe}"[:128]
    return "executor.invocation"
