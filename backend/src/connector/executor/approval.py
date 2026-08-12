"""Approval_Surface + binding — the trusted human gate outside model control (Component 4).

``ApprovalService.approve`` is the trusted web/API surface that binds a human approval to an
*exact* prepared operation. It is deliberately **not** driven by chat or model output
(Req 2.2): the only inputs are an opaque ``Operation_ID`` and a trusted approval context from
which the approver identity is derived via the #278 identity seam (never from request/model
arguments — Req 2.7, 13.2).

Responsibilities (design → Components → 4):

1. **Load the operation + its exact diff** by ``Operation_ID`` (Req 2.1). The exact diff is
   the operation's stored, immutable ``files`` — the very content the approver reviews.
2. **Derive the approver identity** from the trusted approval context through
   :class:`IdentityContract278` (Req 2.7).
3. **Enforce separation of duties** — when policy requires it, the approver's subject must
   differ from the requester's subject (Req 2.6).
4. **Write an Approval_Record bound to the stored ``Canonical_Hash`` with an expiry** as a
   *conditional approval transition* on the store, never modifying the prepared content
   (Req 2.3, 2.4). Because the record binds to the stored hash, changed content is a different
   ``Operation_ID``/hash and any prior approval is invalid for it (Req 2.5).
"""

from __future__ import annotations

# Standard library
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

# Local modules
from connector.executor.models import ApprovalRecord

if TYPE_CHECKING:
    # Local modules
    from connector.executor.models import PreparedOperation
    from connector.executor.seams import IdentityContract278
    from connector.executor.store import InMemoryOperationStore

__all__ = ["ApprovalService", "ApprovalRejected", "TRUSTED_APPROVAL_SOURCES"]

# The only surfaces from which an approval is accepted. Approval is a trusted web/API action,
# never chat/model output (Req 2.2); any other source is refused.
TRUSTED_APPROVAL_SOURCES: frozenset[str] = frozenset({"web", "api"})

# The default approval validity window (Req 2.4). The executor rejects an approval whose
# expiry has passed at execution time (Req 5.5).
_DEFAULT_TTL_SECONDS = 3600


class ApprovalRejected(Exception):
    """Raised when an approval cannot be granted (fail-closed).

    ``reason`` names the failed condition — one of ``"untrusted_source"``,
    ``"operation_absent"``, ``"identity_unverified"``, or ``"separation_of_duties"`` — so the
    trusted surface can surface it without leaking operation content.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ApprovalService:
    """Trusted approval surface that binds an approval to a stored operation's canonical hash.

    Collaborators are injected for testability:

    - ``store`` — the #279 store seam (loads the operation, applies the conditional approval
      transition).
    - ``identity`` — the #278 identity seam that derives the verified approver identity from a
      trusted approval context.
    - ``require_separation_of_duties`` — when ``True`` (the default), the approver subject must
      differ from the requester subject.
    - ``approval_ttl_seconds`` — the approval validity window used to compute ``expires_at``.
    """

    def __init__(
        self,
        *,
        store: "InMemoryOperationStore",
        identity: "IdentityContract278",
        require_separation_of_duties: bool = True,
        approval_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._identity = identity
        self._require_sod = require_separation_of_duties
        self._ttl_seconds = approval_ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def approve(
        self,
        operation_id: str,
        *,
        approval_ctx: object,
        source: str = "web",
    ) -> ApprovalRecord:
        """Bind a human approval to the operation and record it as a conditional transition.

        Raises :class:`ApprovalRejected` (fail-closed) on an untrusted source, an absent
        operation, an unverifiable identity, or a separation-of-duties violation. On success
        the written :class:`ApprovalRecord` is bound to the stored ``Canonical_Hash`` and
        carries an ``expires_at`` (Req 2.3, 2.4).
        """
        # Trusted-surface-only: reject chat/model-originated approval outright (Req 2.2).
        if source not in TRUSTED_APPROVAL_SOURCES:
            raise ApprovalRejected("untrusted_source")

        # Load the operation and its exact diff (its immutable stored files) (Req 2.1).
        operation = self._store.get_operation(operation_id)
        if operation is None:
            raise ApprovalRejected("operation_absent")

        # Derive the approver identity from the trusted context via the #278 seam (Req 2.7).
        approver = self._identity.approver_identity(approval_ctx)
        if not approver.subject or approver.subject == "anonymous":
            raise ApprovalRejected("identity_unverified")

        # Separation of duties: approver must differ from requester when required (Req 2.6).
        separation_ok = approver.subject != operation.requester_identity.subject
        if self._require_sod and not separation_ok:
            raise ApprovalRejected("separation_of_duties")

        now = self._clock()
        approval = ApprovalRecord(
            operation_id=operation_id,
            approver_identity=approver,
            bound_canonical_hash=operation.canonical_hash,
            approved_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self._ttl_seconds)).isoformat(),
            separation_of_duties_ok=separation_ok,
        )

        # Conditional approval transition — a separate record that never mutates the prepared
        # content (Req 6.4, 8.2). Emits the durable dispatch stream event.
        self._store.apply_approval_transition(operation_id, approval)
        return approval

    def load_exact_diff(self, operation: "PreparedOperation") -> tuple[str, ...]:
        """Return the exact diff (stored file paths) rendered for human review (Req 2.1).

        The exact content the approver reviews is the operation's immutable stored ``files``;
        this convenience returns the affected paths for the review surface without exposing any
        credential or provider artifact.
        """
        return tuple(f.path for f in operation.files)
