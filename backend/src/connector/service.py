"""Connector service layer — provider-agnostic orchestration.

This module holds the connector's public entry points that the agent-facing ``@tool``
functions call. It is deliberately provider-agnostic: it resolves the validated
:class:`~connector.config.ConnectorConfig`, selects a concrete
:class:`~connector.provider.SourceControlProvider` via
:func:`~connector.registry.get_provider`, and speaks only in the agnostic data
models from :mod:`connector.models` (Req 9.1).

Two public operations live here:

- :func:`read_iac_files` — the **read path** (this task). Fetches existing IaC files from
  the configured repository/target branch so the agent can review the current source of
  truth before proposing changes (Req 3.1, 3.2, 3.4).
- ``propose_change`` — the **propose path** (added by task 8.1). It will run the full
  fail-closed safety pipeline (enablement → input validation → authorization → allowlist →
  rate limit → credential fetch → IaC validation → provider ops → audit) alongside
  :func:`read_iac_files` in this same module.

Both operations share the small helpers below (config/provider resolution and the
"configured repository + target branch" selection), so the propose path can reuse them
without duplication.

Design contract for the read path (see
``.kiro/specs/source-control-connector/design.md`` → Connector service):

    def read_iac_files(paths: list[str]) -> FileFetchResult:
        # Req 3.1: fetch up to config.max_files_per_request from configured repo+branch
        # Req 3.2: reject if len(paths) > max -> limit_exceeded result (no provider call)
        # Req 3.4: missing files -> not-found result listing each, no proposal
"""

from __future__ import annotations

# Standard library
import hashlib
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Sequence, TypeVar, cast

# Local modules
from connector.audit import AuditSink
from connector.config import AuthorizationPolicy, Decision, SourceControlConfig
from connector.iac_validation import IaCValidationError, validate_iac
from connector.models import (
    ChangeProposalResult,
    FileFetchResult,
    ProposalResult,
    ProposedFile,
)
from connector.provider import (
    ProviderAuthError,
    ProviderConflictError,
    ProviderError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from connector.registry import get_provider
from utils.logger import logger
from utils.request_context import get_request_context
from utils.security import (
    INJECTION_PATTERNS,
    InputValidationError,
    RateLimitExceeded,
    SecurityViolationError,
    check_rate_limit,
    get_rate_limit_key,
    sanitize_log_data,
    validate_prompt,
)

if TYPE_CHECKING:
    # Local modules
    from connector.config import AllowlistEntry
    from connector.provider import SourceControlProvider

__all__ = ["read_iac_files", "propose_change"]

_T = TypeVar("_T")

# The rate-limit endpoint label for per-user proposal limiting (Req 8.1).
_RATE_LIMIT_ENDPOINT = "scm_propose"

# Precompiled injection patterns reused for the tool-boundary re-check (Req 11.3, 11.4).
_INJECTION_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in INJECTION_PATTERNS)

# The stable prefix for every deterministic proposal-branch name (Req 8.1, 8.2).
_PROPOSAL_BRANCH_PREFIX = "gbaw"

# How many hex characters of the idempotency-key digest are appended to the proposal-branch
# name. 12 hex chars (48 bits) is ample to keep distinct proposals on distinct branches while
# keeping the deterministic branch name short and readable.
_BRANCH_KEY_LENGTH = 12

# ---------------------------------------------------------------------------
# Durable audit sink wiring (Req 13.1, 13.2, 13.3)
# ---------------------------------------------------------------------------
#
# Audit entries are written through a durable, confirmed CloudWatch Logs sink
# (``connector.audit.AuditSink``) instead of the fire-and-forget ``logger``. The sink is
# cached per audit-log-group name so repeated proposals reuse one boto3 client and its
# chained sequence token. ``_active_config`` holds the config resolved by the current
# ``propose_change`` call so the signature-stable ``_audit``/``_record_intent``/
# ``_record_outcome`` helpers can reach
# ``config.audit_log_group`` without threading it through every terminal call site. Tests may
# monkeypatch ``_get_audit_sink`` (or call ``_reset_audit_sinks``) to inject a fake sink.
_audit_sinks: dict[str, AuditSink] = {}
_active_config: SourceControlConfig | None = None


def _get_audit_sink(config: SourceControlConfig | None) -> AuditSink | None:
    """Return a cached :class:`AuditSink` for ``config.connector.audit_log_group`` (or ``None``).

    The sink is cached per log-group name so a boto3 ``logs`` client and its sequence token
    are reused across proposals. When ``config`` is missing or carries no ``audit_log_group``
    there is no durable target, so ``None`` is returned and the audit write is treated as
    failed by the caller (fail closed — Req 13.3). ``SourceControlConfig.load()`` already
    requires ``audit_log_group`` on the enabled path, so a live proposal always has one.
    """
    connector = getattr(config, "connector", None) if config is not None else None
    log_group = getattr(connector, "audit_log_group", None) if connector is not None else None
    if not log_group:
        return None
    sink = _audit_sinks.get(log_group)
    if sink is None:
        sink = AuditSink(log_group)
        _audit_sinks[log_group] = sink
    return sink


def _reset_audit_sinks() -> None:
    """Clear the cached sinks and active config (test hook)."""
    global _active_config
    _audit_sinks.clear()
    _active_config = None


def _resolve_config(config: SourceControlConfig | None) -> SourceControlConfig:
    """Return the supplied ``config`` or load it fresh from ``GBAW_SCM_*`` env vars.

    Injection is supported so unit/property tests can pass a purpose-built config; in
    production the tools call with no argument and the validated config is loaded here.
    """
    return config if config is not None else SourceControlConfig.load()


def _resolve_provider(
    config: SourceControlConfig,
    provider: SourceControlProvider | None,
) -> SourceControlProvider:
    """Return the supplied ``provider`` or build the adapter for ``config`` (Req 9.4).

    Injection lets tests substitute a ``FakeProvider``; production callers omit it and
    the concrete adapter is selected by :func:`get_provider`.
    """
    return provider if provider is not None else get_provider(config)


def _default_repo_and_branch(config: SourceControlConfig) -> tuple[str, str]:
    """Return the default ``(repository, target_branch)`` selectors for an operation.

    When a caller omits the ``repository``/``target_branch`` selectors, the connector
    defaults to the first allowlist entry and its first branch. These are still only
    *requested* selectors: they are matched against the allowlist and the effective
    repo/branch always come from the matched entry (Req 11.3, 11.4). The allowlist now
    lives on the domain contract's ``authorization_policy``.
    """
    entry = config.domain.authorization_policy[0]
    return entry.repo, entry.target_branches[0]


def _context_groups() -> list[str]:
    """Return the requesting user's groups from the trusted request context (Req 5.1).

    Groups are derived **only** from the request-scoped identity context, never from
    model/tool input, so a prompt-injected model cannot influence authorization. Any
    non-list value is normalized to a list (or an empty list) for the policy check.
    """
    ctx = get_request_context()
    groups = ctx.get("groups") or []
    if not isinstance(groups, list):
        groups = list(groups) if isinstance(groups, (tuple, set)) else []
    return groups


def authorize_operation(
    config: SourceControlConfig,
    *,
    req_repo: str,
    req_branch: str,
    paths: Sequence[str],
    groups: Sequence[str],
) -> Decision:
    """Authorize an operation against all five dimensions before any adapter op (Req 6).

    Wraps the domain contract's operator-approved allowlist in an
    :class:`~connector.config.AuthorizationPolicy` and evaluates the requested
    ``(repo, branch, paths)`` and requesting ``groups`` against
    ``config.domain.authorized_groups``. The same helper is called at the top of **both**
    :func:`read_iac_files` and :func:`propose_change`, so reads and writes enforce the
    identical repository/branch/path/extension/group policy (Req 6.1, 6.2). The returned
    :class:`Decision` carries the effective repo/branch from the matched entry on success,
    or the failed dimension on denial (Req 6.3).
    """
    policy = AuthorizationPolicy(entries=config.domain.authorization_policy)
    return policy.authorize(
        repo=req_repo,
        branch=req_branch,
        paths=list(paths),
        groups=list(groups),
        authorized_groups=list(config.domain.authorized_groups),
    )


def read_iac_files(
    paths: list[str],
    *,
    repository: str | None = None,
    target_branch: str | None = None,
    config: SourceControlConfig | None = None,
    provider: SourceControlProvider | None = None,
) -> FileFetchResult:
    """Fetch existing IaC files from a selected allowlisted repository and target branch.

    Behavior (Req 3.1, 3.2, 3.4, 11.2, 11.3, 11.4, 11.5):

    - If the number of requested ``paths`` exceeds ``config.max_files_per_request``, no
      provider fetch is performed and a :class:`FileFetchResult` with
      ``limit_exceeded=True`` (and empty ``files``/``missing``) is returned (Req 3.2).
    - The requested ``repository``/``target_branch`` selectors, the requested ``paths``, and
      the requesting user's groups are enforced against all five authorization dimensions
      (repository, branch, path, extension, group) via :func:`authorize_operation`, exactly
      as the propose path is (Req 6.1). When the selectors are omitted, they default to the
      first allowlist entry and its first branch. On a violation of **any** dimension the
      read is rejected: no provider fetch is performed, the rejection is logged naming the
      failed dimension, and an empty :class:`FileFetchResult` is returned (Req 6.3). The
      group dimension now runs on reads too — an unauthenticated caller (no intersecting
      groups) is rejected.
    - On authorization the configured provider fetches exactly the requested paths from the
      **matched allowlist entry's** repository/branch (never free-form input); the returned
      result carries the files that were found and names every path that does not exist in
      ``missing``, without creating any proposal (Req 3.1, 3.4, 11.4).

    ``config`` and ``provider`` are optional injection points for testing; production
    callers omit them so the validated config is loaded and the concrete adapter selected
    automatically.
    """
    resolved_config = _resolve_config(config)

    # Req 3.2: reject an over-limit request BEFORE contacting the provider so no
    # source-control fetch is issued when the caller asks for too many files.
    if len(paths) > resolved_config.connector.max_files_per_request:
        logger.warning(
            "IaC file read rejected: request exceeds the configured per-request maximum",
            event="scm_read",
            action="read",
            outcome="limit_exceeded",
            requested_count=len(paths),
            max_files_per_request=resolved_config.connector.max_files_per_request,
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=True)

    # Requested selectors default to the first allowlist entry when omitted; when supplied
    # they must match an entry exactly. The effective repo/branch always come from the
    # matched allowlist entry, never from free-form input (Req 11.2, 11.3, 11.4).
    default_repo, default_branch = _default_repo_and_branch(resolved_config)
    req_repo = repository if repository is not None else default_repo
    req_branch = target_branch if target_branch is not None else default_branch

    # Req 6.1/6.3: enforce all five authorization dimensions (repo · branch · path ·
    # extension · group) BEFORE any provider fetch, identically to the propose path. The
    # group dimension now runs on reads too; identity/groups come only from the request
    # context, never from model/tool input.
    decision = authorize_operation(
        resolved_config,
        req_repo=req_repo,
        req_branch=req_branch,
        paths=paths,
        groups=_context_groups(),
    )
    if not decision.allowed:
        # A violation of any dimension rejects with NO provider fetch, naming the dimension.
        logger.warning(
            "IaC file read rejected: authorization policy denied the request",
            event="scm_rejected",
            action="read",
            outcome="rejected",
            repository=req_repo,
            target_branch=req_branch,
            reason=decision.failed_dimension,
            failed_dimension=decision.failed_dimension,
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=False)
    repo, branch = decision.repo, decision.branch

    resolved_provider = _resolve_provider(resolved_config, provider)

    # Req 3.1 / 3.4: fetch exactly the requested paths from the matched repo+branch.
    # The provider reports missing paths in the result; no proposal is created here.
    fetched = resolved_provider.get_files(repo, branch, list(paths))

    # Req 7.1: capture the Verified_Source_Snapshot — the current head of the target branch
    # at read time — and surface it on the result as an opaque revision token. The agent
    # passes this back as ``base_revision`` when it proposes a change, so the connector can
    # require a read-before-write and reject a stale/unverified proposal.
    revision = resolved_provider.latest_commit_sha(repo, branch)
    result = FileFetchResult(
        files=fetched.files,
        missing=fetched.missing,
        limit_exceeded=fetched.limit_exceeded,
        revision=revision,
    )

    if result.missing:
        logger.info(
            "IaC file read completed with missing files",
            event="scm_read",
            action="read",
            outcome="partial" if result.files else "not_found",
            repository=repo,
            target_branch=branch,
            found_count=len(result.files),
            missing=list(result.missing),
        )

    return result


# ---------------------------------------------------------------------------
# Propose path (the core safety-critical write pipeline)
# ---------------------------------------------------------------------------
#
# ``propose_change`` runs the full fail-closed safety pipeline in EXACTLY this
# order, failing closed at every gate (see design.md → Connector service):
#
#   1. enablement          — disabled connector declines, exposes nothing
#   2. input validation    — validate_prompt (strict) + INJECTION_PATTERNS re-check
#   3. authorization       — identity/groups from the request contextvar only
#   4. allowlist           — exact, case-sensitive, full-string (repo, branch) match;
#                            effective repo/branch come from the matched entry, never input
#   4b. snapshot present    — a Verified_Source_Snapshot (base_revision) must be supplied;
#                            absent -> reject with no adapter op (Req 7.2). Its head match is
#                            re-verified in step 7 before the first mutating op (Req 7.1).
#   5. rate limit          — per-user sliding window
#   6. IaC validation      — decline empty file sets; validate parseable/structural IaC
#   7. provider ops        — latest_commit_sha → stable idempotency key → deterministic
#                            proposal branch → durable INTENT event → create_branch →
#                            commit_files → open_change_proposal, with reconcile-before-run/
#                            -retry so a retried proposal never duplicates branch/commit/
#                            proposal. Credential acquisition is adapter-owned behind
#                            ProviderAuth: a ProviderAuthError here (fail-closed, no retry)
#                            preserves the credential fail-closed behavior the removed Gate 6
#                            provided.
#   8. outcome audit       — durable OUTCOME event after the provider ops resolve
#
# The audit path is a durable INTENT + OUTCOME model with reconciliation, NOT cross-system
# atomicity (Req 9.1, 9.2). A durable INTENT is written before the first mutating op; if that
# write is unconfirmed the action aborts before any mutation (safe — nothing happened yet).
# After the provider ops resolve a durable OUTCOME event is written, correlated to the INTENT
# by the idempotency key. If a mutation succeeded but the OUTCOME write is unconfirmed the
# connector does NOT roll back and does NOT report false success: it returns a reconcilable
# result and leaves the true outcome reconcilable from the INTENT + provider state.
# Rejection/decline paths (which perform no mutation) emit a single OUTCOME event with no
# preceding intent. Every terminal path returns a secret-free :class:`ProposalResult`.


def _retry_transient(
    operation: Callable[[], _T],
    *,
    max_attempts: int,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
) -> _T:
    """Run ``operation`` retrying **only** on :class:`ProviderTransientError` (Req 10.5).

    A minimal, purpose-built retry helper: the shared ``utils.resilience.retry_with_backoff``
    only retries specific boto3 error *names* and would not retry the connector's typed
    ``ProviderTransientError``, so we implement the narrow behavior the pipeline needs here.

    Only transient errors are retried; every other exception (``ProviderAuthError``,
    ``ProviderUnavailableError``, ``ProviderConflictError``, ...) propagates immediately so
    invalid credentials are never retried (Req 10.2) and unavailability is reported at once
    (Req 10.1). The operation is attempted at most ``max_attempts`` times; on the final
    transient failure the error is re-raised (Req 10.6).
    """
    last_exc: ProviderTransientError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except ProviderTransientError as exc:
            last_exc = exc
            if attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            time.sleep(delay + random.uniform(0, delay * 0.25))
    raise last_exc  # pragma: no cover - loop always returns or raises above


# Sentinel returned by a reconcile callable to say "the mutating effect is already applied,
# do NOT repeat the operation" for ops whose real return value is ``None`` (create_branch)
# or otherwise unused (commit_files). It is deliberately distinct from ``None`` so the
# reconcile short-circuit is unambiguous even when the effect carries no meaningful value.
_ALREADY_APPLIED: object = object()


def _retry_mutating(
    operation: Callable[[], _T],
    reconcile: Callable[[], _T | object | None],
    *,
    max_attempts: int,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
) -> _T | object:
    """Run a **mutating** op with reconcile-before-retry so retries never duplicate state.

    This is the idempotent counterpart to :func:`_retry_transient`. It shares the same
    retry-count semantics (at most ``max_attempts`` attempts, only
    :class:`ProviderTransientError` is retried, every other typed error propagates
    immediately — Req 10.2, 10.5, 10.6) but, because repeating a mutating operation after
    an *ambiguous* transient failure could create a duplicate branch/commit/proposal
    (Req 12.5), it **reconciles the provider state before each retry** instead of blindly
    re-invoking the operation (Req 12.1).

    ``reconcile`` is a zero-argument callable that re-queries the provider (a read-only
    operation, safe to repeat) and returns:

    - a **non-``None`` value** when the intended effect is already present on the provider —
      the operation must NOT be repeated. The value is returned to the caller as the
      operation's result. Ops with a meaningful result (``open_change_proposal``) return the
      existing artifact; ops without one return the :data:`_ALREADY_APPLIED` sentinel
      (Req 12.2, 12.3, 12.4).
    - ``None`` when the effect is not yet present — it is safe to retry the operation.

    Reconciliation is also performed once after the final failed attempt, so an effect that
    landed on the last try is recognized rather than reported as a failure.
    """
    last_exc: ProviderTransientError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except ProviderTransientError as exc:
            last_exc = exc
            # Ambiguous transient failure: reconcile provider state BEFORE deciding whether
            # to retry, so an already-applied effect is never duplicated (Req 8.1, 8.2).
            reconciled = reconcile()
            if reconciled is not None:
                return reconciled
            if attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            time.sleep(delay + random.uniform(0, delay * 0.25))
    raise last_exc  # pragma: no cover - loop always returns or raises above


def _idempotent_mutate(
    operation: Callable[[], _T],
    reconcile: Callable[[], _T | object | None],
    *,
    max_attempts: int,
) -> _T | object:
    """Run a mutating op idempotently against BOTH pre-existing state and ambiguous retries.

    Because the proposal branch is now **deterministic** in the stable idempotency key, the
    same logical proposal always targets the same branch/commit/proposal. This helper makes
    each mutating step safe to (re)run without ever creating a duplicate (Req 8.1, 8.2):

    - It **reconciles first**: if ``reconcile()`` reports the intended effect is already
      present — a branch that already exists, a commit already on the deterministic branch, or
      an already-open Change_Proposal for the deterministic head→base — the operation is
      skipped and the reconciled value is returned, so a retried proposal reuses existing
      state instead of duplicating it.
    - Otherwise it runs the op via :func:`_retry_mutating`, which also reconciles before each
      retry so an *ambiguous* transient failure (effect applied, then the provider raised) is
      recognized rather than repeated.
    """
    pre = reconcile()
    if pre is not None:
        return pre
    return _retry_mutating(operation, reconcile, max_attempts=max_attempts)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string for audit timestamps (Req 6.3)."""
    return datetime.now(timezone.utc).isoformat()


def _audit(level: str, message: str, /, **fields: object) -> bool:
    """Write one audit entry to the durable sink, returning its confirmed-write result.

    The structured event (``message`` + ``level`` + sanitized ``fields`` + ``timestamp``) is
    written through the durable, confirmed CloudWatch Logs sink
    (:func:`_get_audit_sink`). The returned ``bool`` is the sink's confirmed-write result:
    ``True`` only when CloudWatch Logs confirmed the write, ``False`` on any unconfirmed write
    or failure — including when no audit log group is configured (fail closed, Req 13.1,
    13.2, 13.3). The two-event audit model uses this: :func:`_record_intent` gates the start
    of a mutation on a confirmed INTENT write (a safe pre-mutation abort — nothing has been
    mutated yet, so this is *not* a cross-system atomicity claim), while
    :func:`_record_outcome` writes the terminal OUTCOME after the provider ops resolve. If a
    mutation has already succeeded but the OUTCOME write is unconfirmed, the connector does
    **not** roll back or claim atomicity: it returns a reconcilable result (Req 9.2).

    Every string field is passed through ``sanitize_log_data`` as defense-in-depth so no
    secret can leak into the audit log (Req 6.6); the SCM_Credential is never placed in a
    field in the first place. A timestamp is always attached (Req 6.3). The sink sanitizes
    again, which is harmless.

    A best-effort, non-durable ``logger`` line is also emitted for local/operator visibility;
    it never gates the action — only the confirmed sink write does.
    """
    safe_fields = {
        key: (sanitize_log_data(value) if isinstance(value, str) else value) for key, value in fields.items()
    }
    safe_fields.setdefault("timestamp", _now_iso())

    # Non-durable local visibility only; its outcome never gates the action.
    try:
        getattr(logger, level)(message, **safe_fields)
    except Exception:  # noqa: BLE001 - local logging must never affect the audit result
        pass

    # The gating result is the CONFIRMED durable write to the dedicated audit log group.
    sink = _get_audit_sink(_active_config)
    if sink is None:
        return False
    event = {"message": message, "level": level, **safe_fields}
    return sink.write(event)


def _record_intent(
    idempotency_key: str,
    *,
    requesting_user: str,
    repository: str,
    target_branch: str,
    base_revision: str,
    paths: Sequence[str],
) -> bool:
    """Write the durable INTENT audit event before the first mutating adapter op (Req 9.1).

    The INTENT records *what the connector is about to attempt* — keyed by the stable
    idempotency key so it correlates with the later OUTCOME — before any branch/commit/
    proposal is created. It carries the requesting user, the effective repo/branch, the
    verified ``base_revision``, and the proposed file **paths** only: never file contents and
    never secrets. Returns the sink's confirmed-write result.

    A confirmed INTENT is a precondition for starting the mutation. If this write is
    unconfirmed the caller aborts *before* any mutation — a genuinely safe fail-closed abort
    (nothing has been mutated), **not** a cross-system atomicity claim: it only means "do not
    start work we cannot audit" (Req 9.2). Together the recorded INTENT plus provider state
    let a later run reconcile an ambiguous outcome via the deterministic proposal branch.
    """
    return _audit(
        "info",
        "Change proposal intent recorded",
        event="scm_intent",
        action="intent",
        idempotency_key=idempotency_key,
        requesting_user=requesting_user,
        repository=repository,
        target_branch=target_branch,
        base_revision=base_revision,
        paths=list(paths),
    )


def _record_outcome(
    idempotency_key: str | None,
    *,
    level: str,
    message: str,
    action: str,
    outcome: str,
    requesting_user: str,
    repository: str | None = None,
    target_branch: str | None = None,
    proposal_branch: str | None = None,
    proposal_id: str | None = None,
    proposal_url: str | None = None,
    reason: str | None = None,
    **extra: object,
) -> bool:
    """Write the durable OUTCOME audit event after the adapter result resolves (Req 9.1).

    The OUTCOME records *what actually happened* — ``outcome`` is one of
    ``created`` / ``declined`` / ``rejected`` / ``error`` / ``reconciled`` — and carries the
    same ``idempotency_key`` as the INTENT so the two events correlate (the key is ``None``
    for reject/decline paths that never reached the mutation stage and therefore have no
    preceding intent). On a created outcome it records the proposal id/url; on a failure it
    records a ``reason``. It never carries file contents or secrets. Returns the sink's
    confirmed-write result so the success path can detect an unconfirmed OUTCOME.
    """
    fields: dict[str, object] = {
        "event": "scm_outcome",
        "action": action,
        "outcome": outcome,
        "requesting_user": requesting_user,
    }
    if idempotency_key is not None:
        fields["idempotency_key"] = idempotency_key
    if repository is not None:
        fields["repository"] = repository
    if target_branch is not None:
        fields["target_branch"] = target_branch
    if proposal_branch is not None:
        fields["proposal_branch"] = proposal_branch
    if proposal_id is not None:
        fields["proposal_id"] = proposal_id
    if proposal_url is not None:
        fields["proposal_url"] = proposal_url
    if reason is not None:
        fields["reason"] = reason
    # ``event`` is always ``scm_outcome`` for this helper; never let a caller override it.
    extra.pop("event", None)
    fields.update(extra)
    return _audit(level, message, **fields)


def _finalize_outcome(
    result: ProposalResult,
    *,
    level: str,
    message: str,
    action: str,
    outcome: str,
    requesting_user: str,
    idempotency_key: str | None = None,
    repository: str | None = None,
    target_branch: str | None = None,
    proposal_branch: str | None = None,
    proposal_id: str | None = None,
    proposal_url: str | None = None,
    reason: str | None = None,
    **extra: object,
) -> ProposalResult:
    """Emit a single terminal OUTCOME audit event and return ``result`` unchanged.

    Used by every rejection / decline / provider-error terminal path. These paths either
    performed no mutation at all (so there is no preceding intent and no atomicity to claim)
    or already failed with a non-success result; in both cases the returned result is itself
    the safe, fail-closed outcome. The durable OUTCOME record is written best-effort and does
    **not** gate the result — the connector makes no cross-system atomicity claim between the
    audit store and the provider (Req 9.2). This is the deliberate reversal of the previous
    "audit-write failure aborts atomically" behavior.
    """
    _record_outcome(
        idempotency_key,
        level=level,
        message=message,
        action=action,
        outcome=outcome,
        requesting_user=requesting_user,
        repository=repository,
        target_branch=target_branch,
        proposal_branch=proposal_branch,
        proposal_id=proposal_id,
        proposal_url=proposal_url,
        reason=reason,
        **extra,
    )
    return result


def _intent_abort_result() -> ProposalResult:
    """Return the safe result used when the pre-mutation INTENT write is unconfirmed (Req 9.2).

    This abort happens **before** any provider mutation, so nothing has been created — it is a
    genuinely safe fail-closed decline, not a cross-system atomicity claim. The connector
    simply refuses to start work it cannot durably audit.
    """
    return ProposalResult(
        status="error",
        proposal_id=None,
        proposal_url=None,
        message=(
            "The change proposal was not started because its intent could not be durably "
            "recorded. No branch, commit, or change proposal was created."
        ),
    )


def _reconcilable_result(
    proposal: ChangeProposalResult,
    repo: str,
    branch: str,
) -> ProposalResult:
    """Return the reconcilable result for a successful mutation with an unconfirmed OUTCOME.

    A change proposal was created on the provider but the terminal OUTCOME audit write was
    not confirmed. The connector does **not** report a false success and does **not** roll
    back the mutation (no cross-system atomicity is claimed — Req 9.2). Instead it returns a
    distinct ``status="reconcilable"`` result: the proposal may exist and is reconcilable from
    the durably recorded INTENT plus provider state (the deterministic proposal branch +
    :meth:`find_open_change_proposal`), and a retry will not create a duplicate.
    """
    return ProposalResult(
        status="reconcilable",
        proposal_id=proposal.proposal_id,
        proposal_url=proposal.proposal_url,
        message=(
            f"A change proposal was created against {repo}@{branch}, but its audit outcome "
            f"could not be durably confirmed. The proposal may exist and is reconcilable from "
            f"the recorded intent and provider state; a retry will not create a duplicate."
        ),
    )


def _looks_injected(*texts: str) -> bool:
    """Return ``True`` if any of ``texts`` matches a known injection pattern (Req 11.4)."""
    for text in texts:
        if not text:
            continue
        for pattern in _INJECTION_RE:
            if pattern.search(text):
                return True
    return False


def _slug(text: str, *, max_length: int = 40) -> str:
    """Turn free-text ``intent`` into a branch-safe slug for the proposal branch name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or "change"


def _idempotency_key(
    *,
    repo: str,
    target_branch: str,
    base_revision: str,
    files: Sequence[ProposedFile],
    user_id: str,
) -> str:
    """Derive a **stable** idempotency key from a proposal's request inputs (Req 8.1, 8.2).

    The key is a SHA-256 over a canonical, order-independent serialization of everything
    that identifies the *logical* proposal:

    ``repo | target_branch | base_revision | sorted(path + ":" + sha256(content)) | user_id``

    Each proposed file contributes ``"<path>:<sha256(content)>"`` and those per-file entries
    are **sorted**, so the key is independent of the order the files were supplied and is
    content-addressed (a different file body yields a different key). Because every component
    comes from the request/snapshot inputs, a retry of the *same* logical proposal — same
    repo, verified base revision, file set, and requesting user — produces the *same* key,
    which anchors the deterministic branch name below and makes the whole mutation idempotent.
    """
    file_entries = sorted(f"{f.path}:{hashlib.sha256(f.content.encode('utf-8')).hexdigest()}" for f in files)
    canonical = "|".join([repo, target_branch, base_revision, "|".join(file_entries), user_id])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deterministic_branch_name(intent: str, idempotency_key: str) -> str:
    """Derive a **deterministic** proposal-branch name from the idempotency key (Req 8.1, 8.2).

    The name is ``gbaw/<slug>-<key[:12]>`` — a stable prefix, a human-readable slug of the
    intent, and a truncated hex digest of the stable :func:`_idempotency_key`. It carries no
    random token and no timestamp, so the *same* logical proposal always maps to the *same*
    branch: ``create_branch`` becomes naturally idempotent (an existing branch is reused, never
    duplicated), a repeated commit targets the same content-addressed tree, and an already-open
    Change_Proposal for the deterministic head→base is returned rather than opened twice. The
    resulting name is always distinct from the target branch it is proposed against.
    """
    return f"{_PROPOSAL_BRANCH_PREFIX}/{_slug(intent)}-{idempotency_key[:_BRANCH_KEY_LENGTH]}"


def _build_change_proposal_body(
    description: str,
    intent: str,
    files: Sequence[ProposedFile],
    user_id: str,
) -> str:
    """Compose a change-proposal description referencing every file plus agent+user attribution.

    Satisfies Req 2.3 (identify the intended change and every affected IaC file) and Req 6.5
    (attribution that the proposal was generated by the Agent on behalf of the Requesting_User).
    """
    file_lines = "\n".join(f"- {f.path}" for f in files)
    return (
        f"{description.strip()}\n\n"
        f"**Intent:** {intent.strip()}\n\n"
        f"**Affected IaC files:**\n{file_lines}\n\n"
        f"---\n"
        f"This change proposal was generated by the GBAW agent on behalf of "
        f"requesting user `{user_id}`. It is unmerged and requires human review before merge."
    )


def propose_change(
    intent: str,
    files: Sequence[ProposedFile],
    iac_format: str,
    title: str,
    description: str,
    *,
    base_revision: str,
    repository: str | None = None,
    target_branch: str | None = None,
    config: SourceControlConfig | None = None,
    provider: SourceControlProvider | None = None,
) -> ProposalResult:
    """Run the full fail-closed propose pipeline and open exactly one Change_Proposal.

    Gates execute in the exact order documented at the top of this section; on any failure
    the Connector performs no further source-control operation, leaves no partial proposal
    reported as success, records an audit entry, and returns a typed, secret-free
    :class:`ProposalResult`.

    ``base_revision`` is the **required** Verified_Source_Snapshot: the opaque source
    revision the agent obtained from a prior read (``FileFetchResult.revision``). After
    authorization and before creating any branch/commit/proposal, the connector requires it
    to be present and to still equal the current head of the target branch; an absent or
    stale ``base_revision`` rejects the proposal without creating a branch, commit, or
    Change_Proposal, and an accepted proposal is anchored to that verified revision
    (Req 7.1, 7.2).

    ``repository``/``target_branch`` are the *requested* values (from the agent/tool layer);
    they are only used to select an allowlist entry by exact match — the effective repo and
    branch always come from the matched allowlist entry, never from this input (Req 5.5,
    11.5). ``config`` and ``provider`` are optional injection points for testing; production
    callers omit them.
    """
    global _active_config
    resolved_config = _resolve_config(config)
    # Publish the resolved config so the signature-stable ``_audit``/``_record_intent``/
    # ``_record_outcome`` helpers can reach ``config.audit_log_group`` for the durable sink
    # write (Req 13.1).
    _active_config = resolved_config

    # --- Gate 1: enablement (Req 1.2) ---------------------------------------------------
    # A disabled connector exposes nothing and declines cleanly. No audit entry is required
    # for the normal off state, but we still return a safe, structured decline.
    if not resolved_config.enabled:
        return ProposalResult(
            status="declined",
            proposal_id=None,
            proposal_url=None,
            message="The source control connector is disabled; no change proposal was created.",
        )

    # Identity is derived strictly from the authenticated request context, never from
    # model/tool input, so a prompt-injected model cannot escalate or redirect a write
    # (Req 7.1, 11.5). Read it up front so every audit entry can attribute the user.
    ctx = get_request_context()
    user_id = ctx.get("user_id")
    user_groups = ctx.get("groups") or []
    if not isinstance(user_groups, list):
        user_groups = list(user_groups) if isinstance(user_groups, (tuple, set)) else []

    # --- Gate 2: input validation / injection re-check (Req 11.3, 11.4) -----------------
    # Guardrails run upstream in invoke_agent; this is the tool-boundary re-check. Any flag
    # rejects the action before any source-control operation is performed.
    try:
        validate_prompt(intent, strict_mode=True)
    except (InputValidationError, SecurityViolationError):
        return _finalize_outcome(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message="The request was rejected by input validation and no proposal was created.",
            ),
            level="warning",
            message="Change proposal rejected by input validation",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            reason="input_validation_failed",
        )

    if _looks_injected(intent, title, description):
        return _finalize_outcome(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message="The request was rejected by prompt-injection detection and no proposal was created.",
            ),
            level="warning",
            message="Change proposal rejected by prompt-injection detection",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            reason="injection_pattern_detected",
        )

    # --- Gate 3: authentication (Req 5.1, 5.2, 7.1) -------------------------------------
    # An authenticated identity is a precondition for authorization. Identity comes only
    # from the request context; an unauthenticated request is rejected before any
    # five-dimension check (a caller could otherwise present intersecting groups without a
    # verified identity). The group dimension itself is enforced by Gate 4 below.
    if not user_id:
        return _finalize_outcome(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message="You are not authorized to propose infrastructure changes.",
            ),
            level="warning",
            message="Change proposal rejected: unauthenticated request",
            action="decline",
            outcome="rejected",
            requesting_user="anonymous",
            reason="unauthenticated",
        )

    # --- Gate 4: five-dimension authorization (Req 6.1, 6.2, 6.3) -----------------------
    # Enforce repository · branch · path · extension · group against the operator-approved
    # policy BEFORE any adapter op, identically to the read path. Requested repo/branch
    # default to the first allowlist entry when the caller omits them; the proposed file
    # paths supply the path/extension dimensions. On any dimension violation the request is
    # rejected with no provider op and a rejection audit that NAMES the failed dimension.
    # The effective repo/branch always come from the matched entry, never from input.
    default_entry: AllowlistEntry = resolved_config.domain.authorization_policy[0]
    req_repo = repository if repository is not None else default_entry.repo
    req_branch = target_branch if target_branch is not None else default_entry.target_branches[0]
    proposed_paths = [f.path for f in files]

    decision = authorize_operation(
        resolved_config,
        req_repo=req_repo,
        req_branch=req_branch,
        paths=proposed_paths,
        groups=user_groups,
    )
    if not decision.allowed:
        return _finalize_outcome(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message=(
                    "The request was not permitted by the authorization policy "
                    f"({decision.failed_dimension}); no proposal was created."
                ),
            ),
            level="warning",
            message="Change proposal rejected by authorization policy",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            repository=req_repo,
            target_branch=req_branch,
            reason=decision.failed_dimension,
            failed_dimension=decision.failed_dimension,
        )
    repo, branch = decision.repo, decision.branch

    # --- Gate 4b: verified source snapshot present (Req 7.1, 7.2) -----------------------
    # A read-before-write is required: the caller must supply the Verified_Source_Snapshot
    # (``base_revision``) it read. An absent/empty snapshot rejects the proposal here, before
    # any adapter op — no branch, commit, or Change_Proposal is created (Req 7.2). The head
    # match itself is re-verified against the current target head in Gate 7, just before the
    # first mutating op (Req 7.1). Identity/effective repo/branch are already resolved so the
    # rejection audit is complete.
    if not base_revision:
        return _finalize_outcome(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message=(
                    "The change proposal requires a verified source snapshot: read the "
                    "target files first and pass the returned revision as base_revision. "
                    "No proposal was created."
                ),
            ),
            level="warning",
            message="Change proposal rejected: missing verified source snapshot",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            reason="missing_snapshot",
        )

    # --- Gate 5: per-user rate limit (Req 8.1, 8.2) -------------------------------------
    try:
        check_rate_limit(
            get_rate_limit_key(user_id, _RATE_LIMIT_ENDPOINT),
            resolved_config.connector.rate_limit_max,
            resolved_config.connector.rate_limit_window_seconds,
        )
    except RateLimitExceeded:
        reset_at = (
            datetime.now(timezone.utc) + timedelta(seconds=resolved_config.connector.rate_limit_window_seconds)
        ).isoformat()
        return _finalize_outcome(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message=(
                    f"Rate limit reached: at most {resolved_config.connector.rate_limit_max} "
                    f"change proposals per "
                    f"{resolved_config.connector.rate_limit_window_seconds}s. "
                    f"Capacity resets by {reset_at}."
                ),
            ),
            level="warning",
            message="Change proposal rejected by rate limit",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            reason="rate_limit_exceeded",
        )

    # --- Gate 6: IaC validation; decline empty file sets (Req 2.7, 11.1, 11.2) ----------
    # NOTE: credential acquisition is no longer a service gate. It is owned entirely by the
    # Provider_Adapter behind the neutral ``ProviderAuth`` contract (Req 11.1): the adapter
    # fetches the credential on its first provider operation and raises ``ProviderAuthError``
    # if acquisition fails. That error is caught in Gate 7 below and mapped to a safe,
    # no-retry error result, so fail-closed credential behavior is preserved without the core
    # ever handling (or importing) ``get_secret``.
    proposed_files = list(files)
    if not proposed_files:
        return _finalize_outcome(
            ProposalResult(
                status="declined",
                proposal_id=None,
                proposal_url=None,
                message=(
                    "The requested change carries no IaC file modifications, so it cannot be "
                    "expressed as a change proposal; no proposal was created."
                ),
            ),
            level="info",
            message="Change proposal declined: no IaC file modifications",
            action="decline",
            outcome="declined",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            reason="empty_file_set",
        )

    try:
        validate_iac(proposed_files, iac_format)
    except IaCValidationError as exc:
        return _finalize_outcome(
            ProposalResult(
                status="declined",
                proposal_id=None,
                proposal_url=None,
                message=(
                    f"The proposed IaC failed validation for '{exc.file}': {exc.reason}. " f"No proposal was created."
                ),
            ),
            level="warning",
            message="Change proposal declined: IaC validation failed",
            action="decline",
            outcome="declined",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            reason=f"iac_validation_failed:{exc.file}",
        )

    # --- Gate 7: provider operations (transient-only retries) ---------------------------
    resolved_provider = _resolve_provider(resolved_config, provider)
    attempts = resolved_config.connector.retry_max_attempts

    effective_title = title.strip() or f"Proposed IaC change: {_slug(intent)}"
    body = _build_change_proposal_body(description, intent, proposed_files, user_id or "unknown")
    commit_message = effective_title

    branch_created = False
    proposal_branch = ""
    # The stable idempotency key correlates the INTENT and OUTCOME audit events. It is derived
    # only after the snapshot is verified (below), so it is ``None`` for any provider error
    # raised by the pre-mutation verification read; those errors emit an OUTCOME with no key.
    idempotency_key: str | None = None
    try:
        # Re-read the current head of the target branch. This single read serves two roles
        # (Req 3.3, 7.1): it is the verification re-read for the Verified_Source_Snapshot AND
        # the base the proposal branch is created from.
        base_sha = _retry_transient(
            lambda: resolved_provider.latest_commit_sha(repo, branch),
            max_attempts=attempts,
        )

        # Req 7.1: verify the snapshot the caller read is still current. If the target head
        # has advanced since the read, the snapshot is STALE — reject without creating any
        # branch/commit/proposal (nothing has been mutated yet). Only on a verified match do
        # we proceed, and the proposal branch is based on that verified revision (which
        # equals ``base_sha``), never on "latest at some later creation time".
        if base_sha != base_revision:
            # No mutation has occurred and no intent was recorded — a single OUTCOME event.
            return _finalize_outcome(
                ProposalResult(
                    status="rejected",
                    proposal_id=None,
                    proposal_url=None,
                    message=(
                        "The change proposal is based on a stale source snapshot: the target "
                        "branch has advanced since it was read. Re-read the files and retry "
                        "with the current revision. No proposal was created."
                    ),
                ),
                level="warning",
                message="Change proposal rejected: stale verified source snapshot",
                action="decline",
                outcome="rejected",
                requesting_user=user_id or "anonymous",
                repository=repo,
                target_branch=branch,
                reason="stale_snapshot",
            )

        # Derive the stable idempotency key and the DETERMINISTIC proposal-branch name from
        # the verified request inputs (Req 8.1, 8.2). The key is content-addressed over the
        # verified ``base_sha`` (== base_revision) and the proposed file bodies, so a retry of
        # the SAME logical proposal maps to the SAME key and therefore the SAME branch. There
        # is no random token and no uniqueness-regeneration loop: the branch name is a pure
        # function of the request, which is what makes the mutation genuinely idempotent.
        idempotency_key = _idempotency_key(
            repo=repo,
            target_branch=branch,
            base_revision=base_sha,
            files=proposed_files,
            user_id=user_id or "unknown",
        )
        proposal_branch = _deterministic_branch_name(intent, idempotency_key)

        # --- Durable INTENT event (Req 9.1, 9.2) ------------------------------------------
        # Record what the connector is about to attempt BEFORE the first mutating adapter op,
        # keyed by the stable idempotency key so it correlates with the OUTCOME below. The
        # INTENT carries the requesting user, effective repo/branch, verified base revision,
        # and proposed file PATHS only (no file contents, no secrets). If this durable write
        # is unconfirmed we abort here — before any branch/commit/proposal exists. That abort
        # is genuinely safe (nothing has been mutated) and is NOT a cross-system atomicity
        # claim: it only means "do not start work we cannot audit". The recorded INTENT plus
        # provider state also make an ambiguous outcome reconcilable later.
        if not _record_intent(
            idempotency_key,
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            base_revision=base_sha,
            paths=proposed_paths,
        ):
            return _intent_abort_result()

        # Create the branch, then commit the complete file set (Req 2.1, 2.3). Both are
        # MUTATING ops run through :func:`_idempotent_mutate`, which reconciles provider state
        # BEFORE running (so pre-existing state from an earlier run is reused, not duplicated)
        # AND before each retry (so an ambiguous transient failure is recognized, not
        # repeated) — never creating a second branch/commit (Req 8.1, 8.2).

        # Reconcile create_branch: the proposal branch is deterministic, so if it already
        # exists (a prior attempt created it) it is REUSED rather than re-created (Req 8.1).
        def _reconcile_branch() -> object | None:
            exists = _retry_transient(
                lambda: resolved_provider.branch_exists(repo, proposal_branch),
                max_attempts=attempts,
            )
            return _ALREADY_APPLIED if exists else None

        _idempotent_mutate(
            lambda: resolved_provider.create_branch(repo, proposal_branch, base_sha),
            _reconcile_branch,
            max_attempts=attempts,
        )
        branch_created = True

        # Reconcile commit_files with a CONTENT-ADDRESSED check (Req 8.1, 8.2): the proposal
        # branch is deterministic and freshly anchored to the verified ``base_sha``, so it is
        # the connector's own branch created for exactly this content. If its head has moved
        # past ``base_sha``, our commit of this proposed tree already landed on it, so the
        # commit is treated as applied and NOT repeated; if the head is still at ``base_sha``
        # the commit has not yet landed and is performed. Because the branch identity is tied
        # to the idempotency key (not a random name), this decision is deterministic and never
        # duplicates the commit.
        def _reconcile_commit() -> object | None:
            head = _retry_transient(
                lambda: resolved_provider.latest_commit_sha(repo, proposal_branch),
                max_attempts=attempts,
            )
            return _ALREADY_APPLIED if head and head != base_sha else None

        _idempotent_mutate(
            lambda: resolved_provider.commit_files(repo, proposal_branch, proposed_files, commit_message),
            _reconcile_commit,
            max_attempts=attempts,
        )

        # Open exactly one change proposal with agent+user attribution (Req 2.2, 2.6, 6.5).
        # Reconcile open_change_proposal: because the head (deterministic proposal branch) and
        # base are fixed, if an open Change_Proposal for this head→base already exists it is
        # RETURNED instead of opening a duplicate (Req 8.1, 8.2).
        def _reconcile_proposal() -> ChangeProposalResult | None:
            return _retry_transient(
                lambda: resolved_provider.find_open_change_proposal(repo, proposal_branch, branch),
                max_attempts=attempts,
            )

        # Both the operation (``open_change_proposal``) and the reconcile
        # (``_reconcile_proposal``) resolve to a ``ChangeProposalResult`` here — the
        # ``_ALREADY_APPLIED`` sentinel is only used by the value-less branch/commit steps —
        # so the widened ``_T | object`` return is narrowed back to the concrete proposal type.
        proposal = cast(
            ChangeProposalResult,
            _idempotent_mutate(
                lambda: resolved_provider.open_change_proposal(repo, proposal_branch, branch, effective_title, body),
                _reconcile_proposal,
                max_attempts=attempts,
            ),
        )
    except ProviderAuthError:
        # Invalid/unauthorized credential — never retried (Req 10.2).
        return _finalize_outcome(
            _provider_error_result(
                "The source-control provider rejected the credential; no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider rejected credential",
            action="create",
            outcome="error",
            idempotency_key=idempotency_key,
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            proposal_branch=proposal_branch or None,
            branch_created=branch_created,
            reason="provider_auth_error",
        )
    except ProviderUnavailableError:
        # Provider unreachable / timed out (Req 10.1) — safe, non-destructive.
        return _finalize_outcome(
            _provider_error_result(
                "The source-control provider is unavailable; no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider unavailable",
            action="create",
            outcome="error",
            idempotency_key=idempotency_key,
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            proposal_branch=proposal_branch or None,
            branch_created=branch_created,
            reason="provider_unavailable",
        )
    except ProviderConflictError:
        # Conflict (Req 10.4) — existing target content preserved, no destructive resolution.
        return _finalize_outcome(
            _provider_error_result(
                "The proposal could not be applied cleanly due to a conflict; existing content "
                "was preserved and no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider reported a conflict",
            action="create",
            outcome="error",
            idempotency_key=idempotency_key,
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            proposal_branch=proposal_branch or None,
            branch_created=branch_created,
            reason="provider_conflict",
        )
    except (ProviderTransientError, ProviderError):
        # Transient retries exhausted (Req 10.5, 10.6) or another provider failure. If the
        # branch was already created but the change proposal was not, we report failure and
        # NEVER report success (Req 10.3).
        return _finalize_outcome(
            _provider_error_result(
                "The source-control operation failed; no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider operation error",
            action="create",
            outcome="error",
            idempotency_key=idempotency_key,
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            proposal_branch=proposal_branch or None,
            branch_created=branch_created,
            reason="provider_operation_failed",
        )

    # --- Gate 8: durable OUTCOME event; NOT cross-system atomicity (Req 9.1, 9.2) --------
    # The mutation succeeded: a Change_Proposal was created on the provider. Write the durable
    # OUTCOME event, correlated to the INTENT by the idempotency key. If this write is
    # unconfirmed we do NOT roll back the mutation and do NOT report a false success — no
    # atomicity is claimed across the audit store and the provider. Instead we return a
    # reconcilable result: the proposal may exist and is reconcilable from the recorded INTENT
    # plus provider state (the deterministic proposal branch + find_open_change_proposal), and
    # a retry will not create a duplicate.
    outcome_confirmed = _record_outcome(
        idempotency_key,
        level="info",
        message="Change proposal created",
        action="create",
        outcome="created",
        requesting_user=user_id or "anonymous",
        repository=repo,
        target_branch=branch,
        proposal_branch=proposal_branch,
        proposal_id=proposal.proposal_id,
        proposal_url=proposal.proposal_url,
    )
    if not outcome_confirmed:
        return _reconcilable_result(proposal, repo, branch)

    return ProposalResult(
        status="created",
        proposal_id=proposal.proposal_id,
        proposal_url=proposal.proposal_url,
        message=(
            f"Opened change proposal {proposal.proposal_id} against {repo}@{branch}. "
            f"It is unmerged and awaiting human review."
        ),
    )


def _provider_error_result(message: str, branch_created: bool) -> ProposalResult:
    """Build a secret-free error result for a failed provider operation.

    ``branch_created`` records whether a branch was created before the failure so callers/
    audit can distinguish an incomplete outcome; the returned result never reports success
    and never carries a proposal id/url (Req 10.3).
    """
    return ProposalResult(
        status="error",
        proposal_id=None,
        proposal_url=None,
        message=message,
    )
