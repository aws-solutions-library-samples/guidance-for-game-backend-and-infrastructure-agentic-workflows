#!/usr/bin/env python3
"""Scan tracked text files for high-confidence non-public content."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

SYNTHETIC_ACCOUNT_IDS = frozenset(
    {
        "000000000000",
        "123456789012",
    }
)
SYNTHETIC_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
PUBLIC_EMAIL_ADDRESSES = frozenset({"opensource-codeofconduct" + "@amazon.com"})
SYNTHETIC_ACCESS_KEYS = frozenset({"AKIAIOSFODNN7EXAMPLE"})

ACCOUNT_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])")
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    r"(?![A-Za-z0-9.-])"
)
INTERNAL_DOMAIN_PATTERN = re.compile(
    r"\b(?:"
    r"a2z\.com|"
    r"aws\.dev|"
    r"code\.amazon\.com|"
    r"issues\.amazon\.com|"
    r"quip-amazon\.com|"
    r"sim\.amazon\.com|"
    r"w\.amazon\.com|"
    r"[A-Za-z0-9.-]+\.corp\.amazon\.com"
    r")\b",
    re.IGNORECASE,
)
ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
GITHUB_TOKEN_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,255}|github_pat_[A-Za-z0-9_]{20,255})\b"
)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----")
JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / name
        for name in result.stdout.decode("utf-8").split("\0")
        if name
    ]


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def load_denylist(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term.casefold())
    return tuple(terms)


def scan_text(path: str, text: str, denylist: Sequence[str] = ()) -> list[Finding]:
    findings: set[Finding] = set()

    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in ACCOUNT_ID_PATTERN.finditer(line):
            if match.group(0) not in SYNTHETIC_ACCOUNT_IDS:
                findings.add(Finding(path, line_number, "non-synthetic-account-id"))

        for match in EMAIL_PATTERN.finditer(line):
            address = match.group(0).casefold()
            domain = match.group(2).casefold()
            if address not in PUBLIC_EMAIL_ADDRESSES and domain not in SYNTHETIC_EMAIL_DOMAINS:
                findings.add(Finding(path, line_number, "non-synthetic-email"))

        if INTERNAL_DOMAIN_PATTERN.search(line):
            findings.add(Finding(path, line_number, "internal-domain"))

        for match in ACCESS_KEY_PATTERN.finditer(line):
            if match.group(0) not in SYNTHETIC_ACCESS_KEYS:
                findings.add(Finding(path, line_number, "access-key"))

        if GITHUB_TOKEN_PATTERN.search(line):
            findings.add(Finding(path, line_number, "github-token"))

        if PRIVATE_KEY_PATTERN.search(line):
            findings.add(Finding(path, line_number, "private-key"))

        if JWT_PATTERN.search(line):
            findings.add(Finding(path, line_number, "jwt"))

        folded_line = line.casefold()
        if any(term.casefold() in folded_line for term in denylist):
            findings.add(Finding(path, line_number, "private-denylist"))

    return sorted(findings)


def candidate_files(paths: Iterable[str]) -> list[Path]:
    supplied = list(paths)
    if not supplied:
        return repository_files()

    candidates = []
    for value in supplied:
        path = Path(value)
        candidate = path if path.is_absolute() else REPOSITORY_ROOT / path
        if not candidate.exists():
            raise FileNotFoundError(f"scan path does not exist: {value}")
        if not candidate.is_file():
            raise ValueError(f"scan path is not a file: {value}")
        candidates.append(candidate)
    return candidates


def repository_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--denylist-file",
        type=Path,
        default=os.environ.get("GBAW_PUBLIC_CONTENT_DENYLIST_FILE"),
        help="untracked file containing one private term per line",
    )
    parser.add_argument("paths", nargs="*", help="files to scan; defaults to all tracked files")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        denylist = load_denylist(args.denylist_file)
    except OSError as error:
        print(f"Unable to read the public-content denylist: {error}", file=sys.stderr)
        return 2

    try:
        candidates = candidate_files(args.paths)
    except (OSError, ValueError) as error:
        print(f"Unable to select public-content files: {error}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    checked = 0
    for path in candidates:
        if not path.is_file():
            continue
        text = read_text(path)
        if text is None:
            continue
        checked += 1
        findings.extend(scan_text(repository_relative(path), text, denylist))

    if findings:
        for finding in sorted(findings):
            print(f"{finding.path}:{finding.line}: public-content/{finding.rule}")
        print(f"Public-content scan failed with {len(findings)} finding(s).", file=sys.stderr)
        return 1

    print(f"Public-content scan passed ({checked} text files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
