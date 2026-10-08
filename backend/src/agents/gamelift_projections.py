"""Bounded, code-owned projections for raw GameLift provider responses.

Part of #457. The GameLift specialist reaches AWS GameLift through boto3
``describe_*`` calls whose raw responses are provider-controlled: they carry
full ARNs (which embed the account ID), may grow unbounded across locations and
policies, and their exceptions carry provider-authored text. None of that may
reach model context verbatim.

This module keeps the transform *in code we own* rather than trusting the model
to ignore sensitive fields:

* Each ``project_*`` function copies only an explicit allowlist of operational
  fields from each item, and every copied value is additionally validated by a
  code-owned typed validator. Field-name allowlisting alone is not enough: an
  allowed key (``FleetId``, ``Name``, ``Location``, a numeric count, ...) still
  carries a *value* the provider (or a compromised upstream) controls, so each
  value must satisfy an exact scalar type, a bounded string length, and finite
  bounded numeric magnitude before it may cross into model context.
* Unknown / future / customer-controlled fields (endpoints, credential ARNs,
  private IPs, arbitrary nested blobs) are dropped by construction. Allowed
  fields carrying an invalid value (wrong type, oversized string, NaN/Inf,
  out-of-range magnitude, oversized nested collection) are dropped predictably
  rather than passed through.
* Full ARNs are never projected. Caller-supplied identifiers (the ``fleet_id``
  argument and its echo) are non-sensitive and retained for correlation, still
  subject to the string bound.
* Collections are bounded by ``GAMELIFT_MAX_PROJECTED_ITEMS`` and, in aggregate,
  by ``GAMELIFT_MAX_PROJECTED_CHARS``; exceeding either the item cap or a
  residual ``NextToken`` marks the result ``truncated``/``incomplete`` so the
  model is told the view is partial instead of silently losing rows.
* Provider exceptions are mapped to a small, typed, sanitized error vocabulary.
  The raw exception message is never logged or returned.

Dispositions are kept mutually distinct via the ``status`` field:

* ``ok``        — full result set returned.
* ``empty``     — the call succeeded and returned a valid, present, empty list
                  *and* no more pages. Never used for a malformed shape.
* ``denied``    — the provider refused the call (authorization / access).
* ``incomplete``— the call failed; only part of the result set was retrieved
                  (residual ``NextToken`` — including an empty page that still
                  carries a continuation token); the response shape was
                  malformed (non-mapping top-level response, collection
                  missing/wrongly typed, or every item non-projectable); or a
                  mixed collection retained its valid rows while discarding
                  malformed ones. All malformed/partial-shape cases surface via
                  ``error.code`` == ``malformed_response`` and are never ``ok``.
* ``truncated`` — a wholly-valid result set was bounded by this projection's
                  item cap or the serialized-payload budget (measured against
                  the final envelope). (A mixed collection that discarded
                  malformed rows is ``incomplete``/``malformed_response`` with a
                  boolean ``truncated: true`` marker, not the ``truncated``
                  status, even though it is likewise partial.)
"""

from __future__ import annotations

# Standard library
import datetime
import ipaddress
import json
import math
import re
from typing import Any, Callable

# Third-party packages
from botocore.exceptions import BotoCoreError, ClientError

# Local modules
from utils.logger import logger

# Hard cap on how many projected items may reach model context per call. A
# single fleet can report many location rows and many scaling policies; without
# a cap an adversarial or unusual account could flood the context window.
GAMELIFT_MAX_PROJECTED_ITEMS = 100

# Aggregate payload budget: after projecting (and item-capping) a collection, the
# serialized projection must also stay within this many characters. This bounds
# the case where each item is individually valid but the collection as a whole
# would still flood model context (e.g. many long-but-legal Location strings).
GAMELIFT_MAX_PROJECTED_CHARS = 20_000

# Upper bound on the number of DISTINCT warning entries that may reach model
# context. Warnings are deduplicated by their code-owned ``(Source, Code)`` key
# with an integer ``Count``, so a maximal failure fan-out (every per-fleet
# describe / deployment call failing) collapses to a small, bounded set of
# triples rather than one entry per failure. Bounding the distinct entries keeps
# failures from crowding real fleet rows out of the aggregate envelope.
GAMELIFT_MAX_WARNINGS = 12

# Per-value bounds. Allowed *string* fields are capped in Unicode code points;
# allowed *numeric* fields must be finite and within this magnitude. These are
# operational GameLift values (region-like strings, session counts, thresholds),
# so the bounds are deliberately generous but finite.
GAMELIFT_MAX_STRING_LENGTH = 256
# Provider counts, policy thresholds, and adjustment values are operational
# metrics, not identifiers. Keep them below the 12-digit account-identifier
# range in addition to requiring finite values.
GAMELIFT_MAX_ABS_NUMBER = 1_000_000_000

# Disposition vocabulary — kept distinct on purpose (see module docstring).
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_DENIED = "denied"
STATUS_INCOMPLETE = "incomplete"
STATUS_TRUNCATED = "truncated"

# Typed, sanitized error codes. These are stable identifiers the model may see;
# they carry no provider-authored text, ARNs, or network coordinates.
ERROR_ACCESS_DENIED = "access_denied"
ERROR_NOT_FOUND = "not_found"
ERROR_THROTTLED = "throttled"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_PROVIDER_ERROR = "provider_error"
# Raised locally (not by the provider) when the response shape itself is
# unusable: the collection is missing/wrongly typed, or every item is
# non-projectable. A malformed response is never an authoritative zero result.
ERROR_MALFORMED_RESPONSE = "malformed_response"

# The complete typed, sanitized error vocabulary. Only these codes may appear as
# an envelope ``error.code``; code-owned warning markers (e.g.
# ``stale_list_time_data``, ``additional_warnings_truncated``) are deliberately
# excluded so a warning marker can never be promoted into the error code.
TYPED_ERROR_CODES = frozenset(
    {
        ERROR_ACCESS_DENIED,
        ERROR_NOT_FOUND,
        ERROR_THROTTLED,
        ERROR_INVALID_REQUEST,
        ERROR_PROVIDER_ERROR,
        ERROR_MALFORMED_RESPONSE,
    }
)

# AWS/GameLift error codes that mean "authorization refused".
_DENIED_CODES = {
    "AccessDeniedException",
    "AccessDenied",
    "UnauthorizedException",
    "UnauthorizedOperation",
    "NotAuthorized",
}
_NOT_FOUND_CODES = {"NotFoundException", "ResourceNotFoundException"}
_THROTTLE_CODES = {"ThrottlingException", "Throttling", "TooManyRequestsException", "LimitExceededException"}
_INVALID_CODES = {"InvalidRequestException", "ValidationException", "InvalidFleetStatusException"}

# Error code -> distinct disposition. Denied and not-found are pinned to their
# own dispositions; every other failure is ``incomplete``.
_ERROR_CODE_STATUS = {
    ERROR_ACCESS_DENIED: STATUS_DENIED,
    ERROR_NOT_FOUND: STATUS_INCOMPLETE,
    ERROR_THROTTLED: STATUS_INCOMPLETE,
    ERROR_INVALID_REQUEST: STATUS_INCOMPLETE,
    ERROR_PROVIDER_ERROR: STATUS_INCOMPLETE,
}


def classify_error(exc: BaseException) -> str:
    """Map a provider exception to a typed, sanitized error code.

    The raw message is never returned or logged; only the stable code crosses
    into model context.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc.response, dict) else ""
        if code in _DENIED_CODES:
            return ERROR_ACCESS_DENIED
        if code in _NOT_FOUND_CODES:
            return ERROR_NOT_FOUND
        if code in _THROTTLE_CODES:
            return ERROR_THROTTLED
        if code in _INVALID_CODES:
            return ERROR_INVALID_REQUEST
        return ERROR_PROVIDER_ERROR
    if isinstance(exc, BotoCoreError):
        return ERROR_PROVIDER_ERROR
    return ERROR_PROVIDER_ERROR


def _status_for_error_code(code: str) -> str:
    """Pin each typed error code to a distinct disposition."""
    return _ERROR_CODE_STATUS.get(code, STATUS_INCOMPLETE)


def error_result(collection_key: str, exc: BaseException) -> dict[str, Any]:
    """Build a sanitized error result for a failed provider call.

    ``not_found`` is surfaced through ``error.code`` while keeping the
    disposition distinct from ``denied`` and ``empty``.
    """
    code = classify_error(exc)
    return {
        "status": _status_for_error_code(code),
        collection_key: [],
        "error": {"code": code},
    }


# ---------------------------------------------------------------------------
# Code-owned value validators
#
# Field-name allowlisting is not enough: an allowed string field still carries a
# provider-controlled *value*. Every model-visible string must satisfy its
# field-specific grammar and must never carry sensitive coordinates — an ARN
# prefix, a bare account ID, a URL scheme, an IP/network address, or control
# characters — even when the value is short. Values that do not match their
# grammar are dropped by construction rather than passed through.
# ---------------------------------------------------------------------------

# Any C0/C1 control character (incl. newlines, NUL) disqualifies a string: it
# can inject log/JSON structure or smuggle payload past a naive length check.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Any 12 consecutive digits can carry an AWS account ID. Reject the sequence
# even when it is embedded in a longer digit or name run; preserving such a
# value is unnecessary for these model-visible operational fields.
_ACCOUNT_ID = re.compile(r"\d{12}")

# Conservatively treat every four-component numeric dotted run as a network
# coordinate candidate, including embedded, padded, and oversized spellings.
# These model-visible GameLift operational strings do not need that shape, so
# rejecting it avoids parser-dependent interpretations at the trust boundary.
_DOTTED_QUAD_TEXT = re.compile(r"\d+(?:\.\d+){3}")

# URL / URI schemes (http, https, s3, file, ftp, ...) — the "scheme://" shape.
_URL_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://")

# Field grammars. These are deliberately strict allowlists of the character
# classes GameLift actually uses for each field kind.
#
# * identifier — fleet/resource IDs: ``fleet-...``, ``sc-...``; ASCII word chars
#   and dashes only. An ARN contains ``:`` and ``/`` so it fails this grammar.
# * region     — GameLift Location: ``us-west-2``, ``eu-west-1``, ``local-...``;
#   lowercase letters, digits, and single dashes.
# * name       — human/policy names: word chars, space, dash, underscore, dot.
# * token      — enum-like values (ComparisonOperator, ScalingAdjustmentType,
#   metric/policy names, ...): word chars, dash, dot; no whitespace or slashes.
_RE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,254}$")
_RE_REGION = re.compile(r"^[a-z]{2,}(-[a-z0-9]+){1,4}$")
_RE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,254}$")
_RE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,254}$")

# Fleet-ID grammar. Only the exact canonical fleet-UUID shape is exempt from the
# 12-digit account-identifier rejection. A GameLift fleet ID is a lowercase
# resource prefix (``fleet-`` or ``containerfleet-``, matching the pinned
# botocore ``FleetId`` pattern ``^[a-z]*fleet-[a-zA-Z0-9\-]+``) followed by a
# resource suffix. The suffix of a *created* fleet is a UUID whose last group is
# 12 hex characters; when those 12 characters are all decimal digits the generic
# ``\d{12}`` account-ID rejection would wrongly discard a real fleet (~0.36% of
# fleets). We therefore recognise ONLY the exact canonical shape and exempt only
# it from the digit-run rule; every other 12-digit value (ARN-shaped, padded,
# uppercased, free-form) stays rejected.
_RE_FLEET_ID = re.compile(r"^[a-z]*fleet-[A-Za-z0-9][A-Za-z0-9\-]{0,120}$")
_RE_CANONICAL_FLEET_ID = re.compile(
    r"^(?:fleet|containerfleet)-" r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# The botocore GameLift ``FleetId`` shape caps the identifier at 128 characters;
# bound to that maximum rather than the generic string length so an oversized
# value cannot pass the prefix grammar.
GAMELIFT_MAX_FLEET_ID_LENGTH = 128

# AWS enum allowlists, pinned to the botocore GameLift service model. A drift
# test (``test_enum_allowlists_match_botocore_model``) compares each set to
# ``botocore.session.get_session().get_service_model("gamelift")`` so an SDK
# upgrade that adds/removes a value is caught. Enum fields are AWS-owned, so a
# hostname-shaped or free-form value is rejected rather than accepted as a loose
# token (which allowed dotted runs such as ``metadata.internal.example.invalid``).
_FLEET_STATUS_ENUM = frozenset(
    {
        "NEW",
        "DOWNLOADING",
        "VALIDATING",
        "BUILDING",
        "ACTIVATING",
        "ACTIVE",
        "DELETING",
        "ERROR",
        "TERMINATED",
        "NOT_FOUND",
        "EXPIRED",
    }
)
_FLEET_TYPE_ENUM = frozenset({"ON_DEMAND", "SPOT"})
_COMPUTE_TYPE_ENUM = frozenset({"EC2", "ANYWHERE"})
_OPERATING_SYSTEM_ENUM = frozenset(
    {"WINDOWS_2012", "AMAZON_LINUX", "AMAZON_LINUX_2", "WINDOWS_2016", "AMAZON_LINUX_2023", "WINDOWS_2022"}
)
_PROTECTION_POLICY_ENUM = frozenset({"NoProtection", "FullProtection"})
_PLAYER_GATEWAY_MODE_ENUM = frozenset({"DISABLED", "ENABLED", "REQUIRED"})
_CONTAINER_FLEET_STATUS_ENUM = frozenset(
    {"PENDING", "CREATING", "CREATED", "ACTIVATING", "ACTIVE", "UPDATING", "DELETING", "EXPIRED"}
)
_CONTAINER_BILLING_TYPE_ENUM = frozenset({"ON_DEMAND", "SPOT"})
_DEPLOYMENT_STATUS_ENUM = frozenset(
    {"IN_PROGRESS", "IMPAIRED", "COMPLETE", "ROLLBACK_IN_PROGRESS", "ROLLBACK_COMPLETE", "CANCELLED", "PENDING"}
)
_LOG_DESTINATION_ENUM = frozenset({"NONE", "CLOUDWATCH", "S3"})
_CONTAINER_GROUP_TYPE_ENUM = frozenset({"GAME_SERVER", "PER_INSTANCE"})
_CONTAINER_GROUP_STATUS_ENUM = frozenset({"READY", "COPYING", "FAILED"})
_CONTAINER_OS_ENUM = frozenset({"AMAZON_LINUX_2023"})

# EC2 instance-type grammar: a family token, a dot, and an ANCHORED size token.
# The family is a lowercase alphanumeric token that may carry a SINGLE dash-joined
# subtoken (``c7i-flex``, ``u7i-12tb``); each token is length-bounded and the
# family as a whole must contain at least one digit (every value in the pinned
# botocore ``EC2InstanceType`` enum does). The size is one of the known EC2 size
# shapes — ``nano``/``micro``/``small``/``medium``/``large``, ``xlarge`` and
# ``<N>xlarge``, ``metal`` and ``metal-<N>xl``. The digit requirement and the
# single-dash, length-bounded family together reject a digit-free host label such
# as ``metadata.large``, a dash-encoded IPv4 label such as ``ip-10-0-0-1.large``
# (too many dash groups), and an oversized family; a two-label hostname shape such
# as ``metadata.internal`` fails because ``internal`` is not a size token, and an
# embedded dotted-quad is rejected earlier by the network-coordinate check. Every
# value in the pinned enum matches (verified by
# ``test_instance_type_grammar_matches_every_pinned_ec2_type``).
_RE_INSTANCE_TYPE = re.compile(
    r"^(?=[a-z0-9-]*[0-9])[a-z][a-z0-9]{0,14}(?:-[a-z0-9]{1,14})?"
    r"\.(?:nano|micro|small|medium|large|metal|metal-[0-9]{1,3}xl|[0-9]{1,3}xlarge|xlarge)$"
)

# Validator kinds keyed on field grammar.
_IDENTIFIER = "identifier"
_REGION = "region"
_NAME = "name"
_TOKEN = "token"
_NUMBER = "num"
_TIMESTAMP = "timestamp"
_FLEET_ID = "fleet_id"
_INSTANCE_TYPE = "instance_type"
# Enum kinds — each validated against its hard-coded, model-pinned allowlist.
_ENUM_FLEET_STATUS = "enum_fleet_status"
_ENUM_FLEET_TYPE = "enum_fleet_type"
_ENUM_COMPUTE_TYPE = "enum_compute_type"
_ENUM_OPERATING_SYSTEM = "enum_operating_system"
_ENUM_PROTECTION_POLICY = "enum_protection_policy"
_ENUM_PLAYER_GATEWAY_MODE = "enum_player_gateway_mode"
_ENUM_CONTAINER_FLEET_STATUS = "enum_container_fleet_status"
_ENUM_CONTAINER_BILLING_TYPE = "enum_container_billing_type"
_ENUM_DEPLOYMENT_STATUS = "enum_deployment_status"
_ENUM_LOG_DESTINATION = "enum_log_destination"
_ENUM_CONTAINER_GROUP_TYPE = "enum_container_group_type"
_ENUM_CONTAINER_GROUP_STATUS = "enum_container_group_status"
_ENUM_CONTAINER_OS = "enum_container_os"

_STRING_GRAMMARS: dict[str, "re.Pattern[str]"] = {
    _IDENTIFIER: _RE_IDENTIFIER,
    _REGION: _RE_REGION,
    _NAME: _RE_NAME,
    _TOKEN: _RE_TOKEN,
    _INSTANCE_TYPE: _RE_INSTANCE_TYPE,
}

# Enum kind -> its allowlist.
_ENUM_ALLOWLISTS: dict[str, frozenset[str]] = {
    _ENUM_FLEET_STATUS: _FLEET_STATUS_ENUM,
    _ENUM_FLEET_TYPE: _FLEET_TYPE_ENUM,
    _ENUM_COMPUTE_TYPE: _COMPUTE_TYPE_ENUM,
    _ENUM_OPERATING_SYSTEM: _OPERATING_SYSTEM_ENUM,
    _ENUM_PROTECTION_POLICY: _PROTECTION_POLICY_ENUM,
    _ENUM_PLAYER_GATEWAY_MODE: _PLAYER_GATEWAY_MODE_ENUM,
    _ENUM_CONTAINER_FLEET_STATUS: _CONTAINER_FLEET_STATUS_ENUM,
    _ENUM_CONTAINER_BILLING_TYPE: _CONTAINER_BILLING_TYPE_ENUM,
    _ENUM_DEPLOYMENT_STATUS: _DEPLOYMENT_STATUS_ENUM,
    _ENUM_LOG_DESTINATION: _LOG_DESTINATION_ENUM,
    _ENUM_CONTAINER_GROUP_TYPE: _CONTAINER_GROUP_TYPE_ENUM,
    _ENUM_CONTAINER_GROUP_STATUS: _CONTAINER_GROUP_STATUS_ENUM,
    _ENUM_CONTAINER_OS: _CONTAINER_OS_ENUM,
}


def _looks_like_network_coordinate(value: str) -> bool:
    """True if the string is, or merely *contains*, an IP address or CIDR block.

    Detects three shapes, including when the coordinate is embedded inside an
    otherwise grammar-valid value (dashes/underscores/letters around it):

    * the whole value (or a ``host/prefix`` split) parses as an IP address;
    * any whitespace/comma/semicolon-delimited token parses as an IP address;
    * a dotted-quad IPv4 (with an optional ``/prefix`` or ``-prefix``) or a
      colon-hex IPv6 run appears anywhere in the value.
    """
    candidate = value.split("/", 1)[0]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    # Whole tokens delimited by whitespace/comma/semicolon.
    for token in re.split(r"[\s,;]+", value):
        host = token.split("/", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            continue
    # Conservatively reject every four-component numeric dotted run before
    # canonical parsing. The operational fields do not require this shape, and
    # treating it uniformly avoids parser-dependent network interpretations.
    if _DOTTED_QUAD_TEXT.search(value):
        return True
    # Embedded colon-hex IPv6 runs. Any colon-bearing hextet sequence that parses
    # as an IPv6 address is a network coordinate.
    for token in re.split(r"[^0-9A-Fa-f:]+", value):
        if ":" in token:
            try:
                ipaddress.IPv6Address(token)
                return True
            except ValueError:
                continue
    return False


def _has_sensitive_coordinate(value: str) -> bool:
    """True if the string carries an ARN prefix, account ID, URL scheme, or IP."""
    if value.startswith("arn:"):
        return True
    if _ACCOUNT_ID.search(value):
        return True
    if _URL_SCHEME.search(value):
        return True
    if _looks_like_network_coordinate(value):
        return True
    return False


def _valid_fleet_id(value: Any) -> bool:
    """Exact ``str`` that is a canonical/accepted GameLift fleet ID.

    Applies the same control-char / ARN / URL / network-coordinate rejections as
    every other string field, requires the pinned botocore ``FleetId`` prefix
    grammar, and then applies the 12-digit account-identifier rejection — with a
    single exemption: a value that is the EXACT canonical ``fleet-``/
    ``containerfleet-`` + lowercase 8-4-4-4-12 hex UUID shape may carry an
    all-decimal 12-char UUID tail. Every other 12-digit value (ARN-shaped,
    padded, uppercased, free-form) stays rejected.
    """
    if not isinstance(value, str):
        return False
    if len(value) > GAMELIFT_MAX_FLEET_ID_LENGTH:
        return False
    if _CONTROL_CHARS.search(value):
        return False
    # ARN prefix, URL scheme, and network coordinates are always disqualifying.
    if value.startswith("arn:"):
        return False
    if _URL_SCHEME.search(value):
        return False
    if _looks_like_network_coordinate(value):
        return False
    if _RE_FLEET_ID.match(value) is None:
        return False
    # The 12-digit account-identifier rejection applies unless the value is the
    # exact canonical fleet-UUID shape (whose UUID tail may be all digits).
    if _ACCOUNT_ID.search(value) and _RE_CANONICAL_FLEET_ID.match(value) is None:
        return False
    return True


def _valid_typed_string(value: Any, kind: str) -> bool:
    """Exact ``str`` matching ``kind``'s grammar and free of sensitive content.

    Rejects, regardless of length: non-strings, oversized strings, control
    characters, ARN prefixes, bare account IDs, URL schemes, IP/network
    coordinates, and anything failing the field-specific grammar. Enum kinds are
    validated against their hard-coded, model-pinned allowlist (membership also
    guarantees no sensitive coordinate, since enum values are AWS-owned tokens).
    """
    if kind is _FLEET_ID:
        return _valid_fleet_id(value)
    if kind in _ENUM_ALLOWLISTS:
        return isinstance(value, str) and value in _ENUM_ALLOWLISTS[kind]
    if not isinstance(value, str):
        return False
    if len(value) > GAMELIFT_MAX_STRING_LENGTH:
        return False
    if _CONTROL_CHARS.search(value):
        return False
    if _has_sensitive_coordinate(value):
        return False
    grammar = _STRING_GRAMMARS.get(kind)
    if grammar is None:
        return False
    return grammar.match(value) is not None


def _valid_number(value: Any) -> bool:
    """Finite real number within the magnitude bound.

    ``bool`` is a subclass of ``int`` but is not a numeric metric here, so it is
    rejected. A Python ``int`` is arbitrary precision and ``float(huge_int)``
    raises ``OverflowError``; the int-vs-float comparison below is exact in
    CPython and never overflows. Floats are checked with ``math.isfinite`` so
    ``NaN`` / ``inf`` are rejected before the magnitude check.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= GAMELIFT_MAX_ABS_NUMBER
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) <= GAMELIFT_MAX_ABS_NUMBER
    return False


# Timestamp fields (e.g. ``CreationTime``) arrive from boto3 as timezone-aware
# ``datetime`` objects, which ``json.dumps`` cannot serialize. They are
# operationally useful (fleet age) and non-sensitive, so project them to a
# bounded ISO-8601 string. An already-string ISO value is accepted only when it
# matches this strict grammar and carries no sensitive coordinate; anything else
# is dropped.
_RE_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?([+-]\d{2}:?\d{2}|Z)?$")


def _project_timestamp(value: Any) -> str | None:
    """Return a bounded ISO-8601 string for a timestamp field, or ``None``.

    Accepts a ``datetime`` (rendered via ``isoformat``) or a string already in
    strict ISO-8601 form. The rendered/validated string is additionally bounded
    in length and rejected if it somehow carries a sensitive coordinate.
    """
    if isinstance(value, datetime.datetime):
        rendered = value.isoformat()
    elif isinstance(value, str):
        rendered = value
    else:
        return None
    if len(rendered) > GAMELIFT_MAX_STRING_LENGTH:
        return None
    if _CONTROL_CHARS.search(rendered):
        return None
    if _has_sensitive_coordinate(rendered):
        return None
    if _RE_ISO_TIMESTAMP.match(rendered) is None:
        return None
    return rendered


def _copy_validated(item: dict[str, Any], fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """Return only allowlisted fields whose value passes its field grammar.

    ``fields`` is a tuple of ``(field_name, kind)`` pairs where ``kind`` is one
    of the string grammar kinds (identifier/region/name/token) or ``_NUMBER``.
    Anything not named — future provider fields, full ARNs, endpoints, arbitrary
    nested blobs — is dropped by construction. An allowed field carrying an
    invalid value (wrong type, failed grammar, sensitive coordinate, oversized
    string, non-finite / oversized number) is also dropped, predictably.
    """
    projected: dict[str, Any] = {}
    for key, kind in fields:
        value = item.get(key)
        if value is None:
            continue
        if kind is _NUMBER:
            if _valid_number(value):
                projected[key] = value
        elif kind is _TIMESTAMP:
            rendered = _project_timestamp(value)
            if rendered is not None:
                projected[key] = rendered
        elif _valid_typed_string(value, kind):
            projected[key] = value
    return projected


def _copy_validated_numbers(counts: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    """Project a nested numeric map (instance/group counts): validated numbers only."""
    projected: dict[str, Any] = {}
    for key in allowed:
        value = counts.get(key)
        if value is not None and _valid_number(value):
            projected[key] = value
    return projected


def _serialized_length(result: dict[str, Any]) -> int:
    """Length of the exact JSON representation the model will see.

    Measured with the same escaping (``json.dumps`` default ``ensure_ascii``)
    the runtime uses, so control characters and non-ASCII code points that
    expand under escaping cannot slip a payload past the budget. Covers keys,
    the ``status``/``truncated`` envelope, and every escaped scalar.
    """
    return len(json.dumps(result))


def _project_collection(
    response: Any,
    collection_key: str,
    project_item: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Bound, project, validate, and classify a successful provider response.

    Dispositions are kept distinct. A malformed response shape — a non-mapping
    top-level response, the collection missing or of the wrong type, or a
    non-empty collection every item of which is non-projectable — is
    ``incomplete`` with a ``malformed_response`` error code, never an
    authoritative ``empty`` or a complete ``ok``. ``empty`` is reserved for a
    valid, present, empty list with no continuation token. An empty page that
    still carries a continuation token is ``incomplete``.

    A *mixed* collection (some valid dict rows alongside discarded non-dict or
    wholly-invalid rows) retains the bounded valid rows but is reported as
    ``incomplete``/``malformed_response`` and ``truncated``: the view is partial
    because rows were dropped, so it must never be ``ok``. ``ok`` is reserved for
    a wholly valid, complete payload with nothing discarded and no continuation
    token.
    """
    # A non-mapping top-level response (None, list, str, int, ...) is itself
    # malformed. Classify it as typed incomplete rather than raising on ``.get``.
    if not isinstance(response, dict):
        return {
            "status": STATUS_INCOMPLETE,
            collection_key: [],
            "error": {"code": ERROR_MALFORMED_RESPONSE},
        }

    raw_items = response.get(collection_key)
    has_more_pages = bool(response.get("NextToken"))

    # Missing or wrongly-typed collection: the shape itself is unusable.
    if not isinstance(raw_items, list):
        return {
            "status": STATUS_INCOMPLETE,
            collection_key: [],
            "error": {"code": ERROR_MALFORMED_RESPONSE},
        }

    # A valid, present, empty list: distinguish a clean zero result from an
    # empty page that still has a continuation token.
    if not raw_items:
        if has_more_pages:
            return {"status": STATUS_INCOMPLETE, collection_key: [], "truncated": True}
        return {"status": STATUS_EMPTY, collection_key: []}

    dict_items = [item for item in raw_items if isinstance(item, dict)]
    # Track whether ANY row was discarded as malformed: non-dict entries, or dict
    # entries that projected to nothing usable. A discarded row means the view is
    # partial, so the result must be incomplete/malformed_response, never ok.
    non_dict_dropped = len(dict_items) != len(raw_items)

    capped = len(dict_items) > GAMELIFT_MAX_PROJECTED_ITEMS
    considered = dict_items[:GAMELIFT_MAX_PROJECTED_ITEMS]
    projected_all = [project_item(item) for item in considered]
    # Drop rows that projected to nothing usable (every operational value failed
    # validation); they carry no signal and could hide a malformed row.
    projected = [row for row in projected_all if row]
    invalid_row_dropped = len(projected) != len(projected_all)

    if not projected:
        # The provider returned items, but none survived projection (wrong item
        # types, or every operational value invalid) — the response was
        # malformed/invalid, not an authoritative zero inventory.
        return {
            "status": STATUS_INCOMPLETE,
            collection_key: [],
            "error": {"code": ERROR_MALFORMED_RESPONSE},
        }

    # Decide the final disposition envelope BEFORE enforcing the payload budget,
    # so the budget is measured against the exact keys the model will see. A
    # mixed collection (malformed rows discarded) carries the longer
    # incomplete + malformed_response + truncated envelope; that envelope — not a
    # shorter proxy — must fit within the char budget.
    mixed_malformed = non_dict_dropped or invalid_row_dropped

    result: dict[str, Any] = {collection_key: projected}
    if mixed_malformed:
        result["status"] = STATUS_INCOMPLETE
        result["truncated"] = True
        result["error"] = {"code": ERROR_MALFORMED_RESPONSE}
    elif capped:
        result["status"] = STATUS_TRUNCATED
        result["truncated"] = True
    elif has_more_pages:
        result["status"] = STATUS_INCOMPLETE
        result["truncated"] = True
    else:
        result["status"] = STATUS_OK

    # Aggregate payload budget, measured against the ACTUAL final envelope the
    # model will see (status + truncated + any error code + escaped scalars):
    # drop rows from the tail until the serialized result fits. If trimming a row
    # made the view partial, the envelope must reflect that (truncated + typed
    # incomplete) even if it was previously a complete ``ok``.
    while len(projected) > 1 and _serialized_length(result) > GAMELIFT_MAX_PROJECTED_CHARS:
        projected.pop()
        if result["status"] == STATUS_OK:
            # A complete payload trimmed for size is no longer complete: it is a
            # bounded, partial view.
            result["status"] = STATUS_TRUNCATED
            result["truncated"] = True
    return result


# ---------------------------------------------------------------------------
# Per-operation allowlists (field name + typed validator) and item projectors
# ---------------------------------------------------------------------------
_UTILIZATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("FleetId", _FLEET_ID),
    ("ActiveServerProcessCount", _NUMBER),
    ("ActiveGameSessionCount", _NUMBER),
    ("CurrentPlayerSessionCount", _NUMBER),
    ("MaximumPlayerSessionCount", _NUMBER),
    ("Location", _REGION),
)

_CAPACITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("FleetId", _FLEET_ID),
    ("InstanceType", _INSTANCE_TYPE),
    ("Location", _REGION),
)
_INSTANCE_COUNT_FIELDS = ("DESIRED", "MINIMUM", "MAXIMUM", "PENDING", "ACTIVE", "IDLE", "TERMINATING")
_GROUP_COUNT_FIELDS = ("PENDING", "ACTIVE", "IDLE", "TERMINATING")

_SCALING_POLICY_FIELDS: tuple[tuple[str, str], ...] = (
    ("FleetId", _FLEET_ID),
    ("Name", _NAME),
    ("Status", _TOKEN),
    ("ScalingAdjustment", _NUMBER),
    ("ScalingAdjustmentType", _TOKEN),
    ("ComparisonOperator", _TOKEN),
    ("Threshold", _NUMBER),
    ("EvaluationPeriods", _NUMBER),
    ("MetricName", _TOKEN),
    ("PolicyType", _TOKEN),
    ("UpdateStatus", _TOKEN),
    ("Location", _REGION),
)


def _project_utilization_item(item: dict[str, Any]) -> dict[str, Any]:
    return _copy_validated(item, _UTILIZATION_FIELDS)


def _project_capacity_item(item: dict[str, Any]) -> dict[str, Any]:
    projected = _copy_validated(item, _CAPACITY_FIELDS)
    instance_counts = item.get("InstanceCounts")
    if isinstance(instance_counts, dict):
        counts = _copy_validated_numbers(instance_counts, _INSTANCE_COUNT_FIELDS)
        if counts:
            projected["InstanceCounts"] = counts
    group_counts = item.get("GameServerContainerGroupCounts")
    if isinstance(group_counts, dict):
        counts = _copy_validated_numbers(group_counts, _GROUP_COUNT_FIELDS)
        if counts:
            projected["GameServerContainerGroupCounts"] = counts
    return projected


def _project_scaling_policy_item(item: dict[str, Any]) -> dict[str, Any]:
    projected = _copy_validated(item, _SCALING_POLICY_FIELDS)
    target = item.get("TargetConfiguration")
    if isinstance(target, dict):
        target_value = target.get("TargetValue")
        if target_value is not None and _valid_number(target_value):
            projected["TargetConfiguration"] = {"TargetValue": target_value}
    return projected


def project_fleet_utilization(response: dict[str, Any]) -> dict[str, Any]:
    return _project_collection(response, "FleetUtilization", _project_utilization_item)


def project_fleet_capacity(response: dict[str, Any]) -> dict[str, Any]:
    return _project_collection(response, "FleetCapacity", _project_capacity_item)


def project_scaling_policies(response: dict[str, Any]) -> dict[str, Any]:
    return _project_collection(response, "ScalingPolicies", _project_scaling_policy_item)


# ---------------------------------------------------------------------------
# Fleet-listing projections (list_gamelift_fleets)
#
# The listing tool assembles two collections — classic fleets (from
# ``describe_fleet_attributes``) and container-fleet summaries (built by the
# specialist from several container APIs) — and reports distinct counts. Both
# collections are projected and bounded here with the SAME validators used
# above, so the specialist never hand-rolls its own field handling. The listing
# envelope carries a distinct ``status`` plus independent boolean flags
# (``truncated``, ``paginated``, ``partial``, ``stale``) and a typed sanitized
# ``error`` code, and the aggregate char budget is enforced against the FINAL
# serialized envelope.
# ---------------------------------------------------------------------------

# Classic fleet (describe_fleet_attributes) reviewed allowlist. ARNs, role ARNs,
# launch paths/parameters, log paths, metric groups, free-text description, and
# any unreviewed/future field are dropped by construction.
_CLASSIC_FLEET_FIELDS: tuple[tuple[str, str], ...] = (
    ("FleetId", _FLEET_ID),
    ("Name", _NAME),
    ("Status", _ENUM_FLEET_STATUS),
    ("FleetType", _ENUM_FLEET_TYPE),
    ("ComputeType", _ENUM_COMPUTE_TYPE),
    ("InstanceType", _INSTANCE_TYPE),
    ("OperatingSystem", _ENUM_OPERATING_SYSTEM),
    ("CreationTime", _TIMESTAMP),
    ("NewGameSessionProtectionPolicy", _ENUM_PROTECTION_POLICY),
)

# Container-fleet summary reviewed allowlist. ``FleetType`` is a code-owned
# constant marker injected by the specialist; the remaining fields come from
# container APIs and are validated here. Nested group-definition fields are
# projected by :data:`_CONTAINER_GROUP_FIELDS`.
_CONTAINER_FLEET_FIELDS: tuple[tuple[str, str], ...] = (
    ("FleetType", _TOKEN),
    ("Status", _ENUM_CONTAINER_FLEET_STATUS),
    ("InstanceType", _INSTANCE_TYPE),
    ("BillingType", _ENUM_CONTAINER_BILLING_TYPE),
    ("GameServerContainerGroupDefinitionName", _NAME),
    ("GameServerContainerGroupDefinitionVersion", _NUMBER),
    ("GameServerContainerGroupsPerInstance", _NUMBER),
    ("MaximumGameServerContainerGroupsPerInstance", _NUMBER),
    ("DeploymentStatus", _ENUM_DEPLOYMENT_STATUS),
    ("LogDestinationType", _ENUM_LOG_DESTINATION),
    ("PlayerGatewayMode", _ENUM_PLAYER_GATEWAY_MODE),
    ("LocationCount", _NUMBER),
)

_CONTAINER_GROUP_FIELDS: tuple[tuple[str, str], ...] = (
    ("Name", _NAME),
    ("VersionNumber", _NUMBER),
    ("ContainerGroupType", _ENUM_CONTAINER_GROUP_TYPE),
    ("Status", _ENUM_CONTAINER_GROUP_STATUS),
    ("OperatingSystem", _ENUM_CONTAINER_OS),
    ("TotalMemoryLimitMebibytes", _NUMBER),
    ("TotalVcpuLimit", _NUMBER),
)


def project_classic_fleet(item: dict[str, Any]) -> dict[str, Any]:
    """Project one raw ``describe_fleet_attributes`` item to the reviewed set.

    A classic row without a grammar-valid ``FleetId`` is useless and misleading
    (it cannot be correlated to a fleet), so the whole row is discarded even if
    other allowed fields validate. ``FleetId`` is validated with the canonical
    fleet-ID grammar, which accepts the exact ``fleet-``/``containerfleet-`` +
    lowercase 8-4-4-4-12 hex UUID shape even when its UUID tail is all digits,
    while still rejecting ARN-shaped, padded, uppercased, and free-form 12-digit
    values. The caller counts a discarded row as a malformed row so the listing
    is reported partial rather than silently ``ok``.
    """
    if not isinstance(item, dict):
        return {}
    projected = _copy_validated(item, _CLASSIC_FLEET_FIELDS)
    if "FleetId" not in projected:
        return {}
    return projected


def project_container_group_summary(definition: dict[str, Any] | None) -> dict[str, Any]:
    """Project a container group-definition summary with validated fields."""
    if not isinstance(definition, dict):
        return {}
    return _copy_validated(definition, _CONTAINER_GROUP_FIELDS)


def project_container_fleet_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Project a container-fleet summary: validated scalar fields + nested group.

    The specialist assembles a raw summary dict (merging container fleet,
    group-definition, and deployment data). This validates every scalar against
    its field grammar and projects the nested ``ContainerGroupDefinition`` with
    the group-definition allowlist.

    ``FleetType`` is a code-owned constant marker the specialist injects; it is
    not provider-derived evidence that a real fleet was described. Row validity
    is therefore decided on the PROVIDER-derived fields only: if every provider
    field fails validation, the row is discarded (returns ``{}``) rather than
    surviving on the marker alone, so an all-invalid container fleet is counted
    as a discarded malformed row instead of being presented as a real fleet.
    """
    if not isinstance(summary, dict):
        return {}
    projected = _copy_validated(summary, _CONTAINER_FLEET_FIELDS)
    group = summary.get("ContainerGroupDefinition")
    group_projected = project_container_group_summary(group if isinstance(group, dict) else None)
    if group_projected:
        projected["ContainerGroupDefinition"] = group_projected
    # Decide row validity on provider-derived fields only. The injected
    # ``FleetType`` marker must not keep an otherwise-empty row alive.
    provider_fields = {key: value for key, value in projected.items() if key != "FleetType"}
    if not provider_fields:
        return {}
    return projected


def _bound_projected_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Item-cap already-projected rows, dropping empty projections.

    Returns the bounded, non-empty rows and whether the item cap was hit.
    """
    non_empty = [row for row in rows if row]
    capped = len(non_empty) > GAMELIFT_MAX_PROJECTED_ITEMS
    return non_empty[:GAMELIFT_MAX_PROJECTED_ITEMS], capped


def _dedupe_warnings(warnings: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Collapse code-owned warnings to bounded ``{Source, Code, Count}`` triples.

    Each input warning is a code-owned ``{Source, Code}`` pair (never provider
    text). Duplicates are merged by their ``(Source, Code)`` key with an integer
    ``Count`` of how many times they occurred, preserving first-seen order, and
    the number of DISTINCT entries is capped at
    :data:`GAMELIFT_MAX_WARNINGS`. This keeps a maximal failure fan-out from
    growing one warning per failure and crowding real rows out of the envelope.
    Entries beyond the cap still contribute their occurrences to a final
    ``{Source: "warnings", Code: "additional_warnings_truncated"}`` summary whose
    ``Count`` is the TOTAL number of folded occurrences (summed across every
    distinct key beyond the cap), so the overflow is visible rather than silently
    dropped.
    """
    order: list[tuple[str, str]] = []
    counts: dict[tuple[str, str], int] = {}
    for warning in warnings:
        source = warning.get("Source", "unknown")
        code = warning.get("Code", "unknown")
        key = (source, code)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1

    # Keep the first (GAMELIFT_MAX_WARNINGS - 1) distinct entries verbatim and,
    # if more distinct entries exist, fold the remainder into a single bounded
    # overflow summary so the cap is never exceeded and overflow stays visible.
    deduped: list[dict[str, Any]] = []
    if len(order) <= GAMELIFT_MAX_WARNINGS:
        for source, code in order:
            deduped.append({"Source": source, "Code": code, "Count": counts[(source, code)]})
        return deduped

    kept = order[: GAMELIFT_MAX_WARNINGS - 1]
    for source, code in kept:
        deduped.append({"Source": source, "Code": code, "Count": counts[(source, code)]})
    overflow_occurrences = sum(counts[key] for key in order[len(kept) :])
    deduped.append({"Source": "warnings", "Code": "additional_warnings_truncated", "Count": overflow_occurrences})
    return deduped


def build_fleet_list_result(
    *,
    classic_rows: list[dict[str, Any]],
    container_rows: list[dict[str, Any]],
    warnings: list[dict[str, str]],
    listing_denied: bool = False,
    classic_truncated: bool = False,
    container_truncated: bool = False,
    stale: bool = False,
    malformed: bool = False,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Assemble the bounded, code-owned fleet-listing envelope.

    ``classic_rows`` / ``container_rows`` are already-projected rows. This caps
    each collection, computes a distinct ``status`` plus independent boolean
    flags, pins a typed ``error`` code when the listing was refused or failed,
    reports ``FleetCounts`` from the RETURNED rows (never overclaiming), and
    enforces the aggregate char budget against the FINAL serialized envelope.

    Flags are independent, not collapsed into one status:

    * ``truncated`` — a collection was bounded by the item cap or the aggregate
      char budget;
    * ``paginated`` — a provider listing reported more pages than were consumed
      (surfaced via ``container_truncated`` / ``classic_truncated`` when the
      source paginator residual maps to a cap);
    * ``partial`` — at least one sub-call failed (``warnings`` non-empty) so the
      view is incomplete;
    * ``stale`` — at least one row fell back to list-time data because its
      describe call failed.

    ``malformed`` is set when the provider returned fleet items some or all of
    which failed projection, so the discarded rows must not be read as an
    authoritative ``empty`` inventory. When set alongside retained valid rows,
    the envelope is ``incomplete``/``malformed_response`` with ``truncated`` and
    ``partial`` markers (a mixed collection that kept valid rows while discarding
    malformed ones), never ``ok``. When set with NO rows retained (a terminal
    malformed listing), the envelope is ``incomplete``/``malformed_response``
    with ``partial`` but NO truncation marker: there is no retained view to be a
    truncation of.
    """
    classic_capped_rows, classic_item_capped = _bound_projected_rows(classic_rows)
    container_capped_rows, container_item_capped = _bound_projected_rows(container_rows)

    rows_retained = bool(classic_capped_rows or container_capped_rows)

    # A mixed collection that retained valid rows while discarding malformed ones
    # is a partial view and must carry the boolean ``truncated: true`` marker
    # (distinct from the ``truncated`` status). A TERMINAL malformed listing (no
    # rows retained) is not a truncated view of anything, so ``malformed`` folds
    # into the truncated flag ONLY when rows were retained.
    truncated = (
        classic_item_capped
        or container_item_capped
        or classic_truncated
        or container_truncated
        or (malformed and rows_retained)
    )
    partial = bool(warnings) or malformed

    deduped_warnings = _dedupe_warnings(warnings)

    result: dict[str, Any] = {
        "ClassicFleets": classic_capped_rows,
        "ContainerFleets": container_capped_rows,
        "FleetCounts": {
            "Classic": len(classic_capped_rows),
            "Container": len(container_capped_rows),
            "Total": len(classic_capped_rows) + len(container_capped_rows),
        },
        "Warnings": deduped_warnings,
    }

    # Distinct status. ``denied`` is pinned when the provider refused the whole
    # listing. Otherwise a failed sub-call, discarded malformed rows, or
    # truncation is ``incomplete`` with the relevant flags; a wholly-valid
    # payload bounded only by caps is ``truncated``; a wholly-valid complete
    # payload is ``ok``; a valid empty inventory with no failures and no
    # continuation is ``empty``.
    if listing_denied:
        result["status"] = STATUS_DENIED
        result["error"] = {"code": error_code or ERROR_ACCESS_DENIED}
    elif partial:
        result["status"] = STATUS_INCOMPLETE
        pinned_error = error_code or (ERROR_MALFORMED_RESPONSE if malformed else None)
        if pinned_error:
            result["error"] = {"code": pinned_error}
    elif truncated:
        result["status"] = STATUS_TRUNCATED
    elif not classic_capped_rows and not container_capped_rows:
        result["status"] = STATUS_EMPTY
    else:
        result["status"] = STATUS_OK

    # Independent flags (only set when true; never collapsed into status).
    if truncated:
        result["truncated"] = True
    if truncated and (classic_truncated or container_truncated):
        result["paginated"] = True
    if partial:
        result["partial"] = True
    if stale:
        result["stale"] = True

    _enforce_fleet_list_budget(result)
    return result


def _enforce_fleet_list_budget(result: dict[str, Any]) -> None:
    """Trim both collections from the tail until the final envelope fits.

    Measured against the exact serialized envelope the model sees (status,
    flags, counts, warnings, and escaped scalars). Trimming makes the view
    partial, so set the ``truncated`` flag and move a complete ``ok``/``empty``
    status to ``truncated``. ``FleetCounts`` is kept in sync with the retained
    rows so it never overclaims.
    """

    def _total_rows() -> int:
        return len(result["ClassicFleets"]) + len(result["ContainerFleets"])

    while _total_rows() > 0 and _serialized_length(result) > GAMELIFT_MAX_PROJECTED_CHARS:
        # Trim from the larger collection first to converge quickly.
        if len(result["ContainerFleets"]) >= len(result["ClassicFleets"]) and result["ContainerFleets"]:
            result["ContainerFleets"].pop()
        elif result["ClassicFleets"]:
            result["ClassicFleets"].pop()
        else:
            result["ContainerFleets"].pop()
        result["FleetCounts"] = {
            "Classic": len(result["ClassicFleets"]),
            "Container": len(result["ContainerFleets"]),
            "Total": len(result["ClassicFleets"]) + len(result["ContainerFleets"]),
        }
        result["truncated"] = True
        if result.get("status") in (STATUS_OK, STATUS_EMPTY):
            result["status"] = STATUS_TRUNCATED


def log_sanitized_failure(operation: str, correlation_token: str, exc: BaseException) -> None:
    """Log a bounded, non-sensitive record of a provider failure.

    Only three things are logged, none provider- or caller-controlled in an
    unbounded way:

    * ``operation`` — a constant, code-owned operation name (e.g.
      ``describe_fleet_utilization``);
    * the typed, sanitized error code from :func:`classify_error`;
    * a bounded, non-sensitive correlation token.

    The raw exception message (which may embed ARNs / account IDs / URLs) and the
    caller-supplied fleet identifier are **never** logged. We deliberately do not
    attach the exception object to the Loguru record: Loguru's traceback
    rendering would serialize ``str(exc)`` (and, with ``diagnose``, local values)
    into the sink, which is exactly the provider-authored disclosure this
    function exists to prevent. The sanitized code preserves the operationally
    useful signal (denied vs throttled vs provider error) without the message.
    """
    safe_operation = _bounded_token(operation, 64)
    safe_token = _bounded_token(correlation_token, 64)
    logger.bind(
        gamelift_operation=safe_operation,
        error_code=classify_error(exc),
        correlation_token=safe_token,
    ).error("GameLift provider call failed")


def log_sanitized_local_fault(operation: str, correlation_token: str, error_code: str) -> None:
    """Log a bounded, non-sensitive record of a LOCAL (non-provider) shape fault.

    Used when assembling a projection raised on an unexpected local shape (for
    example an unhashable field reaching a dict key) rather than a provider call
    failing. Logs a distinct code-owned message so the record is not confused
    with a provider call failure, plus the already-typed sanitized ``error_code``
    and a bounded correlation token. No provider text, caller identifier, ARN, or
    account ID is logged, and the exception object is never attached (see
    :func:`log_sanitized_failure`).
    """
    logger.bind(
        gamelift_operation=_bounded_token(operation, 64),
        error_code=_bounded_token(error_code, 64),
        correlation_token=_bounded_token(correlation_token, 64),
    ).error("GameLift response assembly failed on a local shape fault")


def _bounded_token(value: Any, max_length: int) -> str:
    """Coerce a value to a bounded, single-line, non-sensitive token.

    Non-strings become ``"<non-string>"``; strings are truncated to
    ``max_length`` code points with newlines stripped so a crafted identifier
    cannot inject log lines or smuggle a large payload into the sink.
    """
    if not isinstance(value, str):
        return "<non-string>"
    single_line = value.replace("\n", " ").replace("\r", " ")
    if len(single_line) > max_length:
        return single_line[:max_length] + "…"
    return single_line
