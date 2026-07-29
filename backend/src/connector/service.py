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

# Standard library
from __future__ import annotations

import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Sequence, TypeVar

# Local modules
from connector.audit import AuditSink
from connector.config import ConnectorConfig
from connector.iac_validation import IaCValidationError, validate_iac
from connector.registry import get_provider
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
from utils.logger import logger
from utils.request_context import get_request_context
from utils.secrets import get_secret
from utils.security import (
    INJECTION_PATTERNS,
    InputValidationError,
    RateLimitExceeded,
    SecurityViolationError,
    check_rate_limit,
    get_rate_limit_key,
    sanitize_log_data,
    validate_prompt,
    verify_request_authorization,
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

# How many times to regenerate a proposal-branch name when it already exists (Req 2.8).
_MAX_BRANCH_NAME_ATTEMPTS = 10

# ---------------------------------------------------------------------------
# Durable audit sink wiring (Req 13.1, 13.2, 13.3)
# ---------------------------------------------------------------------------
#
# Audit entries are written through a durable, confirmed CloudWatch Logs sink
# (``connector.audit.AuditSink``) instead of the fire-and-forget ``logger``. The sink is
# cached per audit-log-group name so repeated proposals reuse one boto3 client and its
# chained sequence token. ``_active_config`` holds the config resolved by the current
# ``propose_change`` call so the signature-stable ``_audit``/``_finalize`` helpers can reach
# ``config.audit_log_group`` without threading it through every terminal call site. Tests may
# monkeypatch ``_get_audit_sink`` (or call ``_reset_audit_sinks``) to inject a fake sink.
_audit_sinks: dict[str, AuditSink] = {}
_active_config: ConnectorConfig | None = None


def _get_audit_sink(config: ConnectorConfig | None) -> AuditSink | None:
    """Return a cached :class:`AuditSink` for ``config.audit_log_group`` (or ``None``).

    The sink is cached per log-group name so a boto3 ``logs`` client and its sequence token
    are reused across proposals. When ``config`` is missing or carries no ``audit_log_group``
    there is no durable target, so ``None`` is returned and the audit write is treated as
    failed by the caller (fail closed — Req 13.3). ``config.load()`` already requires
    ``audit_log_group`` on the enabled path, so in practice a live proposal always has one.
    """
    log_group = getattr(config, "audit_log_group", None) if config is not None else None
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


def _resolve_config(config: ConnectorConfig | None) -> ConnectorConfig:
    """Return the supplied ``config`` or load it fresh from ``GBAW_SCM_*`` env vars.

    Injection is supported so unit/property tests can pass a purpose-built config; in
    production the tools call with no argument and the validated config is loaded here.
    """
    return config if config is not None else ConnectorConfig.load()


def _resolve_provider(
    config: ConnectorConfig,
    provider: SourceControlProvider | None,
) -> SourceControlProvider:
    """Return the supplied ``provider`` or build the adapter for ``config`` (Req 9.4).

    Injection lets tests substitute a ``FakeProvider``; production callers omit it and
    the concrete adapter is selected by :func:`get_provider`.
    """
    return provider if provider is not None else get_provider(config)


def _default_repo_and_branch(config: ConnectorConfig) -> tuple[str, str]:
    """Return the default ``(repository, target_branch)`` selectors for an operation.

    When a caller omits the ``repository``/``target_branch`` selectors, the connector
    defaults to the first allowlist entry and its first branch. These are still only
    *requested* selectors: they are matched against the allowlist and the effective
    repo/branch always come from the matched entry (Req 11.3, 11.4).
    """
    entry = config.allowlist[0]
    return entry.repo, entry.target_branches[0]


def read_iac_files(
    paths: list[str],
    *,
    repository: str | None = None,
    target_branch: str | None = None,
    config: ConnectorConfig | None = None,
    provider: SourceControlProvider | None = None,
) -> FileFetchResult:
    """Fetch existing IaC files from a selected allowlisted repository and target branch.

    Behavior (Req 3.1, 3.2, 3.4, 11.2, 11.3, 11.4, 11.5):

    - If the number of requested ``paths`` exceeds ``config.max_files_per_request``, no
      provider fetch is performed and a :class:`FileFetchResult` with
      ``limit_exceeded=True`` (and empty ``files``/``missing``) is returned (Req 3.2).
    - The requested ``repository``/``target_branch`` selectors are matched against the
      **whole** allowlist by exact, case-sensitive, full-string comparison via
      :func:`_match_allowlist`. When they are omitted, they default to the first allowlist
      entry and its first branch, preserving prior single-entry behavior. On a selector
      MISS (no exact allowlist entry) the read is rejected: no provider fetch is performed,
      the rejection is logged, and an empty :class:`FileFetchResult` is returned (Req 11.5).
    - On a match the configured provider fetches exactly the requested paths from the
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
    if len(paths) > resolved_config.max_files_per_request:
        logger.warning(
            "IaC file read rejected: request exceeds the configured per-request maximum",
            event="scm_read",
            action="read",
            outcome="limit_exceeded",
            requested_count=len(paths),
            max_files_per_request=resolved_config.max_files_per_request,
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=True)

    # Requested selectors default to the first allowlist entry when omitted; when supplied
    # they must match an entry exactly. The effective repo/branch always come from the
    # matched allowlist entry, never from free-form input (Req 11.2, 11.3, 11.4).
    default_repo, default_branch = _default_repo_and_branch(resolved_config)
    req_repo = repository if repository is not None else default_repo
    req_branch = target_branch if target_branch is not None else default_branch

    matched = _match_allowlist(resolved_config, req_repo, req_branch)
    if matched is None:
        # Req 11.5: a read-path selector miss is rejected with NO provider fetch.
        logger.warning(
            "IaC file read rejected: repository/branch not in allowlist",
            event="scm_rejected",
            action="read",
            outcome="rejected",
            repository=req_repo,
            target_branch=req_branch,
            reason="allowlist_miss",
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=False)
    repo, branch = matched

    resolved_provider = _resolve_provider(resolved_config, provider)

    # Req 3.1 / 3.4: fetch exactly the requested paths from the matched repo+branch.
    # The provider reports missing paths in the result; no proposal is created here.
    result = resolved_provider.get_files(repo, branch, list(paths))

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
#   5. rate limit          — per-user sliding window
#   6. credential fetch    — get_secret; fail-closed on failure/timeout
#   7. IaC validation      — decline empty file sets; validate parseable/structural IaC
#   8. provider ops        — latest_commit_sha → unique branch → create_branch →
#                            commit_files → open_change_proposal, transient-only retries
#   9. success audit       — audit-write failure aborts atomically
#
# Every terminal path returns a secret-free :class:`ProposalResult` and writes an
# audit entry; a failed audit write turns any outcome into an audit-persistence error
# so success is never reported without a durable audit record (Req 6.4).


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
            # to retry, so an already-applied effect is never duplicated (Req 12.1, 12.5).
            reconciled = reconcile()
            if reconciled is not None:
                return reconciled
            if attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            time.sleep(delay + random.uniform(0, delay * 0.25))
    raise last_exc  # pragma: no cover - loop always returns or raises above


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
    13.2, 13.3). ``_finalize`` uses this to abort the action atomically rather than reporting
    success without a durable audit record.

    Every string field is passed through ``sanitize_log_data`` as defense-in-depth so no
    secret can leak into the audit log (Req 6.6); the SCM_Credential is never placed in a
    field in the first place. A timestamp is always attached (Req 6.3). The sink sanitizes
    again, which is harmless.

    A best-effort, non-durable ``logger`` line is also emitted for local/operator visibility;
    it never gates the action — only the confirmed sink write does.
    """
    safe_fields = {
        key: (sanitize_log_data(value) if isinstance(value, str) else value)
        for key, value in fields.items()
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


def _audit_persistence_error() -> ProposalResult:
    """Return the audit-persistence error result used when an audit write fails (Req 6.4)."""
    return ProposalResult(
        status="error",
        proposal_id=None,
        proposal_url=None,
        message=(
            "The change proposal could not be completed because its audit record could not "
            "be persisted. No proposal was reported as successful."
        ),
    )


def _finalize(
    result: ProposalResult,
    *,
    level: str,
    message: str,
    **fields: object,
) -> ProposalResult:
    """Write the terminal audit entry and return ``result`` (or an audit-persistence error).

    Centralizes the audit-then-return contract for every terminal path so that an
    audit-write failure uniformly aborts the action and returns an audit-persistence error
    instead of the intended result — never reporting success without a durable record
    (Req 6.4, Property 22).
    """
    if not _audit(level, message, **fields):
        return _audit_persistence_error()
    return result


def _looks_injected(*texts: str) -> bool:
    """Return ``True`` if any of ``texts`` matches a known injection pattern (Req 11.4)."""
    for text in texts:
        if not text:
            continue
        for pattern in _INJECTION_RE:
            if pattern.search(text):
                return True
    return False


def _match_allowlist(
    config: ConnectorConfig,
    req_repo: str,
    req_branch: str,
) -> tuple[str, str] | None:
    """Return the effective ``(repo, branch)`` for an exact allowlist match, else ``None``.

    The match is **case-sensitive, full-string** on both the repository and the branch, with
    no partial/prefix/substring/wildcard matching (Req 5.2). The returned values are taken
    from the matched allowlist entry, so a source-control operation is only ever issued
    against an operator-approved repository/branch regardless of model or user input
    (Req 5.5, 11.5).
    """
    for entry in config.allowlist:
        if entry.repo == req_repo:
            for branch in entry.target_branches:
                if branch == req_branch:
                    return entry.repo, branch
    return None


def _slug(text: str, *, max_length: int = 40) -> str:
    """Turn free-text ``intent`` into a branch-safe slug for the proposal branch name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or "change"


def _generate_branch_name(intent: str) -> str:
    """Generate a unique-ish proposal-branch name ``gbaw/<slug>-<utc>-<rand>`` (Req 2.8)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"gbaw/{_slug(intent)}-{stamp}-{secrets.token_hex(4)}"


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
    repository: str | None = None,
    target_branch: str | None = None,
    config: ConnectorConfig | None = None,
    provider: SourceControlProvider | None = None,
) -> ProposalResult:
    """Run the full fail-closed propose pipeline and open exactly one Change_Proposal.

    Gates execute in the exact order documented at the top of this section; on any failure
    the Connector performs no further source-control operation, leaves no partial proposal
    reported as success, records an audit entry, and returns a typed, secret-free
    :class:`ProposalResult`.

    ``repository``/``target_branch`` are the *requested* values (from the agent/tool layer);
    they are only used to select an allowlist entry by exact match — the effective repo and
    branch always come from the matched allowlist entry, never from this input (Req 5.5,
    11.5). ``config`` and ``provider`` are optional injection points for testing; production
    callers omit them.
    """
    global _active_config
    resolved_config = _resolve_config(config)
    # Publish the resolved config so the signature-stable ``_audit``/``_finalize`` helpers can
    # reach ``config.audit_log_group`` for the durable sink write (Req 13.1).
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
        return _finalize(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message="The request was rejected by input validation and no proposal was created.",
            ),
            level="warning",
            message="Change proposal rejected by input validation",
            event="scm_rejected",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            reason="input_validation_failed",
        )

    if _looks_injected(intent, title, description):
        return _finalize(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message="The request was rejected by prompt-injection detection and no proposal was created.",
            ),
            level="warning",
            message="Change proposal rejected by prompt-injection detection",
            event="scm_rejected",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            reason="injection_pattern_detected",
        )

    # --- Gate 3: authorization (Req 7.1-7.4) --------------------------------------------
    authorized = verify_request_authorization(
        user_id,
        required_groups=list(resolved_config.authorized_groups),
        user_groups=user_groups,
        require_authentication=True,
    )
    if not authorized:
        reason = "unauthenticated" if not user_id else "not_in_authorized_group"
        return _finalize(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message="You are not authorized to propose infrastructure changes.",
            ),
            level="warning",
            message="Change proposal rejected by authorization gate",
            event="scm_rejected",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            reason=reason,
        )

    # --- Gate 4: allowlist exact match (Req 5.2, 5.3, 11.5, 11.6) ------------------------
    # Requested repo/branch default to the first allowlist entry when the caller omits them
    # (production tools operate on the configured repo); when supplied they must match an
    # entry exactly. The effective repo/branch always come from the matched entry.
    default_entry: AllowlistEntry = resolved_config.allowlist[0]
    req_repo = repository if repository is not None else default_entry.repo
    req_branch = target_branch if target_branch is not None else default_entry.target_branches[0]

    matched = _match_allowlist(resolved_config, req_repo, req_branch)
    if matched is None:
        return _finalize(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message="The requested repository or branch is not in the allowlist; request rejected.",
            ),
            level="warning",
            message="Change proposal rejected: repository/branch not in allowlist",
            event="scm_rejected",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            repository=req_repo,
            target_branch=req_branch,
            reason="allowlist_miss",
        )
    repo, branch = matched

    # --- Gate 5: per-user rate limit (Req 8.1, 8.2) -------------------------------------
    try:
        check_rate_limit(
            get_rate_limit_key(user_id, _RATE_LIMIT_ENDPOINT),
            resolved_config.rate_limit_max,
            resolved_config.rate_limit_window_seconds,
        )
    except RateLimitExceeded:
        reset_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=resolved_config.rate_limit_window_seconds)
        ).isoformat()
        return _finalize(
            ProposalResult(
                status="rejected",
                proposal_id=None,
                proposal_url=None,
                message=(
                    f"Rate limit reached: at most {resolved_config.rate_limit_max} change "
                    f"proposals per {resolved_config.rate_limit_window_seconds}s. "
                    f"Capacity resets by {reset_at}."
                ),
            ),
            level="warning",
            message="Change proposal rejected by rate limit",
            event="scm_rejected",
            action="decline",
            outcome="rejected",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            reason="rate_limit_exceeded",
        )

    # --- Gate 6: credential retrieval, fail-closed (Req 4.6) ----------------------------
    # The credential is fetched (and cached) from Secrets Manager before any provider op so
    # a retrieval failure aborts with no branch/PR. The value itself is NEVER placed in an
    # audit field or the returned message (Req 4.7, 6.6).
    try:
        credential = get_secret(
            resolved_config.credential_secret_id, source="secretsmanager"
        )
    except Exception:  # noqa: BLE001 - any retrieval failure is fail-closed
        credential = None
    if not credential:
        return _finalize(
            ProposalResult(
                status="error",
                proposal_id=None,
                proposal_url=None,
                message="The source-control credential could not be retrieved; no proposal was created.",
            ),
            level="error",
            message="Change proposal aborted: credential retrieval failed",
            event="scm_credential_error",
            action="create",
            outcome="error",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            reason="credential_retrieval_failed",
        )

    # --- Gate 7: IaC validation; decline empty file sets (Req 2.7, 11.1, 11.2) ----------
    proposed_files = list(files)
    if not proposed_files:
        return _finalize(
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
            event="scm_proposal",
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
        return _finalize(
            ProposalResult(
                status="declined",
                proposal_id=None,
                proposal_url=None,
                message=(
                    f"The proposed IaC failed validation for '{exc.file}': {exc.reason}. "
                    f"No proposal was created."
                ),
            ),
            level="warning",
            message="Change proposal declined: IaC validation failed",
            event="scm_proposal",
            action="decline",
            outcome="declined",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            reason=f"iac_validation_failed:{exc.file}",
        )

    # --- Gate 8: provider operations (transient-only retries) ---------------------------
    resolved_provider = _resolve_provider(resolved_config, provider)
    attempts = resolved_config.retry_max_attempts

    effective_title = title.strip() or f"Proposed IaC change: {_slug(intent)}"
    body = _build_change_proposal_body(description, intent, proposed_files, user_id or "unknown")
    commit_message = effective_title

    branch_created = False
    proposal_branch = ""
    try:
        # Base the proposal branch on the latest commit of the target branch (Req 3.3).
        base_sha = _retry_transient(
            lambda: resolved_provider.latest_commit_sha(repo, branch),
            max_attempts=attempts,
        )

        # Generate a unique proposal-branch name, regenerating on collision (Req 2.8).
        for _ in range(_MAX_BRANCH_NAME_ATTEMPTS):
            candidate = _generate_branch_name(intent)
            exists = _retry_transient(
                lambda c=candidate: resolved_provider.branch_exists(repo, c),
                max_attempts=attempts,
            )
            if not exists:
                proposal_branch = candidate
                break
        if not proposal_branch:
            raise ProviderError("could not generate a unique proposal branch name")

        # Create the branch, then commit the complete file set (Req 2.1, 2.3). These are
        # MUTATING ops, so each retry reconciles provider state first to avoid duplicates
        # after an ambiguous transient failure (Req 12.1, 12.2, 12.3, 12.5).

        # Reconcile create_branch: if the proposal branch now exists, the create already
        # landed — skip it and continue from the existing branch (Req 12.2).
        def _reconcile_branch() -> object | None:
            exists = _retry_transient(
                lambda: resolved_provider.branch_exists(repo, proposal_branch),
                max_attempts=attempts,
            )
            return _ALREADY_APPLIED if exists else None

        _retry_mutating(
            lambda: resolved_provider.create_branch(repo, proposal_branch, base_sha),
            _reconcile_branch,
            max_attempts=attempts,
        )
        branch_created = True

        # Reconcile commit_files with a best-effort heuristic (Req 12.3): the proposal
        # branch was created pointing at ``base_sha``; if its head has since advanced to a
        # different SHA, a commit beyond the base was applied, so treat the commit as done
        # and skip it. This can only mistake a concurrent external push on the freshly
        # created proposal branch for our commit, which is acceptable for a proposal branch
        # the connector owns; the alternative (blindly re-committing) risks a duplicate
        # commit, which Req 12.3/12.5 forbid.
        def _reconcile_commit() -> object | None:
            head = _retry_transient(
                lambda: resolved_provider.latest_commit_sha(repo, proposal_branch),
                max_attempts=attempts,
            )
            return _ALREADY_APPLIED if head and head != base_sha else None

        _retry_mutating(
            lambda: resolved_provider.commit_files(
                repo, proposal_branch, proposed_files, commit_message
            ),
            _reconcile_commit,
            max_attempts=attempts,
        )

        # Open exactly one change proposal with agent+user attribution (Req 2.2, 2.6, 6.5).
        # Reconcile open_change_proposal: if an open proposal for head→base already exists,
        # return it instead of opening a duplicate (Req 12.4).
        def _reconcile_proposal() -> ChangeProposalResult | None:
            return _retry_transient(
                lambda: resolved_provider.find_open_change_proposal(
                    repo, proposal_branch, branch
                ),
                max_attempts=attempts,
            )

        proposal = _retry_mutating(
            lambda: resolved_provider.open_change_proposal(
                repo, proposal_branch, branch, effective_title, body
            ),
            _reconcile_proposal,
            max_attempts=attempts,
        )
    except ProviderAuthError:
        # Invalid/unauthorized credential — never retried (Req 10.2).
        return _finalize(
            _provider_error_result(
                "The source-control provider rejected the credential; no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider rejected credential",
            event="scm_proposal",
            action="create",
            outcome="error",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            proposal_branch=proposal_branch or None,
            branch_created=branch_created,
            reason="provider_auth_error",
        )
    except ProviderUnavailableError:
        # Provider unreachable / timed out (Req 10.1) — safe, non-destructive.
        return _finalize(
            _provider_error_result(
                "The source-control provider is unavailable; no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider unavailable",
            event="scm_proposal",
            action="create",
            outcome="error",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            proposal_branch=proposal_branch or None,
            branch_created=branch_created,
            reason="provider_unavailable",
        )
    except ProviderConflictError:
        # Conflict (Req 10.4) — existing target content preserved, no destructive resolution.
        return _finalize(
            _provider_error_result(
                "The proposal could not be applied cleanly due to a conflict; existing content "
                "was preserved and no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider reported a conflict",
            event="scm_proposal",
            action="create",
            outcome="error",
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
        return _finalize(
            _provider_error_result(
                "The source-control operation failed; no proposal was completed.",
                branch_created,
            ),
            level="error",
            message="Change proposal failed: provider operation error",
            event="scm_proposal",
            action="create",
            outcome="error",
            requesting_user=user_id or "anonymous",
            repository=repo,
            target_branch=branch,
            proposal_branch=proposal_branch or None,
            branch_created=branch_created,
            reason="provider_operation_failed",
        )

    # --- Gate 9: success audit; audit-write failure aborts atomically (Req 6.3, 6.4) ----
    return _finalize(
        ProposalResult(
            status="created",
            proposal_id=proposal.proposal_id,
            proposal_url=proposal.proposal_url,
            message=(
                f"Opened change proposal {proposal.proposal_id} against {repo}@{branch}. "
                f"It is unmerged and awaiting human review."
            ),
        ),
        level="info",
        message="Change proposal created",
        event="scm_proposal",
        action="create",
        outcome="created",
        requesting_user=user_id or "anonymous",
        repository=repo,
        target_branch=branch,
        proposal_branch=proposal_branch,
        proposal_id=proposal.proposal_id,
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
