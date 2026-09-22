"""Additive E4 operations control-plane contract prelude (issue #416, E4).

This module is the blocking, additive contract prelude every E4 agent
(backend, frontend, infrastructure) builds against. It defines and validates the
eight immutable v1 control-plane contracts:

* ``operations-kill-switch`` — the single deployment-wide AppConfig kill-switch
  document. It carries exactly the deployment-wide ``operations_enabled`` flag,
  the one ``gamelift.capacity-adjustment`` capability's ``prepare``/``dispatch``/
  ``execute`` phase booleans, an immutable monotonic ``config_version``, and
  ``issued_at``/``not_after`` freshness. No unknown capability or field is
  allowed, so the schema is usable directly as an AppConfig JSON Schema
  validator. The default safe document disables everything.
* ``operations-capability-discovery`` — the server-computed projection the UI
  reads before showing any control. It distinguishes available / provisioned /
  enabled and static vs dynamic gates.
* ``operations-list-request`` / ``operations-list-response`` — a bounded
  operation page (max 50) with an opaque, tamper-evident cursor.
* ``operations-detail-projection`` — a bounded operation detail/evidence
  projection exposing lifecycle phases, state, verification, and rollback
  visibility while excluding email/display name/token/ARN/account/fleet id/raw
  provider payload.
* ``operations-control-request`` / ``operations-control-response`` — the admin
  control request/response. A control carries only desired booleans plus the
  expected ``config_version`` — never identity, credential, or policy.
* ``operations-control-audit-record`` — the immutable, hash-bound record of one
  admin control decision.

Like the E1 observation and E2 capacity layers, this layer is **additive**: it
reuses the immutable ``common`` ``$defs`` but never joins the published
write-contract ``SCHEMA_NAMES`` set, the ``CAPACITY_SCHEMA_NAMES`` set, the
``EXECUTION_SCHEMA_NAMES`` set, or their validators/registries/hashes. It loads
and validates through its own self-contained ``CONTROL_SCHEMA_NAMES`` registry,
so it cannot change any published v1 contract or its playbook hash.
"""

from __future__ import annotations

# Standard library
import base64
import binascii
import hmac
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files
from typing import Any

# Third-party packages
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

# Local modules
from operations.contracts.canonical import (
    CanonicalizationError,
    canonical_sha256,
    canonicalize,
    load_json,
)

CONTROL_CONTRACT_VERSION = "1.0"

CAPABILITY_ID = "gamelift.capacity-adjustment"
CONTROL_PHASES = ("prepare", "dispatch", "execute")

# The single deployment-wide list of capabilities the kill-switch governs. E4
# supports exactly one; adding another is a new contract version, never a silent
# additive field.
SUPPORTED_CAPABILITIES = frozenset({CAPABILITY_ID})

# The maximum operation list page. The list request and response schemas both
# cap at this, and the semantic validator re-checks it so a response can never
# exceed the bound even if a schema is loosened by mistake.
MAX_PAGE_SIZE = 50

COMMON_SCHEMA_NAME = "common"

KILL_SWITCH_SCHEMA_NAME = "operations-kill-switch"
CAPABILITY_DISCOVERY_SCHEMA_NAME = "operations-capability-discovery"
LIST_REQUEST_SCHEMA_NAME = "operations-list-request"
LIST_RESPONSE_SCHEMA_NAME = "operations-list-response"
DETAIL_PROJECTION_SCHEMA_NAME = "operations-detail-projection"
CONTROL_REQUEST_SCHEMA_NAME = "operations-control-request"
CONTROL_RESPONSE_SCHEMA_NAME = "operations-control-response"
CONTROL_AUDIT_RECORD_SCHEMA_NAME = "operations-control-audit-record"

CONTROL_SCHEMA_NAMES = frozenset(
    {
        KILL_SWITCH_SCHEMA_NAME,
        CAPABILITY_DISCOVERY_SCHEMA_NAME,
        LIST_REQUEST_SCHEMA_NAME,
        LIST_RESPONSE_SCHEMA_NAME,
        DETAIL_PROJECTION_SCHEMA_NAME,
        CONTROL_REQUEST_SCHEMA_NAME,
        CONTROL_RESPONSE_SCHEMA_NAME,
        CONTROL_AUDIT_RECORD_SCHEMA_NAME,
    }
)

# ADR 0001 authority lattice ordering; lower is more restrictive.
_AUTHORITY_ORDER = {
    "disabled": 0,
    "observe": 1,
    "advise": 2,
    "remediate": 3,
    "operate": 4,
}

# Frozen API routes every E4 agent (backend handler, frontend client, infra
# template ``RouteKey``) must agree on. They mirror the E1/E2/E3 route shape:
# a leading ``/operations`` prefix and the ``{operationId}`` path variable.
ROUTE_PREFIX = "/operations"
CAPABILITIES_ROUTE = "/operations/capabilities"
OPERATIONS_LIST_ROUTE = "/operations"
OPERATION_DETAIL_ROUTE_TEMPLATE = "/operations/{operationId}"
CONTROL_ROUTE = "/operations/control"
KILL_SWITCH_ROUTE = "/operations/control/kill-switch"

# API Gateway ``RouteKey`` form (HTTP method + path). Frozen; a handler, client,
# and template must all agree on these exact strings.
ROUTE_KEYS = {
    "capabilities": "GET /operations/capabilities",
    "operations_list": "GET /operations",
    "operation_detail": "GET /operations/{operationId}",
    "control": "POST /operations/control",
    "kill_switch": "GET /operations/control/kill-switch",
}


class ControlContractError(ValueError):
    """A control-plane document failed its schema or semantic contract."""

    def __init__(self, schema_name: str, errors: list[str]) -> None:
        self.schema_name = schema_name
        self.errors = tuple(errors)
        super().__init__(f"{schema_name} contract validation failed: {'; '.join(errors)}")


class CursorError(ValueError):
    """An opaque list cursor is malformed, truncated, or tampered with."""


def _schema_directory():
    return files("operations.contracts").joinpath("schemas", "v1")


@lru_cache(maxsize=None)
def _load_schema_cached(schema_name: str) -> dict[str, Any]:
    schema_path = _schema_directory().joinpath(f"{schema_name}.schema.json")
    document = load_json(schema_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"control-plane schema must be a JSON object: {schema_name}")
    return document


def load_control_schema(schema_name: str) -> dict[str, Any]:
    """Load a defensive copy of one immutable additive control-plane schema."""
    if schema_name not in CONTROL_SCHEMA_NAMES:
        raise ValueError(f"unknown control-plane contract schema: {schema_name}")
    return deepcopy(_load_schema_cached(schema_name))


@lru_cache(maxsize=1)
def _control_registry() -> Registry:
    resources = []
    for schema_name in (COMMON_SCHEMA_NAME, *sorted(CONTROL_SCHEMA_NAMES)):
        schema = _load_schema_cached(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _format_path(error) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


# -- Canonical hashing -----------------------------------------------------


def control_audit_record_hash(record: dict[str, Any]) -> str:
    """Return the tagged SHA-256 of one audit record excluding ``record_hash``.

    The hash binds every other field of the record. It excludes only itself, so
    a stored record can carry its own hash and remain verifiable.
    """
    payload = {key: value for key, value in record.items() if key != "record_hash"}
    return canonical_sha256(payload)


# -- Opaque, tamper-evident cursor codec -----------------------------------
#
# The cursor is base64url(payload_json_canonical) + "." + base64url(hmac). The
# HMAC key is the caller's responsibility to keep server-side and constant; the
# codec's contract is that decoding rejects any byte-level tamper, truncation,
# or key mismatch rather than returning a forged position.

_CURSOR_SEPARATOR = "."

# The opaque cursor wire bound. The list schemas cap the cursor string at this
# length; the codec re-checks it on both encode and decode so an attacker cannot
# force an unbounded base64 decode or HMAC over an oversized blob.
CURSOR_MAX_LENGTH = 512


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise CursorError(f"cursor segment is not valid base64url: {exc}") from exc


def encode_cursor(position: dict[str, Any], *, key: bytes) -> str:
    """Mint an opaque, HMAC-signed continuation token for a list position.

    The position is canonicalized (RFC 8785) before signing so an identical
    logical position always mints an identical cursor and any field reorder is
    irrelevant to verification.
    """
    if not key:
        raise CursorError("cursor signing key must be non-empty")
    try:
        payload = canonicalize(position)
    except CanonicalizationError as exc:
        raise CursorError(f"cursor position is not canonicalizable: {exc}") from exc
    signature = hmac.new(key, payload, "sha256").digest()
    token = f"{_b64url_encode(payload)}{_CURSOR_SEPARATOR}{_b64url_encode(signature)}"
    if len(token) > CURSOR_MAX_LENGTH:
        raise CursorError(f"encoded cursor exceeds the {CURSOR_MAX_LENGTH}-character bound")
    return token


def decode_cursor(cursor: str, *, key: bytes) -> dict[str, Any]:
    """Decode and verify an opaque cursor, rejecting any tamper.

    Raises ``CursorError`` on a malformed token, a bad signature, a truncated
    token, or a key mismatch. It never returns a forged or partially trusted
    position.
    """
    if not key:
        raise CursorError("cursor signing key must be non-empty")
    if not isinstance(cursor, str) or _CURSOR_SEPARATOR not in cursor:
        raise CursorError("cursor is missing its signature segment")
    if len(cursor) > CURSOR_MAX_LENGTH:
        raise CursorError(f"cursor exceeds the {CURSOR_MAX_LENGTH}-character wire bound")
    payload_segment, _, signature_segment = cursor.partition(_CURSOR_SEPARATOR)
    if not payload_segment or not signature_segment:
        raise CursorError("cursor has an empty segment")
    if len(payload_segment) > CURSOR_MAX_LENGTH or len(signature_segment) > CURSOR_MAX_LENGTH:
        raise CursorError(f"cursor segment exceeds the {CURSOR_MAX_LENGTH}-character wire bound")
    payload = _b64url_decode(payload_segment)
    provided_signature = _b64url_decode(signature_segment)
    expected_signature = hmac.new(key, payload, "sha256").digest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise CursorError("cursor signature does not verify")
    try:
        position = load_json(payload)
    except CanonicalizationError as exc:
        raise CursorError(f"cursor payload is not valid I-JSON: {exc}") from exc
    if not isinstance(position, dict):
        raise CursorError("cursor payload is not a JSON object")
    return position


# -- Normalized UTC instant parsing ----------------------------------------
#
# Control-plane timestamps are constrained by the schemas to the normalized UTC
# ``Z`` form (``YYYY-MM-DDThh:mm:ss(.sss)Z``), so lexicographic order already
# equals chronological order. Freshness comparisons still parse to a
# timezone-aware instant rather than comparing strings, so equal moments written
# with different fractional precision (``...00Z`` vs ``...00.000Z``) compare
# equal and any future timestamp form cannot reintroduce lexicographic ambiguity.


def _instant(value: str) -> datetime:
    """Parse a normalized UTC ``Z`` timestamp into a timezone-aware instant."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# -- Semantic validators ---------------------------------------------------


def _phase_switches_ordered(switches: dict[str, bool]) -> bool:
    """Return whether phase enablement respects prepare >= dispatch >= execute."""
    if switches["execute"] and not switches["dispatch"]:
        return False
    if switches["dispatch"] and not switches["prepare"]:
        return False
    return True


def _kill_switch_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if _instant(document["not_after"]) <= _instant(document["issued_at"]):
        errors.append("not_after must be strictly after issued_at")

    capabilities = document["capabilities"]
    unknown = set(capabilities) - SUPPORTED_CAPABILITIES
    if unknown:
        errors.append(f"unknown capabilities are not allowed: {sorted(unknown)}")

    switches = capabilities[CAPABILITY_ID]
    if not _phase_switches_ordered(switches):
        errors.append(f"{CAPABILITY_ID} phases must satisfy prepare >= dispatch >= execute")
    if not document["operations_enabled"] and any(switches.values()):
        errors.append("operations_enabled is false but a capability phase is enabled")
    return errors


def _capability_discovery_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, capability in enumerate(document["capabilities"]):
        capability_id = capability["capability_id"]
        if capability_id in seen:
            errors.append(f"capabilities[{index}] duplicates capability_id {capability_id}")
        seen.add(capability_id)

        # Availability lattice: enabled implies provisioned implies available.
        if capability["enabled"] and not capability["provisioned"]:
            errors.append(f"capabilities[{index}] is enabled but not provisioned")
        if capability["provisioned"] and not capability["available"]:
            errors.append(f"capabilities[{index}] is provisioned but not available")

        # A disabled capability cannot advertise an enabled phase.
        if not capability["enabled"] and any(capability["phases"].values()):
            errors.append(f"capabilities[{index}] is disabled but advertises an enabled phase")

        # The deployment master switch caps every capability.
        if capability["enabled"] and not document["operations_enabled"]:
            errors.append(f"capabilities[{index}] is enabled while operations are disabled deployment-wide")

        # A disabled deployment mode can never enable a capability.
        if capability["enabled"] and document["deployment_mode"] == "disabled":
            errors.append(f"capabilities[{index}] is enabled under a disabled deployment mode")

        gate_ids = [gate["gate_id"] for gate in capability["gates"]]
        if len(gate_ids) != len(set(gate_ids)):
            errors.append(f"capabilities[{index}] has a duplicate gate_id")
        if capability["enabled"] and not all(gate["satisfied"] for gate in capability["gates"]):
            errors.append(f"capabilities[{index}] is enabled with an unsatisfied gate")
    return errors


def _list_response_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    operations = document["operations"]
    if len(operations) > MAX_PAGE_SIZE:
        errors.append(f"operations exceeds the {MAX_PAGE_SIZE} page bound")
    if document["page_size"] > MAX_PAGE_SIZE:
        errors.append(f"page_size exceeds the {MAX_PAGE_SIZE} bound")
    if len(operations) > document["page_size"]:
        errors.append("operations returned more entries than page_size")
    seen: set[str] = set()
    for index, summary in enumerate(operations):
        operation_id = summary["operation_id"]
        if operation_id in seen:
            errors.append(f"operations[{index}] duplicates operation_id {operation_id}")
        seen.add(operation_id)
        if _instant(summary["updated_at"]) < _instant(summary["created_at"]):
            errors.append(f"operations[{index}] updated_at precedes created_at")
    return errors


def _detail_projection_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if _instant(document["updated_at"]) < _instant(document["created_at"]):
        errors.append("updated_at precedes created_at")

    verification = document["verification"]
    if not verification["applicable"] and verification["outcome"] != "not_applicable":
        errors.append("verification outcome must be not_applicable when it does not apply")
    if verification["applicable"] and verification["outcome"] == "not_applicable":
        errors.append("verification outcome cannot be not_applicable when it applies")

    rollback = document["rollback"]
    if not rollback["applicable"] and rollback["outcome"] != "not_applicable":
        errors.append("rollback outcome must be not_applicable when it does not apply")
    if rollback["applicable"] and rollback["outcome"] == "not_applicable":
        errors.append("rollback outcome cannot be not_applicable when it applies")

    seen_phases: set[str] = set()
    for index, phase in enumerate(document["phases"]):
        name = phase["phase"]
        if name in seen_phases:
            errors.append(f"phases[{index}] duplicates phase {name}")
        seen_phases.add(name)
    return errors


def _control_request_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    desired = document["desired"]
    switches = desired["capabilities"][CAPABILITY_ID]
    if not _phase_switches_ordered(switches):
        errors.append(f"{CAPABILITY_ID} desired phases must satisfy prepare >= dispatch >= execute")
    if not desired["operations_enabled"] and any(switches.values()):
        errors.append("desired operations_enabled is false but a capability phase is enabled")
    return errors


def _control_response_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    outcome = document["outcome"]
    reason_code = document.get("reason_code")
    effective = document.get("effective")

    if outcome == "applied":
        if effective is None:
            errors.append("applied outcome must include the effective document")
        elif effective["config_version"] != document["config_version"]:
            errors.append("applied config_version does not match the effective document")
        if reason_code is not None and reason_code != "APPLIED":
            errors.append("applied outcome reason_code must be APPLIED")
    else:
        if effective is not None:
            errors.append(f"{outcome} outcome must not include an effective document")
        if outcome == "version_conflict" and reason_code not in (None, "VERSION_CONFLICT"):
            errors.append("version_conflict outcome reason_code must be VERSION_CONFLICT")
        if outcome == "denied" and reason_code not in (None, "AUTHORITY_DENIED", "PHASE_ORDER_INVALID"):
            errors.append("denied outcome reason_code is not a denial reason")
    return errors


def _control_audit_record_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if document["record_hash"] != control_audit_record_hash(document):
        errors.append("record_hash does not match the canonical record")

    outcome = document["outcome"]
    previous = document["previous_config_version"]
    resulting = document["resulting_config_version"]
    if outcome == "applied":
        if resulting <= previous:
            errors.append("applied record must advance resulting_config_version beyond previous")
    else:
        if resulting != previous:
            errors.append(f"{outcome} record must not advance the config_version")

    switches = document["desired"]["capabilities"][CAPABILITY_ID]
    if not desired_switches_ordered(switches):
        errors.append(f"{CAPABILITY_ID} desired phases must satisfy prepare >= dispatch >= execute")
    return errors


def desired_switches_ordered(switches: dict[str, bool]) -> bool:
    """Public alias for the phase ordering invariant used across E4 agents."""
    return _phase_switches_ordered(switches)


_SEMANTIC_VALIDATORS = {
    KILL_SWITCH_SCHEMA_NAME: _kill_switch_errors,
    CAPABILITY_DISCOVERY_SCHEMA_NAME: _capability_discovery_errors,
    LIST_RESPONSE_SCHEMA_NAME: _list_response_errors,
    DETAIL_PROJECTION_SCHEMA_NAME: _detail_projection_errors,
    CONTROL_REQUEST_SCHEMA_NAME: _control_request_errors,
    CONTROL_RESPONSE_SCHEMA_NAME: _control_response_errors,
    CONTROL_AUDIT_RECORD_SCHEMA_NAME: _control_audit_record_errors,
}


def validate_control_contract(schema_name: str, document: object) -> None:
    """Validate a document against its exact E4 schema and semantic invariants."""
    if schema_name not in CONTROL_SCHEMA_NAMES:
        raise ValueError(f"unknown control-plane contract schema: {schema_name}")
    schema = load_control_schema(schema_name)
    try:
        canonicalize(document)
    except CanonicalizationError as exc:
        raise ControlContractError(schema_name, [f"document is outside the canonical I-JSON domain: {exc}"]) from exc

    validator = Draft202012Validator(
        schema,
        registry=_control_registry(),
        format_checker=FormatChecker(),
    )
    schema_errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    errors = [_format_path(error) for error in schema_errors]

    if not errors and isinstance(document, dict):
        semantic = _SEMANTIC_VALIDATORS.get(schema_name)
        if semantic is not None:
            errors.extend(semantic(document))

    if errors:
        raise ControlContractError(schema_name, errors)


def default_safe_kill_switch(*, config_version: int, issued_at: str, not_after: str) -> dict[str, Any]:
    """Return the default safe kill-switch document, with everything disabled."""
    return {
        "contract_version": CONTROL_CONTRACT_VERSION,
        "config_version": config_version,
        "issued_at": issued_at,
        "not_after": not_after,
        "operations_enabled": False,
        "capabilities": {
            CAPABILITY_ID: {
                "prepare": False,
                "dispatch": False,
                "execute": False,
            }
        },
    }
