"""E3 executor service: verify, Describe-before-write, exactly-once (#415).

The executor service is the behavioral core of the E3 execute phase. It ties the
independent precondition :class:`~operations.execution_verifier.ExecutionVerifier`,
the normalized :class:`~operations.execute.gamelift_adapter.GameLiftExecutionAdapter`
(single write: ``UpdateFleetCapacity``), and the fenced
:class:`~operations.execute.execution_store.DynamoDbExecutionStore` together and
enforces the exact safety contract:

#. **Idempotent replay.** Acquire the execution lease keyed by the stable
   ``logical_action_id``. If a terminal result was already recorded, return it
   verbatim — no verify, no Describe, no write. One operation produces at most
   one logical update.
#. **Re-verify everything.** Re-verify every server-owned precondition against
   the reloaded prepared operation and stored approval before touching the
   provider.
#. **Describe before any write.** Read the current capacity. It must equal the
   hash-bound expected state. If it already equals the target, record a
   ``RECONCILED`` success with **no** ``UpdateFleetCapacity`` call. If it differs
   from both the expected state and the target, the world drifted from the
   approved plan: fail closed (``FAILED`` / ``STATE_DRIFT``) with no write.
#. **Write exactly once.** Otherwise issue ``UpdateFleetCapacity`` exactly once.
#. **Never blind-retry.** On a timeout / lost response, Describe again. If the
   fleet now matches the target the lost-response write landed → ``SUCCEEDED``;
   if the outcome cannot be conclusively confirmed, record
   ``HUMAN_RECONCILIATION_REQUIRED`` (``RESULT_INCONCLUSIVE``) — never a blind
   retry.
#. **Bounded post-action verification.** Poll ``DescribeFleetCapacity`` a bounded
   number of times; if the target is never observed, record ``FAILED`` /
   ``VERIFICATION_FAILED``.
#. **Atomic record.** Record the provider intent, result, verification, state
   change, and ledger under lease+generation fencing in one transaction.

Every adapter call the service makes after acquiring the fenced lease —
``describe_capacity`` (the pre-write read, the confirming read after a clear
rejection, and every post-write verification poll) and ``update_capacity`` — is
wrapped so that *no* provider exception class can escape ``execute`` and leave a
held lease with no recorded outcome. Each failure is converted to a bounded,
public-safe typed outcome and a fenced terminal record:

* A failure of the **pre-write** Describe (rejection, timeout, or a malformed
  provider response) records ``FAILED`` with **no** write issued — the world is
  untouched.
* A failure of a Describe *after a write was issued* (a verification poll, or the
  confirming read after a clear rejection) can never be proven safe, so it
  records ``HUMAN_RECONCILIATION_REQUIRED`` (``RESULT_INCONCLUSIVE``) — never a
  silent pass and never a blind retry.

Raw provider text is never surfaced: adapter errors are reduced to the bounded
``failure_reason_code`` domain before anything is recorded or returned.

The invocation accepts ONLY ``operation_id`` — the executor relies on the
IAM-authenticated workflow caller for authorization and never on request content.
Rollback is a separate inverse E2 prepared+approved operation, never automatic.
"""

from __future__ import annotations

# Standard library
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

# Local modules
from operations.contracts.execution import (
    CONTRACT_VERSION,
    OUTCOME_FAILED,
    OUTCOME_HUMAN_RECONCILIATION_REQUIRED,
    OUTCOME_RECONCILED,
    OUTCOME_SUCCEEDED,
    execution_intent_hash,
    validate_execution_contract,
)
from operations.execute.execution_store import ExecutionCommitOutcome, LeaseAcquisition
from operations.execute.gamelift_adapter import ProviderWriteInconclusive, ProviderWriteRejected
from operations.execution_verifier import ExecutionVerificationError, ExecutionVerifier, VerifiedExecutionPlan

# Bounded default number of post-action DescribeFleetCapacity polls.
_DEFAULT_MAX_VERIFY_POLLS = 5


class ExecutorServiceError(RuntimeError):
    """A safe, protocol-neutral executor failure (unverifiable precondition)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("execution could not proceed")


@dataclass(frozen=True, slots=True)
class ExecutionInvocation:
    """Untrusted executor invocation input: identifier only, no content."""

    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.startswith("op_"):
            raise ExecutorServiceError("invocation is invalid")

    @classmethod
    def from_payload(cls, payload: object) -> ExecutionInvocation:
        """Parse the complete untrusted invocation payload; reject any extra field."""
        if not isinstance(payload, dict) or set(payload) != {"operation_id"}:
            raise ExecutorServiceError("invocation is invalid")
        return cls(operation_id=payload["operation_id"])


class ExecutionStorePort(Protocol):
    """Persistence port the executor service depends on."""

    def acquire_execution_lease(
        self, *, operation_id: str, logical_action_id: str, lease_holder: str, lease_not_after: datetime
    ) -> LeaseAcquisition: ...

    def record_execution_result(
        self,
        *,
        operation_id: str,
        logical_action_id: str,
        generation: int,
        expected_state: str,
        new_state: str,
        result: Mapping[str, Any],
    ) -> ExecutionCommitOutcome: ...


class ExecutionAdapterPort(Protocol):
    """The single-write GameLift adapter port."""

    def describe_capacity(self, *, fleet_id: str, location: str) -> dict[str, int]: ...

    def update_capacity(self, *, fleet_id: str, location: str, desired: int, minimum: int, maximum: int) -> None: ...


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ExecutorService:
    """Verify, Describe-before-write, write-at-most-once, verify, record."""

    def __init__(
        self,
        *,
        verifier: ExecutionVerifier,
        adapter: ExecutionAdapterPort,
        store: ExecutionStorePort,
        clock: Callable[[], datetime] = _system_clock,
        max_verify_polls: int = _DEFAULT_MAX_VERIFY_POLLS,
        lease_seconds: int = 30,
    ) -> None:
        if max_verify_polls < 1:
            raise ValueError("max_verify_polls must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._verifier = verifier
        self._adapter = adapter
        self._store = store
        self._clock = clock
        self._max_verify_polls = max_verify_polls
        self._lease_seconds = lease_seconds

    def execute(
        self,
        invocation: ExecutionInvocation,
        *,
        prepared_operation: Mapping[str, Any],
        approval: Mapping[str, Any],
        lease_holder: str,
    ) -> dict[str, Any]:
        """Run one bounded execution attempt and return the recorded result."""
        # 1/2. Verify every precondition before anything else (fail closed).
        try:
            plan = self._verifier.verify(prepared_operation=prepared_operation, approval=approval)
        except ExecutionVerificationError as exc:
            raise ExecutorServiceError(exc.reason.value) from exc

        if prepared_operation["operation_id"] != invocation.operation_id:
            raise ExecutorServiceError("operation identity mismatch")

        logical_action_id = plan.logical_action_id
        # Acquire the fenced lease (or replay a recorded terminal result).
        lease_not_after = self._clock() + timedelta(seconds=self._lease_seconds)
        acquisition = self._store.acquire_execution_lease(
            operation_id=invocation.operation_id,
            logical_action_id=logical_action_id,
            lease_holder=lease_holder,
            lease_not_after=lease_not_after,
        )
        if acquisition.recorded_result is not None:
            return acquisition.recorded_result

        target = dict(plan.intent["parameters"])
        expected = dict(plan.intent["expected_current_capacity"])

        # 3. Describe before any write. A pre-write Describe failure of any class
        # (rejection, timeout/lost response, or malformed provider response) means
        # we never touched the provider: record a bounded FAILED with no write.
        current = self._describe(plan)
        if current is None:
            return self._record(
                plan,
                acquisition,
                outcome=OUTCOME_FAILED,
                write_issued=False,
                observed=expected,
                failure_reason="PROVIDER_ERROR",
                new_state="failed",
            )
        if current == target:
            # Already at target: reconcile with no write.
            return self._record(plan, acquisition, outcome=OUTCOME_RECONCILED, write_issued=False, observed=current)
        if current != expected:
            # World drifted from the hash-bound approved plan: fail closed.
            return self._record(
                plan,
                acquisition,
                outcome=OUTCOME_FAILED,
                write_issued=False,
                observed=current,
                failure_reason="STATE_DRIFT",
                new_state="failed",
            )

        # 4. Issue exactly one UpdateFleetCapacity.
        write_issued = True
        try:
            self._adapter.update_capacity(
                fleet_id=plan.fleet_id,
                location=plan.location,
                desired=target["desired"],
                minimum=target["minimum"],
                maximum=target["maximum"],
            )
        except ProviderWriteInconclusive:
            # 5. Lost response: Describe before any retry; never blind-retry. If
            # the confirming Describe/poll cannot conclusively read the target, the
            # write's effect is unknown → HUMAN_RECONCILIATION_REQUIRED.
            observed, matched = self._poll_until_target(plan, target)
            if matched:
                return self._record(plan, acquisition, outcome=OUTCOME_SUCCEEDED, write_issued=True, observed=observed)
            return self._record(
                plan,
                acquisition,
                outcome=OUTCOME_HUMAN_RECONCILIATION_REQUIRED,
                write_issued=True,
                observed=observed if observed is not None else expected,
                failure_reason="RESULT_INCONCLUSIVE",
                new_state="failed",
            )
        except ProviderWriteRejected as exc:
            # A clear rejection: the write deterministically did not take effect.
            # A single confirming Describe (not a poll) reads the unchanged state;
            # if it unexpectedly already matches the target the write in fact
            # landed, so record success. If that confirming Describe itself fails,
            # the write was still a clear rejection so the world is unchanged;
            # record a bounded provider-error FAILED (no write took effect).
            observed = self._describe(plan)
            if observed == target:
                return self._record(plan, acquisition, outcome=OUTCOME_SUCCEEDED, write_issued=True, observed=observed)
            return self._record(
                plan,
                acquisition,
                outcome=OUTCOME_FAILED,
                write_issued=True,
                observed=observed if observed is not None else expected,
                failure_reason=exc.error_code or "PROVIDER_ERROR",
                new_state="failed",
            )

        # 6. Bounded post-action verification. A write WAS issued; if the
        # verification Describe cannot conclusively observe the target (never
        # reached it, or every poll failed) we cannot prove the write's effect →
        # HUMAN_RECONCILIATION_REQUIRED unless we positively confirmed the target.
        observed, matched = self._poll_until_target(plan, target)
        if matched:
            return self._record(
                plan, acquisition, outcome=OUTCOME_SUCCEEDED, write_issued=write_issued, observed=observed
            )
        if observed is None:
            # Every verification Describe failed: the write's effect is unknown.
            return self._record(
                plan,
                acquisition,
                outcome=OUTCOME_HUMAN_RECONCILIATION_REQUIRED,
                write_issued=write_issued,
                observed=expected,
                failure_reason="RESULT_INCONCLUSIVE",
                new_state="failed",
            )
        return self._record(
            plan,
            acquisition,
            outcome=OUTCOME_FAILED,
            write_issued=write_issued,
            observed=observed,
            failure_reason="VERIFICATION_FAILED",
            new_state="failed",
        )

    # -- Internals -------------------------------------------------------

    def _describe(self, plan: VerifiedExecutionPlan) -> dict[str, int] | None:
        """Describe current capacity, converting every adapter failure to ``None``.

        No provider exception class (``ProviderWriteRejected``,
        ``ProviderWriteInconclusive``, a malformed-response ``ValueError``, or any
        other unexpected error) is allowed to escape: the caller decides the
        bounded outcome from the phase. Raw provider text never surfaces.
        """
        try:
            return self._adapter.describe_capacity(fleet_id=plan.fleet_id, location=plan.location)
        except BaseException:  # noqa: BLE001 - classify to a bounded outcome, never leak
            return None

    def _poll_until_target(
        self, plan: VerifiedExecutionPlan, target: dict[str, int]
    ) -> tuple[dict[str, int] | None, bool]:
        """Poll DescribeFleetCapacity up to the bounded limit for the target.

        Returns ``(observed, matched)``. ``observed`` is ``None`` when *every*
        poll failed to read the provider (the write's effect is unconfirmable);
        otherwise it is the last successfully observed capacity. A describe
        failure never escapes and never counts as a match.
        """
        observed: dict[str, int] | None = None
        for _ in range(self._max_verify_polls):
            read = self._describe(plan)
            if read is None:
                continue
            observed = read
            if observed == target:
                return observed, True
        return observed, False

    def _record(
        self,
        plan: VerifiedExecutionPlan,
        acquisition: LeaseAcquisition,
        *,
        outcome: str,
        write_issued: bool,
        observed: dict[str, int],
        failure_reason: str | None = None,
        new_state: str = "succeeded",
    ) -> dict[str, Any]:
        """Build, validate, and atomically record the terminal execution result."""
        now = self._clock()
        target = dict(plan.intent["parameters"])
        verification = {
            "execution_contract_version": CONTRACT_VERSION,
            "operation_id": plan.intent["operation_id"],
            "logical_action_id": plan.logical_action_id,
            "verified_at": _iso(now),
            "observed_capacity": dict(observed),
            "expected_capacity": target,
            "matches_target": observed == target,
            "attempts_observed": 1,
        }
        result: dict[str, Any] = {
            "execution_contract_version": CONTRACT_VERSION,
            "operation_id": plan.intent["operation_id"],
            "logical_action_id": plan.logical_action_id,
            "intent_hash": execution_intent_hash(plan.intent),
            "outcome": outcome,
            "provider_write_issued": write_issued,
            "verification": verification,
            "recorded_at": _iso(now),
        }
        if failure_reason is not None:
            result["failure_reason_code"] = failure_reason

        # Fail closed on a malformed result rather than persisting garbage.
        validate_execution_contract("gamelift-capacity-execution-result", result)

        commit_state = "succeeded" if outcome in (OUTCOME_SUCCEEDED, OUTCOME_RECONCILED) else new_state
        commit_outcome = self._store.record_execution_result(
            operation_id=plan.intent["operation_id"],
            logical_action_id=plan.logical_action_id,
            generation=acquisition.generation,
            expected_state="dispatched",
            new_state=commit_state,
            result=result,
        )
        if commit_outcome is ExecutionCommitOutcome.PRECONDITION_FAILED:
            # A superseded writer or a state race: do not fabricate success.
            raise ExecutorServiceError("execution result could not be recorded")
        return result
