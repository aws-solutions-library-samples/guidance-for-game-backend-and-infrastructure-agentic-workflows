"""Normalize syft SPDX-JSON SBOMs for determinism and public safety.

Syft's raw SPDX-JSON output contains volatile, non-reproducible fields (a
``created`` timestamp and a random ``documentNamespace``) and may embed
absolute runner paths or hostnames. For a release artifact we need two
properties syft does not give us:

* **Determinism** -- two runs over identical source produce identical bytes.
* **Public safety** -- no absolute filesystem paths or machine-specific values
  leak into a world-readable artifact.

:func:`normalize_sbom` rewrites exactly those fields and re-serializes with the
same deterministic rules as the manifest. It never alters the package
inventory (the security-relevant content) -- only the document metadata and
any absolute ``fileName`` / path-shaped values. It does NOT scrub registry
hostnames: syft only records public package-registry hosts (pypi, npm, ...),
and the exclusion guard independently rejects a fixed list of AWS-owned host
suffixes (the AWS service domain, the retail domain, and the AWS developer-tools
domain) plus the internal-domain markers it lists, so a release that carries one
of those fails closed rather than publishing it.
"""

from __future__ import annotations

# Standard library
import json
import re
from datetime import datetime, timezone
from typing import Any

# Deterministic replacements for the volatile document identity.
_DETERMINISTIC_NAMESPACE_PREFIX = "https://spdx.org/spdxdocs/gbaw-release"
_ABSOLUTE_PATH_RE = re.compile(r"(?:/[^\s\"]+)+")


def _to_utc_z(value: str) -> str:
    """Return an SPDX-2.3 ``YYYY-MM-DDThh:mm:ssZ`` instant for ``value``.

    SPDX 2.3 requires a UTC ``Z`` suffix with no sub-second part. ``git`` commit
    dates (``%cI``) carry the committer's offset, e.g. ``-07:00``; a GitHub web
    squash-merge commonly records one. We convert the instant to UTC and drop
    any offset so spdx-tools accepts the normalized SBOM. A value that is
    already a bare ``Z`` instant round-trips unchanged.
    """

    text = value.strip()
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError("commit_date is not a valid ISO-8601 instant") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewrite_absolute_paths(value: str) -> str:
    # Collapse any absolute POSIX path into its basename-anchored relative form
    # so a runner's working directory (e.g. /home/runner/work/...) never leaks.
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        # Keep SPDX/URL-ish tokens intact (handled separately); only touch
        # things that look like filesystem paths beginning at root.
        if raw.startswith("//"):
            return raw
        return "./" + raw.rsplit("/", 1)[-1]

    return _ABSOLUTE_PATH_RE.sub(repl, value)


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {key: _scrub(val) for key, val in obj.items()}
    if isinstance(obj, list):
        return [_scrub(item) for item in obj]
    if isinstance(obj, str) and obj.startswith("/"):
        return _rewrite_absolute_paths(obj)
    return obj


def normalize_sbom(
    raw: bytes,
    *,
    commit_date: str,
    source_name: str,
    tag: str | None = None,
    commit: str | None = None,
) -> bytes:
    """Return a deterministic, public-safe SPDX-JSON SBOM.

    ``commit_date`` (ISO-8601, the only permitted timestamp) is converted to a
    UTC ``YYYY-MM-DDThh:mm:ssZ`` instant and replaces the volatile
    ``creationInfo.created``. The ``documentNamespace`` is rebuilt from
    ``source_name`` plus, when supplied, ``tag`` and ``commit`` so it is unique
    per release and still byte-deterministic. The package inventory is preserved
    verbatim apart from absolute path scrubbing.
    """

    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("SBOM root is not a JSON object")

    created = _to_utc_z(commit_date)
    creation_info = document.get("creationInfo")
    if isinstance(creation_info, dict):
        creation_info["created"] = created

    namespace_parts = [source_name]
    if tag:
        namespace_parts.append(tag)
    if commit:
        namespace_parts.append(commit)
    document["documentNamespace"] = f"{_DETERMINISTIC_NAMESPACE_PREFIX}/{'/'.join(namespace_parts)}"
    # ``name`` is deterministic; drop any volatile comment.
    document["name"] = source_name
    document.pop("documentComment", None)

    scrubbed = _scrub(document)

    text = json.dumps(scrubbed, sort_keys=True, indent=2, ensure_ascii=True)
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")
