"""Core data models for the Source Control Connector **executor** (write half).

These frozen dataclasses are the vocabulary of the deterministic write path described in
``.kiro/specs/source-control-connector-executor/design.md`` (Components/Interfaces + Data
Models). They deliberately mirror the design's model list *exactly*:

- :class:`DraftedChange` — the untrusted client/model input to the Preparation_Service. It
  carries **no** ``base_revision`` field: a client- or model-supplied revision is never
  accepted; the base revision is always fetched server-side from the provider (Req 1.2).
- :class:`PreparationResult` — the only thing returned to the client: an opaque
  ``operation_id`` plus a status/reason. It carries **no** PR URL and no provider artifact,
  because preparation writes nothing (Req 1.7, 11.3).
- :class:`PreparedOperation` — the immutable, write-once record the store holds. Its field
  list mirrors the design's logical model one-for-one.
- :class:`ApprovalRecord` — the trusted-surface approval bound to the stored
  ``canonical_hash`` with an expiry (Req 2.3, 2.4).
- :class:`EffectiveAuthority` — the stamped two-layer authorization decision plus the policy
  inputs that formed it (Req 7.4).
- :class:`RiskLevel` — the operation risk ordering used by the request-time check and the
  effective-authority intersection.
- :class:`TargetSelector` — the *requested* repo/branch (matched to the allowlist, never
  trusted verbatim).
- :class:`ExecutorEvent` — the executor Lambda's deliberately minimal input: **only** the
  opaque ``operation_id`` (Req 4.2, 4.3).
- :class:`ExecutionOutcome` — the executor's terminal outcome (no PR URL to the model).
- :data:`RequesterIdentity` / :data:`ApproverIdentity` — the identity shapes consumed from
  the #278 seam (owned by that issue; a minimal concrete shape is provided here so the
  default adapter and the preparation/approval logic can run before #278 lands).

The concrete field *contracts* for the canonical hash, the ledger record, and the
write-once/transition record shapes are owned by the foundation issues #277 and #279; the
lightweight :class:`LedgerEntry` and :class:`ReconcileOutcome` here are the shapes this spec
*consumes* through the seams in :mod:`connector.executor.seams`, not a redefinition of those
contracts.
"""

from __future__ import annotations

# Standard library
from dataclasses import dataclass, field
from enum import IntEnum

# Local modules
from connector.models import ChangeProposalResult, ProposedFile

__all__ = [
    "RiskLevel",
    "TargetSelector",
    "RequesterIdentity",
    "ApproverIdentity",
    "EffectiveAuthority",
    "DraftedChange",
    "PreparationResult",
    "PreparedOperation",
    "ApprovalRecord",
    "ExecutorEvent",
    "ExecutionOutcome",
    "LedgerEntry",
    "ReconcileOutcome",
]


class RiskLevel(IntEnum):
    """Ordered operation-risk classification (Req 7.2, 7.3).

    Modeled as an ``IntEnum`` so the effective-authority *intersection* is a simple
    ``min`` over each policy layer's ceiling and an operation is authorized iff its risk is
    ``<=`` the intersected ceiling. The member ``name`` is the stable string used wherever
    the risk is serialized (e.g. as part of the canonical-hash input).
    """

    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class TargetSelector:
    """The *requested* repository/branch for a drafted change.

    These are matched against the operator-approved allowlist, never trusted verbatim; the
    effective repo/branch always come from the matched allowlist entry (mirrors the baseline
    read/propose selection discipline). Either field may be ``None`` to request the default
    allowlist entry / its first branch.
    """

    repository: str | None = None
    branch: str | None = None


@dataclass(frozen=True)
class RequesterIdentity:
    """Identity of the principal that drafted/requested a change (consumed from #278).

    The concrete claim shape is owned by the #278 identity contract; this minimal shape
    (verified ``subject`` plus the principal's ``groups``) is what this spec consumes and is
    sufficient for the default identity adapter and the authorization logic to run before
    #278 lands. Identity is always derived from a trusted context, never from model input.
    """

    subject: str
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApproverIdentity:
    """Identity of the human approver (consumed from #278; OAuth/OIDC/Cognito claims).

    As with :class:`RequesterIdentity`, the concrete claim shape is owned by #278. Separation
    of duties compares this ``subject`` against the requester's ``subject`` when policy
    requires it (Req 2.6).
    """

    subject: str
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectiveAuthority:
    """The stamped two-layer authorization decision plus its inputs (Req 7.3, 7.4).

    ``decision`` is a stable string (``"authorized"`` / ``"denied"``) that is part of the
    bound canonical-hash input, so the stamped authority is part of the context an approval
    binds to. ``inputs`` records the applicable policy-layer identifiers that were intersected
    to produce the decision; ``risk_ceiling`` is the intersected maximum risk the layers
    jointly permit; ``failed_layer`` names the layer that denied the operation (``None`` on an
    authorized decision).
    """

    decision: str
    inputs: tuple[str, ...] = ()
    risk_ceiling: RiskLevel | None = None
    failed_layer: str | None = None

    @property
    def authorized(self) -> bool:
        """Return ``True`` iff the stamped decision authorized the operation."""
        return self.decision == "authorized"


@dataclass(frozen=True)
class DraftedChange:
    """Untrusted drafted change submitted to the Preparation_Service.

    ``files`` (path + content) and ``target`` are client/model supplied and therefore
    untrusted; ``target`` is matched to the allowlist rather than trusted verbatim. There is
    intentionally **no** ``base_revision`` field — the preparation pipeline fetches the base
    revision directly from the provider and never accepts a client/model-supplied revision
    (Req 1.2).
    """

    files: tuple[ProposedFile, ...]
    iac_format: str
    target: TargetSelector
    intent: str
    title: str
    description: str


@dataclass(frozen=True)
class PreparationResult:
    """The opaque result returned to the client from preparation.

    ``operation_id`` is the *only* thing returned to the caller; ``status`` is
    ``"prepared"`` or ``"rejected"`` and ``reason`` names the failed dimension / validation
    error on a rejection. There is intentionally **no** PR URL or provider artifact — nothing
    was written during preparation (Req 1.7, 11.3).
    """

    operation_id: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class PreparedOperation:
    """Immutable, write-once operation record (design → Data Models → PreparedOperation).

    The field list mirrors the design one-for-one: an opaque ``operation_id``, the binding
    ``canonical_hash`` (#277), the ``operation_contract_version`` the executor interprets, the
    exact stored ``files``, the effective ``target_repo``/``target_branch``, the server-fetched
    ``base_revision`` (re-verified at execute time), the stamped ``effective_authority`` and
    ``risk``, the ``requester_identity`` (#278), the ``duplicate_content_key`` (computed
    separately from the retry idempotency token), and ``created_at``.
    """

    operation_id: str
    canonical_hash: str
    operation_contract_version: str
    files: tuple[ProposedFile, ...]
    target_repo: str
    target_branch: str
    base_revision: str
    effective_authority: EffectiveAuthority
    risk: RiskLevel
    requester_identity: RequesterIdentity
    duplicate_content_key: str
    created_at: str


@dataclass(frozen=True)
class ApprovalRecord:
    """Trusted-surface approval bound to the stored canonical hash with an expiry.

    ``bound_canonical_hash`` equals the stored operation's ``canonical_hash`` (Req 2.3), so a
    change to the bound content yields a different hash / operation and this record cannot
    authorize it. ``expires_at`` is the approval expiry (Req 2.4) and
    ``separation_of_duties_ok`` records that the approver differed from the requester when
    policy required it (Req 2.6).
    """

    operation_id: str
    approver_identity: ApproverIdentity
    bound_canonical_hash: str
    approved_at: str
    expires_at: str
    separation_of_duties_ok: bool


@dataclass(frozen=True)
class ExecutorEvent:
    """The isolated executor Lambda's input contract — the opaque operation id only.

    No files, no target, no revision, and no free-form instruction ever drive the executor;
    only ``operation_id`` does (Req 4.2, 4.3).
    """

    operation_id: str


@dataclass(frozen=True)
class ExecutionOutcome:
    """The executor's terminal outcome, returned to the workflow (never to the model).

    ``status`` is one of ``"executed"`` / ``"rejected"`` / ``"reconciled"`` / ``"error"``;
    ``reason`` names the failed gate on a rejection. ``proposal_ref`` is an internal provider
    reference used only for the ledger/reconciliation — no PR URL is surfaced to the model
    (Req 11.3, 11.5).
    """

    operation_id: str
    status: str
    reason: str | None = None
    proposal_ref: str | None = None


@dataclass(frozen=True)
class LedgerEntry:
    """Append-only audit-ledger record shape **consumed** from the #277 ledger contract.

    The concrete ledger record contract is owned by #277; this is the minimal shape this
    spec consumes so the store's append-only semantics and the executor's attempt/outcome
    recording can be built and tested before #277 lands. ``sequence`` orders entries under an
    operation (the ``LEDGER#<seq>`` sort key), ``event`` is the record kind
    (``intent``/``attempt``/``provider_result``/``recovery``/``outcome``), and
    ``idempotency_token`` (#277) correlates attempts of the same logical operation.
    """

    operation_id: str
    sequence: int
    event: str
    outcome: str | None = None
    provider_ref: str | None = None
    idempotency_token: str | None = None
    recorded_at: str | None = None
    fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReconcileOutcome:
    """Reconciliation result shape **consumed** from the #279 state/recovery model.

    The concrete reconciliation model is owned by #279; this shape captures what the executor
    needs to decide reconcile-before-retry: whether the deterministic branch already exists,
    whether the commit already landed, and any already-open change proposal for the operation.
    ``resolved`` is ``True`` when the intended provider state is already fully present.
    """

    operation_id: str
    branch_exists: bool = False
    commit_present: bool = False
    proposal: ChangeProposalResult | None = None
    resolved: bool = False
