"""Isolated Executor Lambda — the ordered, fail-closed write gate sequence (Component 6).

The executor is the sole holder of the write credential and accepts **only** an opaque
``Operation_ID`` (:class:`ExecutorEvent`; Req 4.2, 4.3). It re-anchors the *baseline* propose
pipeline on the stored, immutable operation and runs an ordered gate sequence where every
rejection performs **no** provider write and appends an :class:`~connector.executor.models.LedgerEntry`
to the append-only ``Audit_Ledger`` (design → Components → 6):

1. **Caller authentication** — the invocation is accepted only from the Durable_Workflow Step
   Functions role; any other caller is rejected (Req 4.7, 4.8).
2. **Load** the :class:`PreparedOperation` and its bound :class:`ApprovalRecord` by
   ``Operation_ID``; an absent operation *or* absent approval rejects (Req 4.4, 11.4).
3. **Verify** the ``Operation_Contract_Version`` is supported and the stored content hashes to
   the stored ``Canonical_Hash`` — and that the approval binds that same hash, so changed
   content (a different hash/id) can never be authorized by a prior approval (Req 4.5, 4.6,
   6.9, 2.5).
4. **Reject an expired approval** (Req 5.5).
5. **Re-validate** capability posture, request-time policy, current policy version, resource
   enrollment, normalized paths/extensions, ``Target_Authorization``, and **re-verify the base
   revision server-side** directly from the provider (Req 5.1–5.4, 5.6, 7.6, 7.7).
6. **Acquire the write credential** (executor role only) and build the write-only
   :class:`ExecutorWriter` (Req 4.1, 9.6).
7. **Write** — derive ``gbaw/<short-operation-id>`` then ``create_branch`` → ``commit_files`` →
   ``open_change_proposal`` reusing the baseline ``_idempotent_mutate`` reconcile-before-retry
   so retries never duplicate branch/commit/proposal (Req 4.9, 10.1–10.5).
8. **Record** every attempt and the terminal provider/recovery outcome to the ledger; return an
   :class:`ExecutionOutcome` that carries an internal ``proposal_ref`` only — never a PR URL to
   the model (Req 4.10, 8.6, 10.6, 11.1–11.3, 11.5).
"""

from __future__ import annotations

# Standard library
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

# Local modules
from connector.executor import opid
from connector.executor.authorization import (
    CapabilityPosture,
    PolicyLayer,
    compute_effective_authority,
    request_time_check,
    target_authorization,
)
from connector.executor.models import ExecutionOutcome, ExecutorEvent, RiskLevel
from connector.executor.writer import ExecutorWriter, build_writer
from connector.provider import ProviderError
from connector.service import _ALREADY_APPLIED, _idempotent_mutate
from utils.secrets import get_secret

if TYPE_CHECKING:
    # Local modules
    from connector.config import AuthorizationPolicy
    from connector.executor.models import ApprovalRecord, PreparedOperation, RequesterIdentity
    from connector.executor.seams import OperationContracts277
    from connector.executor.store import InMemoryOperationStore
    from connector.executor.writer import CredentialAcquirer
    from connector.models import ChangeProposalResult, ProposedFile
    from connector.provider import SourceControlProvider

__all__ = ["ExecutorDependencies", "Executor", "configure_executor", "handler"]


def _now() -> datetime:
    """Return the current timezone-aware UTC time (execution-time clock)."""
    return datetime.now(timezone.utc)


def _caller_identity(context: object) -> str | None:
    """Extract the invoking caller identity from the Lambda ``context`` (Req 4.7).

    Accepts a mapping (``caller_identity`` / ``invoked_role_arn``) or any object exposing those
    attributes. The caller is verified against the sole permitted workflow role; the real
    isolation is enforced by IAM (only the workflow role can invoke the executor), and this is
    the in-code fail-closed mirror of that grant.
    """
    if context is None:
        return None
    if isinstance(context, dict):
        value = context.get("caller_identity") or context.get("invoked_role_arn")
        return str(value) if value else None
    value = getattr(context, "caller_identity", None) or getattr(context, "invoked_role_arn", None)
    return str(value) if value else None


@dataclass(frozen=True)
class ExecutorDependencies:
    """Injected collaborators + re-validation inputs for the executor gate sequence.

    Every policy input is injectable so the gate sequence is fully testable against the default
    #277 contracts, the in-repo #279 store, and a ``FakeProvider``. ``workflow_role_arn`` is the
    only caller permitted past gate 1; ``write_secret_arn`` is the secret the executor role
    reads at gate 6.
    """

    store: "InMemoryOperationStore"
    contracts: "OperationContracts277"
    provider: "SourceControlProvider"
    policy: "AuthorizationPolicy"
    authorized_groups: tuple[str, ...]
    capability_posture: CapabilityPosture
    workflow_role_arn: str
    write_secret_arn: str
    policy_layers: tuple[PolicyLayer, ...] = ()
    principal_authority: Callable[["RequesterIdentity"], RiskLevel] = field(default=lambda _requester: RiskLevel.HIGH)
    is_enrolled: Callable[[str], bool] = field(default=lambda _repo: True)
    policy_version_ok: Callable[[], bool] = field(default=lambda: True)
    credential_acquirer: "CredentialAcquirer" = get_secret
    max_attempts: int = 3
    clock: Callable[[], datetime] = _now


class Executor:
    """Runs the ordered, fail-closed executor gate sequence over an opaque ``Operation_ID``."""

    def __init__(self, deps: ExecutorDependencies) -> None:
        self._d = deps

    # -- ledger ------------------------------------------------------------------------

    def _append_ledger(
        self,
        operation_id: str,
        *,
        event: str,
        outcome: str | None = None,
        provider_ref: str | None = None,
        idempotency_token: str | None = None,
    ) -> None:
        """Append one append-only :class:`LedgerEntry` for the operation (Req 8.6, 4.10)."""
        sequence = self._d.store.next_ledger_sequence(operation_id)
        entry = self._d.contracts.ledger_record(
            operation_id=operation_id,
            sequence=sequence,
            event=event,
            outcome=outcome,
            provider_ref=provider_ref,
            idempotency_token=idempotency_token,
            recorded_at=self._d.clock().isoformat(),
        )
        self._d.store.append_ledger(entry)

    def _reject(self, operation_id: str, reason: str) -> ExecutionOutcome:
        """Append a rejection ledger entry (no provider write) and return the outcome."""
        self._append_ledger(operation_id, event="outcome", outcome=f"rejected:{reason}")
        return ExecutionOutcome(operation_id=operation_id, status="rejected", reason=reason)

    # -- entry point -------------------------------------------------------------------

    def handle(self, event: ExecutorEvent, context: object) -> ExecutionOutcome:
        """Execute the gate sequence; return an :class:`ExecutionOutcome` (never a PR URL)."""
        operation_id = event.operation_id

        # --- Gate 1: caller authentication (Req 4.7, 4.8) -------------------------------
        caller = _caller_identity(context)
        if caller != self._d.workflow_role_arn:
            return self._reject(operation_id, "caller_not_workflow_role")

        # --- Gate 2: load operation + bound approval (Req 4.4, 11.4) --------------------
        operation = self._d.store.get_operation(operation_id)
        if operation is None:
            return self._reject(operation_id, "operation_absent")
        approval = self._d.store.get_latest_approval(operation_id)
        if approval is None:
            return self._reject(operation_id, "approval_absent")

        token = opid.idempotency_token_for(operation)

        # --- Gate 3: canonical hash + supported contract version (Req 4.5, 4.6, 6.9) ----
        if not opid.verify_contract_version(self._d.contracts, operation.operation_contract_version):
            return self._reject(operation_id, "unsupported_contract_version")
        recomputed = self._d.contracts.canonical_hash(operation)
        if recomputed != operation.canonical_hash:
            return self._reject(operation_id, "hash_mismatch")
        # The approval must bind the same stored hash; changed content -> different hash/id and
        # a prior approval cannot authorize it (Req 2.5).
        if approval.bound_canonical_hash != operation.canonical_hash:
            return self._reject(operation_id, "hash_mismatch")

        # --- Gate 4: reject an expired approval (Req 5.5) -------------------------------
        if self._approval_expired(approval):
            return self._reject(operation_id, "approval_expired")

        # --- Gate 5: full re-validation (Req 5.1-5.4, 5.6, 7.6, 7.7) --------------------
        revalidation_reason = self._revalidate(operation)
        if revalidation_reason is not None:
            return self._reject(operation_id, revalidation_reason)

        # --- Gates 6-8: acquire credential, write, record (Req 4.1, 4.9, 4.10) ----------
        return self._execute_write(operation, token=token)

    # -- gate helpers ------------------------------------------------------------------

    def _approval_expired(self, approval: "ApprovalRecord") -> bool:
        """Return ``True`` iff the approval's expiry has passed at execution time (Req 5.5)."""
        try:
            expires_at = datetime.fromisoformat(approval.expires_at)
        except ValueError:
            # An unparseable expiry fails closed (treated as expired).
            return True
        now = self._d.clock()
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return now >= expires_at

    def _revalidate(self, operation: "PreparedOperation") -> str | None:
        """Re-run every pre-execution check; return a rejection reason or ``None`` if all pass.

        Re-checks (each a fail-closed gate): capability posture, request-time policy, current
        policy version, resource enrollment, effective-authority intersection,
        ``Target_Authorization`` over normalized paths/extensions, and a server-side
        base-revision re-verification (Req 5.1-5.4, 5.6, 7.6, 7.7).
        """
        # Capability posture at the execution entry boundary (Req 7.1).
        if not self._d.capability_posture.is_enabled():
            return "capability_disabled"

        # Request-time policy: risk within principal authority and capability maximum (Req 5.1).
        if not request_time_check(
            principal_authority=self._d.principal_authority(operation.requester_identity),
            capability_maximum=self._d.capability_posture.capability_maximum,
            operation_risk=operation.risk,
        ):
            return "request_time_denied"

        # Current policy version (Req 7.7) and resource enrollment (Req 5.2).
        if not self._d.policy_version_ok():
            return "policy_version_stale"
        if not self._d.is_enrolled(operation.target_repo):
            return "resource_not_enrolled"

        # Effective-authority intersection re-check.
        effective = compute_effective_authority(self._d.policy_layers, operation.risk)
        if not effective.authorized:
            return f"effective_authority_denied:{effective.failed_layer or 'unknown'}"

        # Target authorization over normalized paths/extensions (Req 5.3, 5.6, 7.6).
        decision = target_authorization(
            self._d.policy,
            repo=operation.target_repo,
            branch=operation.target_branch,
            paths=[f.path for f in operation.files],
            groups=operation.requester_identity.groups,
            authorized_groups=self._d.authorized_groups,
        )
        if not decision.allowed:
            return f"target_authorization_denied:{decision.failed_dimension}"

        # Re-verify the base revision directly from the provider (Req 5.4). A moved/stale head
        # rejects with no write, reusing the baseline stale-head discipline.
        try:
            current_head = self._d.provider.latest_commit_sha(operation.target_repo, operation.target_branch)
        except ProviderError:
            return "provider_unavailable"
        if current_head != operation.base_revision:
            return "stale_base_revision"

        return None

    # -- write + reconcile (gates 6-8) -------------------------------------------------

    def _execute_write(self, operation: "PreparedOperation", *, token: str) -> ExecutionOutcome:
        """Acquire the credential, perform the reconcile-before-retry write, record the ledger.

        Reuses the baseline :func:`connector.service._idempotent_mutate` so each mutating step
        reconciles provider state before running and before each retry, never duplicating the
        branch/commit/proposal (Req 10.1-10.5). Provider errors fail closed with a recorded
        recovery/error outcome and no false success (Req 10.3).
        """
        operation_id = operation.operation_id
        repo = operation.target_repo
        target_branch = operation.target_branch
        base_sha = operation.base_revision
        files: list[ProposedFile] = list(operation.files)
        attempts = self._d.max_attempts

        # --- Gate 6: acquire write credential (executor role only) + build writer -------
        try:
            writer: ExecutorWriter = build_writer(
                self._d.provider,
                secret_arn=self._d.write_secret_arn,
                acquirer=self._d.credential_acquirer,
            )
        except ProviderError:
            self._append_ledger(operation_id, event="outcome", outcome="error:provider_auth", idempotency_token=token)
            return ExecutionOutcome(operation_id=operation_id, status="error", reason="provider_auth_error")

        branch = opid.branch_name(self._d.contracts, operation_id)
        title = f"[gbaw] Prepared change {operation_id}"
        body = _build_change_body(operation)

        # Durable intent before the first mutating op (Req 8.6).
        self._append_ledger(operation_id, event="intent", idempotency_token=token)

        try:
            # create_branch — reconcile: reuse an already-existing deterministic branch.
            def _reconcile_branch() -> object | None:
                return _ALREADY_APPLIED if writer.branch_exists(repo, branch) else None

            _idempotent_mutate(
                lambda: writer.create_branch(repo, branch, base_sha),
                _reconcile_branch,
                max_attempts=attempts,
            )
            self._append_ledger(operation_id, event="attempt", outcome="branch_ready", idempotency_token=token)

            # commit_files — reconcile: a head moved past base_sha means our commit landed.
            def _reconcile_commit() -> object | None:
                head = writer.latest_commit_sha(repo, branch)
                return _ALREADY_APPLIED if head and head != base_sha else None

            _idempotent_mutate(
                lambda: writer.commit_files(repo, branch, files, title),
                _reconcile_commit,
                max_attempts=attempts,
            )
            self._append_ledger(operation_id, event="attempt", outcome="commit_ready", idempotency_token=token)

            # open_change_proposal — reconcile: return an already-open proposal for head->base.
            def _reconcile_proposal() -> "ChangeProposalResult | None":
                return writer.find_open_change_proposal(repo, branch, target_branch)

            proposal = cast(
                "ChangeProposalResult",
                _idempotent_mutate(
                    lambda: writer.open_change_proposal(repo, branch, target_branch, title, body),
                    _reconcile_proposal,
                    max_attempts=attempts,
                ),
            )
        except ProviderError as exc:
            # Fail closed: record the recovery/error outcome and never report success (Req 10.3).
            self._append_ledger(
                operation_id,
                event="recovery",
                outcome=f"error:{type(exc).__name__}",
                idempotency_token=token,
            )
            return ExecutionOutcome(operation_id=operation_id, status="error", reason="provider_operation_failed")

        # --- Gate 8: terminal outcome to the ledger; no PR URL to the model -------------
        self._append_ledger(
            operation_id,
            event="outcome",
            outcome="executed",
            provider_ref=proposal.proposal_id,
            idempotency_token=token,
        )
        # ExecutionOutcome carries an internal proposal_ref only — never a PR URL (Req 11.3, 11.5).
        return ExecutionOutcome(
            operation_id=operation_id,
            status="executed",
            proposal_ref=proposal.proposal_id,
        )


def _build_change_body(operation: "PreparedOperation") -> str:
    """Compose a secret-free change-proposal body from the stored operation.

    References every affected file path and attributes the change to the requester subject.
    Carries no file contents beyond what the commit already carries and no credential.
    """
    file_lines = "\n".join(f"- {f.path}" for f in operation.files)
    return (
        f"Prepared operation `{operation.operation_id}`.\n\n"
        f"**Affected IaC files:**\n{file_lines}\n\n"
        f"---\n"
        f"This change proposal was prepared on behalf of requesting user "
        f"`{operation.requester_identity.subject}` and approved out of model control. It is "
        f"unmerged and requires human review before merge."
    )


# Module-level handler wiring. The concrete dependencies are injected during the GATED deploy
# wiring (task 9.3 attaches the executor-role IAM and configures this); no provider-write IAM
# is attached before the #280 security gate passes.
_configured_executor: Executor | None = None


def configure_executor(executor: Executor) -> None:
    """Install the :class:`Executor` the module-level :func:`handler` delegates to."""
    global _configured_executor
    _configured_executor = executor


def handler(event: ExecutorEvent, context: object) -> ExecutionOutcome:
    """Lambda entry point — delegates to the configured :class:`Executor` (fail-closed).

    Accepts only the opaque :class:`ExecutorEvent`; the executor must be configured during the
    gated deploy wiring. An unconfigured executor fails closed rather than performing any write.
    """
    if _configured_executor is None:
        raise RuntimeError(
            "executor is not configured; configure_executor(...) is wired during the gated "
            "deploy step (task 9.3) once the #280 security gate has passed"
        )
    return _configured_executor.handle(event, context)
