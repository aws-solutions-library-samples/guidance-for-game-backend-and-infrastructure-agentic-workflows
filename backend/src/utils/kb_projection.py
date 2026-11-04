"""KB-owned projection of Bedrock Knowledge Base retrieval responses (#463).

This module is the single model-facing output boundary for Knowledge Base
retrieval. It takes the *raw* Bedrock Agent Runtime ``Retrieve`` response (see
the ``bedrock-agent-runtime`` service model: ``RetrieveResponse`` with
``retrievalResults``, ``nextToken`` and ``guardrailAction``) and produces a
bounded, sanitized, deterministically-serialized envelope safe to hand to a
model.

It is intentionally narrow to Knowledge Base retrieval — not a generic
projection framework — so it does not compete with the broader projection work
tracked separately (#466).

Guarantees enforced here:

* Retrieval count is validated to a minimum / default / hard maximum before it
  ever reaches the provider.
* Query text is bounded at the provider boundary.
* Each chunk's text is bounded by BOTH bytes and characters via deterministic
  truncation, AND field-sanitized so unsafe substrings (control characters,
  raw ARN shapes, URL schemes, 12-digit account-identifier substrings,
  certificate / credential markers, and network-coordinate shapes) never cross
  the boundary. Useful bounded prose is preserved; only unsafe substrings are
  deterministically redacted.
* Metadata is projected through an explicit field allowlist with bounded,
  field-sanitized values; everything else is dropped. Allowlisting a *key* is
  never sufficient — its value is validated too.
* Source location is projected through an explicit allowlist that excludes
  deployment-specific / sensitive identifiers. Only a coarse source ``type`` is
  surfaced (never a URI, id, query, or documentId).
* Numeric scores are accepted only as finite, in-bounds values; ``bool`` is not
  treated as numeric. The final envelope is serialized with ``allow_nan=False``
  so a non-finite value can never produce non-standard JSON.
* The received-row count is the ``len()`` of the provider's returned list, so a
  provider that over-returns beyond the effective count is reported truthfully
  and local truncation is marked explicitly.
* The final serialized envelope is bounded by an aggregate byte cap measured
  AFTER all status / state / error fields are present.
* Distinct, truthful states are preserved: ``complete``, ``empty``,
  ``partial``, ``truncated``, ``malformed``, ``unavailable``, ``denied``. When a
  single ``state`` cannot express two overlapping truths at once (e.g. aggregate
  trimming while already partial), code-owned booleans carry the rest so nothing
  incomplete is ever reported complete.
* An authoritative ``guardrailAction`` of ``INTERVENED`` produces a code-owned
  denied outcome with no provider text.
* Only code-owned, bounded, sanitized error codes / messages are returned.
  Provider exception text and provider-authored bodies never appear in the
  envelope or the logs.
"""

from __future__ import annotations

# Standard library
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Local modules
from utils.logger import logger


# ---------------------------------------------------------------------------
# Validated limits (min / default / hard max) and output bounds.
# All are read once at import from GBAW_ env vars with defensive coercion so a
# malformed deployment value cannot crash the runtime or disable a bound.
# ---------------------------------------------------------------------------
def _bounded_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded int from the environment.

    The effective value is ALWAYS hard-bounded, even under inconsistent
    min/max overrides (if ``minimum > maximum`` the maximum wins as the hard
    ceiling). Raw, unbounded environment values are NEVER logged — only the
    variable name and the code-owned default are surfaced.
    """
    lo, hi = (minimum, maximum) if minimum <= maximum else (maximum, maximum)

    def clamp(value: int) -> int:
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value

    raw = os.getenv(name)
    if raw is None:
        return clamp(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        # Do NOT echo the raw value; it may carry injected/sensitive text.
        logger.warning(f"Invalid integer for {name}; using code-owned default")
        return clamp(default)
    return clamp(value)


# Retrieval-count contract: never below MIN, defaults to DEFAULT, never above MAX.
KB_RESULTS_MIN: int = _bounded_int("GBAW_KB_RESULTS_MIN", default=1, minimum=1, maximum=50)
KB_RESULTS_MAX: int = _bounded_int("GBAW_KB_RESULTS_MAX", default=10, minimum=KB_RESULTS_MIN, maximum=50)
KB_RESULTS_DEFAULT: int = _bounded_int(
    "GBAW_KB_RESULTS_DEFAULT", default=3, minimum=KB_RESULTS_MIN, maximum=KB_RESULTS_MAX
)

# Query input bound at the provider boundary (characters AND UTF-8 bytes).
KB_QUERY_MAX_CHARS: int = _bounded_int("GBAW_KB_QUERY_MAX_CHARS", default=1024, minimum=1, maximum=8192)
# UTF-8 byte bound so a short multibyte query cannot exceed the provider's byte
# budget. Deterministic, boundary-safe clipping is applied in bound_query().
KB_QUERY_MAX_BYTES: int = _bounded_int("GBAW_KB_QUERY_MAX_BYTES", default=4096, minimum=1, maximum=32768)

# Per-chunk text bounds (deterministic truncation applies whichever is hit first).
KB_CHUNK_MAX_CHARS: int = _bounded_int("GBAW_KB_CHUNK_MAX_CHARS", default=2000, minimum=1, maximum=20000)
KB_CHUNK_MAX_BYTES: int = _bounded_int("GBAW_KB_CHUNK_MAX_BYTES", default=6000, minimum=1, maximum=60000)

# Metadata value bound and count bound.
KB_METADATA_VALUE_MAX_CHARS: int = _bounded_int(
    "GBAW_KB_METADATA_VALUE_MAX_CHARS", default=256, minimum=1, maximum=2048
)

# Aggregate serialized-envelope byte cap, measured AFTER status/state/error
# fields are present.
KB_ENVELOPE_MAX_BYTES: int = _bounded_int("GBAW_KB_ENVELOPE_MAX_BYTES", default=24000, minimum=512, maximum=200000)

# Explicit metadata field allowlist. Only these keys are ever surfaced, and only
# with bounded, field-sanitized values. Deployment-specific / ownership /
# data-source identifiers are intentionally excluded.
_METADATA_ALLOWLIST: frozenset[str] = frozenset(
    {
        "title",
        "heading",
        "section",
        "category",
        "topic",
        "service",
        "doc_type",
    }
)

# Explicit source-location allowlist. Only a coarse, non-sensitive source type is
# surfaced. Concrete URIs, IDs, URLs, and queries (which routinely encode
# bucket names, prefixes, deployment IDs, account-scoped ARNs, SQL, etc.) are
# never surfaced. Types come from RetrievalResultLocationType.
_LOCATION_TYPE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "S3",
        "WEB",
        "CONFLUENCE",
        "SALESFORCE",
        "SHAREPOINT",
        "CUSTOM",
        "KENDRA",
        "SQL",
        "ONEDRIVE",
        "GOOGLEDRIVE",
    }
)


# ---------------------------------------------------------------------------
# Field-level sanitization.
#
# Every model-visible string (chunk text and every projected metadata value)
# passes through ``_sanitize_visible``. It deterministically REDACTS unsafe
# substrings rather than dropping the whole field, so useful bounded prose
# survives while sensitive shapes never cross the boundary.
# ---------------------------------------------------------------------------
_REDACTION = "[REDACTED]"

# Control characters: C0 (minus \t \n \r), DEL, and the full C1 range
# (\x80-\x9f). These must never reach the model boundary or the logs.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Ordered so more specific shapes are redacted before broad ones.
_SENSITIVE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # ARN shapes (any partition/service).
    re.compile(r"arn:[a-z0-9\-]*:[^\s\"']*", re.IGNORECASE),
    # URL / URI schemes (s3://, https://, http://, ftp://, file://, ssh://, etc.).
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s\"']*", re.IGNORECASE),
    # PEM / certificate / credential blocks. Redact the ENTIRE block including
    # the multiline base64 body, not just the BEGIN/END markers, so the
    # certificate / private-key material never survives. DOTALL so the body's
    # newlines are consumed. Ordered before the generic marker fallbacks.
    re.compile(r"-----BEGIN[^-]*-----.*?-----END[^-]*-----", re.IGNORECASE | re.DOTALL),
    # Unterminated BEGIN marker (no matching END): conservatively redact the
    # marker AND the following bounded body — runs of base64-ish body tokens and
    # their line breaks — so a dangling secret body is not surfaced. Bounded to
    # 4096 chars so a pathological input cannot cause runaway matching.
    re.compile(r"-----BEGIN[^-]*-----[\sA-Za-z0-9+/=]{0,4096}", re.IGNORECASE),
    # Any remaining stray BEGIN/END marker (defensive fallback).
    re.compile(r"-----(?:BEGIN|END)[^-]*-----", re.IGNORECASE),
    # Bearer / Basic auth scheme followed by its token. Placed before the
    # generic credential-marker rule so "authorization: Bearer TOKEN" fully
    # consumes the token rather than leaving a trailing fragment.
    re.compile(r"\b(?:bearer|basic)\b\s+\S+", re.IGNORECASE),
    # Credential assignments require an explicit ':' or '=' delimiter. This
    # avoids corrupting ordinary documentation phrases such as "change your
    # password regularly" or "configure the authorization policy" while still
    # redacting actual assigned values. Bearer/Basic tokens are handled by the
    # preceding scheme-specific rule.
    re.compile(
        r"\b(?:aws_secret_access_key|aws_access_key_id|secret(?:[_ -]?key)?|access(?:[_ -]?key)?|"
        r"api(?:[_ -]?key)|password|passwd|private(?:[_ -]?key)|authorization)\b"
        r"\s*[:=]\s*[^\s\"']+",
        re.IGNORECASE,
    ),
    # AWS access-key id shape (AKIA/ASIA + upper alnum).
    re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{12,}\b"),
    # IPv6 address shapes (including compressed :: forms).
    re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b"),
    re.compile(r"(?<![:\w])::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}\b"),
    # Four-component dotted numeric runs (canonical, padded, or noncanonical
    # >255 components), optional :port. Deliberately broader than a strict IPv4
    # so a padded/noncanonical run like 010.001.000.005 cannot slip through.
    re.compile(r"\b\d{1,4}\.\d{1,4}\.\d{1,4}\.\d{1,4}(?::\d{1,5})?\b"),
    # host:port shapes (a dotted DNS-ish host followed by :port).
    re.compile(r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}:\d{1,5}\b"),
    # Any 12-digit substring (AWS account-id shape), even inside a longer digit
    # run. No digit-boundary guard: a 12-run embedded in a bigger number is
    # exactly what we must not surface.
    re.compile(r"\d{12}"),
)


def _sanitize_visible(text: str, *, max_chars: int) -> Tuple[str, bool]:
    """Redact unsafe substrings from a model-visible string, then bound length.

    Deterministic: control characters are stripped, each sensitive shape is
    replaced with a fixed marker, and the result is truncated to ``max_chars``.
    Order (redact then truncate) guarantees a partial sensitive token can never
    survive a length cut.

    Returns ``(cleaned, changed)`` where ``changed`` is True when redaction or
    control-character stripping actually altered the value (length-only
    truncation is reported separately by the caller). The raw hostile input is
    never logged or returned.
    """
    if not text:
        return "", False
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    for pattern in _SENSITIVE_PATTERNS:
        cleaned = pattern.sub(_REDACTION, cleaned)
    changed = cleaned != text
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned, changed


# ---------------------------------------------------------------------------
# Code-owned, bounded error codes. Provider exception text NEVER appears.
# ---------------------------------------------------------------------------
class KBErrorCode:
    DENIED = "KB_ACCESS_DENIED"
    UNAVAILABLE = "KB_UNAVAILABLE"
    MALFORMED = "KB_MALFORMED_RESPONSE"
    INTERVENED = "KB_GUARDRAIL_INTERVENED"


_ERROR_MESSAGES: Dict[str, str] = {
    KBErrorCode.DENIED: "Knowledge base access was denied.",
    KBErrorCode.UNAVAILABLE: "The knowledge base is temporarily unavailable.",
    KBErrorCode.MALFORMED: "The knowledge base returned an unreadable response.",
    KBErrorCode.INTERVENED: "The response was blocked by a content guardrail.",
}

# Provider error-code fragments that map to a "denied" (vs generic unavailable)
# state. Matched against the boto ClientError error code only — never the
# free-form provider message.
_DENIED_ERROR_CODES: frozenset[str] = frozenset(
    {
        "AccessDeniedException",
        "AccessDenied",
        "UnauthorizedException",
        "ForbiddenException",
    }
)


class KBProjectionState:
    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    TRUNCATED = "truncated"
    MALFORMED = "malformed"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"


# States that represent a safe, cacheable RESULT envelope. Error / transient /
# authorization / malformed / guardrail states are NEVER cached (F9), so a
# transient or authorization condition cannot become stale for the cache TTL.
_CACHEABLE_STATES: frozenset[str] = frozenset(
    {
        KBProjectionState.COMPLETE,
        KBProjectionState.EMPTY,
        KBProjectionState.PARTIAL,
        KBProjectionState.TRUNCATED,
    }
)


def is_cacheable_envelope(serialized: str) -> bool:
    """Return True only for safe result envelopes (never error/denied/malformed).

    Used by the caching layer so an unavailable / denied / malformed / guardrail
    outcome is never stored and re-served for the cache TTL.
    """
    try:
        envelope = json.loads(serialized)
    except (ValueError, TypeError):
        return False
    if not isinstance(envelope, dict):
        return False
    if envelope.get("errorCode"):
        return False
    return envelope.get("state") in _CACHEABLE_STATES


def get_projection_limits() -> Dict[str, int]:
    """Return the effective validated limits (for tests / diagnostics)."""
    return {
        "results_min": KB_RESULTS_MIN,
        "results_default": KB_RESULTS_DEFAULT,
        "max_results": KB_RESULTS_MAX,
        "query_max_chars": KB_QUERY_MAX_CHARS,
        "query_max_bytes": KB_QUERY_MAX_BYTES,
        "chunk_max_chars": KB_CHUNK_MAX_CHARS,
        "chunk_max_bytes": KB_CHUNK_MAX_BYTES,
        "metadata_value_max_chars": KB_METADATA_VALUE_MAX_CHARS,
        "max_envelope_bytes": KB_ENVELOPE_MAX_BYTES,
    }


def _finite_number(value: Any) -> Optional[float]:
    """Return a finite float for a real numeric value, else None.

    ``bool`` is explicitly NOT numeric here. NaN / +Inf / -Inf are rejected.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def validate_number_of_results(requested: Any) -> int:
    """Clamp a requested count into [MIN, MAX], defaulting on invalid input."""
    if isinstance(requested, bool):
        return KB_RESULTS_DEFAULT
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return KB_RESULTS_DEFAULT
    if value < KB_RESULTS_MIN:
        return KB_RESULTS_MIN
    if value > KB_RESULTS_MAX:
        return KB_RESULTS_MAX
    return value


def bound_query(text: Any) -> str:
    """Bound the query string at the provider boundary (deterministic).

    Applies, in order: coercion to str, control-char stripping, a character
    bound, then a code-owned UTF-8 BYTE bound with boundary-safe clipping (so a
    short-but-multibyte query cannot exceed the provider's byte budget and no
    partial code point survives), then a final strip.
    """
    query: str = text if isinstance(text, str) else ("" if text is None else str(text))
    # Strip null bytes / control chars that could confuse downstream logging.
    query = _CONTROL_CHARS_RE.sub("", query)
    if len(query) > KB_QUERY_MAX_CHARS:
        query = query[:KB_QUERY_MAX_CHARS]
    encoded = query.encode("utf-8")
    if len(encoded) > KB_QUERY_MAX_BYTES:
        # Clip on a UTF-8 boundary deterministically (errors="ignore" drops any
        # trailing partial code point).
        query = encoded[:KB_QUERY_MAX_BYTES].decode("utf-8", errors="ignore")
    return query.strip()


@dataclass(frozen=True)
class EffectiveRequest:
    """The full effective, validated request — also the cache identity."""

    kb_id: str
    region: str
    query: str
    number_of_results: int
    min_score: float

    def cache_key_material(self) -> str:
        return f"{self.kb_id}\x1f{self.region}\x1f{self.query}\x1f{self.number_of_results}\x1f{self.min_score}"


def _truncate_text(text: str) -> Tuple[str, bool]:
    """Deterministically truncate by chars then bytes. Returns (text, truncated)."""
    truncated = False
    if len(text) > KB_CHUNK_MAX_CHARS:
        text = text[:KB_CHUNK_MAX_CHARS]
        truncated = True
    encoded = text.encode("utf-8")
    if len(encoded) > KB_CHUNK_MAX_BYTES:
        # Cut on a UTF-8 boundary deterministically.
        clipped = encoded[:KB_CHUNK_MAX_BYTES]
        text = clipped.decode("utf-8", errors="ignore")
        truncated = True
    return text, truncated


def _coarse_source_type(location: Any) -> Optional[str]:
    """Return only a coarse, allowlisted source type — never a URI/id/query.

    Source location must stay coarse (F8): no basename label, documentId, URL,
    query, or bucket/prefix is ever surfaced.
    """
    if not isinstance(location, dict):
        return None
    loc_type = location.get("type")
    if isinstance(loc_type, str) and loc_type in _LOCATION_TYPE_ALLOWLIST:
        return loc_type
    return None


def _project_metadata(metadata: Any) -> Tuple[Dict[str, str], bool]:
    """Project metadata through the explicit allowlist with sanitized values.

    Allowlisting a key is necessary but NOT sufficient: the value is
    field-sanitized (control chars, ARNs, URLs, account ids, credential markers,
    network coordinates redacted) and bounded. ``bool`` values are rendered
    as-is (json true/false semantics) but never treated as sensitive text.

    Returns ``(projected, redacted)`` where ``redacted`` is True when any
    surfaced value had unsafe content stripped/redacted.
    """
    if not isinstance(metadata, dict):
        return {}, False
    projected: Dict[str, str] = {}
    redacted = False
    for key in sorted(metadata.keys()):
        if key not in _METADATA_ALLOWLIST:
            continue
        value = metadata[key]
        if isinstance(value, bool):
            projected[key] = "true" if value else "false"
            continue
        if isinstance(value, (int, float)):
            number = _finite_number(value)
            if number is None:
                continue
            safe, changed = _sanitize_visible(str(value), max_chars=KB_METADATA_VALUE_MAX_CHARS)
            projected[key] = safe
            redacted = redacted or changed
            continue
        if not isinstance(value, str):
            continue
        safe, changed = _sanitize_visible(value, max_chars=KB_METADATA_VALUE_MAX_CHARS)
        projected[key] = safe
        redacted = redacted or changed
    return projected, redacted


def _project_one_result(result: Any) -> Optional[Dict[str, Any]]:
    """Project a single raw retrieval result. Returns None if unusable.

    Signals its own quality on the returned dict:
    * ``textTruncated`` — text was length-bounded.
    * ``textRedacted`` — unsafe substrings / control chars were removed.
    * ``metadataRedacted`` — an allowlisted metadata value was redacted.
    * ``scoreInvalid`` — a score was PRESENT but non-finite / wrong-type.

    ``scoreInvalid`` lets the caller mark the outcome non-complete rather than
    silently treating a malformed row as complete (F6). An ABSENT score is
    optional per the service model and is not an error.
    """
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, dict):
        return None
    # content.type is OPTIONAL in the installed service model: accept an ABSENT
    # type, but if a type is PRESENT it must be TEXT. A present non-TEXT type
    # (IMAGE / ROW / byteContent / etc.) carrying a ``text`` member must not be
    # surfaced as chunk text just because a text field happens to exist (F4).
    content_type = content.get("type")
    if content_type is not None and content_type != "TEXT":
        return None
    text = content.get("text")
    if not isinstance(text, str) or not text:
        # Non-text content (byteContent / audio / video / row) is not surfaced
        # as chunk text; treat as unusable for this text-retrieval path.
        return None
    bounded_text, truncated = _truncate_text(text)
    # Field-sanitize AFTER truncation-length bounding, then re-bound by chars so
    # redaction can never let the visible text exceed the char cap.
    safe_text, text_redacted = _sanitize_visible(bounded_text, max_chars=KB_CHUNK_MAX_CHARS)

    source_type = _coarse_source_type(result.get("location", {}))

    # Score is OPTIONAL. Distinguish absent (fine) from present-but-invalid.
    raw_score = result.get("score")
    score_present = raw_score is not None
    score_val = _finite_number(raw_score)
    score_invalid = score_present and score_val is None
    if score_val is not None:
        score_val = round(score_val, 4)

    projected_metadata, metadata_redacted = _project_metadata(result.get("metadata"))

    projected: Dict[str, Any] = {
        "text": safe_text,
        "textTruncated": truncated,
        "textRedacted": text_redacted,
        "metadataRedacted": metadata_redacted,
        "scoreInvalid": score_invalid,
        "sourceType": source_type,
        "score": score_val,
        "metadata": projected_metadata,
    }
    return projected


def _dump(envelope: Dict[str, Any]) -> str:
    """Deterministic, non-NaN JSON dump (F3: allow_nan=False)."""
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _serialize_bounded(envelope: Dict[str, Any]) -> str:
    """Serialize deterministically and enforce the aggregate byte cap.

    The status / state / error / counts fields are always preserved. Only the
    ``results`` list is trimmed (deterministically, from the end) to fit the
    aggregate cap. Aggregate trimming is recorded on a code-owned
    ``aggregateTruncated`` boolean so it cannot be masked by an already-partial
    state (F5), and ``truncated`` is used only when the state was otherwise
    ``complete``.
    """
    serialized = _dump(envelope)
    if len(serialized.encode("utf-8")) <= KB_ENVELOPE_MAX_BYTES:
        return serialized

    results = list(envelope.get("results", []))
    trimmed_any = False
    while results and len(_dump({**envelope, "results": results}).encode("utf-8")) > KB_ENVELOPE_MAX_BYTES:
        results.pop()
        trimmed_any = True

    envelope = {**envelope, "results": results}
    if trimmed_any:
        # Independent signal: aggregate trimming happened regardless of state.
        envelope["aggregateTruncated"] = True
        if envelope.get("state") == KBProjectionState.COMPLETE:
            envelope["state"] = KBProjectionState.TRUNCATED
    envelope["returnedCount"] = len(results)

    serialized = _dump(envelope)
    if len(serialized.encode("utf-8")) <= KB_ENVELOPE_MAX_BYTES:
        return serialized

    # Even an empty results list overflows (pathological); return a minimal,
    # code-owned envelope rather than anything unbounded.
    minimal = {
        "schemaVersion": envelope.get("schemaVersion"),
        "state": KBProjectionState.TRUNCATED,
        "results": [],
        "returnedCount": 0,
        "resultCount": envelope.get("resultCount", 0),
        "aggregateTruncated": True,
    }
    return _dump(minimal)


SCHEMA_VERSION = "kb-projection-v1"


def project_error(error_code: str) -> str:
    """Build a bounded, code-owned error envelope. No provider text ever.

    Any code that is not a member of the finite code-owned enum is normalized
    to ``UNAVAILABLE`` (F5) so an unknown / caller-supplied / hostile code is
    never echoed into the serialized envelope.
    """
    if error_code not in _ERROR_MESSAGES:
        error_code = KBErrorCode.UNAVAILABLE
    if error_code == KBErrorCode.DENIED:
        state = KBProjectionState.DENIED
    elif error_code == KBErrorCode.MALFORMED:
        state = KBProjectionState.MALFORMED
    elif error_code == KBErrorCode.INTERVENED:
        state = KBProjectionState.DENIED
    else:
        state = KBProjectionState.UNAVAILABLE
    envelope: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "state": state,
        "errorCode": error_code,
        "message": _ERROR_MESSAGES.get(error_code, _ERROR_MESSAGES[KBErrorCode.UNAVAILABLE]),
        "results": [],
        "returnedCount": 0,
        "resultCount": 0,
    }
    if error_code == KBErrorCode.INTERVENED:
        # Code-owned intervened marker distinct from an access denial.
        envelope["guardrailIntervened"] = True
    return _serialize_bounded(envelope)


def _structured_error_code(exc: BaseException) -> str:
    """Extract the boto ClientError code DEFENSIVELY; never raises.

    ``response`` may not be a dict, and ``response['Error']`` may not be a
    mapping (a malformed / hostile provider or exception shape). Any unexpected
    shape yields ``""``. The extracted code is used ONLY for classification —
    never logged raw — because a malformed ``Error.Code`` could carry an ARN,
    account id, control text, or an unbounded string.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def classify_provider_error(exc: BaseException) -> str:
    """Map a provider exception to a code-owned error code WITHOUT its text.

    Only the structured boto error code is inspected — never ``str(exc)`` — and
    it is extracted defensively so a non-mapping ``Error`` value (string / list /
    number) can never raise inside a catch path.
    """
    error_code = _structured_error_code(exc)
    if error_code in _DENIED_ERROR_CODES:
        return KBErrorCode.DENIED
    return KBErrorCode.UNAVAILABLE


def project_retrieve_response(raw: Any, effective: EffectiveRequest) -> str:
    """Project a raw Bedrock Agent Runtime Retrieve response into a safe envelope.

    ``raw`` is the dict returned by ``bedrock-agent-runtime.retrieve``.
    """
    if not isinstance(raw, dict):
        logger.warning("KB projection: provider returned a non-dict response; reporting malformed")
        return project_error(KBErrorCode.MALFORMED)

    # Authoritative guardrail action (F6). INTERVENED is a denied/blocked outcome
    # and must never surface as ordinary content or as empty/complete. Any
    # non-string / unexpected action value is treated as malformed rather than
    # silently ignored.
    guardrail_action = raw.get("guardrailAction")
    if guardrail_action is not None:
        if not isinstance(guardrail_action, str):
            logger.warning("KB projection: non-string guardrailAction; reporting malformed")
            return project_error(KBErrorCode.MALFORMED)
        if guardrail_action == "INTERVENED":
            logger.warning("KB projection: guardrail INTERVENED; returning denied outcome")
            return project_error(KBErrorCode.INTERVENED)
        if guardrail_action != "NONE":
            # Unknown/unexpected action value — do not assume it is benign.
            logger.warning("KB projection: unexpected guardrailAction; reporting malformed")
            return project_error(KBErrorCode.MALFORMED)

    raw_results = raw.get("retrievalResults")
    if raw_results is None:
        # Response object with no results key at all is not a valid present
        # empty collection — treat as malformed rather than complete/empty.
        logger.warning("KB projection: provider response missing retrievalResults; reporting malformed")
        return project_error(KBErrorCode.MALFORMED)
    if not isinstance(raw_results, list):
        logger.warning("KB projection: retrievalResults is not a list; reporting malformed")
        return project_error(KBErrorCode.MALFORMED)

    # nextToken must be a (non-empty) string when present. A present-but-wrong
    # shape (e.g. a dict/number) is a malformed continuation signal, not "no
    # more results" (F6).
    next_token = raw.get("nextToken")
    if next_token is not None and not isinstance(next_token, str):
        logger.warning("KB projection: malformed nextToken shape; reporting malformed")
        return project_error(KBErrorCode.MALFORMED)
    has_continuation = isinstance(next_token, str) and bool(next_token)

    # The received-row count is KNOWN: it is len() of the returned list. This is
    # not a provider fan-out — we already hold the list — but it lets us report
    # a provider that over-returns beyond the effective count truthfully (F4).
    received_count = len(raw_results)
    effective_cap = effective.number_of_results

    projected: List[Dict[str, Any]] = []
    dropped_unusable = 0
    any_text_truncated = False
    any_text_redacted = False
    any_metadata_redacted = False
    any_score_invalid = False
    local_truncated = False  # We stopped before consuming all received rows.

    for row in raw_results:
        if len(projected) >= effective_cap:
            # More rows were received than we will return: local (client-side)
            # truncation. Record it deterministically without scanning further.
            local_truncated = True
            break
        score = row.get("score") if isinstance(row, dict) else None
        score_num = _finite_number(score)
        if score_num is not None and score_num < effective.min_score:
            continue
        item = _project_one_result(row)
        if item is None:
            dropped_unusable += 1
            continue
        if item.get("textTruncated"):
            any_text_truncated = True
        if item.get("textRedacted"):
            any_text_redacted = True
        if item.get("metadataRedacted"):
            any_metadata_redacted = True
        if item.get("scoreInvalid"):
            any_score_invalid = True
        projected.append(item)

    # ------------------------------------------------------------------
    # Truthful state + independent code-owned signals (F5).
    # One `state` cannot express every overlapping truth, so continuation,
    # dropped rows, per-chunk truncation, local truncation, and aggregate
    # truncation each get an explicit boolean. `state` is the primary label;
    # the booleans guarantee nothing incomplete is reported as complete.
    # ------------------------------------------------------------------
    if not projected and received_count == 0 and not has_continuation:
        state = KBProjectionState.EMPTY
    elif has_continuation:
        state = KBProjectionState.PARTIAL
    elif not projected and received_count > 0:
        # Everything present was filtered/dropped: incomplete, not present-empty.
        state = KBProjectionState.PARTIAL
    elif any_score_invalid:
        # A present-but-invalid provider score is a malformed row shape (F6):
        # the row is surfaced but the outcome is not "complete".
        state = KBProjectionState.PARTIAL
    elif dropped_unusable > 0 or any_text_truncated or local_truncated or any_text_redacted or any_metadata_redacted:
        # Redaction alters visible content, so the outcome cannot be "complete"
        # (F3). It is not a length truncation, but "truncated" is the nearest
        # non-complete result state; the independent booleans below carry the
        # precise reason.
        state = KBProjectionState.TRUNCATED
    else:
        state = KBProjectionState.COMPLETE

    envelope: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "state": state,
        "results": projected,
        "returnedCount": len(projected),
        # resultCount reflects the KNOWN received count (len of provider list),
        # so an over-returning provider is never reported as fewer/complete.
        "resultCount": received_count,
        "hasMore": bool(has_continuation),
        "droppedUnusable": dropped_unusable,
        "textTruncated": bool(any_text_truncated),
        "textRedacted": bool(any_text_redacted),
        "metadataRedacted": bool(any_metadata_redacted),
        "scoreInvalid": bool(any_score_invalid),
        "localTruncated": bool(local_truncated),
    }
    return _serialize_bounded(envelope)
