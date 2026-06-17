"""
Knowledge Base tools for Strands agents.

Wraps strands_tools.retrieve with proper @tool decorator for Strands compatibility.

Performance Optimization:
- KB retrieval results are cached with configurable TTL
- Reduces redundant AWS API calls for repeated queries
- Thread-safe cache implementation
"""

# Standard library
import hashlib
import threading
import time
from typing import Any, Dict, Optional, Tuple

# Third-party packages
from strands import tool
from strands_tools.retrieve import retrieve as _retrieve_impl

# Local modules
from config.settings import RETRY_BASE_DELAY, RETRY_MAX_ATTEMPTS
from utils.logger import logger
from utils.resilience import retry_with_backoff

# KB result cache configuration
_KB_CACHE_TTL_SECONDS = 3600  # 1 hour cache TTL
_kb_cache: Dict[str, Tuple[float, Any]] = {}  # {cache_key: (timestamp, result)}
_kb_cache_lock = threading.Lock()


def _get_cache_key(kb_id: str, text: str, num_results: int, score: float) -> str:
    """Generate a cache key for KB query."""
    key_data = f"{kb_id}:{text}:{num_results}:{score}"
    return hashlib.md5(key_data.encode(), usedforsecurity=False).hexdigest()


def _get_cached_result(cache_key: str) -> Optional[Any]:
    """Get cached result if valid, None if expired or not found."""
    with _kb_cache_lock:
        if cache_key in _kb_cache:
            timestamp, result = _kb_cache[cache_key]
            if time.time() - timestamp < _KB_CACHE_TTL_SECONDS:
                logger.debug(f"♻️ KB cache hit: {cache_key[:8]}...")
                return result
            else:
                # Expired, remove from cache
                del _kb_cache[cache_key]
    return None


def _set_cached_result(cache_key: str, result: Any) -> None:
    """Store result in cache."""
    with _kb_cache_lock:
        _kb_cache[cache_key] = (time.time(), result)
        logger.debug(f"💾 KB result cached: {cache_key[:8]}...")


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
    Retrieve relevant documentation from Bedrock Knowledge Base.

    Searches the knowledge base using semantic similarity and returns relevant document chunks.
    Automatically uses KNOWLEDGE_BASE_ID from environment if knowledgeBaseId is not provided.

    Args:
        text: The query text to search for
        numberOfResults: Maximum number of results to return (default: 3)
        knowledgeBaseId: KB ID (optional, uses env var if not provided)
        region: AWS region (default: us-west-2)
        score: Minimum relevance score 0.0-1.0 (default: 0.5)
        profile_name: AWS profile name (optional)
        enableMetadata: Include metadata in response (default: False)

    Returns:
        Retrieved documentation content
    """
    # Call the underlying retrieve implementation
    tool_use = {
        "toolUseId": "kb-retrieve",
        "input": {
            "text": text,
            "numberOfResults": numberOfResults,
            "knowledgeBaseId": knowledgeBaseId,
            "region": region,
            "score": score,
            "profile_name": profile_name,
            "enableMetadata": enableMetadata,
        },
    }

    return _retrieve_impl(tool_use)


def create_kb_retrieve_tool(kb_id: str, region: str = "us-west-2"):
    """
    Create a KB retrieve tool bound to a specific knowledge base.

    Args:
        kb_id: Knowledge Base ID
        region: AWS region (default: us-west-2)

    Returns:
        A tool function bound to the specified KB
    """

    @tool
    def kb_retrieve(text: str, numberOfResults: int = 3, score: float = 0.5):
        """
        Retrieve relevant documentation from the knowledge base.

        Results are cached for 1 hour to reduce redundant API calls.

        Args:
            text: The query text to search for
            numberOfResults: Maximum number of results to return (default: 3)
            score: Minimum relevance score 0.0-1.0 (default: 0.5)

        Returns:
            Retrieved documentation content
        """
        # Check cache first
        cache_key = _get_cache_key(kb_id, text, numberOfResults, score)
        cached = _get_cached_result(cache_key)
        if cached is not None:
            return cached

        # Cache miss - call API with retry
        tool_use = {
            "toolUseId": "kb-retrieve",
            "input": {
                "text": text,
                "numberOfResults": numberOfResults,
                "knowledgeBaseId": kb_id,
                "region": region,
                "score": score,
                "profile_name": None,
                "enableMetadata": False,
            },
        }

        @retry_with_backoff(max_attempts=RETRY_MAX_ATTEMPTS, base_delay=RETRY_BASE_DELAY)
        def _call_kb():
            return _retrieve_impl(tool_use)

        result = _call_kb()

        # Cache the result
        _set_cached_result(cache_key, result)

        return result

    return kb_retrieve


def clear_kb_cache() -> None:
    """Clear the KB result cache. Useful for testing or forcing refresh."""
    with _kb_cache_lock:
        _kb_cache.clear()
        logger.debug("🗑️ KB cache cleared")


def get_kb_cache_stats() -> Dict[str, Any]:
    """Get KB cache statistics."""
    with _kb_cache_lock:
        valid_count = 0
        expired_count = 0
        current_time = time.time()

        for cache_key, (timestamp, _) in _kb_cache.items():
            if current_time - timestamp < _KB_CACHE_TTL_SECONDS:
                valid_count += 1
            else:
                expired_count += 1

        return {
            "total_entries": len(_kb_cache),
            "valid_entries": valid_count,
            "expired_entries": expired_count,
            "ttl_seconds": _KB_CACHE_TTL_SECONDS,
        }
