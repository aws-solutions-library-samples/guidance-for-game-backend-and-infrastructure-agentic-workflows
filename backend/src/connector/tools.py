"""Agent-facing ``@tool`` functions for the Source Control Connector.

This is the only Connector layer the Orchestrator/LLM sees (design.md → Agent-facing
tools). The two tools expose the read and propose operations with **provider-agnostic,
primitive/JSON-serialisable** signatures — no provider-specific type appears in any tool
name, parameter, or return value (Req 9.1). Each tool merely adapts primitive input into
the connector's agnostic data models, delegates to the provider-agnostic service layer,
and adapts the service's structured result back into a plain, JSON-serialisable ``dict``.

Contract (design.md):

    get_iac_file(paths: list[str]) -> dict
        {"files": [{"path","content"}], "missing": [...], "limit_exceeded": bool,
         "error": <optional>}

    propose_infrastructure_change(intent, files, iac_format, title, description) -> dict
        {"status", "proposal_id", "proposal_url", "message"}

The real enforcement (enablement, validation, authorization, allowlist, rate limit,
credential handling, IaC validation, audit) lives in :mod:`connector.service`; these
tools only steer the LLM via their docstrings. Every result is returned as a structured,
secret-free ``dict`` — the tools **never raise to the model**: any unexpected exception is
caught and converted into a safe error dict so the agent can relay a clean message rather
than crashing the tool call.
"""

# Standard library
from __future__ import annotations

# Third-party packages
from strands import tool

# Local modules
from connector.models import ProposedFile
from connector.service import propose_change, read_iac_files
from utils.logger import logger


@tool
def get_iac_file(
    paths: list[str],
    repository: str | None = None,
    target_branch: str | None = None,
) -> dict:
    """Read existing IaC file(s) from an allowlisted repository/branch for review.

    Use this before proposing a change so your proposal is consistent with the current
    source of truth. Reads are always scoped to an operator-approved allowlist entry; the
    ``paths`` you pass select which files to read, not which repository.

    Args:
        paths: Repository-relative file paths to read (e.g. ``["infra/vpc.yaml"]``).
            The number of paths is capped by the configured per-request maximum.
        repository: Optionally selects which allowlisted repository to read from. The
            value must exactly match a configured allowlist entry (case-sensitive,
            full-string); a value that is not on the allowlist is rejected and no read is
            performed. When omitted, the first allowlist entry is used.
        target_branch: Optionally selects which allowlisted target branch to read from,
            matched the same exact way against the selected repository's branches. When
            omitted, the first branch of the selected repository is used. The effective
            repository/branch always come from the matched allowlist entry, never from
            free-form input.

    Returns:
        A JSON-serialisable dict:
        ``{"files": [{"path": str, "content": str}], "missing": [str, ...],
        "limit_exceeded": bool}``. ``missing`` lists any requested path that does not
        exist (no proposal is created for a read). ``limit_exceeded`` is ``True`` when the
        request asked for more files than allowed, in which case no read was performed. On
        an unexpected failure the dict instead contains an ``"error"`` message with empty
        ``files``/``missing``.
    """
    try:
        result = read_iac_files(
            list(paths), repository=repository, target_branch=target_branch
        )
        return {
            "files": [{"path": f.path, "content": f.content} for f in result.files],
            "missing": list(result.missing),
            "limit_exceeded": result.limit_exceeded,
        }
    except Exception:  # noqa: BLE001 - tools must never raise to the model
        logger.exception("get_iac_file failed unexpectedly", event="scm_read", action="read")
        return {
            "files": [],
            "missing": [],
            "limit_exceeded": False,
            "error": "The IaC files could not be read due to an internal error.",
        }


@tool
def propose_infrastructure_change(
    intent: str,
    files: list[dict],
    iac_format: str,
    title: str,
    description: str,
    repository: str | None = None,
    target_branch: str | None = None,
) -> dict:
    """Open a change proposal for Infrastructure-as-Code changes for human review.

    This never mutates live AWS resources. It creates a uniquely-named branch off the
    selected allowlisted target branch, commits the proposed files, and opens exactly one
    unmerged change proposal attributed to the agent on behalf of the requesting user. The
    change flows through review and the existing CI/CD pipeline after a human approves and
    merges.

    Args:
        intent: A short natural-language description of the change being proposed.
        files: The complete set of modified IaC files, each a dict
            ``{"path": str, "content": str, "iac_format"?: str}``. When a file omits
            ``"iac_format"`` the top-level ``iac_format`` argument is used for it.
        iac_format: The default IaC format for the files, one of
            ``{"cloudformation", "terraform"}``.
        title: A non-empty change proposal title.
        description: A description identifying the intended change and affected files.
        repository: Optionally selects which allowlisted repository the proposal targets.
            The value must exactly match a configured allowlist entry (case-sensitive,
            full-string); a value that is not on the allowlist is rejected and no proposal
            is created. When omitted, the first allowlist entry is used.
        target_branch: Optionally selects which allowlisted target branch the proposal is
            opened against, matched the same exact way against the selected repository's
            branches. When omitted, the first branch of the selected repository is used.
            The effective repository/branch always come from the matched allowlist entry,
            never from free-form input.

    Returns:
        A JSON-serialisable dict:
        ``{"status": str, "proposal_id": str | None, "proposal_url": str | None,
        "message": str}``. ``status`` is one of ``"created"``, ``"declined"``,
        ``"rejected"``, or ``"error"``; ``message`` is safe to relay and never contains
        secrets. On an unexpected failure a safe ``"error"`` dict is returned instead.
    """
    try:
        proposed_files = [
            ProposedFile(
                path=str(entry.get("path", "")),
                content=str(entry.get("content", "")),
                iac_format=str(entry.get("iac_format") or iac_format),
            )
            for entry in (files or [])
        ]

        result = propose_change(
            intent=intent,
            files=proposed_files,
            iac_format=iac_format,
            title=title,
            description=description,
            repository=repository,
            target_branch=target_branch,
        )
        return {
            "status": result.status,
            "proposal_id": result.proposal_id,
            "proposal_url": result.proposal_url,
            "message": result.message,
        }
    except Exception:  # noqa: BLE001 - tools must never raise to the model
        logger.exception(
            "propose_infrastructure_change failed unexpectedly",
            event="scm_proposal",
            action="create",
        )
        return {
            "status": "error",
            "proposal_id": None,
            "proposal_url": None,
            "message": "The change proposal could not be completed due to an internal error.",
        }
