"""EKS-owned safety boundary for the EKS specialist's MCP tool output.

The EKS specialist consumes two AWS Labs MCP servers directly as Strands tools:

  * ``aws-api-mcp-server`` — generic AWS CLI dispatch. Only ``call_aws`` is kept,
    constrained to a reviewed read-only ``aws eks`` discovery grammar.
    ``suggest_aws_commands`` (external generic suggestion endpoint returning
    arbitrary/mutating command text) and any other/experimental tool are dropped.
  * ``eks-mcp-server`` — EKS/Kubernetes handlers. Every tool is registered and
    model-facing even in the server's default read-only mode. We keep only a
    reviewed read subset and drop all mutation, secret/log, IAM-policy,
    CloudFormation, manifest-generation, and generic tools.

Authoritative wire shapes (verified against the installed sources):

  * EKS handlers return ``CallToolResult`` with
    ``content=[TextContent(prose), TextContent(json.dumps(model.model_dump()))]``
    on success and ``content=[TextContent(error_prose)]`` with ``isError=True``
    on failure. They build ``CallToolResult`` explicitly, so FastMCP produces NO
    ``structuredContent``. Strands maps each ``TextContent`` to ``{"text": ...}``.
  * ``call_aws`` returns ``list[CallAWSResponse]``; FastMCP wraps a structured
    list return into ``structuredContent={"result": [ ... ]}`` and also emits
    unstructured JSON text. Each serialized entry is a *flattened* dict:
    ``{"cli_command", <ProgramInterpretationResponse fields: "response"
    (InterpretationResponse with "json"/"error"/"error_code"/"pagination_token"/
    "status_code"), "metadata", "validation_failures", ...>, "error"}``.

At the model-facing tool-result boundary this module:

  1. Enforces an explicit reviewed **operation allowlist** (drops everything
     else so it is neither advertised nor invokable).
  2. Guards each tool's **input** before provider dispatch (CLI grammar for
     ``call_aws``; per-tool input validators for EKS tools).
  3. Applies a **bounded per-tool deadline** around delegate streaming so a hung
     provider yields a typed ``unavailable`` outcome.
  4. Projects allowed output onto EKS-owned per-field grammars with finite
     numeric bounds, item/string/nesting caps, deterministic truncation,
     sanitized free-form text, and a versioned code-owned envelope.
  5. Replaces provider-authored error bodies with sanitized typed outcomes;
     provider access-denied stays distinguishable from availability failures.

This is deliberately EKS-owned and self-contained; it is not a generic
cross-domain projection framework. It preserves the MCP client lifecycle,
routing, fallback semantics, IAM, and Kubernetes RBAC — it only narrows and
bounds what the model can invoke and see.
"""

from __future__ import annotations

# Standard library
import asyncio
import json
import math
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence, cast

# Third-party packages
from strands.tools.tool_provider import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolResult, ToolSpec, ToolUse

# Local modules
from utils.logger import logger

_AWS_API_MCP_SERVER = "aws-api-mcp-server"
_EKS_MCP_SERVER = "eks-mcp-server"

# Envelope schema version (code-owned, EKS-scoped).
ENVELOPE_VERSION = "eks-guard-1"

# --------------------------------------------------------------------------- #
# Deterministic bounds                                                        #
# --------------------------------------------------------------------------- #
MAX_ITEMS = 100  # per-collection item cap
MAX_STRING = 512  # per-string char cap (post-sanitize)
MAX_TEXT = 1024  # free-form text (event/guidance/insight) char cap
MAX_LIST_VALUES = 20  # nested string-list cap (api versions, dimensions, ...)
MAX_LABELS = 10  # per-item reviewed-label cap
MAX_ENVELOPE_BYTES = 20_000  # final serialized-envelope UTF-8 byte cap
MAX_BATCH = 5  # hard batch / fan-out cap (well below the server's 20)
MAX_RESULTS_CAP = 100  # bounded effective max_results
MAX_RESULTS_MIN = 1  # reviewed minimum for a supplied max_results
MAX_RESULTS_DEFAULT = 50  # code-owned default sent when the model omits it
DEADLINE_SECONDS = 25.0  # bounded per-tool delegate deadline

# Numeric guard: reject non-finite / absurd integers before they reach a model.
_MAX_ABS_NUMBER = 1e15

# --------------------------------------------------------------------------- #
# Fixed typed outcomes / messages. The message text is guard-chosen; provider #
# bodies never appear here.                                                    #
# --------------------------------------------------------------------------- #
OUTCOME_COMPLETE = "complete"
OUTCOME_EMPTY = "empty"
OUTCOME_DENIED = "denied"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_MALFORMED = "malformed"
OUTCOME_PARTIAL = "partial"
OUTCOME_TRUNCATED = "truncated"

_MSG_DENIED = "This EKS/Kubernetes operation is not available through this assistant."
_MSG_DENIED_CLI = (
    "This AWS command is not available. This assistant only runs a reviewed set of "
    "read-only `aws eks` discovery commands with a fixed argument grammar."
)
_MSG_DENIED_INPUT = "This request was rejected because an argument is not permitted or out of bounds."
_MSG_UNAVAILABLE = "The EKS/Kubernetes request could not be completed. Try a narrower request."
_MSG_MALFORMED = "The EKS/Kubernetes response could not be interpreted safely."

# Fixed typed codes for allowlisted operation logging (no provider text ever).
_CODE_DENIED = "denied"
_CODE_DENIED_INPUT = "denied_input"
_CODE_UNAVAILABLE = "unavailable"
_CODE_MALFORMED = "malformed"

# Operation names permitted to appear in logs. Set after the allowlist is
# defined (see below). Nothing outside this set is logged, and no
# provider-derived text is ever logged.
_LOGGABLE_OPS: frozenset[str] = frozenset()


def _log_outcome(operation: str, code: str) -> None:
    """Log ONLY an allowlisted operation name and a fixed typed code.

    Any operation not on the allowlist is logged as "other" so a provider- or
    model-influenced name can never reach the log line.
    """
    op = operation if operation in _LOGGABLE_OPS else "other"
    logger.info(f"eks_guard op={op} code={code}")


# --------------------------------------------------------------------------- #
# Sanitization of free-form text                                              #
# --------------------------------------------------------------------------- #
# Control characters: C0 (minus tab/newline/CR, which are stripped separately as
# control but harmless as whitespace) AND C1 (0x80-0x9f). C1 controls were
# previously omitted and could ride through free text / scalars.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ARN = re.compile(r"arn:aws[a-z-]*:[^\s\"']*", re.IGNORECASE)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"']*", re.IGNORECASE)
# Any run of 12+ consecutive digits is account-like and redacted, even when
# embedded in a longer alphanumeric token (a word boundary would miss those).
_TWELVE_DIGITS = re.compile(r"\d{12,}")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")
# Dotted-numeric coordinate: any dotted quad of 1+ digit groups. This
# deliberately catches zero-padded (010.001.002.003) AND 4+-digit components
# (1000.1.2.3) that evade the strict 1-3 digit IPv4 grammar.
_PADDED_DOTTED = re.compile(r"\d+(?:\.\d+){3}")
# IPv6 (full or compressed) — 2+ hextet groups separated by colons.
_IPV6 = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}\b")
# host:port network coordinate (dns-ish host followed by :port).
_HOST_PORT = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}:\d{1,5}\b")
# Credential material. Beyond complete PEM blocks and well-known key/JWT shapes,
# also redact:
#   * an UNTERMINATED PEM marker plus the body that follows it (a body with no
#     matching END line would otherwise leak),
#   * Authorization Bearer/Basic tokens,
#   * password/secret/token/apikey-style assignments (`=` or `:` separated).
_CERT_CRED = re.compile(
    r"-----BEGIN[^-]*-----.*?-----END[^-]*-----"  # complete PEM block
    r"|-----BEGIN[^-]*-----[\s\S]*"  # unterminated PEM marker + trailing body
    r"|(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{8,}"
    r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
    r"|(?i:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"  # Authorization scheme + token
    r"|(?i:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"
    r"\s*[:=]\s*[^\s\"']{3,}",  # credential assignment
    re.DOTALL,
)
_REDACTED = "[redacted]"


def _sanitize_text_flagged(value: Any, limit: int = MAX_TEXT) -> tuple[str | None, bool, bool]:
    """Sanitize provider text and report length truncation and redaction.

    Returns ``(text, truncated, redacted)``. ``redacted`` is true whenever
    unsafe content or control characters were removed, so a projected response
    cannot look complete after its meaning changed at this boundary.
    """
    if value is None or not isinstance(value, str):
        return None, False, False
    text = value
    text = _CERT_CRED.sub(_REDACTED, text)
    text = _ARN.sub(_REDACTED, text)
    text = _URL.sub(_REDACTED, text)
    text = _HOST_PORT.sub(_REDACTED, text)
    text = _IPV6.sub(_REDACTED, text)
    text = _IPV4.sub(_REDACTED, text)
    text = _PADDED_DOTTED.sub(_REDACTED, text)
    text = _TWELVE_DIGITS.sub(_REDACTED, text)
    text = _CONTROL_CHARS.sub("", text)
    redacted = text != value
    truncated = len(text) > limit
    return text[:limit], truncated, redacted


def _sanitize_text(value: Any, limit: int = MAX_TEXT) -> str | None:
    """Return sanitized, bounded free text or ``None`` for non-strings."""
    text, _truncated, _redacted = _sanitize_text_flagged(value, limit)
    return text


# A scalar identifier/timestamp field must not carry a sensitive shape. Rather
# than silently redact (which would corrupt an identifier), a scalar carrying a
# sensitive shape is refused entirely (returns None) so it is dropped from the
# projection.
_SENSITIVE_SCALAR_SHAPES = (
    _ARN,
    _URL,
    _HOST_PORT,
    _IPV6,
    _IPV4,
    _PADDED_DOTTED,
    _TWELVE_DIGITS,
    _CERT_CRED,
)


# --------------------------------------------------------------------------- #
# Bounded scalar helpers with per-field grammars                              #
# --------------------------------------------------------------------------- #
def _s(value: Any, limit: int = MAX_STRING) -> str | None:
    """Bounded scalar string (identifiers/timestamps), control chars stripped.

    Only genuine strings are accepted — a mapping/list/number/object is refused
    (returns None) so a provider ``str()`` blob never becomes a scalar. A string
    carrying a sensitive shape (ARN, URL, IPv4/IPv6/CIDR, padded dotted-numeric,
    host:port, 12-digit identifier, credential marker) is also refused so those
    coordinates cannot ride through an "allowed" scalar field."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = _CONTROL_CHARS.sub("", value)[:limit]
    for shape in _SENSITIVE_SCALAR_SHAPES:
        if shape.search(text):
            return None
    return text


def _finite_number(value: Any) -> int | float | None:
    """Return the value only if it is a finite, in-range number (not bool)."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if abs(value) > _MAX_ABS_NUMBER:
        return None
    return value


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _MAX_ABS_NUMBER:
        return None
    return value


def _str_list(value: Any, limit: int = MAX_LIST_VALUES) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for v in list(value)[:limit]:
        s = _s(v)
        if s is not None:
            out.append(s)
    return out


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


# A tiny reviewed label-key allowlist (safe, identity/selector-only). Arbitrary
# keys are excluded because they can carry secrets, ARNs, or config blobs.
_REVIEWED_LABEL_KEYS = frozenset(
    {
        "app",
        "app.kubernetes.io/name",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/component",
        "app.kubernetes.io/part-of",
        "app.kubernetes.io/managed-by",
        "k8s-app",
        "tier",
        "role",
        "component",
        "release",
        "environment",
    }
)
# Safe label value grammar: DNS-ish label values only.
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")


def _reviewed_labels(value: Any) -> dict[str, str]:
    """Only reviewed, safe-value labels survive. Everything else is excluded.

    A reviewed label value must pass BOTH the DNS-ish grammar AND sensitive-shape
    rejection, so an account-like 12-digit value, an ARN/URL, or a network
    coordinate that happens to match the grammar is still excluded."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, val in value.items():
        if len(out) >= MAX_LABELS:
            break
        if key not in _REVIEWED_LABEL_KEYS:
            continue
        if not isinstance(val, str) or not _LABEL_VALUE.match(val):
            continue
        if any(shape.search(val) for shape in _SENSITIVE_SCALAR_SHAPES):
            continue
        out[key] = val
    return out


# --------------------------------------------------------------------------- #
# Envelope construction                                                       #
# --------------------------------------------------------------------------- #
# Stable, code-owned error codes keyed by outcome. These are what the model and
# any downstream consumer key on — they never derive from provider text.
_ERROR_CODES: dict[str, str] = {
    OUTCOME_DENIED: "EKS_DENIED",
    OUTCOME_UNAVAILABLE: "EKS_UNAVAILABLE",
    OUTCOME_MALFORMED: "EKS_MALFORMED",
}


def _dumps(body: Mapping[str, Any]) -> str:
    """Deterministic serialization: sorted keys, compact separators, and
    allow_nan=False so a stray NaN/Infinity raises rather than emitting invalid
    JSON tokens."""
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sanitized_error(tool_use_id: str, operation: str, outcome: str, message: str) -> ToolResult:
    """Typed error envelope, single JSON content block, no provider body. Carries
    a stable code-owned errorCode as well as the outcome/message."""
    body = {
        "v": ENVELOPE_VERSION,
        "operation": operation,
        "outcome": outcome,
        "errorCode": _ERROR_CODES.get(outcome, "EKS_ERROR"),
        "message": message,
    }
    text = _dumps(body)
    return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": text}]}


def _trim_bounded_collections(body: dict[str, Any]) -> bool:
    """Deterministically shrink the largest bounded collection in-place.

    Returns True if something was trimmed. Used to fit the final byte cap while
    preserving as much information as possible rather than withholding all.
    Any pre-existing partial/hasMore flags are preserved (never cleared) so a
    trim can only ever ADD truncation signals, never hide an existing one."""
    best_key = None
    best_len = 0
    for key, val in body.items():
        if isinstance(val, list) and len(val) > best_len:
            best_key, best_len = key, len(val)
    if best_key is None or best_len == 0:
        return False
    keep = max(0, best_len // 2)
    body[best_key] = body[best_key][:keep]
    # Truncation supersedes a plain complete/empty, but must not overwrite an
    # existing partial signal's meaning: we set truncated/hasMore additively.
    body["outcome"] = OUTCOME_TRUNCATED
    body["truncated"] = True
    body["hasMore"] = True
    body["returned"] = keep
    return True


def _ok(tool_use_id: str, operation: str, body: dict[str, Any]) -> ToolResult:
    """Build a success result as one model-visible JSON content block, capped by
    final serialized UTF-8 bytes. Trims bounded collections deterministically
    before withholding everything. The cap is exact — the emitted text is always
    <= MAX_ENVELOPE_BYTES with no slack allowance."""
    envelope = {"v": ENVELOPE_VERSION, "operation": operation, **body}
    if "outcome" not in envelope:
        envelope["outcome"] = OUTCOME_COMPLETE

    for _ in range(24):
        text = _dumps(envelope)
        if len(text.encode("utf-8")) <= MAX_ENVELOPE_BYTES:
            return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]}
        if not _trim_bounded_collections(envelope):
            break

    # Could not fit even after trimming: emit an explicit truncated envelope.
    fallback = {
        "v": ENVELOPE_VERSION,
        "operation": operation,
        "outcome": OUTCOME_TRUNCATED,
        "truncated": True,
        "hasMore": True,
        "message": "response exceeded the maximum size and was withheld",
    }
    text = _dumps(fallback)
    return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]}


# --------------------------------------------------------------------------- #
# Result-content extraction (authoritative shapes)                            #
# --------------------------------------------------------------------------- #
def _structured_content(result: Mapping[str, Any]) -> Any:
    sc = result.get("structuredContent")
    return sc if isinstance(sc, Mapping) else None


def _json_text_blocks(result: Mapping[str, Any]) -> list[Any]:
    """Decode every text content block that parses as JSON (in order).

    Real EKS handlers emit [prose_text, json_text]; the prose block does not
    parse as JSON and is skipped. aws-api emits JSON text alongside
    structuredContent."""
    content = result.get("content")
    if not isinstance(content, list):
        return []
    decoded: list[Any] = []
    for item in content:
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            try:
                decoded.append(json.loads(item["text"]))
            except (json.JSONDecodeError, TypeError):
                continue
    return decoded


def _first_text_block(result: Mapping[str, Any]) -> str | None:
    """Return the first text content block as a raw string (JSON or not).

    ``search_eks_troubleshoot_guide`` returns a SINGLE plain TextContent holding
    the raw HTTP response body (``response.text``), which is not necessarily
    JSON. Only this allowlisted guidance-text tool uses this raw-text path."""
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            return cast(str, item["text"])
    return None


def _extract_eks_payload(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """EKS tools: prefer validated structuredContent, else the first JSON text
    block that is a mapping (the model_dump block)."""
    sc = _structured_content(result)
    if sc is not None:
        # FastMCP would wrap under {"result": ...}; EKS tools don't, but be safe.
        inner = sc.get("result") if "result" in sc and isinstance(sc.get("result"), Mapping) else sc
        if isinstance(inner, Mapping):
            return inner
    for decoded in _json_text_blocks(result):
        if isinstance(decoded, Mapping):
            return decoded
    return None


def _extract_call_aws_entries(result: Mapping[str, Any]) -> list[Any] | None:
    """aws-api call_aws: prefer structuredContent['result'] (validated), else a
    JSON text block that is a list or {'result': [...]}."""
    sc = _structured_content(result)
    if sc is not None and isinstance(sc.get("result"), list):
        return list(sc["result"])
    for decoded in _json_text_blocks(result):
        if isinstance(decoded, list):
            return decoded
        if isinstance(decoded, Mapping) and isinstance(decoded.get("result"), list):
            return list(decoded["result"])
    return None


# --------------------------------------------------------------------------- #
# EKS MCP per-operation projections                                           #
# --------------------------------------------------------------------------- #
def _project_list_k8s_resources(data: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_items = data.get("items")
    kind = _s(data.get("kind"))
    api_version = _s(data.get("api_version"))
    count = _nonneg_int(data.get("count"))
    if not isinstance(raw_items, list) or kind is None or api_version is None or count is None:
        return None

    partial = count != len(raw_items)
    projected = []
    for item in raw_items[:MAX_ITEMS]:
        m = _as_mapping(item)
        if m is None:
            partial = True
            continue
        name = _s(m.get("name"))
        if name is None:
            partial = True
            continue
        projected.append(
            {
                "name": name,
                "namespace": _s(m.get("namespace")),
                "creation_timestamp": _s(m.get("creation_timestamp")),
                "labels": _reviewed_labels(m.get("labels")),
                # annotations intentionally dropped entirely
            }
        )

    locally_truncated = len(raw_items) > MAX_ITEMS
    out: dict[str, Any] = {
        "kind": kind,
        "api_version": api_version,
        "namespace": _s(data.get("namespace")),
        "count": count,
        "items": projected,
    }
    if not raw_items and count == 0:
        out["outcome"] = OUTCOME_EMPTY
    elif locally_truncated:
        out.update(
            {
                "outcome": OUTCOME_TRUNCATED,
                "truncated": True,
                "hasMore": True,
                "returned": len(projected),
            }
        )
    elif partial or len(projected) != len(raw_items):
        out.update({"outcome": OUTCOME_PARTIAL, "partial": True})
    else:
        out["outcome"] = OUTCOME_COMPLETE
    if partial and out["outcome"] == OUTCOME_TRUNCATED:
        out["partial"] = True
    return out


def _project_k8s_events(data: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = data.get("events")
    involved_kind = _s(data.get("involved_object_kind"))
    involved_name = _s(data.get("involved_object_name"))
    count = _nonneg_int(data.get("count"))
    if not isinstance(raw, list) or involved_kind is None or involved_name is None or count is None:
        return None

    partial = count != len(raw)
    text_truncated = False
    text_redacted = False
    projected = []
    for ev in raw[:MAX_ITEMS]:
        m = _as_mapping(ev)
        if m is None:
            partial = True
            continue
        message, msg_trunc, msg_redacted = _sanitize_text_flagged(m.get("message"))
        if message is None:
            partial = True
            continue
        ev_out: dict[str, Any] = {
            "reason": _s(m.get("reason")),
            "type": _s(m.get("type")),
            "message": message,
            "count": _nonneg_int(m.get("count")),
            "first_timestamp": _s(m.get("first_timestamp")),
            "last_timestamp": _s(m.get("last_timestamp")),
            "reporting_component": _s(m.get("reporting_component")),
        }
        if msg_trunc:
            ev_out["message_truncated"] = True
            text_truncated = True
        if msg_redacted:
            ev_out["message_redacted"] = True
            text_redacted = True
        projected.append(ev_out)

    locally_truncated = len(raw) > MAX_ITEMS
    out: dict[str, Any] = {
        "involved_object_kind": involved_kind,
        "involved_object_name": involved_name,
        "involved_object_namespace": _s(data.get("involved_object_namespace")),
        "count": count,
        "events": projected,
    }
    if not raw and count == 0:
        out["outcome"] = OUTCOME_EMPTY
    elif locally_truncated:
        out.update(
            {
                "outcome": OUTCOME_TRUNCATED,
                "truncated": True,
                "hasMore": True,
                "returned": len(projected),
            }
        )
    elif partial or len(projected) != len(raw):
        out.update({"outcome": OUTCOME_PARTIAL, "partial": True})
    else:
        out["outcome"] = OUTCOME_COMPLETE

    if text_truncated:
        out["truncated"] = True
        if out["outcome"] == OUTCOME_COMPLETE:
            out["outcome"] = OUTCOME_TRUNCATED
    if text_redacted:
        out["redacted"] = True
        if out["outcome"] == OUTCOME_COMPLETE:
            out.update({"outcome": OUTCOME_PARTIAL, "partial": True})
    if partial and out["outcome"] == OUTCOME_TRUNCATED:
        out["partial"] = True
    return out


def _project_api_versions(data: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = data.get("api_versions")
    cluster_name = _s(data.get("cluster_name"))
    count = _nonneg_int(data.get("count"))
    if not isinstance(raw, list) or cluster_name is None or count is None:
        return None
    versions = _str_list(raw, limit=MAX_ITEMS)
    out: dict[str, Any] = {
        "cluster_name": cluster_name,
        "api_versions": versions,
        "count": count,
    }
    if not raw and count == 0:
        out["outcome"] = OUTCOME_EMPTY
    elif len(raw) > MAX_ITEMS:
        out.update(
            {
                "outcome": OUTCOME_TRUNCATED,
                "truncated": True,
                "hasMore": True,
                "returned": len(versions),
            }
        )
        if count != len(raw) or len(versions) != MAX_ITEMS:
            out["partial"] = True
    elif count != len(raw) or len(versions) != len(raw):
        out.update({"outcome": OUTCOME_PARTIAL, "partial": True})
    else:
        out["outcome"] = OUTCOME_COMPLETE
    return out


def _project_metrics_guidance(data: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = data.get("metrics")
    resource_type = _s(data.get("resource_type"))
    if not isinstance(raw, list) or resource_type is None:
        return None
    partial = False
    text_truncated = False
    text_redacted = False
    projected = []
    for metric in raw[:MAX_ITEMS]:
        m = _as_mapping(metric)
        if m is None:
            partial = True
            continue
        name = _s(m.get("name"))
        dimensions_raw = m.get("dimensions")
        if name is None or not isinstance(dimensions_raw, list):
            partial = True
            continue
        desc, desc_trunc, desc_redacted = _sanitize_text_flagged(m.get("description"))
        text_truncated = text_truncated or desc_trunc
        text_redacted = text_redacted or desc_redacted
        dimensions = _str_list(dimensions_raw)
        if len(dimensions) != min(len(dimensions_raw), MAX_LIST_VALUES):
            partial = True
        projected.append(
            {
                "name": name,
                "description": desc,
                "unit": _s(m.get("unit")),
                "namespace": _s(m.get("namespace")),
                "dimensions": dimensions,
            }
        )

    locally_truncated = len(raw) > MAX_ITEMS
    out: dict[str, Any] = {"resource_type": resource_type, "metrics": projected}
    if not raw:
        out["outcome"] = OUTCOME_EMPTY
    elif locally_truncated:
        out.update(
            {
                "outcome": OUTCOME_TRUNCATED,
                "truncated": True,
                "hasMore": True,
                "returned": len(projected),
            }
        )
    elif partial or len(projected) != len(raw):
        out.update({"outcome": OUTCOME_PARTIAL, "partial": True})
    else:
        out["outcome"] = OUTCOME_COMPLETE
    if text_truncated:
        out["truncated"] = True
        if out["outcome"] == OUTCOME_COMPLETE:
            out["outcome"] = OUTCOME_TRUNCATED
    if text_redacted:
        out["redacted"] = True
        if out["outcome"] == OUTCOME_COMPLETE:
            out.update({"outcome": OUTCOME_PARTIAL, "partial": True})
    if partial and out["outcome"] == OUTCOME_TRUNCATED:
        out["partial"] = True
    return out


def _project_cloudwatch_metrics(data: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = data.get("data_points")
    if not isinstance(raw, list):
        return None
    # CloudWatchMetricsData requires cluster_name/metric_name/namespace/
    # start_time/end_time/data_points. A payload missing a required top-level
    # string is malformed, NOT a zero-point empty.
    for required in ("cluster_name", "metric_name", "namespace", "start_time", "end_time"):
        if not isinstance(data.get(required), str):
            return None
    projected = []
    for point in raw[:MAX_ITEMS]:
        m = _as_mapping(point)
        if m is None:
            continue
        # Each point requires timestamp + value. A point missing either is
        # dropped (which marks the aggregate partial below).
        ts = _s(m.get("timestamp"))
        value = _finite_number(m.get("value"))
        if ts is None or value is None:
            continue  # missing/invalid required member, or NaN/Inf/huge value
        projected.append({"timestamp": ts, "value": value})
    out: dict[str, Any] = {
        "cluster_name": _s(data.get("cluster_name")),
        "metric_name": _s(data.get("metric_name")),
        "namespace": _s(data.get("namespace")),
        "start_time": _s(data.get("start_time")),
        "end_time": _s(data.get("end_time")),
        "data_points": projected,
    }
    returned = len(projected)
    raw_count = len(raw)
    capped = raw_count > MAX_ITEMS
    if raw_count == 0:
        out["outcome"] = OUTCOME_EMPTY
    elif capped and returned == MAX_ITEMS:
        # Pure local item-cap truncation (more points existed than we kept).
        out["outcome"] = OUTCOME_TRUNCATED
        out["truncated"] = True
        out["hasMore"] = True
        out["returned"] = returned
    elif returned < raw_count:
        # Points were dropped for a data-quality reason (missing required member
        # or non-finite value): the returned set is partial, not truncated.
        out["outcome"] = OUTCOME_PARTIAL
        out["partial"] = True
    else:
        out["outcome"] = OUTCOME_COMPLETE
    return out


def _project_eks_insights(data: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = data.get("insights")
    if not isinstance(raw, list):
        return None
    # EksInsightsData requires cluster_name (a string) and insights.
    if not isinstance(data.get("cluster_name"), str):
        return None
    partial = False
    text_truncated = False
    text_redacted = False
    projected = []
    for insight in raw[:MAX_ITEMS]:
        m = _as_mapping(insight)
        if m is None:
            partial = True
            continue
        # Required members per installed EksInsightItem: id, name, category,
        # last_refresh_time, last_transition_time, description, insight_status
        # (with status + reason). A row missing any required member is dropped and
        # marks the result partial (not a silent clean complete).
        ins_id = _s(m.get("id"))
        name = _s(m.get("name"))
        category = _s(m.get("category"))
        last_refresh = _finite_number(m.get("last_refresh_time"))
        last_transition = _finite_number(m.get("last_transition_time"))
        description, desc_trunc, desc_redacted = _sanitize_text_flagged(m.get("description"))
        status_map = _as_mapping(m.get("insight_status"))
        if (
            ins_id is None
            or name is None
            or category is None
            or last_refresh is None
            or last_transition is None
            or description is None
            or status_map is None
        ):
            partial = True
            continue
        status = _s(status_map.get("status"))
        reason, reason_trunc, reason_redacted = _sanitize_text_flagged(status_map.get("reason"))
        # EksInsightStatus requires BOTH status and reason.
        if status is None or reason is None:
            partial = True
            continue
        recommendation, rec_trunc, rec_redacted = _sanitize_text_flagged(m.get("recommendation"))
        if desc_trunc or reason_trunc or rec_trunc:
            text_truncated = True
        if desc_redacted or reason_redacted or rec_redacted:
            text_redacted = True
        projected.append(
            {
                "id": ins_id,
                "name": name,
                "category": category,
                "kubernetes_version": _s(m.get("kubernetes_version")),
                "description": description,
                "recommendation": recommendation,
                "status": status,
                "reason": reason,
                # resources / additional_info / category_specific_summary dropped
            }
        )
    out: dict[str, Any] = {"cluster_name": _s(data.get("cluster_name")), "insights": projected}
    capped = len(raw) > MAX_ITEMS
    if len(raw) == 0:
        out["outcome"] = OUTCOME_EMPTY
    elif capped:
        # Local item-cap truncation is distinct from server-side pagination.
        out["outcome"] = OUTCOME_TRUNCATED
        out["truncated"] = True
        out["hasMore"] = True
        out["returned"] = len(projected)
    else:
        out["outcome"] = OUTCOME_COMPLETE
    # EKS Insights pagination: a next_token means more insights REMAIN on the
    # server side (partial + hasMore), which is distinct from a LOCAL item-cap
    # truncation. The raw token itself never leaks.
    if data.get("next_token"):
        out["hasMore"] = True
        partial = True
    # Invalid/dropped rows mean the returned set is incomplete for a reason other
    # than capping: mark partial. Partial supersedes a plain complete/empty.
    if partial:
        out["partial"] = True
        if out.get("outcome") in (OUTCOME_COMPLETE, OUTCOME_EMPTY):
            out["outcome"] = OUTCOME_PARTIAL
    # A silently-clipped free-text field is surfaced without hiding an existing
    # partial/truncated/hasMore signal.
    if text_truncated:
        out["truncated"] = True
        if out.get("outcome") in (OUTCOME_COMPLETE, OUTCOME_EMPTY):
            out["outcome"] = OUTCOME_TRUNCATED
    if text_redacted:
        out["redacted"] = True
        if out.get("outcome") in (OUTCOME_COMPLETE, OUTCOME_EMPTY):
            out["outcome"] = OUTCOME_PARTIAL
            out["partial"] = True
    return out


def _project_vpc_config(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """VPC config: keep only bounded counts/booleans. VPC id, CIDR blocks,
    routes, and subnet detail are network coordinates and are dropped.

    Required-shape check against the installed ``EksVpcConfigData`` model: the
    required top-level fields are ``vpc_id``, ``cidr_block``, ``routes``, and
    ``cluster_name``. A response missing any of these is malformed (return None),
    NOT a zero-count complete. ``subnets``/``additional_cidr_blocks``/
    ``remote_*_cidr_blocks`` default to lists in the model, but when present must
    be lists — a present-but-wrong type is malformed."""
    if not isinstance(data.get("vpc_id"), str):
        return None
    if not isinstance(data.get("cidr_block"), str):
        return None
    if not isinstance(data.get("cluster_name"), str):
        return None
    routes = data.get("routes")
    if not isinstance(routes, list):
        return None  # required list missing => malformed, not zero
    optional_lists: dict[str, list[Any]] = {}
    for key in (
        "subnets",
        "additional_cidr_blocks",
        "remote_node_cidr_blocks",
        "remote_pod_cidr_blocks",
    ):
        value = data.get(key)
        if value is not None and not isinstance(value, list):
            return None
        optional_lists[key] = value if isinstance(value, list) else []
    cluster_name = _s(data.get("cluster_name"))
    if cluster_name is None:
        return None
    return {
        "cluster_name": cluster_name,
        "subnet_count": len(optional_lists["subnets"]),
        "route_count": len(routes),
        "has_additional_cidr_blocks": bool(optional_lists["additional_cidr_blocks"]),
        "has_remote_node_cidr_blocks": bool(optional_lists["remote_node_cidr_blocks"]),
        "has_remote_pod_cidr_blocks": bool(optional_lists["remote_pod_cidr_blocks"]),
        "outcome": OUTCOME_COMPLETE,
    }


def _project_troubleshoot_guide(text: str) -> dict[str, Any]:
    """search_eks_troubleshoot_guide returns the raw HTTP response body as a
    single plain-text block; sanitize + bound it. This is the ONLY tool parsed
    from raw (possibly non-JSON) text, and only because its installed handler
    emits ``response.text`` verbatim. A length-clipped body surfaces an explicit
    truncated signal rather than clipping silently."""
    guidance, truncated, redacted = _sanitize_text_flagged(text)
    out: dict[str, Any] = {"guidance": guidance}
    if not guidance:
        out["outcome"] = OUTCOME_EMPTY
    elif truncated:
        out["outcome"] = OUTCOME_TRUNCATED
        out["truncated"] = True
    elif redacted:
        out.update({"outcome": OUTCOME_PARTIAL, "partial": True, "redacted": True})
    else:
        out["outcome"] = OUTCOME_COMPLETE
    if redacted:
        out["redacted"] = True
    return out


# EKS MCP read-tool allowlist -> projector (structured-dict payloads). The
# troubleshoot guide is handled separately below because it is a raw-text tool.
_EKS_TOOL_PROJECTORS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any] | None]] = {
    "list_k8s_resources": _project_list_k8s_resources,
    "get_k8s_events": _project_k8s_events,
    "list_api_versions": _project_api_versions,
    "get_eks_metrics_guidance": _project_metrics_guidance,
    "get_cloudwatch_metrics": _project_cloudwatch_metrics,
    "get_eks_insights": _project_eks_insights,
    "get_eks_vpc_config": _project_vpc_config,
}

# Tools whose payload is a single plain-text block rather than a JSON mapping.
_EKS_TEXT_TOOLS = frozenset({"search_eks_troubleshoot_guide"})

# The full allowlist is the union of the structured-dict projectors and the
# raw-text tools.
_EKS_ALLOWED = frozenset(_EKS_TOOL_PROJECTORS) | _EKS_TEXT_TOOLS

# Now that the allowlist exists, fix up the loggable-operation set (EKS read
# tools plus the single guarded aws-api tool).
_LOGGABLE_OPS = _EKS_ALLOWED | frozenset({"call_aws"})


# --------------------------------------------------------------------------- #
# EKS MCP per-tool INPUT validators (reject before provider dispatch)         #
# --------------------------------------------------------------------------- #
_EKS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_NAMESPACE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_API_VERSION = re.compile(r"^[A-Za-z0-9./-]{1,63}$")
_KIND = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_SELECTOR = re.compile(r"^[A-Za-z0-9=,!()._/ -]{0,256}$")
_METRIC_NAME = re.compile(r"^[A-Za-z0-9_]{1,128}$")

# CloudWatch reviewed input grammar (issue #466). namespace is a bounded enum;
# dimensions are limited to reviewed keys with safe identifier values; the
# optional minutes/limit/period are bounded integers and stat is an enum.
_CW_NAMESPACE_ENUM = frozenset({"ContainerInsights", "AWS/EKS", "ContainerInsights/Prometheus"})
_CW_DIMENSION_KEYS = frozenset({"ClusterName", "PodName", "FullPodName", "Namespace", "Service", "NodeName"})
_CW_DIMENSION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,253}$")
_CW_STAT_ENUM = frozenset({"Average", "Sum", "Minimum", "Maximum", "SampleCount", "p50", "p90", "p95", "p99"})
_CW_MINUTES_MIN, _CW_MINUTES_MAX = 1, 20160  # up to 14 days of look-back
_CW_LIMIT_MIN, _CW_LIMIT_MAX = 1, MAX_ITEMS
_CW_PERIOD_MIN, _CW_PERIOD_MAX = 60, 86400  # CloudWatch period seconds
# start/end absolute-time grammar we can safely accept and bound (ISO-8601-ish).
_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _parse_iso_timestamp(value: str) -> datetime | None:
    """Parse a bounded ISO timestamp into an aware UTC datetime."""
    if not _ISO_TIMESTAMP.fullmatch(value):
        return None
    normalized = value.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# search_eks_troubleshoot_guide installed grammar: <= 300 chars, letters,
# numbers, commas, periods, question marks, colons, and spaces only.
_TROUBLESHOOT_MAX_QUERY = 300
_TROUBLESHOOT_QUERY = re.compile(r"^[A-Za-z0-9,.?: ]{1,300}$")

# Kinds that must never be listed/read through this boundary.
_FORBIDDEN_KINDS = frozenset({"secret", "secrets"})
_METRICS_CATEGORIES = frozenset({"cluster", "node", "pod", "namespace", "service"})


def _reject(reason: str = _MSG_DENIED_INPUT) -> tuple[bool, str]:
    return (False, reason)


def _ok_input() -> tuple[bool, str]:
    return (True, "")


def _v_cluster_name(inp: Mapping[str, Any]) -> tuple[bool, str]:
    name = inp.get("cluster_name")
    if not isinstance(name, str) or not _EKS_NAME.match(name):
        return _reject()
    # Reject sensitive/account-like identifiers (e.g. a bare 12+-digit account
    # number, an ARN/URL/IP shape) even when they satisfy the name grammar.
    if any(shape.search(name) for shape in _SENSITIVE_SCALAR_SHAPES):
        return _reject()
    return _ok_input()


def _v_list_k8s_resources(inp: Mapping[str, Any]) -> tuple[bool, str]:
    ok, msg = _v_cluster_name(inp)
    if not ok:
        return ok, msg
    kind = inp.get("kind")
    if not isinstance(kind, str) or not _KIND.match(kind):
        return _reject()
    if kind.lower() in _FORBIDDEN_KINDS:
        return _reject(_MSG_DENIED)
    api_version = inp.get("api_version")
    if not isinstance(api_version, str) or not _API_VERSION.match(api_version):
        return _reject()
    ns = inp.get("namespace")
    if ns is not None and (not isinstance(ns, str) or not _NAMESPACE.match(ns)):
        return _reject()
    for sel_key in ("label_selector", "field_selector"):
        sel = inp.get(sel_key)
        if sel is not None and (not isinstance(sel, str) or not _SELECTOR.match(sel)):
            return _reject()
    return _ok_input()


def _v_k8s_events(inp: Mapping[str, Any]) -> tuple[bool, str]:
    ok, msg = _v_cluster_name(inp)
    if not ok:
        return ok, msg
    kind = inp.get("kind")
    if not isinstance(kind, str) or not _KIND.match(kind):
        return _reject()
    if kind.lower() in _FORBIDDEN_KINDS:
        return _reject(_MSG_DENIED)
    name = inp.get("name")
    if not isinstance(name, str) or not _K8S_NAME.match(name):
        return _reject()
    ns = inp.get("namespace")
    if ns is not None and (not isinstance(ns, str) or not _NAMESPACE.match(ns)):
        return _reject()
    return _ok_input()


def _v_api_versions(inp: Mapping[str, Any]) -> tuple[bool, str]:
    return _v_cluster_name(inp)


def _v_metrics_guidance(inp: Mapping[str, Any]) -> tuple[bool, str]:
    # resource_type is required by MetricsGuidanceData and must be a reviewed
    # category. A missing/blank resource_type is rejected before dispatch.
    category = inp.get("resource_type")
    if not isinstance(category, str) or category not in _METRICS_CATEGORIES:
        return _reject()
    return _ok_input()


def _v_cloudwatch_metrics(inp: Mapping[str, Any]) -> tuple[bool, str]:
    ok, msg = _v_cluster_name(inp)
    if not ok:
        return ok, msg
    metric = inp.get("metric_name")
    if not isinstance(metric, str) or not _METRIC_NAME.match(metric):
        return _reject()
    # namespace is REQUIRED and must be a reviewed enum value.
    ns = inp.get("namespace")
    if not isinstance(ns, str) or ns not in _CW_NAMESPACE_ENUM:
        return _reject()
    start_raw = inp.get("start_time")
    end_raw = inp.get("end_time")
    if (start_raw is None) != (end_raw is None):
        return _reject()
    if start_raw is not None and end_raw is not None:
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            return _reject()
        start_time = _parse_iso_timestamp(start_raw)
        end_time = _parse_iso_timestamp(end_raw)
        if start_time is None or end_time is None or start_time > end_time:
            return _reject()
        if (end_time - start_time).total_seconds() > _CW_MINUTES_MAX * 60:
            return _reject()
    # Bound + validate metric dimensions: reviewed keys, safe identifier values.
    dims = inp.get("dimensions")
    if not isinstance(dims, Mapping) or not dims or len(dims) > MAX_LIST_VALUES:
        return _reject()
    for k, v in dims.items():
        if not isinstance(k, str) or k not in _CW_DIMENSION_KEYS:
            return _reject()
        if not isinstance(v, str) or not _CW_DIMENSION_VALUE.match(v):
            return _reject()
        if any(shape.search(v) for shape in _SENSITIVE_SCALAR_SHAPES):
            return _reject()
    # Optional bounded numeric knobs with hard bounds.
    for key, lo, hi in (
        ("minutes", _CW_MINUTES_MIN, _CW_MINUTES_MAX),
        ("limit", _CW_LIMIT_MIN, _CW_LIMIT_MAX),
        ("period", _CW_PERIOD_MIN, _CW_PERIOD_MAX),
    ):
        val = inp.get(key)
        if val is not None:
            if isinstance(val, bool) or not isinstance(val, int):
                return _reject()
            if val < lo or val > hi:
                return _reject()
    stat = inp.get("stat")
    if stat is not None and (not isinstance(stat, str) or stat not in _CW_STAT_ENUM):
        return _reject()
    return _ok_input()


def _canonical_cloudwatch_input(inp: Mapping[str, Any]) -> dict[str, Any]:
    """Build the canonical, bounded CloudWatch mapping forwarded to the delegate.

    Only reviewed keys with validated/bounded values are included. Assumes the
    input already passed ``_v_cloudwatch_metrics``."""
    out: dict[str, Any] = {
        "cluster_name": inp["cluster_name"],
        "metric_name": inp["metric_name"],
        "namespace": inp["namespace"],
    }
    for key in ("start_time", "end_time", "stat"):
        if inp.get(key) is not None:
            out[key] = inp[key]
    for key, lo, hi in (
        ("minutes", _CW_MINUTES_MIN, _CW_MINUTES_MAX),
        ("limit", _CW_LIMIT_MIN, _CW_LIMIT_MAX),
        ("period", _CW_PERIOD_MIN, _CW_PERIOD_MAX),
    ):
        if inp.get(key) is not None:
            out[key] = max(lo, min(int(inp[key]), hi))
    if isinstance(inp.get("dimensions"), Mapping):
        out["dimensions"] = {k: v for k, v in inp["dimensions"].items() if k in _CW_DIMENSION_KEYS}
    return out


def _v_insights(inp: Mapping[str, Any]) -> tuple[bool, str]:
    ok, msg = _v_cluster_name(inp)
    if not ok:
        return ok, msg
    ins_id = inp.get("insight_id") or inp.get("id")
    if ins_id is not None and (not isinstance(ins_id, str) or not _EKS_NAME.match(ins_id)):
        return _reject()
    return _ok_input()


def _v_vpc_config(inp: Mapping[str, Any]) -> tuple[bool, str]:
    # Reject any VPC id / network coordinate passed as input.
    if any(k in inp for k in ("vpc_id", "subnet_ids", "cidr_block", "security_group_ids")):
        return _reject()
    return _v_cluster_name(inp)


def _v_troubleshoot(inp: Mapping[str, Any]) -> tuple[bool, str]:
    # Match the installed handler's grammar: query must be non-empty, <= 300
    # chars, and contain only letters, numbers, commas, periods, question marks,
    # colons, and spaces (see eks_kb_handler.search_eks_troubleshoot_guide).
    query = inp.get("query")
    if not isinstance(query, str) or not (0 < len(query) <= _TROUBLESHOOT_MAX_QUERY):
        return _reject()
    if not _TROUBLESHOOT_QUERY.match(query):
        return _reject()
    return _ok_input()


_EKS_INPUT_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], tuple[bool, str]]] = {
    "list_k8s_resources": _v_list_k8s_resources,
    "get_k8s_events": _v_k8s_events,
    "list_api_versions": _v_api_versions,
    "get_eks_metrics_guidance": _v_metrics_guidance,
    "get_cloudwatch_metrics": _v_cloudwatch_metrics,
    "get_eks_insights": _v_insights,
    "get_eks_vpc_config": _v_vpc_config,
    "search_eks_troubleshoot_guide": _v_troubleshoot,
}

# Optional per-tool canonicalizers that rebuild a bounded delegate mapping from a
# validated input (beyond simple key filtering). Only defined where value-level
# clamping/normalization is required.
_EKS_INPUT_CANONICALIZERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "get_cloudwatch_metrics": _canonical_cloudwatch_input,
}

# Reviewed schema constraints injected into the narrowed inputSchema so the model
# is told the real accepted enums/bounds (issue #466 defect 10), not merely which
# fields exist. Values are JSON-Schema fragments merged onto each property.
_EKS_SCHEMA_CONSTRAINTS: dict[str, dict[str, dict[str, Any]]] = {
    "get_cloudwatch_metrics": {
        "namespace": {"enum": sorted(_CW_NAMESPACE_ENUM)},
        "stat": {"enum": sorted(_CW_STAT_ENUM)},
        "minutes": {"type": "integer", "minimum": _CW_MINUTES_MIN, "maximum": _CW_MINUTES_MAX},
        "limit": {"type": "integer", "minimum": _CW_LIMIT_MIN, "maximum": _CW_LIMIT_MAX},
        "period": {"type": "integer", "minimum": _CW_PERIOD_MIN, "maximum": _CW_PERIOD_MAX},
        "metric_name": {"type": "string", "maxLength": 128},
        "cluster_name": {"type": "string", "maxLength": 100},
    },
    "get_eks_metrics_guidance": {
        "resource_type": {"enum": sorted(_METRICS_CATEGORIES)},
    },
    "list_k8s_resources": {
        "cluster_name": {"type": "string", "maxLength": 100},
        "kind": {"type": "string", "maxLength": 64},
        "api_version": {"type": "string", "maxLength": 63},
        "namespace": {"type": "string", "maxLength": 63},
        "label_selector": {"type": "string", "maxLength": 256},
        "field_selector": {"type": "string", "maxLength": 256},
    },
    "get_k8s_events": {
        "cluster_name": {"type": "string", "maxLength": 100},
        "kind": {"type": "string", "maxLength": 64},
        "name": {"type": "string", "maxLength": 253},
        "namespace": {"type": "string", "maxLength": 63},
    },
    "search_eks_troubleshoot_guide": {
        "query": {"type": "string", "maxLength": _TROUBLESHOOT_MAX_QUERY},
    },
}

# Per-tool reviewed input-key allowlists. Any key outside a tool's set causes the
# request to be rejected before dispatch (unknown keys are never forwarded), and
# only these keys are reconstructed into the canonical delegate input. Note the
# insights set carries NO opaque next_token, and the vpc set is cluster-only.
_EKS_ALLOWED_INPUT_KEYS: dict[str, frozenset[str]] = {
    "list_k8s_resources": frozenset(
        {"cluster_name", "kind", "api_version", "namespace", "label_selector", "field_selector"}
    ),
    "get_k8s_events": frozenset({"cluster_name", "kind", "name", "namespace"}),
    "list_api_versions": frozenset({"cluster_name"}),
    "get_eks_metrics_guidance": frozenset({"resource_type"}),
    "get_cloudwatch_metrics": frozenset(
        {
            "cluster_name",
            "metric_name",
            "namespace",
            "start_time",
            "end_time",
            "dimensions",
            "minutes",
            "limit",
            "period",
            "stat",
        }
    ),
    "get_eks_insights": frozenset({"cluster_name", "insight_id"}),
    "get_eks_vpc_config": frozenset({"cluster_name"}),
    "search_eks_troubleshoot_guide": frozenset({"query"}),
}

_EKS_REQUIRED_INPUT_KEYS: dict[str, list[str]] = {
    "list_k8s_resources": ["cluster_name", "kind", "api_version"],
    "get_k8s_events": ["cluster_name", "kind", "name"],
    "list_api_versions": ["cluster_name"],
    "get_eks_metrics_guidance": ["resource_type"],
    "get_cloudwatch_metrics": ["cluster_name", "metric_name", "namespace", "dimensions"],
    "get_eks_insights": ["cluster_name"],
    "get_eks_vpc_config": ["cluster_name"],
    "search_eks_troubleshoot_guide": ["query"],
}


def _canonical_eks_input(name: str, inp: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reject unknown keys and project the input onto the tool's reviewed set.

    Returns the canonical delegate input (only reviewed keys, only when present),
    or None if the input carries an unknown key. Bounds/grammars are still
    enforced by the per-tool validators; this only narrows the key surface so
    unreviewed fields never reach the delegate."""
    allowed = _EKS_ALLOWED_INPUT_KEYS.get(name)
    if allowed is None:
        return None
    for key in inp.keys():
        if key not in allowed:
            return None  # unknown key => reject entirely
    return {key: inp[key] for key in allowed if key in inp}


# --------------------------------------------------------------------------- #
# AWS API MCP (call_aws) — exact read-only `aws eks` CLI grammar              #
# --------------------------------------------------------------------------- #
# suggest_aws_commands and every other/experimental tool are dropped; only
# call_aws survives (subject to the grammar below).

# Reviewed read-only `aws eks` verbs. list-updates is intentionally excluded
# (it exposes deployment update IDs; no reviewed need requires it).
_CLI_REGION = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
_CLI_EKS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# The only input fields the guarded call_aws advertises to the model.
_CALL_AWS_ADVERTISED_KEYS = frozenset({"cli_command", "max_results"})


# Per-verb option grammar. Each verb declares its allowed options and, for
# value-taking options, a validator for the value. "--region" is allowed on
# every verb. No global options, file refs, endpoint/profile/debug/query/output
# overrides, wildcard regions, or unknown flags are permitted.
def _valid_name_value(value: str) -> bool:
    return bool(_CLI_EKS_NAME.fullmatch(value)) and not any(shape.search(value) for shape in _SENSITIVE_SCALAR_SHAPES)


_CLI_VERB_GRAMMAR: dict[str, dict[str, Callable[[str], bool] | None]] = {
    "list-clusters": {},
    "describe-cluster": {"--name": _valid_name_value},
    "list-nodegroups": {"--cluster-name": _valid_name_value},
    "describe-nodegroup": {"--cluster-name": _valid_name_value, "--nodegroup-name": _valid_name_value},
    "list-fargate-profiles": {"--cluster-name": _valid_name_value},
    "describe-fargate-profile": {"--cluster-name": _valid_name_value, "--fargate-profile-name": _valid_name_value},
    "list-addons": {"--cluster-name": _valid_name_value},
    "describe-addon": {"--cluster-name": _valid_name_value, "--addon-name": _valid_name_value},
}
# Required options per verb (besides the always-optional --region).
_CLI_VERB_REQUIRED: dict[str, frozenset[str]] = {
    "describe-cluster": frozenset({"--name"}),
    "list-nodegroups": frozenset({"--cluster-name"}),
    "describe-nodegroup": frozenset({"--cluster-name", "--nodegroup-name"}),
    "list-fargate-profiles": frozenset({"--cluster-name"}),
    "describe-fargate-profile": frozenset({"--cluster-name", "--fargate-profile-name"}),
    "list-addons": frozenset({"--cluster-name"}),
    "describe-addon": frozenset({"--cluster-name", "--addon-name"}),
}


def _canonicalize_cli(command: str) -> str | None:
    """Parse+validate a single command against the exact grammar and return a
    canonical reconstruction, or None if it is not permitted.

    Rejects shell syntax, command substitution, extra/global options, file
    references, endpoint/profile/debug/query/output overrides, wildcard/multiple
    regions, unknown flags, and unknown/mutating verbs — before any dispatch.
    """
    if not isinstance(command, str) or not (1 <= len(command) <= MAX_STRING):
        return None
    # Reject shell metacharacters / substitutions outright.
    if any(ch in command for ch in ("|", ";", "&", ">", "<", "`", "\n", "\r", "$")):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 3 or tokens[0] != "aws" or tokens[1] != "eks":
        return None
    verb = tokens[2]
    grammar = _CLI_VERB_GRAMMAR.get(verb)
    if grammar is None:
        return None

    rest = tokens[3:]
    seen_options: dict[str, str] = {}
    region: str | None = None
    i = 0
    while i < len(rest):
        opt = rest[i]
        if not opt.startswith("--"):
            return None  # positional args not allowed
        # No "--opt=value" form; require separate value token for uniformity.
        if "=" in opt:
            return None
        if opt == "--region":
            if i + 1 >= len(rest):
                return None
            value = rest[i + 1]
            if not _CLI_REGION.match(value):  # rejects "*" and malformed regions
                return None
            if region is not None:  # multiple --region not allowed
                return None
            region = value
            i += 2
            continue
        if opt not in grammar:
            return None  # unknown/global/override option
        validator = grammar[opt]
        if validator is None:
            # boolean flag (none currently) — not expected
            return None
        if i + 1 >= len(rest):
            return None
        value = rest[i + 1]
        if opt in seen_options:  # duplicate option not allowed
            return None
        if not validator(value):
            return None
        seen_options[opt] = value
        i += 2

    required = _CLI_VERB_REQUIRED.get(verb, frozenset())
    if not required.issubset(seen_options.keys()):
        return None

    # Canonical reconstruction: fixed option order, single spacing.
    parts = ["aws", "eks", verb]
    for opt in sorted(seen_options.keys()):
        parts.extend([opt, seen_options[opt]])
    if region is not None:
        parts.extend(["--region", region])
    return " ".join(parts)


def _cli_verb(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    return tokens[2] if len(tokens) >= 3 else None


# Per-verb field allowlists for `aws eks describe-*` payloads.
_CLUSTER_FIELDS = ("name", "status", "version", "platformVersion", "health")
_NODEGROUP_FIELDS = (
    "nodegroupName",
    "status",
    "version",
    "capacityType",
    "instanceTypes",
    "amiType",
    "scalingConfig",
    "health",
)
_ADDON_FIELDS = ("addonName", "status", "addonVersion", "health")


def _pick(data: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Project reviewed describe fields and retain internal quality flags."""
    out: dict[str, Any] = {}
    partial = False
    truncated = False
    for field in fields:
        if field not in data:
            continue
        value = data[field]
        if isinstance(value, str):
            projected = _s(value)
            if projected is None:
                partial = True
            else:
                out[field] = projected
                truncated = truncated or len(value) > len(projected)
        elif isinstance(value, bool):
            out[field] = value
        elif isinstance(value, (int, float)):
            finite = _finite_number(value)
            if finite is None:
                partial = True
            else:
                out[field] = finite
        elif value is None:
            out[field] = None
        elif isinstance(value, list):
            projected_list = _str_list(value)
            out[field] = projected_list
            truncated = truncated or len(value) > MAX_LIST_VALUES
            partial = partial or len(projected_list) != min(len(value), MAX_LIST_VALUES)
        elif isinstance(value, Mapping):
            nested: dict[str, Any] = {}
            truncated = truncated or len(value) > MAX_LIST_VALUES
            for key_value, nested_value in list(value.items())[:MAX_LIST_VALUES]:
                key = _s(key_value)
                if key is None:
                    partial = True
                    continue
                if isinstance(nested_value, str):
                    projected_value = _s(nested_value)
                    if projected_value is None:
                        partial = True
                    else:
                        nested[key] = projected_value
                elif isinstance(nested_value, bool):
                    nested[key] = nested_value
                elif isinstance(nested_value, (int, float)):
                    finite = _finite_number(nested_value)
                    if finite is None:
                        partial = True
                    else:
                        nested[key] = finite
                elif nested_value is None:
                    nested[key] = None
                else:
                    partial = True
            out[field] = nested
        else:
            partial = True
    if partial:
        out["_partial"] = True
    if truncated:
        out["_truncated"] = True
    return out


def _project_eks_cli_json(verb: str, raw_json: str) -> dict[str, Any] | None:
    """Project a raw `aws eks` CLI JSON string onto the reviewed field set.

    Required-member exactness: a LIST verb requires its list member to be present
    and a list (a missing/wrong member is malformed => None, never a fabricated
    empty list); a DESCRIBE verb requires its target object to be present and a
    mapping (a missing/wrong target is malformed => None, never a null-filled
    object). An empty-but-present list is legitimate and marked with ``_empty``
    so the caller can report an explicit empty outcome."""
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, Mapping):
        return None

    def _list_result(member: str, out_key: str) -> dict[str, Any] | None:
        values = parsed.get(member)
        if not isinstance(values, list):
            return None
        projected = _str_list(values, limit=MAX_ITEMS)
        result: dict[str, Any] = {out_key: projected}
        if not values:
            result["_empty"] = True
        if len(values) > MAX_ITEMS:
            result["_truncated"] = True
        if len(projected) != min(len(values), MAX_ITEMS):
            result["_partial"] = True
        return result

    def _object_result(member: str, out_key: str, fields: Sequence[str]) -> dict[str, Any] | None:
        obj = _as_mapping(parsed.get(member))
        if obj is None:
            return None
        projected = _pick(obj, fields)
        partial = bool(projected.pop("_partial", False))
        truncated = bool(projected.pop("_truncated", False))
        # The first reviewed field is the operation's identifying name. A
        # describe response without it cannot be attributed safely.
        if not isinstance(projected.get(fields[0]), str):
            return None
        result: dict[str, Any] = {out_key: projected}
        if partial:
            result["_partial"] = True
        if truncated:
            result["_truncated"] = True
        return result

    if verb == "list-clusters":
        return _list_result("clusters", "clusters")
    if verb == "list-nodegroups":
        return _list_result("nodegroups", "nodegroups")
    if verb == "list-fargate-profiles":
        return _list_result("fargateProfileNames", "fargateProfileNames")
    if verb == "list-addons":
        return _list_result("addons", "addons")
    if verb == "describe-cluster":
        return _object_result("cluster", "cluster", _CLUSTER_FIELDS)
    if verb == "describe-nodegroup":
        return _object_result("nodegroup", "nodegroup", _NODEGROUP_FIELDS)
    if verb == "describe-addon":
        return _object_result("addon", "addon", _ADDON_FIELDS)
    if verb == "describe-fargate-profile":
        return _object_result("fargateProfile", "fargateProfile", ("fargateProfileName", "status"))
    return None


# Bounded structured denial markers used to classify a nested call_aws failure
# as access-denied. We inspect these code/marker fields, then DISCARD them.
_DENIED_ERROR_CODES = frozenset(
    {
        "accessdenied",
        "accessdeniedexception",
        "unauthorizedoperation",
        "unauthorized",
        "forbidden",
        "authfailure",
    }
)
_DENIED_STATUS_CODES = frozenset({401, 403})

# Per-entry classification states.
_ENTRY_OK = "ok"
_ENTRY_DENIED = "denied"
_ENTRY_FAILED = "failed"
_ENTRY_MALFORMED = "malformed"


def _entry_is_denied(entry: Mapping[str, Any], response: Mapping[str, Any] | None) -> bool:
    """Classify a failing entry as access-denied using bounded structured codes
    and markers only (never free-form prose scanning of unbounded fields)."""
    codes: list[str] = []
    status_codes: list[Any] = []
    denial_markers = ("accessdenied", "not authorized", "unauthorized", "forbidden")
    nested_error = response.get("error") if response is not None else None
    for candidate in (entry.get("error"), nested_error):
        if isinstance(candidate, str):
            bounded = candidate[:_CLASSIFY_ERROR_PREFIX].casefold()
            if any(marker in bounded for marker in denial_markers):
                return True
    if response is not None:
        error_code = response.get("error_code")
        if isinstance(error_code, str):
            codes.append(error_code[:64].casefold())
        status_codes.append(response.get("status_code"))
    failed_constraints = entry.get("failed_constraints")
    if isinstance(failed_constraints, list):
        codes.extend(
            constraint[:64].casefold()
            for constraint in failed_constraints[:MAX_LIST_VALUES]
            if isinstance(constraint, str)
        )
    if any(c in _DENIED_ERROR_CODES for c in codes):
        return True
    if any(isinstance(s, int) and s in _DENIED_STATUS_CODES for s in status_codes):
        return True
    return False


def _classify_call_aws_entry(command: str, entry: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Classify and project a single flattened CallAWSResponse entry.

    Returns (state, projected). ``state`` is one of ok/denied/failed/malformed.
    Nested error/error_code/status/failed_constraints are inspected for
    classification, then discarded — never returned or logged. On ok, the
    projection carries a boolean ``hasMore`` when the underlying API reported a
    nested continuation member (nextToken) or pagination_token; the raw token is
    never included."""
    verb = _cli_verb(command)
    if verb is None:
        return _ENTRY_MALFORMED, None

    response = entry.get("response")
    response_map = response if isinstance(response, Mapping) else None

    # Verify the returned entry's cli_command canonicalizes EXACTLY to the
    # requested command. A mismatch (provider returned a different/rewritten
    # command) is malformed — never projected as a clean success.
    returned_cmd = entry.get("cli_command")
    if not isinstance(returned_cmd, str) or _canonicalize_cli(returned_cmd) != command:
        return _ENTRY_MALFORMED, None

    # Bounded failure signals. A denial/error can be reported via a top-level
    # error, a nested error, a nested error_code (even when `error` is falsey),
    # a non-2xx status_code, validation/context failures, or failed_constraints.
    top_error = entry.get("error")
    nested_error = response_map.get("error") if response_map is not None else None
    nested_error_code = response_map.get("error_code") if response_map is not None else None
    status_code = response_map.get("status_code") if response_map is not None else None
    non_2xx = isinstance(status_code, int) and not (200 <= status_code < 300)
    has_failure = bool(
        top_error
        or nested_error
        or (isinstance(nested_error_code, str) and nested_error_code)
        or non_2xx
        or entry.get("validation_failures")
        or entry.get("missing_context_failures")
        or (isinstance(entry.get("failed_constraints"), list) and entry.get("failed_constraints"))
    )
    if has_failure:
        if _entry_is_denied(entry, response_map):
            return _ENTRY_DENIED, None
        return _ENTRY_FAILED, None

    # Success path: require the exact response/object shape.
    if response_map is not None:
        raw = response_map.get("json") or response_map.get("as_json")
    elif isinstance(response, str):
        raw = response
    else:
        raw = None
    if not isinstance(raw, str):
        return _ENTRY_MALFORMED, None

    projected = _project_eks_cli_json(verb, raw)
    if projected is None:
        return _ENTRY_MALFORMED, None

    is_empty = bool(projected.pop("_empty", False))
    is_partial = bool(projected.pop("_partial", False))
    is_truncated = bool(projected.pop("_truncated", False))
    entry_out: dict[str, Any] = {"cli_command": command, **projected}
    if is_empty:
        entry_out["_empty"] = True
    if is_partial:
        entry_out["_partial"] = True
    if is_truncated:
        entry_out["_truncated"] = True

    # Pagination truthfulness: a nested continuation member means results remain.
    has_more = False
    if response_map is not None and response_map.get("pagination_token"):
        has_more = True
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, Mapping) and (parsed.get("nextToken") or parsed.get("NextToken")):
            has_more = True
    except (json.JSONDecodeError, TypeError):
        pass
    if has_more:
        entry_out["hasMore"] = True
    return _ENTRY_OK, entry_out


# --------------------------------------------------------------------------- #
# Bounded delegate streaming with a deadline                                  #
# --------------------------------------------------------------------------- #
async def _drain_final_result(
    delegate: AgentTool, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
) -> Mapping[str, Any] | None:
    """Stream the delegate and return only its final ToolResult mapping.

    Bounded by DEADLINE_SECONDS. On timeout/cancel returns None so the caller
    emits a typed unavailable outcome. Intermediate events are swallowed."""

    async def _run() -> Mapping[str, Any] | None:
        final: Mapping[str, Any] | None = None
        async for event in delegate.stream(tool_use, invocation_state, **kwargs):
            if isinstance(event, ToolResultEvent):
                final = cast(Mapping[str, Any], event.tool_result)
        return final

    # A bounded deadline converts a hung/slow provider into a typed unavailable
    # outcome (return None). External task cancellation, by contrast, must
    # propagate so the surrounding request is actually cancelled rather than
    # masquerading as an availability result.
    #
    # asyncio.wait_for raises asyncio.TimeoutError when ITS OWN timeout fires,
    # and asyncio.CancelledError when the awaiting task is cancelled from the
    # outside. We swallow the former into a typed-unavailable (None) and let the
    # latter propagate.
    #
    # A delegate that raises an ordinary Exception must ALSO become a typed
    # unavailable here — if it propagated, base_specialist's `logger.exception`
    # would log the raw traceback (which can embed ARNs/endpoints/child text).
    # We return None WITHOUT logging the exception text; only the caller's fixed
    # typed code is logged. CancelledError (a BaseException, not Exception) still
    # propagates.
    try:
        return await asyncio.wait_for(_run(), timeout=DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        # Do NOT interpolate or log the exception text; return a typed-unavailable
        # signal (None) so the caller emits a sanitized unavailable outcome.
        return None


# --------------------------------------------------------------------------- #
# Guarded tools                                                               #
# --------------------------------------------------------------------------- #
class _EksProjectedTool(AgentTool):
    """Wrap an allowed EKS MCP read tool: guard input, bound stream, project."""

    def __init__(self, delegate: AgentTool) -> None:
        super().__init__()
        self._delegate = delegate
        self._name = delegate.tool_name

    @property
    def tool_name(self) -> str:
        return self._delegate.tool_name

    @property
    def tool_spec(self) -> ToolSpec:
        """Advertise only the reviewed input fields for this tool.

        The delegate's schema advertises fields the guard rejects at runtime
        (vpc_id, pagination tokens, arbitrary options, write/sensitive fields).
        We narrow the advertised inputSchema.properties/required to the reviewed
        key set so the model is never told those fields are available."""
        spec = cast(ToolSpec, dict(self._delegate.tool_spec))
        allowed = _EKS_ALLOWED_INPUT_KEYS.get(self._name)
        if allowed is None:
            return spec
        input_schema = spec.get("inputSchema")
        if isinstance(input_schema, Mapping):
            new_schema = dict(input_schema)
            json_schema = new_schema.get("json")
            if isinstance(json_schema, Mapping):
                narrowed = dict(json_schema)
                props = narrowed.get("properties")
                if isinstance(props, Mapping):
                    narrowed_props = {
                        k: (dict(v) if isinstance(v, Mapping) else v) for k, v in props.items() if k in allowed
                    }
                else:
                    narrowed_props = {}
                # Inject reviewed enums / numeric bounds so the advertised schema
                # states the real accepted constraints, not merely field presence.
                constraints = _EKS_SCHEMA_CONSTRAINTS.get(self._name, {})
                for key, extra in constraints.items():
                    if key in allowed:
                        prop = dict(narrowed_props.get(key) or {})
                        prop.update(extra)
                        narrowed_props[key] = prop
                narrowed["properties"] = narrowed_props
                narrowed["required"] = list(_EKS_REQUIRED_INPUT_KEYS.get(self._name, []))
                # Reviewed key surface only: forbid unadvertised properties.
                narrowed["additionalProperties"] = False
                new_schema["json"] = narrowed
            spec["inputSchema"] = cast(Any, new_schema)
        return spec

    @property
    def tool_type(self) -> str:
        return self._delegate.tool_type

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        tool_use_id = tool_use["toolUseId"]
        name = self._name
        inp = tool_use.get("input")
        inp = inp if isinstance(inp, Mapping) else {}

        # 1) INPUT guard before any dispatch: reject unknown keys, validate
        #    bounds/grammar, then forward ONLY the canonical reviewed fields.
        canonical_input = _canonical_eks_input(name, inp)
        if canonical_input is None:
            _log_outcome(name, _CODE_DENIED_INPUT)
            yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_DENIED, _MSG_DENIED_INPUT))
            return
        validator = _EKS_INPUT_VALIDATORS.get(name)
        if validator is not None:
            ok, message = validator(canonical_input)
            if not ok:
                _log_outcome(name, _CODE_DENIED_INPUT)
                yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_DENIED, message))
                return

        # Optional value-level canonicalization (e.g. CloudWatch bounded mapping).
        canonicalizer = _EKS_INPUT_CANONICALIZERS.get(name)
        if canonicalizer is not None:
            canonical_input = canonicalizer(canonical_input)

        # Forward only the reviewed canonical fields to the delegate.
        delegate_use: ToolUse = {**tool_use, "input": canonical_input}

        # 2) Bounded delegate streaming.
        result = await _drain_final_result(self._delegate, delegate_use, invocation_state, **kwargs)
        if result is None:
            _log_outcome(name, _CODE_UNAVAILABLE)
            yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_UNAVAILABLE, _MSG_UNAVAILABLE))
            return

        if result.get("status") == "error":
            # Distinguish access-denied from availability, without raw bodies.
            outcome, message = _classify_error(result)
            _log_outcome(name, outcome)
            yield ToolResultEvent(_sanitized_error(tool_use_id, name, outcome, message))
            return

        # 3) Extract authoritative payload + project.
        if name in _EKS_TEXT_TOOLS:
            # Raw single-text tool (troubleshoot guide): project the plain body.
            raw = _first_text_block(result)
            if raw is None:
                _log_outcome(name, _CODE_MALFORMED)
                yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_MALFORMED, _MSG_MALFORMED))
                return
            try:
                text_projected = _project_troubleshoot_guide(raw)
            except Exception:
                _log_outcome(name, _CODE_MALFORMED)
                yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_MALFORMED, _MSG_MALFORMED))
                return
            yield ToolResultEvent(_ok(tool_use_id, name, text_projected))
            return

        payload = _extract_eks_payload(result)
        if payload is None:
            _log_outcome(name, _CODE_MALFORMED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_MALFORMED, _MSG_MALFORMED))
            return
        try:
            projected = _EKS_TOOL_PROJECTORS[name](payload)
        except Exception:
            # No provider-derived text: fixed typed code only.
            _log_outcome(name, _CODE_MALFORMED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_MALFORMED, _MSG_MALFORMED))
            return
        if projected is None:
            _log_outcome(name, _CODE_MALFORMED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, name, OUTCOME_MALFORMED, _MSG_MALFORMED))
            return
        yield ToolResultEvent(_ok(tool_use_id, name, projected))


class _CallAwsGuardedTool(AgentTool):
    """Guard the AWS API MCP ``call_aws`` tool: enforce the read-only `aws eks`
    grammar before dispatch, then project allowed output."""

    def __init__(self, delegate: AgentTool) -> None:
        super().__init__()
        self._delegate = delegate

    @property
    def tool_name(self) -> str:
        return self._delegate.tool_name

    @property
    def tool_spec(self) -> ToolSpec:
        spec = cast(ToolSpec, dict(self._delegate.tool_spec))
        spec["description"] = (
            "Run a reviewed, read-only `aws eks` discovery command "
            "(list-clusters, describe-cluster, list/describe nodegroup, "
            "list/describe fargate-profile, list/describe addon). A fixed "
            "argument grammar is enforced; other services, mutating verbs, "
            "extra options, file references, and shell syntax are rejected."
        )
        # Narrow the advertised input surface to the reviewed fields only. The
        # delegate advertises the full aws-api schema (region/profile overrides,
        # pagination tokens, arbitrary options); we advertise only the reviewed
        # cli_command + bounded max_results so the model is never told those
        # runtime-rejected fields exist.
        input_schema = spec.get("inputSchema")
        if isinstance(input_schema, Mapping):
            new_schema = dict(input_schema)
            json_schema = new_schema.get("json")
            base = dict(json_schema) if isinstance(json_schema, Mapping) else {"type": "object"}
            props = base.get("properties")
            existing = props if isinstance(props, Mapping) else {}
            narrowed_props = {
                k: dict(v) if isinstance(v, Mapping) else v
                for k, v in existing.items()
                if k in _CALL_AWS_ADVERTISED_KEYS
            }
            cli_command = {
                "description": "One reviewed aws eks command or a bounded batch.",
                "oneOf": [
                    {"type": "string", "minLength": 1, "maxLength": MAX_STRING},
                    {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BATCH,
                        "items": {"type": "string", "minLength": 1, "maxLength": MAX_STRING},
                    },
                ],
            }
            narrowed_props["cli_command"] = cli_command
            mr = dict(narrowed_props.get("max_results") or {"type": "integer"})
            mr["minimum"] = MAX_RESULTS_MIN
            mr["maximum"] = MAX_RESULTS_CAP
            narrowed_props["max_results"] = mr
            base["properties"] = narrowed_props
            base["required"] = ["cli_command"]
            base["additionalProperties"] = False
            new_schema["json"] = base
            spec["inputSchema"] = cast(Any, new_schema)
        return spec

    @property
    def tool_type(self) -> str:
        return self._delegate.tool_type

    def _commands(self, tool_use: ToolUse) -> list[str]:
        inp = tool_use.get("input")
        raw = inp.get("cli_command") if isinstance(inp, Mapping) else None
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            if not raw or not all(isinstance(command, str) for command in raw):
                return []
            return list(raw)
        return []

    def _bounded_max_results(self, tool_use: ToolUse) -> tuple[bool, int]:
        """Return a code-owned bounded max_results to send to the delegate.

        Returns (ok, value). A code-owned bound is ALWAYS sent — even when the
        model omits max_results (default) — so the delegate can never run
        unbounded. A supplied value is clamped to [MIN, CAP]. A bool or otherwise
        invalid form is rejected (ok=False) rather than silently dropping the
        bound."""
        inp = tool_use.get("input")
        if not isinstance(inp, Mapping) or "max_results" not in inp:
            return True, MAX_RESULTS_DEFAULT
        raw = inp.get("max_results")
        # bool is an int subclass — reject it explicitly.
        if isinstance(raw, bool) or not isinstance(raw, int):
            return False, MAX_RESULTS_DEFAULT
        if raw < 0:
            return False, MAX_RESULTS_DEFAULT
        clamped = max(MAX_RESULTS_MIN, min(raw, MAX_RESULTS_CAP))
        return True, clamped

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        tool_use_id = tool_use["toolUseId"]
        op = "call_aws"
        raw_input = tool_use.get("input")
        if not isinstance(raw_input, Mapping) or any(key not in _CALL_AWS_ADVERTISED_KEYS for key in raw_input):
            _log_outcome(op, _CODE_DENIED_INPUT)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_DENIED, _MSG_DENIED_INPUT))
            return
        commands = self._commands(tool_use)

        if not commands:
            _log_outcome(op, _CODE_DENIED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_DENIED, _MSG_DENIED_CLI))
            return
        # Hard batch / fan-out cap before validating individual commands.
        if len(commands) > MAX_BATCH:
            _log_outcome(op, _CODE_DENIED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_DENIED, _MSG_DENIED_CLI))
            return

        canonical = [_canonicalize_cli(c) for c in commands]
        if any(c is None for c in canonical):
            _log_outcome(op, _CODE_DENIED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_DENIED, _MSG_DENIED_CLI))
            return
        canonical_cmds: list[str] = [c for c in canonical if c is not None]

        # Canonical reconstruction + always-bounded max_results before dispatch.
        # Only the reviewed keys are reconstructed; unknown input keys are
        # dropped rather than forwarded.
        ok_mr, bounded = self._bounded_max_results(tool_use)
        if not ok_mr:
            _log_outcome(op, _CODE_DENIED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_DENIED, _MSG_DENIED_INPUT))
            return
        new_input: dict[str, Any] = {
            "cli_command": canonical_cmds if len(canonical_cmds) > 1 else canonical_cmds[0],
            "max_results": bounded,
        }
        delegate_use: ToolUse = {**tool_use, "input": new_input}

        result = await _drain_final_result(self._delegate, delegate_use, invocation_state, **kwargs)
        if result is None:
            _log_outcome(op, _CODE_UNAVAILABLE)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_UNAVAILABLE, _MSG_UNAVAILABLE))
            return
        if result.get("status") == "error":
            outcome, message = _classify_error(result)
            _log_outcome(op, outcome)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, outcome, message))
            return

        entries = _extract_call_aws_entries(result)
        # Require an exact list shape. A non-list structured result is malformed.
        if entries is None or not isinstance(entries, list):
            _log_outcome(op, _CODE_MALFORMED)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_MALFORMED, _MSG_MALFORMED))
            return

        projected_entries: list[dict[str, Any]] = []
        any_denied = False
        any_failed = False
        any_malformed = False
        any_has_more = False
        any_projection_partial = False
        any_projection_truncated = False
        empty_flags: list[bool] = []

        for command, entry in zip(canonical_cmds, entries):
            m = _as_mapping(entry)
            if m is None:
                any_malformed = True
                continue
            state, projected = _classify_call_aws_entry(command, m)
            if state == _ENTRY_OK and projected is not None:
                if projected.pop("hasMore", None):
                    any_has_more = True
                empty_flags.append(bool(projected.pop("_empty", False)))
                any_projection_partial = any_projection_partial or bool(projected.pop("_partial", False))
                any_projection_truncated = any_projection_truncated or bool(projected.pop("_truncated", False))
                projected_entries.append(projected)
            elif state == _ENTRY_DENIED:
                any_denied = True
            elif state == _ENTRY_MALFORMED:
                any_malformed = True
            else:  # _ENTRY_FAILED
                any_failed = True

        # Count mismatch: ANY difference between returned entries and requested
        # commands (fewer OR extra) means the batch is not faithfully complete.
        count_mismatch = len(entries) != len(canonical_cmds)

        if not projected_entries:
            # Nothing projected. If any entry was explicitly denied, surface a
            # typed denied outcome; if the output was malformed-only, malformed;
            # otherwise an availability failure.
            if any_denied:
                _log_outcome(op, _CODE_DENIED)
                yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_DENIED, _MSG_DENIED))
                return
            if any_malformed and not any_failed:
                _log_outcome(op, _CODE_MALFORMED)
                yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_MALFORMED, _MSG_MALFORMED))
                return
            _log_outcome(op, _CODE_UNAVAILABLE)
            yield ToolResultEvent(_sanitized_error(tool_use_id, op, OUTCOME_UNAVAILABLE, _MSG_UNAVAILABLE))
            return

        body: dict[str, Any] = {"results": projected_entries}
        mixed_failure = any_denied or any_failed or any_malformed or count_mismatch or any_projection_partial
        if mixed_failure:
            body["outcome"] = OUTCOME_PARTIAL
            body["partial"] = True
        elif any_projection_truncated:
            body.update({"outcome": OUTCOME_TRUNCATED, "truncated": True, "hasMore": True})
        elif empty_flags and all(empty_flags):
            body["outcome"] = OUTCOME_EMPTY
        else:
            body["outcome"] = OUTCOME_COMPLETE
        if any_projection_truncated:
            body["truncated"] = True
            body["hasMore"] = True
        if any_has_more:
            body["hasMore"] = True
            body["partial"] = True
            if body["outcome"] in (OUTCOME_COMPLETE, OUTCOME_EMPTY):
                body["outcome"] = OUTCOME_PARTIAL
        yield ToolResultEvent(_ok(tool_use_id, op, body))


_CLASSIFY_ERROR_PREFIX = 512  # bounded number of chars scanned for denial markers


def _classify_error(result: Mapping[str, Any]) -> tuple[str, str]:
    """Classify a provider error result as denied vs unavailable WITHOUT
    returning or logging the raw body. Only a fixed, bounded PREFIX of the first
    error text block is inspected for a denial marker; the text is then
    discarded (never returned, never logged)."""
    denied = False
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                # Bound the classification input: only a fixed prefix is scanned.
                low = item["text"][:_CLASSIFY_ERROR_PREFIX].lower()
                if "accessdenied" in low or "not authorized" in low or "forbidden" in low or "unauthorized" in low:
                    denied = True
                break  # inspect only the first text block, then discard
    if denied:
        return OUTCOME_DENIED, _MSG_DENIED
    return OUTCOME_UNAVAILABLE, _MSG_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Providers                                                                   #
# --------------------------------------------------------------------------- #
class _GuardedProviderBase(ToolProvider):
    def __init__(self, delegate: ToolProvider) -> None:
        self._delegate = delegate

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self._delegate.add_consumer(consumer_id, **kwargs)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self._delegate.remove_consumer(consumer_id, **kwargs)


class EksMcpGuardedProvider(_GuardedProviderBase):
    """Drop non-allowlisted EKS MCP tools and project allowed read tools."""

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        tools = await self._delegate.load_tools(**kwargs)
        guarded: list[AgentTool] = []
        for tool in tools:
            if tool.tool_name in _EKS_ALLOWED:
                guarded.append(_EksProjectedTool(tool))
            else:
                logger.debug("eks_guard drop server=eks op=other")
        return guarded


class AwsApiMcpGuardedProvider(_GuardedProviderBase):
    """Keep only guarded call_aws; drop suggest_aws_commands and everything else."""

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        tools = await self._delegate.load_tools(**kwargs)
        guarded: list[AgentTool] = []
        for tool in tools:
            if tool.tool_name == "call_aws":
                guarded.append(_CallAwsGuardedTool(tool))
            else:
                logger.debug("eks_guard drop server=aws-api op=other")
        return guarded


def guard_eks_mcp_client(server_name: str, client: ToolProvider) -> ToolProvider:
    """Wrap the EKS specialist's MCP clients with the EKS-owned output guard.

    Non-EKS clients are returned unchanged so this transform is safe to share
    across specialists.
    """
    if server_name == _EKS_MCP_SERVER:
        return EksMcpGuardedProvider(client)
    if server_name == _AWS_API_MCP_SERVER:
        return AwsApiMcpGuardedProvider(client)
    return client
