#!/usr/bin/env python3
"""Unit test for PR #319 review finding #9 — keep rate-limit data for the complete window.

The connector permits a rate-limit window up to ``_RATE_WINDOW_MAX`` (86,400s / 24h), but
the shared sliding-window store (``utils.security._rate_limit_windows``) is a ``TTLCache``.
If a key's TTL were shorter than the window, the key would be evicted mid-window and the
next request would start a FRESH window — letting a caller exceed the configured quota per
window. The fix makes the cache key TTL cover at least the longest permitted window.

This test (a) pins the invariant ``key TTL >= max permitted window`` and (b) functionally
proves that, with a configured window longer than one hour, the Nth+1 request within the
window is still rejected — i.e. the quota holds for the complete window even after more than
an hour has elapsed. A controllable timer drives both the sliding-window deque clock and the
TTLCache expiry so the test is deterministic and makes no real ``sleep``.

Validates: PR #319 review finding 9.
"""

# Standard library
import collections

# Third-party packages
import pytest
from cachetools import TTLCache

# Local modules
import utils.security as security
from connector.config import _RATE_WINDOW_MAX
from utils.security import RateLimitExceeded, check_rate_limit, get_rate_limit_key

pytestmark = pytest.mark.unit


def test_key_ttl_covers_the_longest_permitted_window():
    """The rate-limit key TTL is at least the longest permitted rate-limit window."""
    assert security._RATE_LIMIT_KEY_TTL_SECONDS >= _RATE_WINDOW_MAX, (
        "rate-limit key TTL must cover the longest permitted window so a key is not evicted "
        "mid-window (which would reset the caller's quota)"
    )


def test_quota_holds_across_a_window_longer_than_one_hour(monkeypatch):
    """With a >1h window, the quota is still enforced after more than an hour has elapsed.

    A controllable clock drives both the sliding-window eviction (``_time.monotonic``) and
    the TTLCache expiry (its ``timer``). The cache is created with the module's real TTL
    (which the fix set to cover the max window), so if that TTL were shorter than the window
    the key would expire between requests and the final request would wrongly be allowed.
    """
    clock = {"now": 1000.0}

    def _fake_monotonic() -> float:
        return clock["now"]

    # Drive the deque's expiry clock.
    monkeypatch.setattr(security._time, "monotonic", _fake_monotonic)

    # Replace the shared window store with a fresh cache using the SAME (fixed) TTL but a
    # timer we control, so expiry is deterministic and tied to the same clock.
    fresh_cache: "TTLCache[str, collections.deque]" = TTLCache(
        maxsize=security._RATE_LIMIT_MAX_KEYS,
        ttl=security._RATE_LIMIT_KEY_TTL_SECONDS,
        timer=_fake_monotonic,
    )
    monkeypatch.setattr(security, "_rate_limit_windows", fresh_cache)

    key = get_rate_limit_key("reader-1", "scm_read")
    max_requests = 5
    window_seconds = 7200  # 2 hours — longer than the old 3600s TTL

    # Consume the full quota at the start of the window.
    for _ in range(max_requests):
        check_rate_limit(key, max_requests, window_seconds)

    # Advance well past one hour but still inside the 2-hour window.
    clock["now"] += 5400.0  # +90 minutes

    # The key must NOT have been evicted, so its window still holds all prior timestamps and
    # the next request is rejected — the quota is preserved for the complete window.
    with pytest.raises(RateLimitExceeded):
        check_rate_limit(key, max_requests, window_seconds)

    # After the full window elapses, the oldest timestamps age out and a request is allowed
    # again (sliding-window correctness is retained).
    clock["now"] += window_seconds
    check_rate_limit(key, max_requests, window_seconds)


def test_minimum_key_ttl_matches_the_connector_window_bound():
    """The TTL floor in utils.security stays equal to the connector's longest window."""
    assert security._RATE_LIMIT_MIN_KEY_TTL_SECONDS == _RATE_WINDOW_MAX


@pytest.mark.parametrize("override", ["60", "0", "-5", "not-a-number", ""])
def test_a_short_or_invalid_ttl_override_is_raised_to_the_longest_window(monkeypatch, override):
    """GBAW_RATE_LIMIT_KEY_TTL_SECONDS cannot lower the key TTL below the longest window."""
    monkeypatch.setenv("GBAW_RATE_LIMIT_KEY_TTL_SECONDS", override)
    assert security._resolve_rate_limit_key_ttl_seconds() == _RATE_WINDOW_MAX


def test_a_longer_ttl_override_is_kept(monkeypatch):
    """The override can still raise the TTL above the floor."""
    monkeypatch.setenv("GBAW_RATE_LIMIT_KEY_TTL_SECONDS", str(_RATE_WINDOW_MAX * 2))
    assert security._resolve_rate_limit_key_ttl_seconds() == _RATE_WINDOW_MAX * 2


def test_quota_survives_a_short_ttl_override(monkeypatch):
    """With a 60s TTL override and a 3,600s window, the sixth request after 61s is rejected.

    This reproduces the review finding: the active quota must survive even when the
    operator configures a TTL shorter than the window.
    """
    monkeypatch.setenv("GBAW_RATE_LIMIT_KEY_TTL_SECONDS", "60")
    clock = {"now": 1000.0}

    def _fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(security._time, "monotonic", _fake_monotonic)
    cache: "TTLCache[str, collections.deque]" = TTLCache(
        maxsize=security._RATE_LIMIT_MAX_KEYS,
        ttl=security._resolve_rate_limit_key_ttl_seconds(),
        timer=_fake_monotonic,
    )
    monkeypatch.setattr(security, "_rate_limit_windows", cache)

    key = get_rate_limit_key("reader-1", "scm_read")
    for _ in range(5):
        check_rate_limit(key, 5, 3600)

    clock["now"] += 61.0
    with pytest.raises(RateLimitExceeded):
        check_rate_limit(key, 5, 3600)


def test_an_active_key_is_not_evicted_at_its_first_insertion_deadline(monkeypatch):
    """Expiry follows the last accepted request, not the key's first insertion.

    TTLCache sets expiry only on assignment. Without the write-back in
    check_rate_limit, a key first used at t0 is dropped at t0 + TTL even while its
    caller is mid-burst, and the next request starts an empty window.
    """
    clock = {"now": 1000.0}

    def _fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(security._time, "monotonic", _fake_monotonic)
    ttl = security._RATE_LIMIT_KEY_TTL_SECONDS
    cache: "TTLCache[str, collections.deque]" = TTLCache(
        maxsize=security._RATE_LIMIT_MAX_KEYS, ttl=ttl, timer=_fake_monotonic
    )
    monkeypatch.setattr(security, "_rate_limit_windows", cache)

    key = get_rate_limit_key("reader-1", "scm_read")
    check_rate_limit(key, 5, 3600)  # first insertion at t0

    clock["now"] += ttl - 10.0  # 10s before the first-insertion deadline; t0 has aged out
    for _ in range(5):
        check_rate_limit(key, 5, 3600)  # fill the quota for this window

    clock["now"] += 20.0  # 10s past t0 + TTL, still inside this window
    with pytest.raises(RateLimitExceeded):
        check_rate_limit(key, 5, 3600)
