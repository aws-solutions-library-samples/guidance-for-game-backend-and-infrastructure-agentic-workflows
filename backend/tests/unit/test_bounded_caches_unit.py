"""Unit tests asserting module-level caches are bounded (#126).

Previously these were plain dicts that grew without limit (rate-limit keys never
evicted, KB cache had no max size, per-actor dedup sets unbounded). They are now
cachetools.TTLCache instances with a maxsize cap + TTL eviction.
"""

# Third-party packages
import pytest
from cachetools import TTLCache

pytestmark = pytest.mark.unit


def test_rate_limit_windows_is_bounded():
    # Local modules
    from utils.security import _RATE_LIMIT_MAX_KEYS, _rate_limit_windows

    assert isinstance(_rate_limit_windows, TTLCache)
    assert _rate_limit_windows.maxsize == _RATE_LIMIT_MAX_KEYS


def test_kb_cache_is_bounded():
    # Local modules
    from utils.kb_tools import _KB_CACHE_MAX_ENTRIES, _kb_cache

    assert isinstance(_kb_cache, TTLCache)
    assert _kb_cache.maxsize == _KB_CACHE_MAX_ENTRIES


def test_memory_dedup_cache_is_bounded():
    # Local modules
    from utils.semantic_memory import _MEMORY_DEDUP_MAX_ACTORS, _saved_memory_hashes

    assert isinstance(_saved_memory_hashes, TTLCache)
    assert _saved_memory_hashes.maxsize == _MEMORY_DEDUP_MAX_ACTORS


def test_rate_limit_cache_evicts_beyond_maxsize():
    """A TTLCache must not exceed maxsize even under a flood of distinct keys."""
    # Local modules
    from utils.security import _rate_limit_windows

    cache = _rate_limit_windows
    start = len(cache)
    for i in range(cache.maxsize + 500):
        cache[f"floodkey-{i}"] = __import__("collections").deque()
    assert len(cache) <= cache.maxsize, "rate-limit cache must stay within maxsize"
    # cleanup the flood keys we added
    for i in range(cache.maxsize + 500):
        cache.pop(f"floodkey-{i}", None)
    _ = start
