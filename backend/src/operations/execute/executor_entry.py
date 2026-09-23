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
human-approval path (``service.execute`` with the stored granted approval). A v2
``gamelift.capacity-adjustment/2.0`` autonomous operation is reloaded as its full
immutable bundle from :class:`~operations.autonomy_runtime.store.DynamoDbAutonomyBundleStore`,
re-verified by the independent
:class:`~operations.autonomy_execution_verifier.AutonomyExecutionVerifier`
against the freshly reloaded bundle — full reloaded evidence, current reservation
ownership, a fresh separate autonomy switch, the E4 cached and durable execute
gate, then the immediate pre-write checks — and its VerifiedExecutionPlan is fed
into the existing :meth:`ExecutorService.execute_verified` write core via
:func:`execute_autonomous`, which always **settles** the in-flight reservation on
every terminal/failed handoff. No model output authorizes, alters policy/limits,
dispatches, or reaches the provider; the executor holds the only provider-write
credential and remains the sole GameLift writer.
"""

from __future__ import annotations

# Standard library
import os
import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, cast

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
    generation: int | None = None,
    on_settle_failure: Any = None,
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
    result: dict[str, Any] | None = None
    try:
        evidence = _build_autonomy_evidence(bundle)
        plan = verifier.verify(operation_id=invocation.operation_id, evidence=evidence)
        result = service.execute_verified(invocation, plan=plan, lease_holder=lease_holder)
        return result
    finally:
        # Settle the in-flight reservation on EVERY terminal/failed handoff so the
        # single concurrency slot is released. A settle failure must never mask the
        # original outcome/exception and is NEVER retried here (the durable store
        # already re-reads and fails closed); instead it is surfaced as a
        # metrics-safe signal so a wedged slot is observable and swept later.
        terminal = _settle_terminal(result)
        settle_outcome = _settle_reservation(
            reservation,
            operation_id=invocation.operation_id,
            logical_action_id=logical_action_id,
            terminal=terminal,
            generation=generation,
        )
        if not settle_outcome and on_settle_failure is not None:
            try:
                on_settle_failure()
            except Exception:  # noqa: BLE001 - metrics must never break the handoff
                pass


def _settle_reservation(
    reservation: Any,
    *,
    operation_id: str,
    logical_action_id: str,
    terminal: str,
    generation: int | None,
) -> bool:
    """Best-effort settle that never masks the handoff and never retries.

    Returns ``True`` only when the store reports a released/settled outcome
    (``RESERVED``); a conflict, unavailability, exception, or a store that does
    not report an outcome returns ``False`` so the caller can surface a
    metrics-safe failure. A ``settle`` port that predates the generation fence is
    called without it (no double-settle, no retry loop).
    """
    try:
        if generation is not None:
            try:
                outcome = reservation.settle(
                    operation_id=operation_id,
                    logical_action_id=logical_action_id,
                    terminal=terminal,
                    generation=generation,
                )
            except TypeError:
                outcome = reservation.settle(
                    operation_id=operation_id,
                    logical_action_id=logical_action_id,
                    terminal=terminal,
                )
        else:
            outcome = reservation.settle(
                operation_id=operation_id,
                logical_action_id=logical_action_id,
                terminal=terminal,
            )
    except Exception:  # noqa: BLE001 - a settle failure never masks the handoff outcome
        return False
    return _settle_reported_release(outcome)


def _settle_reported_release(outcome: Any) -> bool:
    """Whether a settle return value indicates a truly released/settled slot."""
    value = getattr(outcome, "outcome", None)
    if value is None:
        # A port that returns nothing (e.g. a fake) is treated as a best-effort
        # success: it raised nothing, so the concurrency slot is considered released.
        return True
    return bool(getattr(value, "value", value) == "reserved")


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


class AutonomyExecutorRuntime:
    """The resolved, code-owned executor runtime (v1 services + optional v2 wiring).

    The v1 human-approval path is always present. The v2 autonomous path is wired
    only when the deployment configures the autonomy control plane (bundle store,
    autonomy verifier, and reservation store); otherwise the v2 pieces are absent
    and a reloaded v2 operation fails closed (nothing to reload/verify), keeping a
    default deployment provider-read-only.
    """

    __slots__ = (
        "service",
        "autonomy_service",
        "reload_store",
        "metrics",
        "bundle_store",
        "autonomy_verifier",
        "reservation",
    )

    def __init__(
        self,
        *,
        service: Any,
        reload_store: EvidenceExecutionReloadStore,
        metrics: Any,
        autonomy_service: Any = None,
        bundle_store: Any = None,
        autonomy_verifier: Any = None,
        reservation: Any = None,
    ) -> None:
        self.service = service
        self.autonomy_service = autonomy_service if autonomy_service is not None else service
        self.reload_store = reload_store
        self.metrics = metrics
        self.bundle_store = bundle_store
        self.autonomy_verifier = autonomy_verifier
        self.reservation = reservation

    @property
    def autonomy_enabled(self) -> bool:
        return self.bundle_store is not None and self.autonomy_verifier is not None and self.reservation is not None


def execute_reloaded(runtime: AutonomyExecutorRuntime, invocation: Any, *, lease_holder: str) -> dict[str, Any]:
    """Reload one operation by id and route it to the v1 or v2 execution path.

    The routing decision is made ONLY from the immutable, server-owned reloaded
    operation envelope (:func:`select_execution_path`), never from model output or
    a request-body field:

    * A v1 operation reloaded with its granted approval runs the UNCHANGED
      ``service.execute`` human-approval path.
    * A v2 operation (no human approval) is reloaded as its full immutable bundle
      and routed through the AutonomyExecutionVerifier + ``execute_verified`` write
      core via :func:`execute_autonomous`.

    Fails closed if the operation cannot be reloaded, is not approved (v1), or its
    envelope does not unambiguously match exactly one known contract.
    """
    # Local modules
    from operations.autonomy_execution_selection import ExecutionPath, SelectionError, select_execution_path
    from operations.execute.executor_service import ExecutorServiceError

    # 1. The v1 human-approval reload (operation + granted approval + state).
    reloaded = runtime.reload_store.load_for_execution(invocation.operation_id)
    if reloaded is not None:
        prepared_operation, approval, state = reloaded
        try:
            path = select_execution_path(prepared_operation)
        except SelectionError as exc:
            raise ExecutorServiceError("operation envelope is not recognized") from exc
        if path is not ExecutionPath.HUMAN_APPROVAL:
            # A v2 operation must never carry a stored human approval.
            raise ExecutorServiceError("autonomous operation must not carry a human approval")
        if state != _APPROVED_STATE:
            raise ExecutorServiceError("operation is not approved for execution")
        v1_result: dict[str, Any] = runtime.service.execute(
            invocation,
            prepared_operation=prepared_operation,
            approval=approval,
            lease_holder=lease_holder,
        )
        return v1_result

    # 2. No v1 approval: attempt the v2 autonomous bundle reload.
    if not runtime.autonomy_enabled:
        raise ExecutorServiceError("operation is unavailable for execution")
    bundle = runtime.bundle_store.load_bundle(invocation.operation_id)
    if bundle is None:
        raise ExecutorServiceError("operation is unavailable for execution")
    operation = bundle.get("operation")
    if not isinstance(operation, Mapping):
        raise ExecutorServiceError("reloaded bundle is malformed")
    try:
        path = select_execution_path(operation)
    except SelectionError as exc:
        raise ExecutorServiceError("operation envelope is not recognized") from exc
    if path is not ExecutionPath.AUTONOMOUS:
        raise ExecutorServiceError("reloaded bundle is not an autonomous operation")

    # The stable logical action id is derived once from the reloaded, hash-bound
    # operation and threaded through so the settle always targets the exact
    # in-flight reservation.
    # Local modules
    from operations.contracts.execution import logical_action_id

    action_id = logical_action_id(operation["operation_id"], operation["prepared_hash"])
    # The reservation was taken by the runtime handler at generation 1; a sweeper
    # reclaim of an expired lease bumps the generation, so settling/ownership at
    # generation 1 correctly fails closed for a reclaimed operation (fencing).
    on_settle_failure = None
    metrics = getattr(runtime, "metrics", None)
    if metrics is not None:

        def on_settle_failure() -> None:  # noqa: E306 - small local closure
            _emit(metrics, "execution.reservation_settle_failed")

    return execute_autonomous(
        invocation,
        bundle=bundle,
        logical_action_id=action_id,
        verifier=runtime.autonomy_verifier,
        service=runtime.autonomy_service,
        reservation=runtime.reservation,
        lease_holder=lease_holder,
        generation=1,
        on_settle_failure=on_settle_failure,
    )


def _build_runtime() -> AutonomyExecutorRuntime:
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
    durable_control_gate = (
        DynamoDbDurableControlGate(client=dynamodb_client, table_name=obs.table_name)
        if kill_switch_gate is not None
        else None
    )

    # The v1 human-approval service (no pre-write hook: v1 behavior is unchanged).
    service = ExecutorService(
        verifier=verifier,
        adapter=adapter,
        store=execution_store,
        kill_switch_gate=kill_switch_gate,
        durable_control_gate=durable_control_gate,
    )

    # The optional v2 autonomous wiring: bundle store, AutonomyExecutionVerifier
    # (with the fresh separate autonomy switch + reservation ownership ports), the
    # reservation store, and an autonomous ExecutorService whose immediate
    # pre-write hook re-checks the separate switch and reservation ownership after
    # the E4 second check and immediately before the single write. Built only when
    # the deployment configures the autonomy control plane; otherwise absent so a
    # default deployment stays provider-read-only.
    bundle_store, autonomy_verifier, reservation_store, autonomy_service = _build_autonomy_wiring(
        settings=settings,
        dynamodb_client=dynamodb_client,
        adapter=adapter,
        execution_store=execution_store,
        kill_switch_gate=kill_switch_gate,
        durable_control_gate=durable_control_gate,
    )

    return AutonomyExecutorRuntime(
        service=service,
        autonomy_service=autonomy_service,
        reload_store=EvidenceExecutionReloadStore(approval_store),
        metrics=metrics,
        bundle_store=bundle_store,
        autonomy_verifier=autonomy_verifier,
        reservation=reservation_store,
    )


def _build_autonomy_wiring(
    *,
    settings: Any,
    dynamodb_client: Any,
    adapter: Any,
    execution_store: Any,
    kill_switch_gate: Any,
    durable_control_gate: Any,
) -> tuple[Any, Any, Any, Any]:
    """Construct the optional v2 autonomous wiring, or ``(None, None, None, None)``.

    Returns the bundle store, AutonomyExecutionVerifier, reservation store, and an
    autonomous ExecutorService whose ``pre_write_hook`` re-checks the separate
    autonomy switch + reservation ownership immediately before the write. Absent
    unless the deployment fully configures the autonomy control plane (fail
    closed), so a default deployment provisions no autonomous writer.
    """
    # Local modules
    from operations.autonomy_execution_verifier import AutonomyExecutionAuthorityContext, AutonomyExecutionVerifier
    from operations.autonomy_runtime.bridges import StoreReservationVerifierPort, SwitchAutonomyPort
    from operations.autonomy_runtime.evaluator_entry import resolve_autonomy_evaluator_settings
    from operations.autonomy_runtime.store import DynamoDbAutonomyBundleStore, DynamoDbReservationStore
    from operations.autonomy_switch import AutonomySwitchGate
    from operations.control.appconfig_extension import AppConfigExtensionClient
    from operations.execute.executor_service import ExecutorService
    from operations.playbook_definition import EXECUTOR_BINDING_VERSION, EXECUTOR_ID

    try:
        autonomy_settings = resolve_autonomy_evaluator_settings()
    except ValueError:
        # Autonomy is default-disabled / not fully configured: no autonomous writer.
        return None, None, None, None

    obs = settings.observation
    table_name = obs.table_name

    reservation_store = DynamoDbReservationStore(client=dynamodb_client, table_name=table_name)
    bundle_store = DynamoDbAutonomyBundleStore(client=dynamodb_client, table_name=table_name)

    autonomy_extension = AppConfigExtensionClient(
        application=autonomy_settings.appconfig_application,
        environment=autonomy_settings.appconfig_environment,
        profile=autonomy_settings.autonomy_switch_profile,
        port=autonomy_settings.appconfig_extension_port,
    )
    switch_gate = AutonomySwitchGate(extension=autonomy_extension)
    switch_port = SwitchAutonomyPort(switch_gate)
    reservation_verifier_port = StoreReservationVerifierPort(reservation_store)

    # The code-owned E5 autonomy authority context (operate authority, enrolled
    # fleet, code-owned policy/playbook/executor identity). The policy id/version/
    # hash are strict, server-owned deployment inputs the operation must bind
    # exactly; they are never taken from the operation, the model, or a request.
    context = AutonomyExecutionAuthorityContext(
        deployment_mode=obs.mode,
        capability_maximum=settings.capability_maximum,
        tenant_id=obs.tenant_id,
        workspace_id=obs.workspace_id,
        expected_policy_id=autonomy_settings.autonomy_policy_id,
        expected_policy_version=autonomy_settings.autonomy_policy_version,
        expected_policy_hash=autonomy_settings.autonomy_policy_hash,
        expected_playbook_hash=_autonomy_playbook_hash(),
        expected_executor_id=EXECUTOR_ID,
        expected_executor_binding_version=EXECUTOR_BINDING_VERSION,
        enrolled_fleet_id=settings.enrolled_fleet_id,
        enrolled_fleet_arn=settings.enrolled_fleet_arn,
        enrolled_location=settings.enrolled_location,
    )
    autonomy_verifier = AutonomyExecutionVerifier(
        context=context,
        autonomy_switch=switch_port,
        reservation=reservation_verifier_port,
    )

    # The autonomous ExecutorService: identical write core with an immediate
    # pre-write hook that re-checks the SEPARATE autonomy switch + reservation
    # ownership after the E4 second check and immediately before the write.
    def _pre_write_hook(plan: Any) -> None:
        switch_port.require_autonomy()
        # Fence the immediate pre-write ownership check on the reserve generation:
        # a reclaimed (expired-lease-swept) operation fails closed here, before the
        # single provider write.
        reservation_verifier_port.require_reservation(
            operation_id=plan.intent["operation_id"],
            logical_action_id=plan.logical_action_id,
            generation=1,
        )

    autonomy_service = ExecutorService(
        verifier=cast("Any", ExecutionVerifierUnused()),
        adapter=adapter,
        store=execution_store,
        kill_switch_gate=kill_switch_gate,
        durable_control_gate=durable_control_gate,
        pre_write_hook=_pre_write_hook,
    )
    return bundle_store, autonomy_verifier, reservation_store, autonomy_service


class ExecutionVerifierUnused:
    """A v1 verifier placeholder for the autonomous service (never consulted).

    The autonomous path only ever calls ``execute_verified`` (which does not use
    the v1 verifier), so the autonomous ``ExecutorService`` is constructed with a
    verifier that fails closed if the v1 ``execute`` entry is ever reached.
    """

    def verify(self, *, prepared_operation: Any, approval: Any) -> Any:
        raise RuntimeError("the autonomous executor service does not run the v1 approval verifier")


def _autonomy_playbook_hash() -> str:
    """Return the code-owned autonomy playbook hash."""
    # Local modules
    from operations.autonomy_playbook_definition import autonomy_playbook_hash

    return autonomy_playbook_hash()


@lru_cache(maxsize=1)
def _runtime() -> AutonomyExecutorRuntime:
    return _build_runtime()


def _emit(metrics: Any, event: str) -> None:
    try:
        metrics.record(event)
    except Exception:  # noqa: BLE001 - metrics must never break a request
        pass


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Step Functions entry: execute one operation (identifier only), v1 or v2."""
    # Local modules
    from operations.execute.executor_service import ExecutionInvocation, ExecutorServiceError

    runtime = _runtime()
    started = time.monotonic()
    try:
        invocation = ExecutionInvocation.from_payload(dict(event) if isinstance(event, Mapping) else event)
        request_id = _request_id(context)
        result: dict[str, Any] = execute_reloaded(runtime, invocation, lease_holder=request_id)
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
