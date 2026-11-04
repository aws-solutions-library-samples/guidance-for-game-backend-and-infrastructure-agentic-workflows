"""Deterministic ``SHA256SUMS`` generation and verification.

The file uses the standard ``sha256sum`` text format (``<hex>  <name>``, two
spaces, binary-mode marker omitted) and is sorted by filename for determinism.
It covers every attached file *except itself*.
"""

from __future__ import annotations

# Standard library
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ChecksumEntry:
    name: str
    digest: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_sha256sums(files: dict[str, bytes]) -> bytes:
    """Return SHA256SUMS bytes covering ``files`` (name -> content), sorted by name.

    LF endings, single trailing newline. The caller must not include the
    SHA256SUMS file itself in ``files``.
    """

    lines = []
    for name in sorted(files):
        digest = sha256_hex(files[name])
        lines.append(f"{digest}  {name}")
    body = "\n".join(lines)
    if body:
        body += "\n"
    return body.encode("utf-8")


def parse_sha256sums(data: bytes) -> list[ChecksumEntry]:
    entries: list[ChecksumEntry] = []
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ValueError("malformed SHA256SUMS line")
        digest, name = parts
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("malformed SHA256SUMS digest")
        entries.append(ChecksumEntry(name=name, digest=digest))
    return entries


def verify_against_sums(sums_data: bytes, files: dict[str, bytes]) -> None:
    """Raise ValueError if ``files`` do not exactly match ``sums_data``.

    Every entry in the sums file must be present and match; no extra files may
    be present. This is the pre-upload tamper check in the publish job.
    """

    entries = parse_sha256sums(sums_data)
    expected = {entry.name: entry.digest for entry in entries}
    if len(expected) != len(entries):
        raise ValueError("duplicate filename in SHA256SUMS")

    actual_names = set(files)
    expected_names = set(expected)
    if actual_names != expected_names:
        raise ValueError(
            f"SHA256SUMS coverage mismatch: missing={sorted(expected_names - actual_names)} "
            f"unexpected={sorted(actual_names - expected_names)}"
        )
    for name, content in files.items():
        if sha256_hex(content) != expected[name]:
            raise ValueError(f"checksum mismatch for {name}")
