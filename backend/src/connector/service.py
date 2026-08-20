"""Connector service layer — provider-agnostic read orchestration.

This module holds the connector's public read entry point that the agent-facing ``@tool``
functions call. It is deliberately provider-agnostic: it resolves the validated
:class:`~connector.config.SourceControlConfig`, selects a concrete
:class:`~connector.provider.SourceControlReader` via
:func:`~connector.registry.get_provider`, and speaks only in the agnostic data models from
:mod:`connector.models`.

Per Architecture Update v1.3 the provider-WRITE path has been removed from the chat runtime
(it now lives in a separate operations control plane / isolated executor, issue #314). Only
the read path ships here:

- :func:`read_iac_files` — the **read path**. Fetches existing IaC files from an allowlisted
  repository/target branch so the agent can review the current source of truth. Every read
  is authorized across seven dimensions (tenant, workspace, repository, branch, path,
  extension, group), size-bounded, rate-limited, and durably audited as an ``scm_read``
  event. It performs no mutation and returns no write-usable revision.

Identity, tenant, and workspace come from the trusted request context populated per the
Read_Path_Contract_278 seam (:func:`_read_path_context`) — never from tool/model arguments.
"""

from __future__ import annotations

# Standard library
import posixpath
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Sequence

# Local modules
from connector.audit import AuditSink
from connector.config import AuthorizationPolicy, Decision, SourceControlConfig
from connector.models import FileFetchResult
from connector.registry import get_provider
from utils.logger import logger
from utils.request_context import get_request_context
from utils.security import (
    RateLimitExceeded,
    check_rate_limit,
    get_rate_limit_key,
    sanitize_log_data,
)

if TYPE_CHECKING:
    # Local modules
    from connector.provider import SourceControlReader

__all__ = ["read_iac_files"]

# The rate-limit endpoint label for per-requester READ limiting.
_RATE_LIMIT_ENDPOINT = "scm_read"


class PathTraversalError(ValueError):
    """A requested path is unsafe (absolute, escaping, or contains an illegal character).

    Raised by :func:`_normalize_path` and converted by :func:`read_iac_files` into a
    fail-closed empty result with a ``path_invalid`` audit, so a prompt-injected request
    cannot read outside the repository root.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.requested_path = path
        self.reason = reason
        super().__init__(f"unsafe path rejected ({reason})")

# ---------------------------------------------------------------------------
# Durable audit sink wiring (repurposed for scm_read events)
# ---------------------------------------------------------------------------
#
# Audit entries are written through a durable CloudWatch Logs sink
# (``connector.audit.AuditSink``). The sink is cached per audit-log-group name so repeated
# reads reuse one boto3 client and its chained sequence token. ``_active_config`` holds the
# config resolved by the current ``read_iac_files`` call so the signature-stable ``_audit``
# helper can reach ``config.connector.audit_log_group`` without threading it through every
# call site. Tests may monkeypatch ``_get_audit_sink`` (or call ``_reset_audit_sinks``) to
# inject a fake sink.
_audit_sinks: dict[str, AuditSink] = {}
_active_config: SourceControlConfig | None = None


def _get_audit_sink(config: SourceControlConfig | None) -> AuditSink | None:
    """Return a cached :class:`AuditSink` for ``config.connector.audit_log_group`` (or ``None``).

    The sink is cached per log-group name so a boto3 ``logs`` client and its sequence token
    are reused across reads. When ``config`` is missing or carries no ``audit_log_group``
    there is no durable target, so ``None`` is returned. ``SourceControlConfig.load()``
    already requires ``audit_log_group`` on the enabled path, so a live read always has one.
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


def _resolve_reader(
    config: SourceControlConfig,
    reader: "SourceControlReader | None",
) -> "SourceControlReader":
    """Return the supplied ``reader`` or build the adapter for ``config``.

    Injection lets tests substitute a ``FakeReader``; production callers omit it and the
    concrete read adapter is selected by :func:`get_provider`.
    """
    return reader if reader is not None else get_provider(config)


def _default_repo_and_branch(config: SourceControlConfig) -> tuple[str, str]:
    """Return the default ``(repository, target_branch)`` selectors for a read.

    When a caller omits the ``repository``/``target_branch`` selectors, the connector
    defaults to the first allowlist entry and its first branch. These are still only
    *requested* selectors: they are matched against the allowlist and the effective
    repo/branch always come from the matched entry.
    """
    entry = config.domain.authorization_policy[0]
    return entry.repo, entry.target_branches[0]


def _read_path_context() -> tuple[str | None, list[str], str, str]:
    """Return ``(user_id, groups, tenant, workspace)`` from the trusted request context.

    This is the single place the connector depends on the Read_Path_Contract_278 shape:
    the requester identity, groups, tenant, and workspace are derived **only** from the
    request-scoped identity context, never from model/tool input, so a prompt-injected model
    cannot influence authorization. Any non-list ``groups`` value is normalized to a list.
    """
    ctx = get_request_context()
    user_id = ctx.get("user_id")
    groups = ctx.get("groups") or []
    if not isinstance(groups, list):
        groups = list(groups) if isinstance(groups, (tuple, set)) else []
    tenant = ctx.get("tenant") or ""
    workspace = ctx.get("workspace") or ""
    return user_id, groups, tenant, workspace


def _normalize_path(path: str) -> str:
    """Normalize a requested path to a canonical repo-relative POSIX path.

    Collapses ``.``/``..`` segments and duplicate slashes and strips leading slashes so
    authorization prefix checks and the subsequent provider read operate on the same
    canonical form. An empty/whitespace path normalizes to an empty string.

    Rejects — rather than silently rewrites — any path that attempts to escape the repository
    root via ``..``, or that contains a NUL or backslash. A leading ``/`` is treated as
    repo-relative (stripped), matching the pre-existing contract; it is not itself an escape and
    the seven-dimension authorization still constrains where the read may land. Because the
    requested paths originate from model/tool output, silently accepting ``../../secrets`` would
    let a prompt-injected request read outside the intended tree; failing closed with
    :class:`PathTraversalError` keeps the read confined to the repo. Callers convert this into a
    fail-closed empty result with an audit.
    """
    if not path or not path.strip():
        return ""
    if "\x00" in path or "\\" in path:
        raise PathTraversalError(path, "path contains an illegal character")
    # A leading "/" is treated as repo-relative (the pre-existing contract strips it), so an
    # absolute-looking path is not itself an escape — it is normalized to repo-relative and the
    # seven-dimension authorization still constrains it. What must be rejected is a path that
    # escapes the repository root via "..".
    normalized = posixpath.normpath(path)
    if normalized == ".":
        return ""
    stripped = normalized.lstrip("/")
    # After normalization + lstrip, a path that still begins with ".." points above the root.
    if stripped == ".." or stripped.startswith("../"):
        raise PathTraversalError(path, "path escapes the repository root")
    return stripped


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string for audit timestamps."""
    return datetime.now(timezone.utc).isoformat()


def _audit(level: str, message: str, /, **fields: object) -> bool:
    """Write one ``scm_read`` audit entry to the durable sink; return the confirmed result.

    The structured event (``message`` + ``level`` + sanitized ``fields`` + ``timestamp``) is
    written through the durable CloudWatch Logs sink (:func:`_get_audit_sink`). Every string
    field is passed through ``sanitize_log_data`` as defense-in-depth so no credential value
    can leak into the audit log; the read credential is never placed in a field in the first
    place. A best-effort, non-durable ``logger`` line is also emitted for local visibility.

    Unlike the removed write path, a read has already occurred and is non-mutating, so the
    caller does **not** gate the read on the returned confirmation; the durable-audit path is
    best-effort for reads.
    """
    safe_fields = {
        key: (sanitize_log_data(value) if isinstance(value, str) else value) for key, value in fields.items()
    }
    safe_fields.setdefault("timestamp", _now_iso())

    # Non-durable local visibility only.
    try:
        getattr(logger, level)(message, **safe_fields)
    except Exception:  # noqa: BLE001 - local logging must never affect the read
        pass

    sink = _get_audit_sink(_active_config)
    if sink is None:
        return False
    event = {"message": message, "level": level, **safe_fields}
    return sink.write(event)


def authorize_operation(
    config: SourceControlConfig,
    *,
    tenant: str,
    workspace: str,
    req_repo: str,
    req_branch: str,
    paths: Sequence[str],
    groups: Sequence[str],
) -> Decision:
    """Authorize a read against all seven dimensions before any adapter op.

    Wraps the domain contract's operator-approved allowlist in an
    :class:`~connector.config.AuthorizationPolicy` and evaluates the requested
    ``(tenant, workspace, repo, branch, paths)`` and requesting ``groups`` against
    ``config.domain.authorized_groups``. The returned :class:`Decision` carries the effective
    repo/branch from the matched entry on success, or the failed dimension on denial.
    """
    policy = AuthorizationPolicy(entries=config.domain.authorization_policy)
    return policy.authorize(
        tenant=tenant,
        workspace=workspace,
        repo=req_repo,
        branch=req_branch,
        paths=list(paths),
        groups=list(groups),
        authorized_groups=list(config.domain.authorized_groups),
    )


def _content_size(result: FileFetchResult) -> int:
    """Return the total byte size of all fetched file contents in ``result``."""
    return sum(len(f.content.encode("utf-8")) for f in result.files)


def read_iac_files(
    paths: list[str],
    *,
    repository: str | None = None,
    target_branch: str | None = None,
    config: SourceControlConfig | None = None,
    reader: "SourceControlReader | None" = None,
) -> FileFetchResult:
    """Fetch existing IaC files from a selected allowlisted repository and target branch.

    Behavior:

    - Each requested path is **normalized** (canonical repo-relative POSIX form) before
      authorization and before any provider read, so authorization and the fetch operate on
      the same canonical path.
    - If the number of requested ``paths`` exceeds ``config.connector.max_files_per_request``,
      no provider fetch is performed and a :class:`FileFetchResult` with
      ``limit_exceeded=True`` is returned.
    - A per-requester **read rate limit** (endpoint ``scm_read``) is enforced; a request
      beyond the configured maximum is rejected with no provider read and a ``rate_limited``
      audit.
    - The requested ``repository``/``target_branch`` selectors, the normalized ``paths``, and
      the requester's tenant/workspace/groups (sourced only from the #278 request context) are
      enforced against all **seven** authorization dimensions (tenant, workspace, repository,
      branch, path, extension, group). On a violation of **any** dimension the read is
      rejected before any provider read, an ``scm_read`` rejection audit naming the failed
      dimension is written, and an empty :class:`FileFetchResult` is returned.
    - On authorization the configured reader fetches exactly the normalized paths from the
      **matched allowlist entry's** repository/branch (never free-form input). A result whose
      total content size exceeds ``config.connector.max_content_bytes`` is rejected
      (``size_exceeded`` audit) with no files served. Otherwise the served read is durably
      audited (requester/tenant/workspace/effective repo/effective branch/normalized paths)
      and returned. The result carries no write-usable revision.

    ``config`` and ``reader`` are optional injection points for testing; production callers
    omit them so the validated config is loaded and the concrete read adapter selected
    automatically.
    """
    global _active_config
    resolved_config = _resolve_config(config)
    # Publish the resolved config so the signature-stable ``_audit`` helper can reach the
    # durable sink's audit log group.
    _active_config = resolved_config

    # Normalize each requested path BEFORE the count check, authorization, and any read so
    # every downstream stage operates on the canonical form. A path that attempts to escape
    # the repository root (absolute, ``..`` escape, or illegal character) is rejected here and
    # the whole request fails closed with no provider read — never silently rewritten.
    try:
        normalized_paths = [_normalize_path(p) for p in paths]
    except PathTraversalError as exc:
        _audit(
            "warning",
            "IaC file read rejected: unsafe path",
            event="scm_read",
            action="read",
            outcome="rejected",
            reason="path_invalid",
            detail=exc.reason,
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=False)

    # Reject an over-limit request BEFORE contacting the provider.
    if len(normalized_paths) > resolved_config.connector.max_files_per_request:
        logger.warning(
            "IaC file read rejected: request exceeds the configured per-request maximum",
            event="scm_read",
            action="read",
            outcome="limit_exceeded",
            requested_count=len(normalized_paths),
            max_files_per_request=resolved_config.connector.max_files_per_request,
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=True)

    # Identity, tenant, workspace, and groups come ONLY from the trusted #278 request
    # context, never from model/tool input.
    user_id, groups, tenant, workspace = _read_path_context()
    requester = user_id or "anonymous"

    # Per-requester read rate limit: an over-limit request is rejected with no provider read.
    try:
        check_rate_limit(
            get_rate_limit_key(requester, _RATE_LIMIT_ENDPOINT),
            resolved_config.connector.rate_limit_max,
            resolved_config.connector.rate_limit_window_seconds,
        )
    except RateLimitExceeded:
        reset_at = (
            datetime.now(timezone.utc) + timedelta(seconds=resolved_config.connector.rate_limit_window_seconds)
        ).isoformat()
        _audit(
            "warning",
            "IaC file read rejected by rate limit",
            event="scm_read",
            action="read",
            outcome="rejected",
            requester=requester,
            tenant=tenant,
            workspace=workspace,
            reason="rate_limited",
            reset_at=reset_at,
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=False)

    # Requested selectors default to the first allowlist entry when omitted; when supplied
    # they must match an entry exactly. The effective repo/branch always come from the
    # matched allowlist entry, never from free-form input.
    default_repo, default_branch = _default_repo_and_branch(resolved_config)
    req_repo = repository if repository is not None else default_repo
    req_branch = target_branch if target_branch is not None else default_branch

    # Enforce all seven authorization dimensions BEFORE any provider read.
    decision = authorize_operation(
        resolved_config,
        tenant=tenant,
        workspace=workspace,
        req_repo=req_repo,
        req_branch=req_branch,
        paths=normalized_paths,
        groups=groups,
    )
    if not decision.allowed:
        # A violation of any dimension rejects with NO provider read, naming the dimension.
        _audit(
            "warning",
            "IaC file read rejected: authorization policy denied the request",
            event="scm_read",
            action="read",
            outcome="rejected",
            requester=requester,
            tenant=tenant,
            workspace=workspace,
            repository=req_repo,
            target_branch=req_branch,
            normalized_paths=list(normalized_paths),
            reason=decision.failed_dimension,
            failed_dimension=decision.failed_dimension,
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=False)
    repo, branch = decision.repo, decision.branch

    resolved_reader = _resolve_reader(resolved_config, reader)

    # Fetch exactly the normalized paths from the matched repo+branch. The reader reports
    # missing paths in the result; no write-usable revision is captured.
    result = resolved_reader.get_files(repo, branch, list(normalized_paths))

    # Reject a read whose total content exceeds the configured maximum size, with no files
    # served.
    if _content_size(result) > resolved_config.connector.max_content_bytes:
        _audit(
            "warning",
            "IaC file read rejected: content exceeds the configured maximum size",
            event="scm_read",
            action="read",
            outcome="rejected",
            requester=requester,
            tenant=tenant,
            workspace=workspace,
            repository=repo,
            target_branch=branch,
            normalized_paths=list(normalized_paths),
            reason="size_exceeded",
        )
        return FileFetchResult(files=(), missing=(), limit_exceeded=False)

    # Durable audit of the served read.
    if result.files:
        outcome = "served"
    elif result.missing:
        outcome = "not_found"
    else:
        outcome = "served"
    _audit(
        "info",
        "IaC file read served",
        event="scm_read",
        action="read",
        outcome=outcome,
        requester=requester,
        tenant=tenant,
        workspace=workspace,
        repository=repo,
        target_branch=branch,
        normalized_paths=list(normalized_paths),
        found_count=len(result.files),
        missing=list(result.missing),
    )

    return result
