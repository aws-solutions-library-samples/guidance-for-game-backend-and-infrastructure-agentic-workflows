"""The four foundation-contract **seams** this spec consumes (#277–#280).

Each seam is an explicit ``Protocol`` describing the interface the executor write half
*depends on*. The concrete internals of every field/algorithm are **owned by the named
foundation issue** and are deliberately not (re)invented here — this module defines only what
this spec consumes, so the executor/preparation logic can be built and property-tested now
against in-repo default adapters (see :mod:`connector.executor.adapters`) and later re-bound
to the accepted contracts by the GATED tail (tasks 11.1–11.3).

- :class:`OperationContracts277` — owned by **#277**: the canonical-hash algorithm, the set
  of supported operation-contract versions, the deterministic ``gbaw/<short-op-id>`` branch
  name, and the append-only ledger record contract.
- :class:`IdentityContract278` — owned by **#278**: the requester/approver identity provider
  and its OAuth/OIDC/Cognito claim shapes.
- :class:`StateRecoveryContract279` — owned by **#279**: the write-once insert, the conditional
  approval transition, and the reconciliation model.
- :class:`ThreatsControls280` — owned by **#280**: the threat model and the required-controls
  security gate that must pass before any provider-write IAM permission is attached.

Design reference: ``.kiro/specs/source-control-connector-executor/design.md`` →
Components and Interfaces → "8. Foundation-contract seams (#277–#280)".
"""

from __future__ import annotations

# Standard library
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Local modules
    from connector.executor.models import (
        ApprovalRecord,
        ApproverIdentity,
        LedgerEntry,
        PreparedOperation,
        ReconcileOutcome,
        RequesterIdentity,
    )

__all__ = [
    "OperationContracts277",
    "IdentityContract278",
    "StateRecoveryContract279",
    "ThreatsControls280",
]


@runtime_checkable
class OperationContracts277(Protocol):
    """Operation-contract seam — **owned by #277**.

    Supplies the binding canonical hash, the supported contract-version set, the deterministic
    provider branch name, and the append-only ledger record contract. This spec *consumes*
    these; the hash algorithm, version set, branch encoding, and ledger record fields are all
    owned and defined by #277 and are not (re)invented in this repo.
    """

    def canonical_hash(self, operation: "PreparedOperation") -> str:
        """Return the binding canonical hash over the operation's exact content + context.

        Owned by #277. Binds an approval to content *and* context; distinct from the retry
        idempotency token. (Req 6.2, 6.7)
        """
        ...

    def supported_contract_versions(self) -> frozenset[str]:
        """Return the set of operation-contract versions the executor can interpret.

        Owned by #277. The executor rejects an operation whose
        ``operation_contract_version`` is not in this set. (Req 4.6, 6.9)
        """
        ...

    def branch_name(self, operation_id: str) -> str:
        """Return the deterministic provider branch name ``gbaw/<short-operation-id>``.

        Owned by #277. A bounded, provider-safe representation of the operation id — **not**
        content-addressed — so a retry of the same operation targets the same branch.
        (Req 6.5, 6.6, 10.5)
        """
        ...

    def ledger_record(self, **fields: object) -> "LedgerEntry":
        """Build an append-only ledger record from the supplied fields.

        Owned by #277. The concrete ledger record contract (including the idempotency-token
        field) is defined by #277; this spec supplies the event fields to record. (Req 8.6)
        """
        ...


@runtime_checkable
class IdentityContract278(Protocol):
    """Identity seam — **owned by #278**.

    Derives the verified requester and approver identities from a trusted request/approval
    context (OAuth/OIDC/Cognito claims). Identities are never taken from model/tool input.
    The concrete claim shapes are owned by #278.
    """

    def requester_identity(self, request_ctx: object) -> "RequesterIdentity":
        """Return the verified requester identity for a preparation request.

        Owned by #278. Derived only from the trusted request context. (Req 13.2)
        """
        ...

    def approver_identity(self, approval_ctx: object) -> "ApproverIdentity":
        """Return the verified approver identity for an approval action.

        Owned by #278. Derived from verified OAuth/OIDC/Cognito claims on a trusted surface.
        (Req 2.7, 13.2)
        """
        ...


@runtime_checkable
class StateRecoveryContract279(Protocol):
    """State / idempotency / recovery seam — **owned by #279**.

    Defines the write-once operation insert, the conditional approval transition that never
    modifies prepared content, and the reconciliation model the executor uses to avoid
    duplicating provider state. The concrete storage/recovery model is owned by #279.
    """

    def insert_operation(self, op: "PreparedOperation") -> None:
        """Insert a prepared operation **write-once**.

        Owned by #279. Must reject a second insert for the same operation id and never mutate
        an existing record (conditional ``attribute_not_exists`` semantics). (Req 8.1, 6.3)
        """
        ...

    def apply_approval_transition(self, op_id: str, approval: "ApprovalRecord") -> None:
        """Apply an approval lifecycle transition as a separate **conditional** record.

        Owned by #279. Records the transition without ever modifying the prepared-content
        record. (Req 6.4, 8.2)
        """
        ...

    def reconcile(self, op_id: str, provider_state: object) -> "ReconcileOutcome":
        """Reconcile the operation against observed provider state.

        Owned by #279. Reports whether the intended branch/commit/proposal already exist so a
        retry reuses them rather than duplicating them. (Req 10.1, 10.3, 10.4)
        """
        ...


@runtime_checkable
class ThreatsControls280(Protocol):
    """Threat-model / required-controls security-gate seam — **owned by #280**.

    The deployment attaches **no** provider-write IAM permission until this gate reports it
    has passed. The concrete threat model and control set are owned by #280.
    """

    def security_gate_passed(self) -> bool:
        """Return ``True`` only when the #280 required-controls security gate has passed.

        Owned by #280. Provider-write IAM is gated on this. (Req 13.4)
        """
        ...
