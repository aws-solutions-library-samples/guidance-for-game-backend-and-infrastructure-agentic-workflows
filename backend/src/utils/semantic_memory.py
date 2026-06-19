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
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Set

# Third-party packages
import boto3
from cachetools import TTLCache

# Local modules
from config.settings import AWS_REGION, BEDROCK_AGENTCORE_MEMORY_ID, BOTO3_CLIENT_CONFIG
from utils.logger import logger

# Module-level boto3 client cache for performance
_memory_client = None

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


def _get_content_hash(actor_id: str, content: str) -> str:
    """Generate a hash for deduplication. Normalizes content to lowercase."""
    key = f"{actor_id}:{content.lower().strip()}"
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


def _is_duplicate_memory(actor_id: str, content: str) -> bool:
    """Check if this memory was already saved for this actor."""
    content_hash = _get_content_hash(actor_id, content)
    return actor_id in _saved_memory_hashes and content_hash in _saved_memory_hashes[actor_id]


def _mark_memory_saved(actor_id: str, content: str) -> None:
    """Mark a memory as saved to prevent future duplicates."""
    content_hash = _get_content_hash(actor_id, content)
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
        bool: True if successful (or skipped as duplicate), False on error
    """
    if not BEDROCK_AGENTCORE_MEMORY_ID:
        logger.warning("⚠️ Memory ID not configured, skipping semantic memory save")
        return False

    # Deduplication check
    if _is_duplicate_memory(actor_id, content):
        logger.debug(f"♻️ Skipping duplicate memory: actor={actor_id}, content='{content[:30]}...'")
        return True  # Return True since this is expected behavior, not an error

    try:
        client = _get_memory_client()
        request_id = str(uuid.uuid4())

        logger.info(f"💾 Saving semantic memory: actor={actor_id}, content='{content[:50]}...'")

        # Create memory record using AWS SDK
        response = client.batch_create_memory_records(
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

        # Mark as saved after successful API call
        _mark_memory_saved(actor_id, content)
        logger.info(f"✅ Semantic memory saved: id={request_id[:8]}, actor={actor_id}")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to save semantic memory: {e}")
        return False


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


# Pre-compiled regex patterns for performance
_NAME_PATTERNS = [
    re.compile(r"my name is (\w+(?:\s+\w+)?)", re.IGNORECASE),
    re.compile(r"i'm (\w+(?:\s+\w+)?)", re.IGNORECASE),
    re.compile(r"call me (\w+(?:\s+\w+)?)", re.IGNORECASE),
    re.compile(r"i am (\w+(?:\s+\w+)?)", re.IGNORECASE),
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
    logger.debug(f"🔍 Scanning for user info in: '{user_message[:100]}...'")

    extracted_any = False

    # Extract name (keep natural sentence format for consistency)
    for pattern in _NAME_PATTERNS:
        match = pattern.search(user_message)
        if match:
            name = match.group(1).title()
            # Validate that extracted text is likely a real name
            if not _is_likely_name(name):
                logger.debug(f"ℹ️ Skipping false positive name: '{name}'")
                continue
            memory_content = f"User's name is {name}"
            logger.info(f"📝 Extracted user name: {name} (actor: {actor_id})")
            success = save_semantic_memory(actor_id, memory_content)
            if success:
                logger.info(f"✅ User name '{name}' saved to LTM for actor {actor_id}")
            else:
                logger.warning(f"⚠️ Failed to save user name to LTM")
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
                if memory_content:
                    logger.info(f"📝 Extracted {label}: {memory_content} (actor: {actor_id})")
                    success = save_semantic_memory(actor_id, memory_content)
                    if success:
                        logger.info(f"✅ {label} saved to LTM for actor {actor_id}")
                    else:
                        logger.warning(f"⚠️ Failed to save {label} to LTM")
                    extracted_any = True

    if not extracted_any:
        logger.debug(f"ℹ️ No patterns matched in message")
