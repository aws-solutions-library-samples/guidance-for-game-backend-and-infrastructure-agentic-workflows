"""
Semantic Memory Utilities for AgentCore Memory.

Provides functions to save and retrieve semantic memories (LTM) using AWS SDK.
Follows AWS best practices and native SDK patterns.

Performance Optimization:
- boto3 client is cached at module level to avoid recreation overhead
"""

# Standard library
import hashlib
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Set

# Third-party packages
import boto3
from botocore.config import Config as BotocoreConfig
from cachetools import TTLCache

# Local modules
from config.settings import AWS_REGION, BEDROCK_AGENTCORE_MEMORY_ID, BOTO3_CLIENT_CONFIG
from utils.logger import logger
from utils.security import log_sanitized_exception

# Module-level boto3 client cache for performance
_memory_client = None

# Dedicated client for the cross-session existence check. The check fails open,
# runs inline on the response path, and only reads a boolean, so it gets a short
# connect/read timeout and one total attempt (``total_max_attempts`` counts the
# first attempt; botocore's ``max_attempts`` counts retries) rather than the
# write client's longer, retrying config, bounding how long a degraded read can
# hold the reply.
_EXISTENCE_CHECK_CLIENT_CONFIG = BotocoreConfig(
    connect_timeout=3,
    read_timeout=5,
    retries={"mode": "standard", "total_max_attempts": 1},
)
_existence_check_client = None
_client_init_lock = threading.Lock()

# Serializes the in-process dedup state and the in-flight set so concurrent
# same-process saves of the same fact collapse to one write. Never held across a
# network call.
_dedup_lock = threading.Lock()

# Content hashes for which a save is currently writing in this process. A second
# caller for a hash already in flight returns without issuing its own write.
_inflight_hashes: Set[str] = set()

# Deduplication cache: per-actor set of saved-content hashes, format
# {actor_id: {content_hash, ...}}. Bounded by a TTLCache on the actor dimension
# (LRU + TTL eviction) so it can't grow without limit in a long-lived runtime —
# dedup is a best-effort optimization, so evicting an idle actor's hashes at
# worst allows one duplicate memory write, which is harmless.
_MEMORY_DEDUP_MAX_ACTORS = int(os.getenv("GBAW_MEMORY_DEDUP_MAX_ACTORS", "5000"))
_MEMORY_DEDUP_TTL_SECONDS = int(os.getenv("GBAW_MEMORY_DEDUP_TTL_SECONDS", "86400"))  # 24h
_saved_memory_hashes: "TTLCache[str, Set[str]]" = TTLCache(
    maxsize=_MEMORY_DEDUP_MAX_ACTORS, ttl=_MEMORY_DEDUP_TTL_SECONDS
)


def _get_memory_client():
    """Get or create cached boto3 client for memory operations."""
    global _memory_client
    if _memory_client is None:
        _memory_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
    return _memory_client


def _get_existence_check_client():
    """Get or create the cached short-timeout client for the existence check."""
    global _existence_check_client
    if _existence_check_client is None:
        with _client_init_lock:
            if _existence_check_client is None:
                _existence_check_client = boto3.client(
                    "bedrock-agentcore", region_name=AWS_REGION, config=_EXISTENCE_CHECK_CLIENT_CONFIG
                )
    return _existence_check_client


def _reset_clients_for_test() -> None:
    """Reset cached boto3 clients. Intended for tests only."""
    global _memory_client, _existence_check_client
    _memory_client = None
    _existence_check_client = None


def _get_content_hash(actor_id: str, content: str) -> str:
    """Generate a deduplication hash from the actor id and normalized content.

    Normalization lowercases the content and collapses runs of whitespace, so
    casing and spacing variants of the same fact share one hash.
    """
    key = f"{actor_id}:{_normalize_content(content)}"
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


def _normalize_content(content: str) -> str:
    """Normalize fact content for equality: lowercase, whitespace-collapsed."""
    return re.sub(r"\s+", " ", content).strip().lower()


# Cap the cross-session existence check: a small topK keeps the read cheap and
# the records returned are short pattern-extracted facts.
_MEMORY_EXISTENCE_TOPK = 5


def _normalize_namespace(namespace: str) -> str:
    """Compare namespaces without leading or trailing slashes.

    Records written with ``namespaces=[actor_id]`` come back in that raw form in
    this deployment, but AgentCore Memory also documents a slash-delimited form
    (``/actor/``); stripping the slashes treats both spellings as one value while
    a child path (``actor/child``) stays distinct.
    """
    return namespace.strip("/")


def _store_has_content(client, actor_id: str, content: str) -> bool:
    """Best-effort check for an equal fact already in the actor's namespace.

    Uses ``retrieve_memory_records`` (the same read action used for retrieval)
    so no additional IAM action is required. The candidate fact is the search
    query; a returned record counts only when one of its ``namespaces`` equals
    the saving ``actor_id`` after both are normalized (the request ``namespace``
    is a prefix filter, so a child or sibling path can appear) and its
    normalized content matches. The check is best-effort, not exact: a duplicate
    can still slip through on approximate-search recall or before a freshly
    written record becomes searchable. Raises on a client error so the caller
    can fail open.
    """
    target = _normalize_content(content)
    target_namespace = _normalize_namespace(actor_id)
    response = client.retrieve_memory_records(
        memoryId=BEDROCK_AGENTCORE_MEMORY_ID,
        namespace=actor_id,
        searchCriteria={"searchQuery": content, "topK": _MEMORY_EXISTENCE_TOPK},
        maxResults=_MEMORY_EXISTENCE_TOPK,
    )
    for summary in response.get("memoryRecordSummaries", []):
        namespaces = summary.get("namespaces") or []
        if not any(isinstance(ns, str) and _normalize_namespace(ns) == target_namespace for ns in namespaces):
            continue
        record_content = summary.get("content") or {}
        text = record_content.get("text")
        if isinstance(text, str) and _normalize_content(text) == target:
            return True
    return False


def _write_confirmed(response: Dict, request_id: str) -> bool:
    """True only when the batch-create response confirms this request succeeded.

    A write is confirmed when the request is not listed among ``failedRecords``
    and at least one record succeeded. When the response echoes
    ``requestIdentifier`` values, the request's own identifier must be present
    in ``successfulRecords``; responses that omit the identifier are accepted on
    a non-empty ``successfulRecords`` with no matching failure. Any other
    outcome (listed as failed, or no successful records) is not confirmed.
    """
    failed = response.get("failedRecords") or []
    failed_ids = {r.get("requestIdentifier") for r in failed}
    if request_id in failed_ids:
        return False
    successful = response.get("successfulRecords") or []
    if not successful:
        return False
    successful_ids = {r.get("requestIdentifier") for r in successful if r.get("requestIdentifier") is not None}
    if successful_ids:
        return request_id in successful_ids
    # The response does not echo request identifiers; a non-empty success list
    # with no matching failure counts as confirmed.
    return True


def _is_duplicate_memory(actor_id: str, content: str) -> bool:
    """Return True if this fact was already saved for this actor in this process.

    Reads the shared dedup cache under :data:`_dedup_lock`.
    """
    content_hash = _get_content_hash(actor_id, content)
    with _dedup_lock:
        return actor_id in _saved_memory_hashes and content_hash in _saved_memory_hashes[actor_id]


def _mark_memory_saved(actor_id: str, content: str) -> None:
    """Mark a fact as saved for this actor to prevent future duplicates.

    Writes the shared dedup cache under :data:`_dedup_lock`.
    """
    content_hash = _get_content_hash(actor_id, content)
    with _dedup_lock:
        if actor_id not in _saved_memory_hashes:
            _saved_memory_hashes[actor_id] = set()
        _saved_memory_hashes[actor_id].add(content_hash)


def save_semantic_memory(actor_id: str, content: str, metadata: Optional[Dict] = None) -> bool:
    """
    Save a semantic memory record for long-term memory (LTM).

    Uses AWS SDK boto3 to call batch_create_memory_records API.
    Memories are scoped to actor_id namespace for cross-session retrieval.
    Duplicate content for the same actor is automatically skipped.

    Args:
        actor_id: User/actor identifier (namespace)
        content: Text content to save as memory
        metadata: Optional metadata dict

    Returns:
        bool: True if saved, already present, or being saved by a concurrent call
        in this process; False on error or an unconfirmed write
    """
    if not BEDROCK_AGENTCORE_MEMORY_ID:
        logger.warning("⚠️ Memory ID not configured, skipping semantic memory save")
        return False

    if contains_anonymized_value(content):
        logger.debug("Skipping long-term memory containing a sensitive identifier")
        return False

    # Fast path: this process already confirmed this fact for this actor.
    if _is_duplicate_memory(actor_id, content):
        logger.debug("♻️ Skipping duplicate memory save")
        return True  # Return True since this is expected behavior, not an error

    # In-flight guard: if another caller in this process is already writing this
    # exact fact, let it be the single writer. Claim the hash under the lock,
    # which is never held across a network call.
    content_hash = _get_content_hash(actor_id, content)
    with _dedup_lock:
        saved = _saved_memory_hashes.get(actor_id)
        if (saved and content_hash in saved) or content_hash in _inflight_hashes:
            logger.debug("♻️ Skipping duplicate memory: already saved or a concurrent save is in flight")
            return True
        _inflight_hashes.add(content_hash)

    try:
        try:
            write_client = _get_memory_client()
        except Exception as e:
            log_sanitized_exception(logger, "❌ Failed to save semantic memory", e)
            return False

        # Cross-session check: a fresh process starts with an empty cache, so the
        # same fact would otherwise be written again every session. Ask the store
        # whether an equal fact already exists in the actor's namespace. The read
        # uses a dedicated short-timeout client and fails open on any error, so an
        # unavailable or slow read never blocks a legitimate write.
        try:
            if _store_has_content(_get_existence_check_client(), actor_id, content):
                _mark_memory_saved(actor_id, content)
                logger.debug("♻️ Memory already present in store, skipping write")
                return True
        except Exception as e:
            # Best-effort check: a persistent failure silently disables dedupe,
            # so log at WARNING. Log only the exception class name — never the
            # message, fact text, or actor id.
            logger.warning(f"⚠️ Memory existence check failed, writing without it: {type(e).__name__}")

        try:
            request_id = str(uuid.uuid4())

            logger.info(f"💾 Saving semantic memory record (req {request_id[:8]})")

            # Create memory record using AWS SDK
            response = write_client.batch_create_memory_records(
                memoryId=BEDROCK_AGENTCORE_MEMORY_ID,
                records=[
                    {
                        "requestIdentifier": request_id,
                        "namespaces": [actor_id],  # Scoped to user
                        "content": {"text": content},
                        "timestamp": datetime.now(timezone.utc),  # Required field
                    }
                ],
            )

            # Mark saved only when the write is confirmed, so a rejected record is
            # retried on the next call instead of being treated as stored.
            if not _write_confirmed(response, request_id):
                logger.warning("⚠️ Semantic memory write not confirmed; will retry")
                return False

            _mark_memory_saved(actor_id, content)
            logger.info(f"✅ Semantic memory saved (req {request_id[:8]})")
            return True

        except Exception as e:
            log_sanitized_exception(logger, "❌ Failed to save semantic memory", e)
            return False
    finally:
        with _dedup_lock:
            _inflight_hashes.discard(content_hash)


# Words that are commonly mistaken for names in patterns like "I'm running" or "I am building"
_NON_NAME_WORDS = {
    # Common verbs
    "running",
    "building",
    "developing",
    "working",
    "using",
    "trying",
    "looking",
    "going",
    "making",
    "doing",
    "playing",
    "getting",
    "having",
    "being",
    "seeing",
    "coming",
    "taking",
    "finding",
    "giving",
    "telling",
    "asking",
    "starting",
    # Articles and common words
    "a",
    "an",
    "the",
    "here",
    "there",
    "not",
    "also",
    "just",
    "really",
    "very",
    "so",
    "too",
    "now",
    "still",
    "back",
    "new",
    "currently",
    "actually",
    # Adjectives often used with "I'm"
    "glad",
    "happy",
    "sorry",
    "sure",
    "ready",
    "able",
    "interested",
    "excited",
    "curious",
    "confused",
    "worried",
    "afraid",
    "certain",
    "unsure",
    # Tech/role words
    "game",
    "software",
    "backend",
    "frontend",
    "dev",
    "developer",
    "engineer",
    "admin",
    "user",
    "player",
    "manager",
    "owner",
    "operator",
}


def _is_likely_name(text: str) -> bool:
    """Check if extracted text is likely a real name, not a common word/verb."""
    if not text:
        return False
    # Check first word against non-name words
    first_word = text.split()[0].lower()
    return first_word not in _NON_NAME_WORDS


# Pre-compiled regex patterns for performance.
# The optional second word (for real surnames like "John Smith") must NOT be a
# common sentence connector, or "my name is Zephyr and I run X" would capture
# "Zephyr and". The negative lookahead blocks connectors; the trailing \b keeps
# genuine surnames that merely START with those letters (e.g. "Anderson", "Orwell").
_NAME_SECOND_WORD = r"(?:\s+(?!(?:and|or|but|who|the|is|a|an|i|we|from|here|there|too)\b)\w+)?"
_NAME_PATTERNS = [
    re.compile(rf"my name is (\w+{_NAME_SECOND_WORD})", re.IGNORECASE),
    re.compile(rf"i'm (\w+{_NAME_SECOND_WORD})", re.IGNORECASE),
    re.compile(rf"call me (\w+{_NAME_SECOND_WORD})", re.IGNORECASE),
    re.compile(rf"i am (\w+{_NAME_SECOND_WORD})", re.IGNORECASE),
]

# Game type/genre patterns - (pattern, label)
# Supports: "I run", "I'm building", "we develop", "we are running", "my/our game"
_GAME_TYPE_PATTERNS = [
    (
        re.compile(
            r"(?:i(?:'m| am) (?:building|running|developing)|i (?:build|run|develop)|we (?:are )?(?:building|running|developing)|we (?:build|run|develop)|(?:my|our))\s+(?:an?\s+)?(MMO|MMORPG|battle royale|survival|FPS|RTS|MOBA|multiplayer|racing|sports)\s*(?:game)?",
            re.IGNORECASE,
        ),
        "Game type",
    ),
    (
        re.compile(
            r"(?:my|our)\s+game\s+is\s+(?:an?\s+)?(MMO|MMORPG|battle royale|survival|FPS|RTS|MOBA|multiplayer)",
            re.IGNORECASE,
        ),
        "Game type",
    ),
]

# Session characteristics patterns
# Supports: "sessions last 2 hours", "sessions that last 45 min", "play for 30 minutes"
_SESSION_PATTERNS = [
    (
        re.compile(
            r"sessions?\s+(?:that\s+)?(?:typically |usually |about )?(?:last|duration|are)\s+(?:about |around )?(\d+)\s*(hours?|minutes?|mins?|hrs?)",
            re.IGNORECASE,
        ),
        "Session duration",
    ),
    (
        re.compile(
            r"(?:players?\s+)?(?:typically |usually )?play(?:s|time)?\s+(?:for\s+)?(?:about |around )?(\d+)\s*(hours?|minutes?|mins?|hrs?)",
            re.IGNORECASE,
        ),
        "Session duration",
    ),
    (
        re.compile(
            r"(?:have|with|about|around|support|handle)\s+(\d+)\s*(?:concurrent |active )?players?", re.IGNORECASE
        ),
        "Concurrent players",
    ),
]

# Infrastructure mapping patterns
_INFRASTRUCTURE_PATTERNS = [
    (
        re.compile(
            r"(?:my|our|the)\s+(\w+(?:\s+\w+)?)\s+(?:runs?\s+on|uses?|is\s+on)\s+(?:fleet[- ]?)([a-zA-Z0-9-]+)",
            re.IGNORECASE,
        ),
        "Game-to-fleet mapping",
    ),
    (
        re.compile(
            r"(?:my|our|the)\s+(\w+(?:\s+\w+)?)\s+(?:runs?\s+on|uses?|is\s+on)\s+(?:cluster[- ]?)([a-zA-Z0-9-]+)",
            re.IGNORECASE,
        ),
        "Game-to-cluster mapping",
    ),
]

# Platform choice patterns
_PLATFORM_PATTERNS = [
    (
        re.compile(r"(?:i |we |i'm |we're )?(?:use|using|run|running|on)\s+(GameLift|Agones|EKS)", re.IGNORECASE),
        "Platform",
    ),
]

# Scaling profile patterns
_SCALING_PATTERNS = [
    (
        re.compile(
            r"(?:we |i )?peak\s+(?:at\s+)?(\d+[kK]?)\s*(?:players?|users?|CCU)?(?:\s+(?:on|during)\s+(\w+))?",
            re.IGNORECASE,
        ),
        "Peak usage",
    ),
    (re.compile(r"off-?peak\s+(?:is\s+)?(?:during\s+)?(\w+)", re.IGNORECASE), "Off-peak time"),
]

# Instance/resource type patterns
_RESOURCE_PATTERNS = [
    (
        re.compile(
            r"(?:use|using|run|running)\s+([a-z]\d+\.(?:small|medium|large|xlarge|\d*xlarge)|graviton)", re.IGNORECASE
        ),
        "Instance type",
    ),
]


# Shapes of the values the input Guardrail anonymizes
# (infrastructure/cloudformation/04-bedrock-guardrails.yaml). Retrieved memory is
# sent to the model outside the input Guardrail, so a fact that carries one of
# these values is never stored: once stored, it would reach the model unmasked on
# every later turn. The first two mirror the template's regexes; the rest
# approximate the EMAIL, IP_ADDRESS and PHONE entities and err toward rejecting.
#
# Internal hostname: the template's `\b[a-z0-9-]+\.internal\b` with the label
# bounded at 63 characters, the DNS label limit. Every pattern here scans the
# whole message on each turn that stores memory, and the unbounded form restarts
# at every hyphen and scans to the end of the run, so a long hyphenated prompt
# would cost seconds of CPU. Every real hostname still matches.
_INTERNAL_HOSTNAME_PATTERN = re.compile(r"\b[a-z0-9-]{1,63}\.internal\b", re.IGNORECASE)
_ANONYMIZED_VALUE_PATTERNS = (
    # AWS account ID (template regex)
    re.compile(r"\b\d{12}\b"),
    # Internal hostname (template regex, label bounded; matched case-insensitively here)
    _INTERNAL_HOSTNAME_PATTERN,
    # Email address
    re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}"),
    # IPv4 address, optionally a CIDR block
    re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?!\d|\.\d)"),
    # IPv6, including compressed documentation-range addresses.
    re.compile(r"(?<![a-f0-9:])(?:[a-f0-9]{0,4}:){2,7}[a-f0-9]{0,4}(?![a-f0-9:])", re.IGNORECASE),
    # IPv4 address encoded in an EC2 private DNS label (ip-10-0-0-1)
    re.compile(r"\bip-\d{1,3}(?:-\d{1,3}){3}\b", re.IGNORECASE),
    # Phone number: 10 to 15 digits, optionally with a leading + and
    # space, dot, hyphen or parenthesis separators
    re.compile(r"(?<![\w+])\+?\(?\d(?:[ \t().-]{0,2}\d){9,14}(?!\w)"),
)


def contains_anonymized_value(text: str) -> bool:
    """Return True if ``text`` contains a value shaped like a Guardrail-anonymized entity."""
    return any(pattern.search(text) for pattern in _ANONYMIZED_VALUE_PATTERNS)


def _anonymized_value_spans(text: str) -> list:
    """Return the ``(start, end)`` spans of every anonymized-shape value in ``text``."""
    return [m.span() for pattern in _ANONYMIZED_VALUE_PATTERNS for m in pattern.finditer(text)]


def _fact_carries_anonymized_value(match: "re.Match[str]", memory_content: str, sensitive_spans: list) -> bool:
    """Return True if a candidate fact must not be stored.

    A fact is rejected when its formatted text contains an anonymized-shape
    value, or when any captured group overlaps such a value in the source
    message or begins immediately after a captured delimiter. The overlap check
    catches a capture that holds only part of the
    value (for example, ``[a-zA-Z0-9-]+`` stops at the first dot of an IP
    address or internal hostname).
    """
    if contains_anonymized_value(memory_content):
        return True
    for group in range(1, match.re.groups + 1):
        start, end = match.span(group)
        if start < 0:
            continue
        if any(start < s_end and s_start <= end for s_start, s_end in sensitive_spans):
            return True
    return False


def _format_game_type(match, label) -> str:
    """Format game type extraction."""
    return f"Game type: {match.group(1).upper()}"


def _format_session(match, label) -> str:
    """Format session characteristics extraction."""
    if "duration" in label.lower():
        return f"Session duration: {match.group(1)} {match.group(2)}"
    return f"Concurrent players: {match.group(1)}"


def _format_infrastructure(match, label) -> str:
    """Format infrastructure mapping extraction."""
    game = match.group(1).title()
    resource = match.group(2)
    resource_type = "fleet" if "fleet" in label.lower() else "cluster"
    return f"Game '{game}' runs on {resource_type}: {resource}"


def _format_platform(match, label) -> str:
    """Format platform choice extraction."""
    return f"Platform: {match.group(1)}"


def _format_scaling(match, label) -> str:
    """Format scaling profile extraction."""
    # Check for "off-peak" first since it contains "peak"
    if "off-peak" in label.lower():
        return f"Off-peak time: {match.group(1)}"
    if "peak" in label.lower():
        peak = match.group(1)
        time = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        return f"Peak players: {peak}" + (f" ({time})" if time else "")
    return None


def _format_resource(match, label) -> str:
    """Format instance/resource type extraction."""
    return f"Instance type: {match.group(1)}"


def extract_and_save_user_info(actor_id: str, user_message: str, ai_response: str) -> None:
    """
    Extract user and game information from conversation and save as semantic memory.

    Extracts all matching patterns from the message, including:
    - Identity: "My name is X", "I'm X", "Call me X"
    - Game type: "I run an MMO", "my battle royale game"
    - Session info: "sessions last 2 hours", "500 concurrent players"
    - Infrastructure: "my MMO runs on fleet-abc"
    - Platform: "we use GameLift", "running on Agones"
    - Scaling: "peak at 10k players on weekends"
    - Resources: "using c5.large instances"

    Args:
        actor_id: User identifier
        user_message: User's message
        ai_response: AI's response (reserved for future use)
    """
    logger.debug("🔍 Scanning message for user info")

    extracted_any = False
    saved_count = 0
    skipped_count = 0
    sensitive_spans = _anonymized_value_spans(user_message)

    # Extract name (keep natural sentence format for consistency)
    for pattern in _NAME_PATTERNS:
        match = pattern.search(user_message)
        if match:
            name = match.group(1).title()
            # Validate that extracted text is likely a real name
            if not _is_likely_name(name):
                logger.debug("ℹ️ Skipping false-positive name match")
                continue
            memory_content = f"User's name is {name}"
            if _fact_carries_anonymized_value(match, memory_content, sensitive_spans):
                skipped_count += 1
                continue
            success = save_semantic_memory(actor_id, memory_content)
            if success:
                saved_count += 1
            else:
                logger.warning("⚠️ Failed to save an extracted user name to LTM")
            extracted_any = True
            break  # Only extract one name

    # Extract game patterns (structured format)
    all_game_patterns = [
        (_GAME_TYPE_PATTERNS, _format_game_type),
        (_SESSION_PATTERNS, _format_session),
        (_INFRASTRUCTURE_PATTERNS, _format_infrastructure),
        (_PLATFORM_PATTERNS, _format_platform),
        (_SCALING_PATTERNS, _format_scaling),
        (_RESOURCE_PATTERNS, _format_resource),
    ]

    for patterns, formatter in all_game_patterns:
        for pattern, label in patterns:
            match = pattern.search(user_message)
            if match:
                memory_content = formatter(match, label)
                if memory_content and _fact_carries_anonymized_value(match, memory_content, sensitive_spans):
                    skipped_count += 1
                    continue
                if memory_content:
                    success = save_semantic_memory(actor_id, memory_content)
                    if success:
                        saved_count += 1
                    else:
                        logger.warning("⚠️ Failed to save an extracted fact to LTM")
                    extracted_any = True

    if skipped_count:
        # Count only: the skipped facts are exactly the values that must stay out of logs.
        logger.info(f"🛡️ Skipped {skipped_count} memory fact(s) shaped like a Guardrail-anonymized value")

    if extracted_any:
        logger.info(f"📝 Extracted and saved-or-already-present {saved_count} user info fact(s)")
    else:
        logger.debug("ℹ️ No user info patterns matched")
