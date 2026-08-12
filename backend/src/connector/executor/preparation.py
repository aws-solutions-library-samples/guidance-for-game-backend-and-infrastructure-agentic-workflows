"""Deterministic Preparation_Service — the write path's fail-closed front door (Component 1).

``PreparationService.prepare`` turns an *untrusted* :class:`DraftedChange` into an immutable,
write-once :class:`PreparedOperation` and returns nothing but an opaque ``Operation_ID``. It
performs **no** provider mutation on any path; the only provider call it makes is a *read* to
fetch the server-side base revision (Req 1.7).

Pipeline (fail-closed at every step, exactly the design's order —
``.kiro/specs/source-control-connector-executor/design.md`` → Components → 1):

1. **Capability posture** — enforced at this write-path entry boundary; a disabled posture
   rejects before anything else (Req 7.1).
2. **Fetch the base revision server-side** directly from the provider via
   ``latest_commit_sha`` — never from a client/model-supplied value (there is no such field on
   :class:`DraftedChange`) (Req 1.1, 1.2).
3. **Validate schema** by reusing :func:`connector.iac_validation.validate_iac`; a failure
   stores nothing and rejects (Req 1.3, 1.4).
4. **Two-layer authorization + risk**: compute + stamp :class:`EffectiveAuthority` as the
   policy-layer intersection, run the request-time check, and evaluate ``Target_Authorization``
   separately by reusing the sibling five-dimension policy (Req 7.2–7.5).
5. **Duplicate-content detection** computed separately from the retry idempotency token
   (Req 6.8).
6. **Store an immutable, write-once** :class:`PreparedOperation` through the #279 store seam,
   carrying the ``Operation_ID``, the #277 ``Canonical_Hash``, the contract version, and the
   stamped authority + risk (Req 1.5, 1.6, 7.4). Re-drafting always mints a **new**
   ``Operation_ID`` and never mutates an existing record (Req 1.8, 6.3).
7. Return the opaque ``Operation_ID`` only — no PR URL, no provider artifact (Req 1.7, 11.3).

Every authorization/policy input (the policy layers, the principal's authority, the risk
assessment) is injectable so the deterministic logic can be property-tested against the
in-repo default adapters and test doubles before the accepted foundation contracts land.
"""

from __future__ import annotations

# Standard library
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

# Local modules
from connector.executor import opid
from connector.executor.adapters import DEFAULT_CONTRACT_VERSION
from connector.executor.authorization import (
    CapabilityPosture,
    PolicyLayer,
    compute_effective_authority,
    request_time_check,
    target_authorization,
)
from connector.executor.models import (
    DraftedChange,
    PreparationResult,
    PreparedOperation,
    RequesterIdentity,
    RiskLevel,
)
from connector.iac_validation import IaCValidationError, validate_iac
from connector.provider import ProviderError

if TYPE_CHECKING:
    # Local modules
    from connector.config import AuthorizationPolicy
    from connector.executor.seams import OperationContracts277, StateRecoveryContract279
    from connector.provider import SourceControlProvider

__all__ = ["PreparationService"]


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string for the operation timestamp."""
    return datetime.now(timezone.utc).isoformat()


class PreparationService:
    """Deterministic preparation of an untrusted drafted change (no provider write).

    All collaborators are injected so the service is fully testable against the default #277
    contracts, the in-repo #279 store, and a ``FakeProvider``:

    - ``provider`` — the source-control provider, used **read-only** to fetch the server-side
      base revision (``latest_commit_sha``).
    - ``store`` — the #279 write-once store seam.
    - ``contracts`` — the #277 operation-contract seam (canonical hash / branch / versions).
    - ``policy`` + ``authorized_groups`` — the sibling five-dimension
      :class:`~connector.config.AuthorizationPolicy` reused for ``Target_Authorization``.
    - ``capability_posture`` — the deployment/tenant/workspace posture enforced at this entry
      boundary.
    - ``policy_layers`` — the applicable authorization layers whose intersection is the
      stamped :class:`EffectiveAuthority`.
    - ``assess_risk`` — maps a draft to its :class:`RiskLevel` (defaults to ``MEDIUM``).
    - ``principal_authority`` — maps a requester to the maximum risk their authority permits
      (defaults to ``HIGH``).
    """

    def __init__(
        self,
        *,
        provider: "SourceControlProvider",
        store: "StateRecoveryContract279",
        contracts: "OperationContracts277",
        policy: "AuthorizationPolicy",
        authorized_groups: Sequence[str],
        capability_posture: CapabilityPosture,
        policy_layers: Sequence[PolicyLayer] = (),
        assess_risk: Callable[[DraftedChange], RiskLevel] | None = None,
        principal_authority: Callable[[RequesterIdentity], RiskLevel] | None = None,
        contract_version: str = DEFAULT_CONTRACT_VERSION,
        new_operation_id: Callable[[], str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._contracts = contracts
        self._policy = policy
        self._authorized_groups = tuple(authorized_groups)
        self._capability_posture = capability_posture
        self._policy_layers = tuple(policy_layers)
        self._assess_risk = assess_risk or (lambda _draft: RiskLevel.MEDIUM)
        self._principal_authority = principal_authority or (lambda _requester: RiskLevel.HIGH)
        self._contract_version = contract_version
        self._new_operation_id = new_operation_id or (lambda: uuid.uuid4().hex)
        self._clock = clock or _now_iso

    def prepare(self, draft: DraftedChange, *, requester: RequesterIdentity) -> PreparationResult:
        """Run the fail-closed preparation pipeline and return the opaque ``Operation_ID``.

        On any failed gate a :class:`PreparationResult` with ``status="rejected"`` and the
        failed dimension in ``reason`` is returned, **no** :class:`PreparedOperation` is
        stored, and no provider mutation is performed. On success a new write-once operation
        is stored and its opaque ``operation_id`` is returned with ``status="prepared"``.
        """
        # --- 1. Capability posture at this write-path entry boundary (Req 7.1) ----------
        if not self._capability_posture.is_enabled():
            return PreparationResult(operation_id="", status="rejected", reason="capability_disabled")

        # Resolve the *requested* target against the allowlist (defaults to the first entry).
        repo, branch = self._resolve_target(draft)

        # --- 2. Base revision fetched SERVER-SIDE from the provider (Req 1.1, 1.2) ------
        # DraftedChange carries no base_revision field, so a client/model value can never be
        # accepted; the stored base revision is always what the provider reports here.
        try:
            base_revision = self._provider.latest_commit_sha(repo, branch)
        except ProviderError:
            return PreparationResult(operation_id="", status="rejected", reason="provider_unavailable")

        # --- 3. Schema validation; on failure store nothing (Req 1.3, 1.4) --------------
        try:
            validate_iac(draft.files, draft.iac_format)
        except IaCValidationError as exc:
            return PreparationResult(operation_id="", status="rejected", reason=f"iac_validation_failed:{exc.file}")

        # --- 4. Two-layer authorization + risk (Req 7.2, 7.3, 7.4, 7.5) -----------------
        risk = self._assess_risk(draft)

        # Request-time check: risk within both the principal's authority and capability max.
        if not request_time_check(
            principal_authority=self._principal_authority(requester),
            capability_maximum=self._capability_posture.capability_maximum,
            operation_risk=risk,
        ):
            return PreparationResult(operation_id="", status="rejected", reason="request_time_denied")

        # Effective authority = intersection of all applicable policy layers, stamped.
        effective_authority = compute_effective_authority(self._policy_layers, risk)
        if not effective_authority.authorized:
            reason = f"effective_authority_denied:{effective_authority.failed_layer or 'unknown'}"
            return PreparationResult(operation_id="", status="rejected", reason=reason)

        # Target authorization evaluated separately (repo · branch · path · extension · group).
        target_decision = target_authorization(
            self._policy,
            repo=repo,
            branch=branch,
            paths=[f.path for f in draft.files],
            groups=requester.groups,
            authorized_groups=self._authorized_groups,
        )
        if not target_decision.allowed:
            return PreparationResult(
                operation_id="",
                status="rejected",
                reason=f"target_authorization_denied:{target_decision.failed_dimension}",
            )
        effective_repo = target_decision.repo or repo
        effective_branch = target_decision.branch or branch

        # --- 5. Duplicate-content detection (separate from retry idempotency, Req 6.8) --
        duplicate_content_key = opid.duplicate_content_key(
            repo=effective_repo,
            target_branch=effective_branch,
            base_revision=base_revision,
            files=draft.files,
        )

        # --- 6. Store an immutable, write-once PreparedOperation (Req 1.5, 1.6, 7.4) ----
        operation_id = self._new_operation_id()
        operation = PreparedOperation(
            operation_id=operation_id,
            canonical_hash="",
            operation_contract_version=self._contract_version,
            files=tuple(draft.files),
            target_repo=effective_repo,
            target_branch=effective_branch,
            base_revision=base_revision,
            effective_authority=effective_authority,
            risk=risk,
            requester_identity=requester,
            duplicate_content_key=duplicate_content_key,
            created_at=self._clock(),
        )
        # The canonical hash binds the exact stored content + context (incl. stamped authority).
        operation = replace(operation, canonical_hash=self._contracts.canonical_hash(operation))

        # Write-once insert; a fresh Operation_ID is minted per prepare, so re-drafting never
        # mutates an existing record (Req 1.8, 6.3, 8.1).
        self._store.insert_operation(operation)

        # --- 7. Return the opaque Operation_ID only (Req 1.7, 11.3) ---------------------
        return PreparationResult(operation_id=operation_id, status="prepared")

    def _resolve_target(self, draft: DraftedChange) -> tuple[str, str]:
        """Return the *requested* ``(repo, branch)``, defaulting to the first allowlist entry.

        The returned values are only the requested selectors; the *effective* repo/branch are
        taken from the matched allowlist entry by :func:`target_authorization` and are what get
        stored on the operation (never free-form input).
        """
        default_repo: str | None = None
        default_branch: str | None = None
        if self._policy.entries:
            entry = self._policy.entries[0]
            default_repo = entry.repo
            default_branch = entry.target_branches[0] if entry.target_branches else None
        repo = draft.target.repository if draft.target.repository is not None else (default_repo or "")
        branch = draft.target.branch if draft.target.branch is not None else (default_branch or "")
        return repo, branch
