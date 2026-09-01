"""Semantic rules for the source-control prepared-operation profile."""

from __future__ import annotations

# Standard library
import hashlib
import posixpath
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

# Local modules
from operations.contracts.canonical import canonical_sha256

BRANCH_PREFIX = "gba-op-"
BRANCH_DIGEST_LENGTH = 20
MAX_RENDERED_DIFF_BYTES = 5_000_000

ContentResolver = Callable[[str], bytes]


def source_control_branch_name(operation_id: str) -> str:
    """Derive a deterministic, non-secret provider branch from an operation ID."""
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"{BRANCH_PREFIX}{digest[:BRANCH_DIGEST_LENGTH]}"


def is_normalized_repository_path(path: str) -> bool:
    """Return whether a path is normalized, relative POSIX repository syntax."""
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return False
    return posixpath.normpath(path) == path


def source_control_content_material(operation: dict[str, Any]) -> dict[str, Any]:
    """Return the exact change material used only for duplicate-content policy."""
    target = operation["target"]
    parameters = operation["parameters"]
    files = sorted(
        (
            {
                "path": file["path"],
                "content_hash": file["content_hash"],
            }
            for file in parameters["files"]
        ),
        key=lambda file: file["path"],
    )
    return {
        "profile": operation["profile"],
        "action": operation["action"],
        "target": {
            "provider": target["provider"],
            "repository_id": target["repository_id"],
            "target_branch": target["target_branch"],
            "base_revision": target["base_revision"],
        },
        "files": files,
        "diff_hash": parameters["diff"]["diff_hash"],
    }


def source_control_content_hash(operation: dict[str, Any]) -> str:
    """Hash source change content without assigning workflow identity."""
    return canonical_sha256(source_control_content_material(operation))


def source_control_request_semantic_errors(request: dict[str, Any]) -> list[str]:
    """Return path errors in an untrusted source-control proposal."""
    errors: list[str] = []
    paths: set[str] = set()
    for index, file in enumerate(request["proposal"]["files"]):
        path = file["path"]
        if not is_normalized_repository_path(path):
            errors.append(f"proposal.files[{index}].path is not a normalized relative POSIX path")
        if path in paths:
            errors.append(f"proposal.files[{index}].path duplicates another file")
        paths.add(path)
    return errors


def source_control_semantic_errors(operation: dict[str, Any]) -> list[str]:
    """Return semantic errors that JSON Schema cannot express."""
    errors: list[str] = []
    parameters = operation["parameters"]

    paths: set[str] = set()
    for index, file in enumerate(parameters["files"]):
        path = file["path"]
        if not is_normalized_repository_path(path):
            errors.append(f"parameters.files[{index}].path is not a normalized relative POSIX path")
        if path in paths:
            errors.append(f"parameters.files[{index}].path duplicates another file")
        paths.add(path)

        if "content" in file:
            expected_hash = f"sha256:{hashlib.sha256(file['content'].encode('utf-8')).hexdigest()}"
            if file["content_hash"] != expected_hash:
                errors.append(f"parameters.files[{index}].content_hash does not match content")

    diff = parameters["diff"]
    if "rendered_diff" in diff:
        expected_hash = f"sha256:{hashlib.sha256(diff['rendered_diff'].encode('utf-8')).hexdigest()}"
        if diff["diff_hash"] != expected_hash:
            errors.append("parameters.diff.diff_hash does not match rendered_diff")

    expected_branch = source_control_branch_name(operation["operation_id"])
    if operation["target"]["proposal_branch"] != expected_branch:
        errors.append("target.proposal_branch is not derived from operation_id")

    expected_content_hash = source_control_content_hash(operation)
    if operation["duplicate_content_hash"] != expected_content_hash:
        errors.append("duplicate_content_hash does not match the source-control content material")

    return errors


def _resolve_content(
    reference: str,
    expected_hash: str,
    field_path: str,
    content_resolver: ContentResolver | None,
    errors: list[str],
) -> bytes | None:
    if content_resolver is None:
        errors.append(f"{field_path} cannot be verified without a content resolver")
        return None
    try:
        content = content_resolver(reference)
    except Exception:  # A resolver is an external trust boundary; normalize all failures.
        errors.append(f"{field_path} could not be resolved")
        return None
    if not isinstance(content, bytes):
        errors.append(f"{field_path} did not resolve to bytes")
        return None
    actual_hash = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if actual_hash != expected_hash:
        errors.append(f"{field_path} does not match its bound hash")
    return content


def source_control_playbook_binding_errors(
    operation: dict[str, Any],
    playbook: dict[str, Any],
    content_resolver: ContentResolver | None = None,
) -> list[str]:
    """Return source-control content errors against one exact playbook."""
    errors: list[str] = []
    limits = playbook["hard_limits"]
    parameters = operation["parameters"]
    files = parameters["files"]

    if len(files) > limits["max_files"]:
        errors.append(f"parameters.files exceeds playbook max_files ({limits['max_files']})")

    total_bytes = 0
    allowed_extensions = set(limits["allowed_extensions"])
    for index, file in enumerate(files):
        path = file["path"]
        extension = PurePosixPath(path).suffix
        if extension not in allowed_extensions:
            errors.append(f"parameters.files[{index}].path extension is not allowed by the playbook")

        if "content" in file:
            content_bytes = file["content"].encode("utf-8")
        else:
            content_bytes = _resolve_content(
                file["content_ref"],
                file["content_hash"],
                f"parameters.files[{index}].content_ref",
                content_resolver,
                errors,
            )

        if content_bytes is None:
            continue
        size = len(content_bytes)
        total_bytes += size
        if size > limits["max_file_bytes"]:
            errors.append(f"parameters.files[{index}] exceeds playbook max_file_bytes ({limits['max_file_bytes']})")

    if total_bytes > limits["max_total_bytes"]:
        errors.append(f"parameters.files exceeds playbook max_total_bytes ({limits['max_total_bytes']})")

    diff = parameters["diff"]
    if "rendered_diff" in diff:
        diff_bytes = diff["rendered_diff"].encode("utf-8")
    else:
        diff_bytes = _resolve_content(
            diff["diff_ref"],
            diff["diff_hash"],
            "parameters.diff.diff_ref",
            content_resolver,
            errors,
        )
    if diff_bytes is not None and len(diff_bytes) > MAX_RENDERED_DIFF_BYTES:
        errors.append(f"parameters.diff exceeds max rendered diff bytes ({MAX_RENDERED_DIFF_BYTES})")

    return errors
