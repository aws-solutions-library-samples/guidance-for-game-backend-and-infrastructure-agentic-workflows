"""Semantic rules for the source-control prepared-operation profile."""

from __future__ import annotations

# Standard library
import hashlib
import posixpath
from typing import Any

# Local modules
from operations.contracts.canonical import canonical_sha256

BRANCH_PREFIX = "gba-op-"
BRANCH_DIGEST_LENGTH = 20


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
