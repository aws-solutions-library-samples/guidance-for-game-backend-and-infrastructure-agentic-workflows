"""E2 prepare orchestration: advise -> prepare -> atomic persist (issue #414).

:class:`PrepareOrchestrator` is the trusted composition that turns one untrusted
``POST /operations/prepare`` body into one immutable, idempotent prepared
operation persisted for approval. It ties together the three protocol-neutral E2
services already proven in isolation:

1. :class:`~operations.advice.AdviceService` computes one deterministic
   ``gamelift-capacity-advice`` from the untrusted proposal plus the trusted
   current-state read (the state port is bound by the caller to the
   ``observation_id`` the body names).
2. :class:`~operations.prepare.PrepareService` turns that advice into exactly one
   ``approval_required`` prepared operation or a ``denied`` outcome, binding the
   whole operation with its deterministic ``prepared_hash``.
3. The approval store's atomic ``persist_prepared_operation`` transaction
   materializes an ``approval_required`` operation once, keyed by the workspace +
   idempotency token and fenced on a canonical *intent* fingerprint: a
   byte-identical retry replays the stored operation; a changed intent under the
   same token conflicts. A ``denied`` outcome is returned WITHOUT any persist.

The untrusted body supplies only ``observation_id`` plus the capacity proposal
fields (contract version, capability id, idempotency token, and the fleet/
location/requested triple). Any identity/policy/risk/playbook/hash/executor/
credential field in the body fails closed. Identity comes only from the trusted
:class:`~operations.identity.VerifiedPrincipal`. The orchestrator performs no
provider write and holds no executor credential.
"""

from __future__ import annotations

# Standard library
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Local modules
from operations.advice import (
    AdviceBoundaryError,
    AdviceRequestContext,
    AdviceService,
    AuthorityCeilings,
    CapacityProposalRequest,
)
from operations.approval_store import PersistOutcome, PersistResult
from operations.contracts import CONTRACT_VERSION
from operations.contracts.canonical import canonicalize
from operations.contracts.capacity import CAPABILITY_ID
from operations.identity import VerifiedPrincipal
from operations.prepare import (
    PrepareBoundaryError,
    PreparedDecision,
    PreparedOperation,
    PrepareRequestContext,
    PrepareService,
)

_OBSERVATION_ID_PATTERN = re.compile(r"^obs_[a-z0-9]{26}$")

# The full authority the E2 capacity capability presents to advise/prepare. The
# capability itself is a remediate-class action; the deployment mode and the
# verified principal still cap the effective authority. These are conservative,
# code-owned inputs, never read from the untrusted body.
_TENANT_POLICY = "operate"
_WORKSPACE_POLICY = "operate"
_PRINCIPAL_AUTHORITY = "remediate"
_CAPABILITY_MAXIMUM = "remediate"
_OPERATION_RISK_POLICY = "operate"


class PrepareOrchestratorError(RuntimeError):
    """A bounded, safe failure returned by the prepare orchestrator."""

    def __init__(self, error_code: str, safe_message: str, *, retryable: bool = False) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        self.retryable = retryable
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """The outcome of one prepare request: decision, operation, and persist state."""

    decision: PreparedDecision
    operation: dict[str, Any]
    authorization: dict[str, Any]
    prepared_hash: str
    persisted: bool
    replayed: bool


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


class PrepareOrchestrator:
    """Compose advise -> prepare -> atomic persist for one capacity operation."""

    def __init__(
        self,
        *,
        prepare_service: PrepareService,
        store: Any,
        clock: Callable[[], datetime],
        deployment_mode: str,
        advice_service: AdviceService | None = None,
        advice_service_factory: Callable[[str], AdviceService] | None = None,
        preparation_expiry_s: int = 900,
    ) -> None:
        if advice_service is None and advice_service_factory is None:
            raise ValueError("one of advice_service or advice_service_factory is required")
        self._advice_service = advice_service
        self._advice_service_factory = advice_service_factory
        self._prepare_service = prepare_service
        self._store = store
        self._clock = clock
        self._deployment_mode = deployment_mode
        if preparation_expiry_s <= 0:
            raise ValueError("preparation_expiry_s must be positive")
        self._preparation_expiry = timedelta(seconds=preparation_expiry_s)

    def prepare(
        self,
        body: object,
        requester: VerifiedPrincipal,
        *,
        request_id: str,
        correlation_id: str,
    ) -> PrepareResult:
        """Parse, advise, prepare, and (when approval-required) persist once."""
        observation_id, proposal = self._parse_body(body)
        advice_service = self._resolve_advice_service(observation_id)

        advice = self._advise(advice_service, proposal, requester, request_id=request_id, correlation_id=correlation_id)
        prepared = self._prepare(advice, proposal, requester, request_id=request_id, correlation_id=correlation_id)

        if prepared.decision is PreparedDecision.DENIED:
            # A deterministically denied operation is never persisted.
            return PrepareResult(
                decision=prepared.decision,
                operation=prepared.operation,
                authorization=prepared.authorization,
                prepared_hash=prepared.prepared_hash,
                persisted=False,
                replayed=False,
            )

        fingerprint = _intent_fingerprint(
            workspace_id=requester.workspace_id,
            observation_id=observation_id,
            proposal=proposal,
        )
        outcome = self._persist(prepared, proposal, requester, fingerprint)
        return outcome

    # -- Internals --------------------------------------------------------

    def _parse_body(self, body: object) -> tuple[str, CapacityProposalRequest]:
        if not isinstance(body, dict):
            raise PrepareOrchestratorError("contract_invalid", "prepare request is invalid")
        # The untrusted body is exactly the capacity proposal request PLUS the
        # bound observation_id. Any other key (identity, policy, risk, playbook,
        # hash, executor, credential, current-state) fails closed here.
        allowed = {"request_contract_version", "capability_id", "idempotency_token", "observation_id", "proposal"}
        if set(body) != allowed:
            raise PrepareOrchestratorError("contract_invalid", "prepare request is invalid")
        observation_id = body.get("observation_id")
        if not isinstance(observation_id, str) or not _OBSERVATION_ID_PATTERN.fullmatch(observation_id):
            raise PrepareOrchestratorError("contract_invalid", "prepare request is invalid")
        proposal_payload = {k: v for k, v in body.items() if k != "observation_id"}
        try:
            proposal = CapacityProposalRequest.from_payload(proposal_payload)
        except AdviceBoundaryError as exc:
            raise PrepareOrchestratorError("contract_invalid", "prepare request is invalid") from exc
        return observation_id, proposal

    def _resolve_advice_service(self, observation_id: str) -> AdviceService:
        """Build a per-request advice service bound to the observation id."""
        if self._advice_service_factory is not None:
            return self._advice_service_factory(observation_id)
        assert self._advice_service is not None
        return self._advice_service

    def _advise(
        self,
        advice_service: AdviceService,
        proposal: CapacityProposalRequest,
        requester: VerifiedPrincipal,
        *,
        request_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        context = AdviceRequestContext(
            requester=requester,
            request_id=request_id,
            correlation_id=correlation_id,
            authority_ceilings=AuthorityCeilings(
                tenant_policy=_TENANT_POLICY,
                workspace_policy=_WORKSPACE_POLICY,
                principal_authority=_PRINCIPAL_AUTHORITY,
                capability_maximum=_CAPABILITY_MAXIMUM,
                operation_risk_policy=_OPERATION_RISK_POLICY,
            ),
        )
        try:
            return advice_service.advise(proposal, context)
        except AdviceBoundaryError as exc:
            raise PrepareOrchestratorError(exc.error_code.value, str(exc), retryable=exc.retryable) from exc

    def _prepare(
        self,
        advice: dict[str, Any],
        proposal: CapacityProposalRequest,
        requester: VerifiedPrincipal,
        *,
        request_id: str,
        correlation_id: str,
    ) -> PreparedOperation:
        context = PrepareRequestContext(
            requester=requester,
            request_id=request_id,
            correlation_id=correlation_id,
            deployment_mode=self._deployment_mode,
            tenant_policy=_TENANT_POLICY,
            workspace_policy=_WORKSPACE_POLICY,
            principal_authority=_PRINCIPAL_AUTHORITY,
            capability_maximum=_CAPABILITY_MAXIMUM,
            operation_risk_policy=_OPERATION_RISK_POLICY,
        )
        try:
            return self._prepare_service.prepare(advice, context, idempotency_token=proposal.idempotency_token)
        except PrepareBoundaryError as exc:
            raise PrepareOrchestratorError(exc.error_code.value, str(exc), retryable=exc.retryable) from exc

    def _persist(
        self,
        prepared: PreparedOperation,
        proposal: CapacityProposalRequest,
        requester: VerifiedPrincipal,
        fingerprint: str,
    ) -> PrepareResult:
        now = _utc(self._clock())
        operation_expiry = _parse_ts(prepared.operation["expires_at"])
        commit_not_after = min(operation_expiry, requester.expires_at, now + self._preparation_expiry)
        state_change = _prepared_state_change(prepared)
        ledger_event = _prepared_ledger_event(prepared)

        result: PersistResult = self._store.persist_prepared_operation(
            prepared_operation=prepared.operation,
            prepared_hash=prepared.prepared_hash,
            workspace_id=requester.workspace_id,
            idempotency_token=proposal.idempotency_token,
            idempotency_fingerprint=fingerprint,
            state_change=state_change,
            ledger_event=ledger_event,
            commit_not_after=commit_not_after,
        )
        if result.outcome is PersistOutcome.PERSISTED:
            return _result(prepared, persisted=True, replayed=False)
        if result.outcome is PersistOutcome.REPLAYED:
            # A matching-intent retry returns the IDENTICAL stored prepared
            # operation, not the freshly recomputed one: the prepared_hash binds
            # the per-request correlation, so only the stored bytes are the
            # authoritative idempotent replay.
            return self._load_replayed(result.operation_id, prepared)
        if result.outcome is PersistOutcome.INTENT_CONFLICT:
            raise PrepareOrchestratorError(
                "idempotency_conflict", "a different operation is bound to this idempotency token"
            )
        if result.outcome is PersistOutcome.DEADLINE_EXPIRED:
            raise PrepareOrchestratorError(
                "preparation_expired", "preparation deadline elapsed before the record was committed"
            )
        raise PrepareOrchestratorError(
            "provider_unavailable", "prepared operation could not be persisted", retryable=True
        )

    def _load_replayed(self, operation_id: str | None, prepared: PreparedOperation) -> PrepareResult:
        """Return the stored prepared operation for an idempotent replay."""
        if not isinstance(operation_id, str) or not operation_id:
            raise PrepareOrchestratorError(
                "provider_unavailable", "prepared operation could not be replayed", retryable=True
            )
        stored = self._store.load_for_approval(operation_id)
        if stored is None:
            raise PrepareOrchestratorError(
                "provider_unavailable", "prepared operation could not be replayed", retryable=True
            )
        operation = stored.copy_prepared_operation()
        return PrepareResult(
            decision=prepared.decision,
            operation=operation,
            authorization=prepared.authorization,
            prepared_hash=stored.prepared_operation_hash,
            persisted=True,
            replayed=True,
        )


def _result(prepared: PreparedOperation, *, persisted: bool, replayed: bool) -> PrepareResult:
    return PrepareResult(
        decision=prepared.decision,
        operation=prepared.operation,
        authorization=prepared.authorization,
        prepared_hash=prepared.prepared_hash,
        persisted=persisted,
        replayed=replayed,
    )


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _intent_fingerprint(*, workspace_id: str, observation_id: str, proposal: CapacityProposalRequest) -> str:
    """Canonical SHA-256 fingerprint of the exact prepare intent.

    Binds the trusted workspace, the bound observation revision, the idempotency
    token, the code-selected capability, and the exact target + requested triple.
    It never includes the live clock, so a retry of the same intent reproduces
    the same fingerprint (a replay); any change conflicts.
    """
    components = {
        "workspace_id": workspace_id,
        "observation_id": observation_id,
        "capability_id": CAPABILITY_ID,
        "idempotency_token": proposal.idempotency_token,
        "target": {"fleet_id": proposal.fleet_id, "location": proposal.location},
        "requested": proposal.requested.as_dict(),
    }
    digest = hashlib.sha256(canonicalize(components)).hexdigest()
    return f"sha256:{digest}"


def _prepared_state_change(prepared: PreparedOperation) -> dict[str, Any]:
    operation = prepared.operation
    return {
        "state_contract_version": CONTRACT_VERSION,
        "state_change_id": f"state.prepared.{operation['operation_id']}",
        "operation_id": operation["operation_id"],
        "prepared_operation_hash": prepared.prepared_hash,
        "previous_state": "prepared",
        "new_state": "pending_approval",
        "attempt": 0,
        "reason_code": "APPROVAL_REQUESTED",
        "changed_at": operation["created_at"],
        "actor": {"actor_type": "system", "actor_id": "operations.prepare"},
        "correlation": dict(operation["correlation"]),
    }


def _prepared_ledger_event(prepared: PreparedOperation) -> dict[str, Any]:
    operation = prepared.operation
    # ``sequence`` here is the *ledger-event contract* field, which the
    # ``ledger-event`` schema constrains to ``minimum: 1`` — it is the 1-based
    # domain event ordinal, NOT the store's physical ``LEDGER#<n>`` sort-key
    # index. This first materialization event is stored at the ``LEDGER#0`` key
    # (see ``approval_store``) yet correctly carries contract sequence ``1``; the
    # two counters are deliberately distinct and MUST NOT be conflated. The
    # evidence read reports the physical key index (0, 1, ...), never this field.
    return {
        "ledger_contract_version": CONTRACT_VERSION,
        "event_id": f"event.prepared.{operation['operation_id']}",
        "event_type": "operation.prepared",
        "operation_id": operation["operation_id"],
        "prepared_operation_hash": prepared.prepared_hash,
        "sequence": 1,
        "occurred_at": operation["created_at"],
        "actor": {"actor_type": "system", "actor_id": "operations.prepare"},
        "correlation": dict(operation["correlation"]),
        "payload": {
            "payload_type": "operation_prepared",
            "playbook_hash": operation["playbook"]["playbook_hash"],
            "profile": operation["profile"],
        },
    }
