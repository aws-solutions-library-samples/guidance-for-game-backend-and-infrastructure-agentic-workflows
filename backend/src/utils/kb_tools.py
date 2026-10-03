"""
Knowledge Base tools for Strands agents.

Provides Bedrock Knowledge Base retrieval as Strands ``@tool`` functions for the
GameLift, EKS, and Cost specialists.

Model-facing output boundary (#463):
- The raw Bedrock Agent Runtime ``Retrieve`` response never reaches the model.
  Every retrieval is projected through ``utils.kb_projection`` into a bounded,
  sanitized, deterministically-serialized envelope with truthful, distinct
  states (complete / empty / partial / truncated / malformed / unavailable /
  denied).
- Retrieval count is validated (min / default / hard max) and the query is
  bounded before the provider is ever called.
- Provider exception text and provider-authored bodies never appear in the
  returned envelope or the logs — only code-owned, bounded error codes.

Performance:
- Projected (safe) envelopes are cached with a bounded, TTL'd cache. The cache
  key is derived from the FULL effective validated request (kb id, region,
  bounded query, validated result count, score), so a cache hit can never
  bypass projection or return a payload that would not have been projected.
"""

# Standard library
import hashlib
import math
import os
import random
import re
import threading
import time
from typing import Any, Dict, Optional

# Third-party packages
import boto3
from cachetools import TTLCache
from strands import tool

# Local modules
from config.settings import BOTO3_CLIENT_CONFIG, RETRY_BASE_DELAY, RETRY_MAX_ATTEMPTS
from utils.kb_projection import (
    EffectiveRequest,
    KBErrorCode,
)
from utils.kb_projection import _structured_error_code as _structured_error_code_impl
from utils.kb_projection import (
    bound_query,
    classify_provider_error,
    get_projection_limits,
    is_cacheable_envelope,
    project_error,
    project_retrieve_response,
    validate_number_of_results,
)
from utils.logger import logger

# Retryable transient error codes/names for the KB-owned retry path. Kept local
# so the KB boundary does not depend on the shared retry decorator, which logs
# str(exc) and would put provider-authored text / ARNs into logs (#463 F1).
_KB_RETRYABLE_ERRORS: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "InternalServerException",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)

# Hard bound for the logged exception-type label. A dynamically named / hostile
# exception class can have an arbitrarily long name carrying control text, so
# the label is length- AND grammar-bounded before it ever reaches the logs
# (#463): a recognized class name passes through, anything whose FULL name is
# not a short, pure identifier is coerced to the fixed "other" fallback so an
# attacker cannot pack a payload into a bounded identifier run.
_SAFE_TYPE_LABEL_MAX = 40
_SAFE_TYPE_LABEL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,%d}" % (_SAFE_TYPE_LABEL_MAX - 1))

# Small allowlist of expected, benign exception class names that may appear on
# the KB path in addition to the retryable set. Anything outside these sets
# must still pass the strict full-identifier + length gate to be logged.
_KB_KNOWN_ERROR_NAMES: frozenset[str] = _KB_RETRYABLE_ERRORS | frozenset(
    {
        "ClientError",
        "BotoCoreError",
        "EndpointConnectionError",
        "ConnectionError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "ValidationException",
        "ResourceNotFoundException",
        "AccessDeniedException",
        "ValueError",
        "TypeError",
        "KeyError",
    }
)


def _safe_type_label(exc: BaseException) -> str:
    """Return a short, grammar-bounded, safe label for an exception's type.

    Never logs or returns the raw class name. A recognized class name is
    returned as-is. Otherwise the name must be a SHORT, PURE identifier
    (``fullmatch`` against a bounded identifier grammar) to be surfaced; any
    name with control characters, whitespace, punctuation, or an oversized run
    collapses to the fixed ``"other"`` fallback so an attacker-controlled name
    can never reach the logs verbatim.
    """
    name = type(exc).__name__
    if name in _KB_KNOWN_ERROR_NAMES:
        return name
    if _SAFE_TYPE_LABEL_RE.fullmatch(name):
        return name
    return "other"


def _is_retryable(exc: BaseException) -> bool:
    """Classify a provider exception as retryable using only structured signals.

    Inspects the exception TYPE name and the structured boto error code — never
    ``str(exc)`` / the free-form provider message.
    """
    if type(exc).__name__ in _KB_RETRYABLE_ERRORS:
        return True
    return _structured_error_code(exc) in _KB_RETRYABLE_ERRORS


# Code-owned HARD maxima for the KB-owned retry path. RETRY_MAX_ATTEMPTS and
# RETRY_BASE_DELAY come from environment-backed settings and are NOT themselves
# hard-bounded, so the KB boundary clamps them here regardless of their value.
_RETRY_ATTEMPTS_HARD_MAX = 5
_RETRY_DELAY_MAX_SECONDS = 30.0
_RETRY_BASE_DELAY_MAX = 5.0


def _bounded_env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    """Read a hard-bounded int from the environment for cache config.

    A malformed / negative / huge value can never crash import or allocate an
    unreviewed cache size (F8). The raw value is never logged.
    """
    lo, hi = (minimum, maximum) if minimum <= maximum else (maximum, maximum)
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning(f"Invalid integer for {name}; using code-owned default")
            value = default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _structured_error_code(exc: BaseException) -> str:
    """Deprecated local alias — see the import above.

    Retained only so ``kb_tools._structured_error_code`` remains importable for
    existing callers/tests. Delegates to the single defensive implementation in
    ``utils.kb_projection`` so the two never diverge.
    """
    return _structured_error_code_impl(exc)


def _sleep_backoff(attempt: int) -> None:
    """Bounded exponential backoff with jitter (patchable ``time.sleep``).

    Hardened so a NaN / negative / huge ``RETRY_BASE_DELAY`` from environment-
    backed settings can never produce a non-finite or unbounded sleep. The
    base delay is coerced into ``[0, _RETRY_BASE_DELAY_MAX]`` first, the
    exponential term is capped, and the final delay (with jitter) is clamped to
    ``[0, _RETRY_DELAY_MAX_SECONDS]``.
    """
    base = RETRY_BASE_DELAY
    if not isinstance(base, (int, float)) or not math.isfinite(float(base)) or base < 0:
        base = 0.0
    base = min(float(base), _RETRY_BASE_DELAY_MAX)
    safe_attempt = attempt if attempt >= 1 else 1
    # Cap the exponent so 2 ** attempt cannot overflow into a huge float.
    exponent = min(safe_attempt - 1, 16)
    delay = min(base * (2**exponent), _RETRY_DELAY_MAX_SECONDS)
    jitter = random.uniform(0, delay * 0.25) if delay > 0 else 0.0
    total = delay + jitter
    if not math.isfinite(total):
        total = 0.0
    total = max(0.0, min(total, _RETRY_DELAY_MAX_SECONDS))
    time.sleep(total)


# KB result cache configuration. TTLCache gives both time-based expiry (lazy +
# on access) AND a hard size cap (LRU eviction). TTL and max-entry values come
# from the environment but are HARD-bounded here (F8): a malformed / negative /
# huge value can never crash import or allocate an unreviewed cache size.
_KB_CACHE_TTL_SECONDS = _bounded_env_int("GBAW_KB_CACHE_TTL_SECONDS", default=3600, minimum=1, maximum=86_400)
_KB_CACHE_MAX_ENTRIES = _bounded_env_int("GBAW_KB_CACHE_MAX_ENTRIES", default=1000, minimum=1, maximum=100_000)
_kb_cache: "TTLCache[str, str]" = TTLCache(maxsize=_KB_CACHE_MAX_ENTRIES, ttl=_KB_CACHE_TTL_SECONDS)
_kb_cache_lock = threading.Lock()

# Re-exported so existing callers / tests that read the module-level limit keep
# working. get_projection_limits() is the authoritative source.
__all__ = [
    "retrieve",
    "create_kb_retrieve_tool",
    "clear_kb_cache",
    "get_kb_cache_stats",
    "get_projection_limits",
]


def _get_cache_key(effective: EffectiveRequest) -> str:
    """Generate a cache key from the FULL effective validated request.

    Including the validated result count (and every other effective field)
    guarantees a hit is only reused for an identical, already-bounded request.
    """
    return hashlib.md5(effective.cache_key_material().encode(), usedforsecurity=False).hexdigest()


def _get_cached_result(cache_key: str) -> Optional[str]:
    """Get cached projected envelope if present (TTLCache drops expired)."""
    with _kb_cache_lock:
        result: Optional[str] = _kb_cache.get(cache_key)
        if result is not None:
            logger.debug(f"♻️ KB cache hit: {cache_key[:8]}...")
        return result


def _set_cached_result(cache_key: str, result: str) -> None:
    """Store a projected (safe) envelope in the cache.

    Only SAFE RESULT envelopes are cached — never a raw provider payload, and
    never an unavailable / denied / malformed / guardrail outcome. Caching a
    transient or authorization state would convert it into stale behavior for
    the cache TTL (#463 F9), so those states are deliberately not stored.
    """
    if not is_cacheable_envelope(result):
        logger.debug("KB result not cached (non-result envelope)")
        return
    with _kb_cache_lock:
        _kb_cache[cache_key] = result
        logger.debug(f"💾 KB result cached: {cache_key[:8]}...")


def _build_agent_runtime_client(region: str) -> Any:
    """Build a read-only Bedrock Agent Runtime client.

    Isolated so tests can patch the provider boundary. Uses the shared boto3
    config (adaptive retries). No profile handling — the runtime uses its task
    role; local dev uses ambient credentials.
    """
    return boto3.client("bedrock-agent-runtime", region_name=region, config=BOTO3_CLIENT_CONFIG)


def _retrieve_projected(effective: EffectiveRequest) -> str:
    """Call the provider (with a KB-owned bounded retry), project, and return.

    Retries transient provider failures with bounded backoff. On EVERY retry
    and on the final failure, only the code-owned attempt number, a bounded and
    grammar-safe exception TYPE label, and our sanitized classification are
    logged — never ``str(exc)``, never the raw type name, never the provider
    body. This is why the KB path does NOT use the shared
    ``retry_with_backoff`` decorator, which logs ``str(exc)`` (#463 F1).
    """

    def _call() -> Any:
        client = _build_agent_runtime_client(effective.region)
        return client.retrieve(
            retrievalQuery={"text": effective.query},
            knowledgeBaseId=effective.kb_id,
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": effective.number_of_results}},
        )

    max_attempts = max(1, min(RETRY_MAX_ATTEMPTS, _RETRY_ATTEMPTS_HARD_MAX))
    for attempt in range(1, max_attempts + 1):
        try:
            raw = _call()
            return project_retrieve_response(raw, effective)
        except Exception as exc:  # noqa: BLE001 - classified without leaking text
            error_code = classify_provider_error(exc)
            retryable = _is_retryable(exc)
            if retryable and attempt < max_attempts:
                # Code-owned fields ONLY: attempt number, a bounded/safe
                # exception type LABEL, and our sanitized classification. The
                # raw type name and provider error code are deliberately NOT
                # logged — either can carry an ARN / account id / control text /
                # unbounded string (F1).
                logger.warning(
                    f"KB retrieve transient failure: attempt={attempt}/{max_attempts} "
                    f"type={_safe_type_label(exc)} classified={error_code}; retrying"
                )
                _sleep_backoff(attempt)
                continue
            logger.warning(
                f"KB retrieve failed: attempt={attempt}/{max_attempts} "
                f"type={_safe_type_label(exc)} classified={error_code}"
            )
            return project_error(error_code)

    # Unreachable: the loop always returns. Defensive code-owned fallback.
    return project_error(KBErrorCode.UNAVAILABLE)  # pragma: no cover


def _resolve_score(score: Any) -> float:
    """Resolve a caller-supplied min score to a finite value in [0.0, 1.0].

    ``bool`` is not numeric; NaN / Infinity are rejected to the default so a
    non-finite provided score can never survive into the effective request or
    the cache identity (#463 F3).
    """
    if isinstance(score, bool):
        return 0.5
    if not isinstance(score, (int, float)):
        try:
            score = float(score)
        except (TypeError, ValueError):
            return 0.5
    value = float(score)
    if not math.isfinite(value):
        return 0.5
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


@tool
def retrieve(
    text: str,
    numberOfResults: int = 3,
    knowledgeBaseId: str = None,
    region: str = "us-west-2",
    score: float = 0.5,
    profile_name: str = None,
    enableMetadata: bool = False,
):
    """
    Retrieve relevant documentation from a Bedrock Knowledge Base.

    Returns a bounded, sanitized JSON envelope of document chunks (never a raw
    provider response). Uses ``GBAW_*_KB_ID`` from the environment when
    ``knowledgeBaseId`` is not provided.

    Args:
        text: The query text to search for.
        numberOfResults: Requested result count (validated to min/default/max).
        knowledgeBaseId: KB ID (optional; falls back to env).
        region: AWS region (default: us-west-2).
        score: Minimum relevance score 0.0-1.0 (default: 0.5).
        profile_name: Ignored (retained for signature compatibility).
        enableMetadata: Ignored; metadata is always projected via an allowlist.

    Returns:
        A JSON string envelope with ``state``, ``results`` (bounded), and counts.
    """
    kb_id = knowledgeBaseId or os.getenv("GBAW_KNOWLEDGE_BASE_ID") or os.getenv("GBAW_GAMELIFT_KB_ID")
    if not kb_id:
        return project_error(KBErrorCode.UNAVAILABLE)

    effective = EffectiveRequest(
        kb_id=kb_id,
        region=region or "us-west-2",
        query=bound_query(text),
        number_of_results=validate_number_of_results(numberOfResults),
        min_score=_resolve_score(score),
    )

    cache_key = _get_cache_key(effective)
    cached = _get_cached_result(cache_key)
    if cached is not None:
        return cached

    result = _retrieve_projected(effective)
    _set_cached_result(cache_key, result)
    return result


def create_kb_retrieve_tool(kb_id: str, region: str = "us-west-2"):
    """
    Create a KB retrieve tool bound to a specific knowledge base.

    Args:
        kb_id: Knowledge Base ID.
        region: AWS region (default: us-west-2).

    Returns:
        A Strands ``@tool`` function bound to the specified KB.
    """

    @tool
    def kb_retrieve(text: str, numberOfResults: int = 3, score: float = 0.5):
        """
        Retrieve relevant documentation from the knowledge base.

        Returns a bounded, sanitized JSON envelope of document chunks. Projected
        envelopes are cached (keyed on the full effective validated request) to
        reduce redundant API calls.

        Args:
            text: The query text to search for.
            numberOfResults: Requested result count (validated to min/default/max).
            score: Minimum relevance score 0.0-1.0 (default: 0.5).

        Returns:
            A JSON string envelope with ``state``, ``results`` (bounded), counts.
        """
        effective = EffectiveRequest(
            kb_id=kb_id,
            region=region or "us-west-2",
            query=bound_query(text),
            number_of_results=validate_number_of_results(numberOfResults),
            min_score=_resolve_score(score),
        )

        cache_key = _get_cache_key(effective)
        cached = _get_cached_result(cache_key)
        if cached is not None:
            return cached

        result = _retrieve_projected(effective)
        _set_cached_result(cache_key, result)
        return result

    return kb_retrieve


def clear_kb_cache() -> None:
    """Clear the KB result cache. Useful for testing or forcing refresh."""
    with _kb_cache_lock:
        _kb_cache.clear()
        logger.debug("🗑️ KB cache cleared")


def get_kb_cache_stats() -> Dict[str, Any]:
    """Get KB cache statistics.

    TTLCache evicts expired entries on access, so every entry currently present
    is valid; size is bounded by maxsize (LRU eviction beyond that).
    """
    with _kb_cache_lock:
        return {
            "total_entries": len(_kb_cache),
            "max_entries": _KB_CACHE_MAX_ENTRIES,
            "ttl_seconds": _KB_CACHE_TTL_SECONDS,
        }
