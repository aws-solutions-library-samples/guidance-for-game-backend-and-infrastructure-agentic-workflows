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

# Per-value bounds. Allowed *string* fields are capped in Unicode code points;
# allowed *numeric* fields must be finite and within this magnitude. These are
# operational GameLift values (region-like strings, session counts, thresholds),
# so the bounds are deliberately generous but finite.
GAMELIFT_MAX_STRING_LENGTH = 256
GAMELIFT_MAX_ABS_NUMBER = 1e12

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

# A run of exactly 12 digits is an AWS account ID; reject it anywhere in a value,
# including when it is *decorated* — flanked by letters, underscores, or other
# name characters (e.g. ``acct_123456789012_prod`` or ``prodX123456789012Y``).
# Word boundaries (``\b``) do not fire between a letter/underscore and a digit,
# so they miss these evasions. Digit-specific negative lookarounds match a run
# of exactly 12 digits with no adjacent digit on either side, regardless of any
# surrounding non-digit characters.
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")

# Dotted-quad IPv4 detection is performed inside
# :func:`_looks_like_network_coordinate` by sliding a 4-octet window over each
# maximal dotted-numeric run, so a valid quad is rejected even when embedded in a
# longer dotted run (``host-10.11.12.13.14-prod``) or flanked by name characters
# (``host-10.11.12.13-prod``, ``net-10.11.12.0-24-prod``). Each candidate quad's
# octets are validated exactly (0-255) so a version-like token is only rejected
# when it is genuinely a legal IPv4 address.

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
# * token      — enum-like values (Status, ComparisonOperator, InstanceType,
#   metric names, ...): word chars, dash, dot; no whitespace or slashes.
_RE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,254}$")
_RE_REGION = re.compile(r"^[a-z]{2,}(-[a-z0-9]+){1,4}$")
_RE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,254}$")
_RE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,254}$")

# Validator kinds keyed on field grammar.
_IDENTIFIER = "identifier"
_REGION = "region"
_NAME = "name"
_TOKEN = "token"
_NUMBER = "num"

_STRING_GRAMMARS: dict[str, "re.Pattern[str]"] = {
    _IDENTIFIER: _RE_IDENTIFIER,
    _REGION: _RE_REGION,
    _NAME: _RE_NAME,
    _TOKEN: _RE_TOKEN,
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
    # Embedded dotted-quad IPv4 anywhere in the value, including inside a longer
    # dotted run (``10.11.12.13.14``) or flanked by name characters. Validate
    # each captured quad so a version-like token is only rejected when every
    # octet is a legal 0-255 IPv4 octet. To catch a valid quad that begins at
    # any octet boundary inside a longer dotted run (both the leading
    # ``10.11.12.13`` of ``10.11.12.13.14`` and the trailing ``11.12.13.14``),
    # slide a 4-octet window over each maximal dotted-numeric run.
    for run in re.findall(r"\d{1,3}(?:\.\d{1,3})+", value):
        octets = run.split(".")
        for i in range(len(octets) - 3):
            candidate = ".".join(octets[i : i + 4])
            try:
                ipaddress.IPv4Address(candidate)
                return True
            except ValueError:
                continue
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


def _valid_typed_string(value: Any, kind: str) -> bool:
    """Exact ``str`` matching ``kind``'s grammar and free of sensitive content.

    Rejects, regardless of length: non-strings, oversized strings, control
    characters, ARN prefixes, bare account IDs, URL schemes, IP/network
    coordinates, and anything failing the field-specific grammar.
    """
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
    ("FleetId", _IDENTIFIER),
    ("ActiveServerProcessCount", _NUMBER),
    ("ActiveGameSessionCount", _NUMBER),
    ("CurrentPlayerSessionCount", _NUMBER),
    ("MaximumPlayerSessionCount", _NUMBER),
    ("Location", _REGION),
)

_CAPACITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("FleetId", _IDENTIFIER),
    ("InstanceType", _TOKEN),
    ("Location", _REGION),
)
_INSTANCE_COUNT_FIELDS = ("DESIRED", "MINIMUM", "MAXIMUM", "PENDING", "ACTIVE", "IDLE", "TERMINATING")
_GROUP_COUNT_FIELDS = ("PENDING", "ACTIVE", "IDLE", "TERMINATING")

_SCALING_POLICY_FIELDS: tuple[tuple[str, str], ...] = (
    ("FleetId", _IDENTIFIER),
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
