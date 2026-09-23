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

E5 bounded-autonomy routing (issue #439)
----------------------------------------
The executor reloads the operation and selects its execution path from the
immutable, server-owned envelope alone
(:func:`~operations.autonomy_execution_selection.select_execution_path`). A v1
``gamelift.capacity-adjustment/1.0`` operation follows the untouched
human-approval path. A v2 ``gamelift.capacity-adjustment/2.0`` autonomous
operation is re-verified by the independent
:class:`~operations.autonomy_execution_verifier.AutonomyExecutionVerifier`
against the freshly reloaded bundle and its VerifiedExecutionPlan is fed into the
existing :meth:`ExecutorService.execute_verified` write core. The autonomous path
always **settles** the in-flight reservation on every terminal/failed handoff
(:func:`execute_autonomous`), releasing the concurrency slot while the store
conservatively retains consumed budget/frequency. No model output authorizes,
alters policy/limits, dispatches, or reaches the provider; the executor holds the
only provider-write credential and remains the sole GameLift writer.
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

# Terminal outcomes that indicate a successful (or already-reconciled) write.
_SUCCESS_OUTCOMES = frozenset({"SUCCEEDED", "RECONCILED"})


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


def _settle_terminal(result: dict[str, Any] | None) -> str:
    """Map a terminal execution result to the reservation settle terminal token."""
    if isinstance(result, Mapping) and result.get("outcome") in _SUCCESS_OUTCOMES:
        return "succeeded"
    return "failed"


def execute_autonomous(
    invocation: Any,
    *,
    bundle: Mapping[str, Any],
    logical_action_id: str,
    verifier: Any,
    service: Any,
    reservation: Any,
    lease_holder: str,
) -> dict[str, Any]:
    """Run the v2 autonomous path over the existing write core, always settling.

    The stable ``logical_action_id`` is computed once by the caller from the
    reloaded, hash-bound operation and threaded through so the settle in the
    ``finally`` always targets the exact in-flight reservation — whether the
    verifier raises, the write core raises, or a terminal result is recorded.

    Order:

    1. Re-verify every precondition against the freshly reloaded bundle
       (:class:`AutonomyExecutionVerifier`), producing a VerifiedExecutionPlan.
    2. Feed the plan into :meth:`ExecutorService.execute_verified`, which runs the
       identical Describe-before-write / write-once / verify / atomic-record
       pipeline and re-checks the separate autonomy switch + reservation ownership
       immediately before the single write via its ``pre_write_hook``.
    3. **Always** settle the in-flight reservation on the terminal/failed handoff,
       releasing the concurrency slot (budget/frequency are retained by the store).

    Any verifier or write-core failure fails closed: no false success is
    fabricated, the in-flight slot is released, and the error propagates so the
    Step Functions workflow records a failed execution.
    """
    # Local modules
    from operations.autonomy_execution_verifier import AutonomyExecutionEvidence

    result: dict[str, Any] | None = None
    try:
        evidence = AutonomyExecutionEvidence(
            policy=dict(bundle["policy"]),
            observation=dict(bundle["observation"]),
            decision=dict(bundle["decision"]),
            operation=dict(bundle["operation"]),
            window_state=dict(bundle["window_state"]),
        )
        plan = verifier.verify(operation_id=invocation.operation_id, evidence=evidence)
        result = service.execute_verified(invocation, plan=plan, lease_holder=lease_holder)
        return result
    finally:
        # Settle the in-flight reservation on EVERY terminal/failed handoff so the
        # single concurrency slot is released. A settle failure must never mask the
        # original outcome/exception, so it is swallowed after a best-effort call.
        try:
            reservation.settle(
                operation_id=invocation.operation_id,
                logical_action_id=logical_action_id,
                terminal=_settle_terminal(result),
            )
        except Exception:  # noqa: BLE001 - a settle failure never masks the handoff outcome
            pass


def _build_autonomy_evidence(bundle: Mapping[str, Any]) -> Any:
    """Build the verifier's evidence bundle from a reloaded v2 bundle mapping."""
    # Local modules
    from operations.autonomy_execution_verifier import AutonomyExecutionEvidence

    return AutonomyExecutionEvidence(
        policy=dict(bundle["policy"]),
        observation=dict(bundle["observation"]),
        decision=dict(bundle["decision"]),
        operation=dict(bundle["operation"]),
        window_state=dict(bundle["window_state"]),
    )


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
    from operations.control.durable_gate import DynamoDbDurableControlGate
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
        durable_control_gate=(
            DynamoDbDurableControlGate(
                client=dynamodb_client,
                table_name=obs.table_name,
            )
            if kill_switch_gate is not None
            else None
        ),
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
