"""Identifier-only E5 bounded-autonomy runtime handler (issue #439).

:class:`AutonomyRuntimeHandler` is the single composition point that turns a
tuple of trusted, server-owned inputs into a durable, identifier-only dispatch of
one autonomous capacity write. It is the piece that makes the previously
forgeable/unwired dispatch path safe and reachable, and it holds **no**
provider-write permission and **no** executor credential: it only hands a durable,
authenticated Step Functions dispatcher an ``operation_id``.

Order of operations (each step fails closed):

1. **Prepare.** :class:`~operations.autonomy_runtime.service.AutonomyRuntimeService`
   deterministically assembles the hash-bound v2 decision and prepared operation
   from trusted inputs only. A ``denied`` decision refuses here — no reservation
   is taken and nothing is dispatched.
2. **Reserve.** The write's budget/frequency/concurrency footprint is atomically
   reserved against the policy-bound rolling window state, fenced on the exact
   ``state_revision`` and the single in-flight slot. A conflict or unavailability
   refuses without dispatch.
3. **Gate.** The composite pre-dispatch gate — static deployment mode exactly
   ``operate`` + the separate AppConfig autonomy switch + the E4 cached
   kill-switch dispatch phase + the E4 durable dispatch intent — must all permit
   *before* StartExecution. A denial here releases the in-flight reservation
   (fail closed) and refuses.
4. **Persist.** The policy, canonical observation, decision, operation, bound
   pre-reservation window state, and reservation are persisted durably so the
   executor can reload the exact bundle by ``operation_id``.
5. **Dispatch.** An identifier-only envelope (``operation_id`` alone) is handed to
   the injected ``start_execution`` callable, which starts the durable Step
   Functions Standard workflow. No policy, limits, observation, window, or
   credential crosses this boundary.

Model or request-body input can never authorize, alter policy/limits, dispatch,
or reach the executor: the service, reservation, and gate receive only
server-owned, hash-bound trusted inputs supplied by the caller.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

# Local modules
from operations.autonomy_runtime.service import (
    AutonomyRuntimeInputs,
    AutonomyRuntimeService,
    PreparedAutonomousOperation,
)
from operations.autonomy_runtime.store import ReservationOutcome, ReservationRequest, ReservationStore
from operations.contracts.execution import logical_action_id


class RuntimeDispatchOutcome(str, Enum):
    """The closed outcome set of a runtime dispatch attempt."""

    DISPATCHED = "dispatched"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class RuntimeDispatchResult:
    """The outcome of one runtime dispatch attempt (identifier-only on success)."""

    outcome: RuntimeDispatchOutcome
    operation_id: str | None = None
    reason: str | None = None


class _PreDispatchGate(Protocol):
    def require_pre_dispatch(self) -> None: ...


class BundleStorePort(Protocol):
    """Durable persistence for the reloadable v2 bundle keyed by operation id."""

    def persist_bundle(
        self,
        *,
        operation_id: str,
        policy: dict[str, Any],
        observation: dict[str, Any],
        decision: dict[str, Any],
        operation: dict[str, Any],
        window_state: dict[str, Any],
        reservation: dict[str, Any],
    ) -> None: ...


class AutonomyRuntimeHandler:
    """Compose prepare -> reserve -> gate -> persist -> identifier-only dispatch."""

    __slots__ = ("_service", "_reservation_store", "_bundle_store", "_gate", "_start_execution")

    def __init__(
        self,
        *,
        service: AutonomyRuntimeService,
        reservation_store: ReservationStore,
        bundle_store: BundleStorePort,
        gate: _PreDispatchGate,
        start_execution: Any,
    ) -> None:
        self._service = service
        self._reservation_store = reservation_store
        self._bundle_store = bundle_store
        self._gate = gate
        self._start_execution = start_execution

    def dispatch(self, inputs: AutonomyRuntimeInputs) -> RuntimeDispatchResult:
        """Run the full identifier-only dispatch pipeline, failing closed at every step."""
        # 1. Prepare the deterministic v2 documents. A denied decision refuses
        # before any reservation is taken or anything is dispatched.
        prepared = self._service.prepare(inputs)
        if not prepared.authorized:
            return RuntimeDispatchResult(RuntimeDispatchOutcome.REFUSED, reason="decision_denied")

        operation = prepared.operation
        operation_id = operation["operation_id"]
        action_id = logical_action_id(operation_id, operation["prepared_hash"])

        # 2. Atomically reserve the write's footprint. A conflict or unavailability
        # refuses without dispatch and without persisting a reservation.
        request = self._reservation_request(inputs, prepared, action_id)
        reservation_result = self._reservation_store.reserve(request)
        if reservation_result.outcome is not ReservationOutcome.RESERVED:
            return RuntimeDispatchResult(
                RuntimeDispatchOutcome.REFUSED, reason="reservation_" + reservation_result.outcome.value
            )

        # 3. Composite pre-dispatch gate BEFORE StartExecution. A denial releases
        # the in-flight reservation (fail closed) and refuses.
        try:
            self._gate.require_pre_dispatch()
        except Exception as exc:  # noqa: BLE001 - any gate denial fails closed
            self._release_in_flight(operation_id, action_id)
            return RuntimeDispatchResult(RuntimeDispatchOutcome.REFUSED, reason="gate_denied")

        # 4. Persist the reloadable bundle. A persistence failure releases the
        # in-flight reservation and refuses rather than dispatching a write the
        # executor cannot reload.
        reservation_snapshot = reservation_result.window_state or {}
        try:
            self._bundle_store.persist_bundle(
                operation_id=operation_id,
                policy=dict(inputs.policy),
                observation=dict(inputs.observation),
                decision=dict(prepared.decision),
                operation=dict(operation),
                window_state=dict(inputs.window_state),
                reservation={
                    "operation_id": operation_id,
                    "logical_action_id": action_id,
                    "state_id": inputs.window_state["state_id"],
                    "window_state": dict(reservation_snapshot),
                },
            )
        except Exception as exc:  # noqa: BLE001 - a persist failure fails closed
            self._release_in_flight(operation_id, action_id)
            return RuntimeDispatchResult(RuntimeDispatchOutcome.REFUSED, reason="persist_failed")

        # 5. Identifier-only dispatch: StartExecution with operation_id alone.
        try:
            self._start_execution({"operation_id": operation_id})
        except Exception as exc:  # noqa: BLE001 - a StartExecution failure fails closed
            self._release_in_flight(operation_id, action_id)
            return RuntimeDispatchResult(RuntimeDispatchOutcome.REFUSED, reason="start_execution_failed")

        return RuntimeDispatchResult(RuntimeDispatchOutcome.DISPATCHED, operation_id=operation_id)

    # -- Internals -------------------------------------------------------

    def _reservation_request(
        self, inputs: AutonomyRuntimeInputs, prepared: PreparedAutonomousOperation, action_id: str
    ) -> ReservationRequest:
        policy = inputs.policy
        window = inputs.window_state
        # The prepared "change" is a per-field capacity delta (not a direction
        # enum). The rolling-window anti-oscillation counter tracks the DESIRED
        # capacity direction, so derive it from the desired delta.
        change = prepared.operation["parameters"]["change"]
        desired_delta = change.get("desired", 0)
        if isinstance(desired_delta, bool) or not isinstance(desired_delta, int) or desired_delta == 0:
            direction = "none"
        elif desired_delta > 0:
            direction = "increase"
        else:
            direction = "decrease"
        return ReservationRequest(
            policy_ref={
                "policy_id": policy["policy_id"],
                "policy_version": policy["policy_version"],
                "policy_hash": policy["policy_hash"],
            },
            state_id=window["state_id"],
            expected_revision=int(window["state_revision"]),
            operation_id=prepared.operation["operation_id"],
            logical_action_id=action_id,
            action_micro_usd=int(window["action_micro_usd"]),
            now_epoch_seconds=int(inputs.evaluated_at.timestamp()),
            change_direction=direction,
        )

    def _release_in_flight(self, operation_id: str, action_id: str) -> None:
        """Best-effort release of the in-flight reservation on a failed handoff.

        Budget/frequency are conservatively retained by the store; only the
        concurrency slot is returned. A settle failure never masks the refusal.
        """
        settle = getattr(self._reservation_store, "settle", None)
        if settle is None:
            return
        try:
            settle(operation_id=operation_id, logical_action_id=action_id, terminal="failed")
        except Exception:  # noqa: BLE001 - a settle failure never masks the refusal
            pass
