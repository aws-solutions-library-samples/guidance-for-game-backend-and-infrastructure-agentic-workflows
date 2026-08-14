"""RFC 8785 JSON canonicalization and tagged SHA-256 digests."""

from __future__ import annotations

# Standard library
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TextIO

# Third-party packages
import rfc8785


class CanonicalizationError(ValueError):
    """The value is not valid I-JSON and cannot be canonicalized."""


def canonicalize(document: Any) -> bytes:
    """Return the RFC 8785 JSON Canonicalization Scheme representation."""
    try:
        return rfc8785.dumps(document)
    except (rfc8785.CanonicalizationError, TypeError) as exc:
        raise CanonicalizationError(str(exc)) from exc


def canonical_sha256(document: Any) -> str:
    """Return a lowercase, algorithm-tagged SHA-256 digest of canonical JSON."""
    return f"sha256:{hashlib.sha256(canonicalize(document)).hexdigest()}"


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalizationError(f"duplicate JSON object member: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise CanonicalizationError(f"non-finite JSON number: {value}")


def load_json(source: str | bytes | Path | TextIO) -> Any:
    """Load JSON while rejecting duplicate keys and non-I-JSON numeric values."""
    try:
        raw: str | bytes
        if isinstance(source, Path):
            raw = source.read_text(encoding="utf-8")
        elif isinstance(source, (str, bytes)):
            raw = source
        else:
            raw = source.read()

        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CanonicalizationError(str(exc)) from exc
