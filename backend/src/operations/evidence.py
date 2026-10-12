"""Bounded, workspace-scoped E2 operation evidence service (issue #414).

``E2EvidenceService`` shapes the ``GET /operations/{operationId}`` response for
the E2 prepare/approval lifecycle: a bounded *preview* of the prepared
operation, its current *state*, the *approval* record when present, a bounded
*ledger*, and an identifier-only future-executor *handoff*.

Trust and safety boundary:

* **Workspace-scoped.** The stored operation's requester must satisfy the
  deployment identity boundary and match the verifed caller's workspace; an
  operation owned by another workspace is invisible (``None``).
* **Bounded and leak-free.** The view surfaces only bounded, public-safe fields
  — never a credential, an executor secret, or raw provider payload. The future
  executor handoff is an *identifier only*.
* **Read-only.** The service performs no provider read, holds no credential, and
  issues no write. Its store dependency is a read-only evidence load.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass, field
from typing import Any, Protocol

# Local modules
from operations.contracts import CONTRACT_VERSION
from operations.identity import ApprovalIdentityBoundary, IdentityBoundaryError

# The ledger is bounded to the most recent entries so a long audit trail can
# never produce an unbounded response body.
_MAX_LEDGER_ENTRIES = 20


@dataclass(frozen=True, slots=True)
class OperationEvidence:
    """The raw stored evidence for one operation, loaded by the store."""

    operation: dict[str, Any]
    prepared_hash: str
    state: str
    approval: dict[str, Any] | None = None
    ledger: list[dict[str, Any]] = field(default_factory=list)


class EvidenceStore(Protocol):
    """Read-only port returning the durable evidence for one operation."""

    def load_operation_evidence(self, operation_id: str) -> OperationEvidence | None: ...


class E2EvidenceService:
    """Return bounded, workspace-scoped evidence for one prepared operation."""

    def __init__(self, *, store: EvidenceStore, identity_boundary: ApprovalIdentityBoundary) -> None:
        self._store = store
        self._identity_boundary = identity_boundary

    def load_evidence(self, *, operation_id: str, requester: Any) -> dict[str, Any] | None:
        """Return a bounded evidence view, or ``None`` if unavailable to caller."""
        evidence = self._store.load_operation_evidence(operation_id)
        if evidence is None:
            return None
        operation = evidence.operation
        if not isinstance(operation, dict):
            return None
        requester_identity = operation.get("requester")
        if not isinstance(requester_identity, dict):
            return None

        # Enforce workspace ownership: the stored requester must sit inside this
        # deployment boundary AND match the verified caller's workspace.
        try:
            self._identity_boundary.validate_stored_requester(requester_identity)
        except IdentityBoundaryError:
            return None
        if requester_identity.get("workspace_id") != getattr(requester, "workspace_id", None):
            return None

        return {
            "evidence_contract_version": CONTRACT_VERSION,
            "operation_id": operation.get("operation_id"),
            "state": evidence.state,
            "prepared_hash": evidence.prepared_hash,
            "preview": _preview(operation),
            "approval": _approval_preview(evidence.approval),
            "ledger": _bounded_ledger(evidence.ledger),
            "handoff": _handoff(operation),
        }


def _preview(operation: dict[str, Any]) -> dict[str, Any]:
    """A bounded, public-safe preview of the prepared operation."""
    calculated_risk = operation.get("calculated_risk") or {}
    authority = operation.get("authority") or {}
    parameters = operation.get("parameters") or {}
    return {
        "phase": operation.get("phase"),
        "profile": operation.get("profile"),
        "action": operation.get("action"),
        "provider": operation.get("provider"),
        "created_at": operation.get("created_at"),
        "expires_at": operation.get("expires_at"),
        "target": operation.get("target"),
        "requested": parameters.get("requested"),
        "change": parameters.get("change"),
        "calculated_risk": {
            "level": calculated_risk.get("level"),
            "score": calculated_risk.get("score"),
        },
        "authority": {
            "effective_authority": authority.get("effective_authority"),
            "decision": authority.get("decision"),
            "reason_codes": authority.get("reason_codes"),
        },
    }


def _approval_preview(approval: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(approval, dict):
        return None
    return {
        "approval_id": approval.get("approval_id"),
        "decision": approval.get("decision"),
        "decided_at": approval.get("decided_at"),
        "expires_at": approval.get("expires_at"),
    }


def _bounded_ledger(ledger: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(ledger, list):
        return []
    bounded = ledger[-_MAX_LEDGER_ENTRIES:]
    return [
        {
            "sequence": entry.get("sequence"),
            "event_type": entry.get("event_type"),
            "occurred_at": entry.get("occurred_at"),
        }
        for entry in bounded
        if isinstance(entry, dict)
    ]


def _handoff(operation: dict[str, Any]) -> dict[str, Any]:
    """An identifier-only future-executor handoff (never a credential)."""
    binding = operation.get("future_executor_binding") or {}
    return {
        "executor_id": binding.get("executor_id"),
        "executor_binding_version": binding.get("executor_binding_version"),
    }
