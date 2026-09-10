"""Agent-facing ``@tool`` functions for the Source Control Connector (read-only).

This is the only Connector layer the Orchestrator/LLM sees. The single tool exposes the
read operation with a **provider-agnostic, primitive/JSON-serialisable** signature — no
provider-specific type appears in the tool name, parameters, or return value. The tool
merely adapts primitive input into the connector's agnostic data models, delegates to the
provider-agnostic service layer, and adapts the service's structured result back into a
plain, JSON-serialisable ``dict``.

Contract:

    get_iac_file(paths: list[str]) -> dict
        {"files": [{"path","content"}], "missing": [...], "limit_exceeded": bool,
         "error": <optional>}

The real enforcement (enablement, path normalization, seven-dimension authorization,
size/rate limits, read-credential handling, durable audit) lives in
:mod:`connector.service`; this tool only steers the LLM via its docstring. Every result is
returned as a structured, secret-free ``dict`` — the tool **never raises to the model**: any
unexpected exception is caught and converted into a safe error dict so the agent can relay a
clean message rather than crashing the tool call.

The provider-write path (proposing IaC changes as pull requests) has been removed from the
chat runtime and moved to a separate operations control plane / isolated executor (#314).
"""

from __future__ import annotations

# Third-party packages
from strands import tool

# Local modules
from connector.service import read_iac_files
from utils.logger import logger


@tool
def get_iac_file(
    paths: list[str],
    repository: str | None = None,
    target_branch: str | None = None,
) -> dict:
    """Read existing IaC file(s) from an allowlisted repository/branch for review.

    Use this to review the current Infrastructure-as-Code source of truth when answering
    IaC-context questions. Reads are always scoped to an operator-approved allowlist entry;
    the ``paths`` you pass select which files to read, not which repository. Every read is
    authorized against the full seven-dimension policy — tenant, workspace, repository,
    branch, path prefix, file extension, and your authorized group membership — before any
    file is fetched; a request that violates any dimension returns no files.

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
        "limit_exceeded": bool}``. ``missing`` lists any requested path that does not exist.
        ``limit_exceeded`` is ``True`` when the request asked for more files than allowed, in
        which case no read was performed. On an unexpected failure the dict instead contains
        an ``"error"`` message with empty ``files``/``missing``.
    """
    try:
        result = read_iac_files(list(paths), repository=repository, target_branch=target_branch)
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
