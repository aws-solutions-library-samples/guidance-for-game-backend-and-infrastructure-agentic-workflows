"""Deterministic ``release-manifest.json`` generation and the operations-mode proof.

The manifest is a byte-for-byte reproducible description of the exact tagged
source state. It carries no run IDs, URLs, actor names, timestamps other than
the commit date, account IDs, ARNs, endpoints, or non-public registry hosts.
Its ``hashes.infrastructure_templates`` group pins every ``*.yaml``/``*.yml``
under ``infrastructure/`` -- both CloudFormation templates and the Kubernetes
RBAC manifests.

Determinism rules (all enforced by :func:`serialize_manifest`):

* keys sorted, two-space indentation, ``ensure_ascii=True``, LF line endings,
  single trailing newline.

The operations-mode proof (:func:`prove_operations_mode_disabled`) is a
fail-closed source check: it requires the ADR to declare the default
``disabled`` AND no tracked file to reference ``GBAW_OPERATIONS_MODE`` at all,
because the mode is unimplemented. Every tracked file is scanned regardless of
suffix; the only exemptions are Markdown files and files under the ``docs/``,
``scripts/release/``, and ``scripts/test/`` prefixes (anchored at the
repository root), where documentation, the proof itself, and tests may name the
variable freely. Each candidate path is ``lstat``'d before it is opened:
regular files are read as raw bytes, and a symlink, directory (such as a
submodule gitlink), FIFO, socket, or device fails the proof closed without
being opened or followed. A file that cannot be stat'ed or read is treated as a
reference and fails the proof closed. The manifest records
``default_operations_mode: disabled`` to mean exactly that: the ADR declares the
default and no other tracked file carries an operations-mode reference.
"""

from __future__ import annotations

# Standard library
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_SCHEMA_VERSION = "gbaw.release-manifest.v1"

# Files whose SHA-256 the manifest pins, grouped by category. Paths are
# repo-relative POSIX and are sorted deterministically within each group.
DEPENDENCY_LOCK_FILES = (
    "backend/uv.lock",
    "backend/requirements.txt",
    "ui/package-lock.json",
)
PROMPT_SOURCE_FILES = ("backend/src/agents/optimized_prompts.py",)

_OPERATIONS_MODE_MARKER = "GBAW_OPERATIONS_MODE"

# The marker's byte encodings in every fixed-width text encoding GitHub-hosted
# tooling could plausibly emit, with and without BOM handled by substring
# search (a BOM is a prefix, so searching for the un-BOM'd encoded bytes still
# matches a file that starts with one). Searching raw bytes means an undecodable
# or deliberately mis-encoded file (e.g. UTF-16LE PowerShell) cannot smuggle the
# marker past a text-only scan, and binary files are covered without decoding.
_OPERATIONS_MODE_MARKER_BYTES = tuple(
    _OPERATIONS_MODE_MARKER.encode(encoding)
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
)

# Path prefixes (POSIX, repo-relative, anchored at the repository root) whose
# files may freely reference the variable without being a deployed default:
# documentation and ADRs discuss it, the release tooling's own proof names it,
# and tests exercise it. A path merely *containing* one of these fragments
# elsewhere (e.g. ``backend/src/operations/docs/...``) is NOT exempt.
_OPS_MODE_ALLOWED_PATH_PREFIXES = (
    "docs/",
    "scripts/release/",
    "scripts/test/",
)


class OperationsModeProofError(RuntimeError):
    """Raised when the default operations mode cannot be proven ``disabled``."""


@dataclass(frozen=True)
class CheckRunResult:
    name: str
    conclusion: str


def sha256_file(repo_root: Path, relative: str) -> str:
    path = repo_root / relative
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def discover_infrastructure_templates(repo_root: Path) -> list[str]:
    base = repo_root / "infrastructure"
    if not base.is_dir():
        return []
    templates = [p.relative_to(repo_root).as_posix() for p in base.rglob("*.yaml")] + [
        p.relative_to(repo_root).as_posix() for p in base.rglob("*.yml")
    ]
    return sorted(templates)


def _hash_group(repo_root: Path, relatives: list[str]) -> dict[str, str]:
    return {rel: sha256_file(repo_root, rel) for rel in sorted(relatives)}


def _ops_mode_reference_is_allowed(relative_posix: str) -> bool:
    """A reference is allowed only in Markdown, docs/, the release tooling, or tests.

    Markdown files anywhere may discuss the variable. Otherwise only files
    whose path *starts with* an allowed prefix are exempt, so a code module
    nested under a ``docs/`` directory deeper in the tree is still scanned.
    """

    if relative_posix.lower().endswith(".md"):
        return True
    return any(relative_posix.startswith(prefix) for prefix in _OPS_MODE_ALLOWED_PATH_PREFIXES)


def _tracked_files(repo_root: Path) -> list[str]:
    """Return repo-relative POSIX paths of every tracked file at this checkout.

    Uses ``git ls-files`` so exactly the committed tree is scanned (ignored and
    untracked files are irrelevant to what a release pins). When the directory
    is not a git work tree (as in unit-test fixtures), fall back to walking the
    filesystem so every file is still scanned. The walk fails CLOSED on any
    directory it cannot list (``os.walk``'s ``onerror`` re-raises), so an
    unreadable directory can never silently hide a file -- unlike
    ``Path.rglob``/``Path.is_file``, which swallow ``PermissionError`` on some
    Python versions.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
        entries = completed.stdout.decode("utf-8", "surrogateescape").split("\0")
        files = [entry for entry in entries if entry]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass

    def _reraise(error: OSError) -> None:
        raise OperationsModeProofError(
            f"a directory under the release tree could not be listed for the operations-mode scan: {error.filename}"
        ) from error

    walked: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(repo_root, onerror=_reraise):
        for name in filenames:
            walked.append((Path(dirpath) / name).relative_to(repo_root).as_posix())
    return sorted(walked)


def _ops_mode_scan_bytes(repo_root: Path, relative_posix: str) -> bytes:
    """Return the bytes to scan for a candidate path, failing closed on trouble.

    Each candidate is ``lstat``'d BEFORE anything is opened, so a FIFO, socket,
    or device is never opened (opening a FIFO with no writer would block the
    whole scan forever). Behaviour by file type:

    * a regular file is read as raw bytes;
    * a symlink fails the proof CLOSED without being read or followed. Its
      link text is not scanned (that would miss a marker in the real target)
      and the link is not followed (that could escape the tree or double-scan
      another file). The release tree contains no symlinks, so a tracked
      symlink is an unexpected shape that must stop the proof, not pass it;
    * a directory candidate (in git mode a submodule gitlink is listed as a
      directory path) fails the proof CLOSED, because the directory's own
      bytes are never scanned and its contents may be outside this checkout;
    * a FIFO, socket, or device fails the proof CLOSED without being opened;
    * any ``lstat``/read error (an unreadable path, or a file inside an
      unreadable directory) fails the proof CLOSED.
    """

    path = repo_root / relative_posix
    try:
        st = os.lstat(path)
    except OSError as error:
        raise OperationsModeProofError(f"{relative_posix} could not be stat'ed for the operations-mode scan") from error

    mode = st.st_mode
    if stat.S_ISLNK(mode):
        # Never read or follow a symlink: its link text could hide a marker in
        # the real target, and following it could escape the tree. The release
        # tree has no symlinks, so a tracked symlink fails the proof closed.
        raise OperationsModeProofError(
            f"{relative_posix} is a symlink and cannot be scanned safely for the operations-mode marker"
        )
    if stat.S_ISREG(mode):
        try:
            return path.read_bytes()
        except (OSError, ValueError) as error:
            raise OperationsModeProofError(
                f"{relative_posix} could not be read for the operations-mode scan"
            ) from error
    if stat.S_ISDIR(mode):
        # A directory candidate (a submodule gitlink in git mode) carries no
        # scannable bytes of its own and may point outside this checkout; fail
        # closed rather than treating it as empty.
        raise OperationsModeProofError(
            f"{relative_posix} is a directory (for example a submodule) and cannot be scanned for the "
            "operations-mode marker"
        )
    # FIFO, socket, block/char device, or anything else: never open it, and do
    # not let it pass silently.
    raise OperationsModeProofError(
        f"{relative_posix} is not a regular file (special file) and cannot be scanned safely for the "
        "operations-mode marker"
    )


def prove_operations_mode_disabled(repo_root: Path) -> str:
    """Prove the default operations mode is ``disabled`` from source, or raise.

    The operations mode is unimplemented: no tracked file may reference
    ``GBAW_OPERATIONS_MODE`` at all. The proof scans EVERY tracked file
    regardless of suffix (via ``git ls-files``) and fails CLOSED on any
    reference outside Markdown and the ``docs/``, ``scripts/release/``, and
    ``scripts/test/`` prefixes -- not merely on a literal non-``disabled``
    assignment, which a Python/CloudFormation/shell/Dockerfile/TypeScript
    default can trivially evade.

    Each candidate is ``lstat``'d before anything is opened. A regular file is
    read as RAW BYTES and searched for the marker encoded as UTF-8, UTF-16-LE,
    UTF-16-BE, UTF-32-LE, and UTF-32-BE (BOM or not); no decoding is performed,
    so a binary file, or a deliberately mis-encoded text file (for example a
    UTF-16LE PowerShell script whose interleaved NUL bytes would defeat a UTF-8
    text read), cannot smuggle the marker past the scan. A symlink is never
    read or followed -- it fails the proof CLOSED, so neither its link text nor
    its target can hide a marker and the scan cannot escape the tree (the
    release tree contains no symlinks). A directory candidate (a submodule
    gitlink listed by ``git ls-files`` as a directory path), a FIFO, a socket,
    or a device likewise fails the proof CLOSED and is never opened (opening a
    FIFO could block indefinitely). Any path that cannot be listed, ``lstat``'d,
    or read (``PermissionError`` / ``OSError``), including a file inside an
    unreadable directory, likewise fails the proof CLOSED rather than being
    skipped. Returns ``"disabled"`` on success.
    """

    adr = repo_root / "docs" / "adr" / "0001-preserve-chat-and-add-optional-operations.md"
    if not adr.is_file():
        raise OperationsModeProofError("operations ADR not found")
    adr_text = adr.read_text(encoding="utf-8")
    if "The default is `disabled`" not in adr_text:
        raise OperationsModeProofError("ADR does not declare the default operations mode as disabled")

    for relative in _tracked_files(repo_root):
        if _ops_mode_reference_is_allowed(relative):
            continue
        raw = _ops_mode_scan_bytes(repo_root, relative)
        if any(encoded in raw for encoded in _OPERATIONS_MODE_MARKER_BYTES):
            raise OperationsModeProofError(
                f"{relative} references GBAW_OPERATIONS_MODE; the mode is unimplemented and "
                "must not appear outside Markdown, documentation, tests, or the release tooling"
            )
    return "disabled"


def build_manifest(
    repo_root: Path,
    *,
    tag: str,
    release_name: str,
    commit: str,
    commit_date: str,
    required_checks: list[CheckRunResult],
    evidence_digest: str,
    gate_summary: dict[str, str],
    attached_file_names: list[str],
    waivers: dict[str, str] | None = None,
) -> dict:
    """Assemble the manifest dictionary (not yet serialized).

    ``commit_date`` is the only timestamp permitted and must be the commit's
    ISO-8601 date. ``evidence_digest`` is the SHA-256 of the evidence json
    block (not the comment URL). ``waivers`` maps each waived gate to the public
    issue reference that granted it, so the manifest records WHICH issue
    authorized a waiver, not merely that a gate was waived.
    """

    operations_mode = prove_operations_mode_disabled(repo_root)

    infra_templates = discover_infrastructure_templates(repo_root)

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tag": tag,
        "release_name": release_name,
        "commit": commit,
        "commit_date": commit_date,
        "default_operations_mode": operations_mode,
        "hashes": {
            "dependency_locks": _hash_group(repo_root, list(DEPENDENCY_LOCK_FILES)),
            "prompt_sources": _hash_group(repo_root, list(PROMPT_SOURCE_FILES)),
            "infrastructure_templates": _hash_group(repo_root, infra_templates),
        },
        "required_checks": [
            {"name": c.name, "conclusion": c.conclusion} for c in sorted(required_checks, key=lambda c: c.name)
        ],
        "evidence": {
            "digest_sha256": evidence_digest,
            "gates": dict(sorted(gate_summary.items())),
            "waivers": dict(sorted((waivers or {}).items())),
        },
        "attached_files": sorted(attached_file_names),
    }
    return manifest


def serialize_manifest(manifest: dict) -> bytes:
    """Serialize deterministically: sorted keys, 2-space indent, ASCII, LF, trailing newline."""

    text = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=True)
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")
