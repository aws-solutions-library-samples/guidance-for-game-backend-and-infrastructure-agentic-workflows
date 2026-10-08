"""Strict validation of the deployed-validation evidence comment.

A maintainer records deployed-validation evidence as a comment on an issue in
this repository. The comment must contain exactly one fenced ``json`` block
that matches a strict versioned schema:

* ``schema_version`` must equal :data:`EVIDENCE_SCHEMA_VERSION`;
* ``commit`` must equal the dispatched commit (full 40-hex, lowercase);
* ``gates`` lists every release-gate item from the issue body, each ``pass`` or
  ``waived``; a ``waived`` gate must name a public issue reference;
* unknown keys anywhere are rejected and all values are size-bounded.

The caller binds this content to the trusted comment metadata and separately
confirms the comment author has write permission or higher on the repository
(via the collaborator-permission endpoint). This module parses and validates
the *content* only.
"""

from __future__ import annotations

# Standard library
import json
import re
from dataclasses import dataclass

EVIDENCE_SCHEMA_VERSION = "gbaw.release-evidence.v1"

# Every release-gate item from the release-gate checklist. The evidence must
# account for every one of these, and no others.
REQUIRED_GATES = (
    "tag_points_at_main_and_checks_green",
    "p0_p1_closed_or_waived",
    "exact_commit_deployed_to_isolated_nonprod",
    "authenticated_shakedowns_pass",
    "unauthorized_requests_denied",
    "kb_ingestion_zero_failures",
    "frontend_runtime_waf_alarms_vuln_ok",
    "public_content_scan_passes",
    "operations_mode_disabled_default_no_write",
    "rollback_documented_and_tested",
)

_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_ISSUE_REF_RE = re.compile(r"\A#[1-9][0-9]*\Z")
_GATE_STATUSES = frozenset({"pass", "waived"})
_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

# Only the P0/P1 release-gate item may be waived. Every other gate encodes a
# hard release invariant (no provider write, unauthorized-request denial,
# public-content scan, operations-mode default, tag/checks) that must be an
# affirmative ``pass`` and can never be waived.
WAIVABLE_GATES = frozenset({"p0_p1_closed_or_waived"})

# Bounds (defensive; the comment is attacker-influenced public content).
_MAX_COMMENT_BYTES = 65536
_MAX_BLOCK_BYTES = 16384
_MAX_STRING = 512
_MAX_GATES = 64


class EvidenceError(ValueError):
    """Raised when the evidence block is missing, malformed, or disallowed."""


@dataclass(frozen=True)
class EvidenceDocument:
    schema_version: str
    commit: str
    gates: dict[str, str]
    waivers: dict[str, str]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """``object_pairs_hook`` that rejects duplicate keys at any object level.

    ``json.loads`` keeps the last of duplicate keys by default, so a block that
    reads one way to a human and another to the parser could smuggle a value.
    """

    seen: set[str] = set()
    result: dict = {}
    for key, value in pairs:
        if key in seen:
            raise EvidenceError(f"duplicate key in evidence json: {key!r}")
        seen.add(key)
        result[key] = value
    return result


def extract_json_block(comment_body: str) -> str:
    """Return the single fenced ``json`` block, or raise if not exactly one."""
    if not isinstance(comment_body, str):
        raise EvidenceError("comment body must be a string")
    if len(comment_body.encode("utf-8")) > _MAX_COMMENT_BYTES:
        raise EvidenceError("comment body exceeds maximum size")
    blocks = _FENCE_RE.findall(comment_body)
    if len(blocks) == 0:
        raise EvidenceError("no fenced json block found in evidence comment")
    if len(blocks) > 1:
        raise EvidenceError("evidence comment must contain exactly one fenced json block")
    block = str(blocks[0])
    if len(block.encode("utf-8")) > _MAX_BLOCK_BYTES:
        raise EvidenceError("evidence json block exceeds maximum size")
    return block


def _require_bounded_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise EvidenceError(f"{field_name} must be a string")
    if len(value) > _MAX_STRING:
        raise EvidenceError(f"{field_name} exceeds maximum length")
    return value


def parse_and_validate(comment_body: str, *, expected_commit: str) -> EvidenceDocument:
    """Parse the evidence comment and validate it against the strict schema.

    ``expected_commit`` is the trusted dispatched commit SHA. The evidence
    ``commit`` must equal it exactly.
    """

    block = extract_json_block(comment_body)
    try:
        document = json.loads(block, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise EvidenceError(f"evidence json is not valid: {error.msg}") from error

    if not isinstance(document, dict):
        raise EvidenceError("evidence json must be an object")

    allowed_keys = {"schema_version", "commit", "gates", "waivers"}
    unknown = set(document) - allowed_keys
    if unknown:
        raise EvidenceError(f"evidence json has unknown keys: {sorted(unknown)}")

    schema_version = _require_bounded_string(document.get("schema_version"), "schema_version")
    if schema_version != EVIDENCE_SCHEMA_VERSION:
        raise EvidenceError(f"unsupported evidence schema_version: {schema_version!r}")

    commit = _require_bounded_string(document.get("commit"), "commit")
    if not _COMMIT_RE.match(commit):
        raise EvidenceError("evidence commit must be a 40-char lowercase hex sha")
    if commit != expected_commit:
        raise EvidenceError("evidence commit does not match the dispatched commit")

    gates_raw = document.get("gates")
    if not isinstance(gates_raw, dict):
        raise EvidenceError("evidence gates must be an object")
    if len(gates_raw) > _MAX_GATES:
        raise EvidenceError("evidence gates object is too large")

    waivers_raw = document.get("waivers", {})
    if not isinstance(waivers_raw, dict):
        raise EvidenceError("evidence waivers must be an object")

    gate_names = {_require_bounded_string(name, "gate name") for name in gates_raw}
    missing = set(REQUIRED_GATES) - gate_names
    if missing:
        raise EvidenceError(f"evidence is missing required gates: {sorted(missing)}")
    extra = gate_names - set(REQUIRED_GATES)
    if extra:
        raise EvidenceError(f"evidence has unknown gates: {sorted(extra)}")

    gates: dict[str, str] = {}
    waivers: dict[str, str] = {}
    for name in REQUIRED_GATES:
        status = _require_bounded_string(gates_raw[name], f"gate {name}")
        if status not in _GATE_STATUSES:
            raise EvidenceError(f"gate {name} must be 'pass' or 'waived', not {status!r}")
        if status == "waived" and name not in WAIVABLE_GATES:
            raise EvidenceError(f"gate {name} may not be waived; only {sorted(WAIVABLE_GATES)} accept 'waived'")
        gates[name] = status
        if status == "waived":
            reference = waivers_raw.get(name)
            reference = _require_bounded_string(reference, f"waiver {name}") if reference is not None else ""
            if not _ISSUE_REF_RE.match(reference):
                raise EvidenceError(f"waived gate {name} needs a public issue reference like '#123'")
            waivers[name] = reference

    waiver_names = {_require_bounded_string(name, "waiver name") for name in waivers_raw}
    stray_waivers = waiver_names - {n for n, s in gates.items() if s == "waived"}
    if stray_waivers:
        raise EvidenceError(f"waivers reference non-waived gates: {sorted(stray_waivers)}")

    return EvidenceDocument(
        schema_version=schema_version,
        commit=commit,
        gates=gates,
        waivers=waivers,
    )
