"""In-repo **default / prototype** implementations of the four foundation seams.

.. warning::

   Every adapter in this module is a **default/prototype** implementation whose sole purpose
   is to let the executor and preparation logic run and be property-tested *before* the
   accepted foundation contracts (#277–#280) land. Each is to be **replaced** by the accepted
   concrete contract in the GATED tail (tasks 11.1–11.3 bind #277/#278/#279; task 9.3 / 11.4
   gate provider-write IAM on #280). Do not treat these as the foundation contracts
   themselves — they define nothing that #277–#280 own; they only make the seams runnable.

Provided defaults:

- :class:`DefaultOperationContracts277` (#277) — canonical hash, supported contract versions,
  deterministic ``gbaw/<short-op-id>`` branch name, and ledger-record assembly. The
  canonical-hash *input* reuses the order-independent, content-addressed serialization pattern
  of :func:`connector.service._idempotency_key` (sorted ``path:sha256(content)`` entries joined
  with the operation context), kept **distinct** from the retry idempotency token: the
  canonical hash is the *binding* hash for approval, while the retry token is what makes a
  mutation idempotent. See the baseline construction in :mod:`connector.service`.
- :class:`DefaultIdentityContract278` (#278) — derives requester/approver identities from a
  trusted context mapping/object (never from model input).
- :class:`DefaultThreatsControls280` (#280) — a security gate that is **closed by default**
  (``security_gate_passed() == False``) so no provider-write IAM is attached until the gate is
  explicitly satisfied.
- :func:`default_state_recovery` (#279) — returns a fresh
  :class:`connector.executor.store.InMemoryOperationStore`, the default write-once /
  conditional-transition / append-only store adapter.
"""

from __future__ import annotations

# Standard library
import hashlib
import re
from typing import TYPE_CHECKING, Any

# Local modules
from connector.executor.models import (
    ApproverIdentity,
    LedgerEntry,
    RequesterIdentity,
)
from connector.executor.store import InMemoryOperationStore

if TYPE_CHECKING:
    # Local modules
    from connector.executor.models import PreparedOperation

__all__ = [
    "DEFAULT_CONTRACT_VERSION",
    "DefaultOperationContracts277",
    "DefaultIdentityContract278",
    "DefaultThreatsControls280",
    "default_state_recovery",
]

# The operation-contract version this prototype stamps and accepts. The real supported-version
# set is owned by #277; this default advertises the single version it produces.
DEFAULT_CONTRACT_VERSION = "1.0"

# Branch-name shaping (mirrors the baseline ``gbaw`` prefix in ``connector.service``). The
# short operation-id segment is bounded and provider-safe — NOT content-addressed (Req 6.5, 6.6).
_BRANCH_PREFIX = "gbaw"
_SHORT_OP_ID_LENGTH = 20


def _content_addressed_files(files: Any) -> list[str]:
    """Return sorted ``"<path>:<sha256(content)>"`` entries for the file set.

    This is the order-independent, content-addressed per-file serialization used by the
    baseline :func:`connector.service._idempotency_key`; a different file body yields a
    different entry and the sort makes the result independent of file order.
    """
    return sorted(f"{f.path}:{hashlib.sha256(f.content.encode('utf-8')).hexdigest()}" for f in files)


class DefaultOperationContracts277:
    """Prototype #277 operation-contract adapter (replace with the accepted contract).

    Implements the :class:`connector.executor.seams.OperationContracts277` shape.
    """

    def __init__(self, supported_versions: frozenset[str] | None = None) -> None:
        self._supported = supported_versions or frozenset({DEFAULT_CONTRACT_VERSION})

    def canonical_hash(self, operation: "PreparedOperation") -> str:
        """Return a SHA-256 binding hash over the operation's exact content + context.

        The canonical input mirrors the design's ``canonical_hash_input``: the effective
        ``target_repo``/``target_branch``, the server-fetched ``base_revision``, the sorted
        content-addressed file entries, the ``operation_contract_version``, and the stamped
        ``effective_authority.decision`` (the stamped authority is part of the bound context).
        Because it is content-addressed, a changed file set yields a different hash — and
        therefore a different operation id — so a prior approval cannot authorize changed
        content (Req 2.5, 6.2, 6.3). This is the *binding* hash, distinct from the retry
        idempotency token.
        """
        file_entries = _content_addressed_files(operation.files)
        canonical = "|".join(
            [
                operation.target_repo,
                operation.target_branch,
                operation.base_revision,
                "|".join(file_entries),
                operation.operation_contract_version,
                operation.effective_authority.decision,
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def supported_contract_versions(self) -> frozenset[str]:
        """Return the contract versions this prototype can interpret."""
        return self._supported

    def branch_name(self, operation_id: str) -> str:
        """Return the deterministic, bounded, provider-safe branch ``gbaw/<short-op-id>``.

        The short segment is a sanitized, length-bounded projection of the *operation id
        alone* — never the content — so two operations with the same id derive the same branch
        (a retry targets the same branch) while identical content under different ids derives
        different branches (Req 6.5, 6.6, 10.5).
        """
        short = re.sub(r"[^a-z0-9]+", "-", operation_id.lower()).strip("-")[:_SHORT_OP_ID_LENGTH].strip("-")
        return f"{_BRANCH_PREFIX}/{short or 'op'}"

    def ledger_record(self, **fields: object) -> LedgerEntry:
        """Assemble an append-only :class:`LedgerEntry` from the supplied fields.

        The concrete ledger record contract is owned by #277; this prototype maps the common
        fields onto :class:`LedgerEntry` and folds any extra keyword fields into its
        ``fields`` tuple as stringified ``(key, value)`` pairs.
        """
        known = {"operation_id", "sequence", "event", "outcome", "provider_ref", "idempotency_token", "recorded_at"}
        extra = tuple(sorted((str(k), str(v)) for k, v in fields.items() if k not in known))
        return LedgerEntry(
            operation_id=str(fields["operation_id"]),
            sequence=int(str(fields["sequence"])),
            event=str(fields["event"]),
            outcome=(None if fields.get("outcome") is None else str(fields.get("outcome"))),
            provider_ref=(None if fields.get("provider_ref") is None else str(fields.get("provider_ref"))),
            idempotency_token=(
                None if fields.get("idempotency_token") is None else str(fields.get("idempotency_token"))
            ),
            recorded_at=(None if fields.get("recorded_at") is None else str(fields.get("recorded_at"))),
            fields=extra,
        )


class DefaultIdentityContract278:
    """Prototype #278 identity adapter (replace with the accepted OAuth/OIDC/Cognito provider).

    Implements the :class:`connector.executor.seams.IdentityContract278` shape by reading a
    verified ``subject`` and ``groups`` from a trusted context — a mapping (``subject``/
    ``user_id`` + ``groups``) or any object exposing those attributes. Identities are never
    taken from model/tool input; a missing subject yields an ``"anonymous"`` principal.
    """

    def _read(self, ctx: object) -> tuple[str, tuple[str, ...]]:
        def _get(name: str, default: Any = None) -> Any:
            if isinstance(ctx, dict):
                return ctx.get(name, default)
            return getattr(ctx, name, default)

        subject = _get("subject") or _get("user_id") or "anonymous"
        raw_groups = _get("groups", ()) or ()
        if isinstance(raw_groups, (list, tuple, set)):
            groups = tuple(str(g) for g in raw_groups)
        else:
            groups = ()
        return str(subject), groups

    def requester_identity(self, request_ctx: object) -> RequesterIdentity:
        """Return the verified requester identity from the trusted request context."""
        subject, groups = self._read(request_ctx)
        return RequesterIdentity(subject=subject, groups=groups)

    def approver_identity(self, approval_ctx: object) -> ApproverIdentity:
        """Return the verified approver identity from the trusted approval context."""
        subject, groups = self._read(approval_ctx)
        return ApproverIdentity(subject=subject, groups=groups)


class DefaultThreatsControls280:
    """Prototype #280 security-gate adapter (replace with the accepted controls gate).

    Implements the :class:`connector.executor.seams.ThreatsControls280` shape. The gate is
    **closed by default** so that, until #280's required controls are satisfied, the deployment
    attaches no provider-write IAM permission (Req 13.4). Tests / gated wiring may construct it
    with ``passed=True`` to model the gate having passed.
    """

    def __init__(self, passed: bool = False) -> None:
        self._passed = passed

    def security_gate_passed(self) -> bool:
        """Return ``True`` only when the (modeled) #280 security gate has passed."""
        return self._passed


def default_state_recovery() -> InMemoryOperationStore:
    """Return a fresh default #279 store adapter (:class:`InMemoryOperationStore`).

    The write-once / conditional-transition / append-only semantics of this in-memory store
    are the contract the accepted #279 DynamoDB adapter must preserve (gated task 11.3).
    """
    return InMemoryOperationStore()
