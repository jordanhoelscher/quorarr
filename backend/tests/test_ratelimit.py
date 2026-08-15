"""The login rate limiter must stay bounded.

Its key is the client IP, which is attacker-influenced behind a proxy, so an
unbounded dict of keys is a memory-exhaustion primitive on the one public
write endpoint. The limiter caps how many keys it tracks, drops keys whose
window has emptied, and then evicts the least recently seen key.
"""

from pensieve.ratelimit import MAX_KEYS, RateLimiter


def test_tracked_keys_never_exceed_the_cap():
    limiter = RateLimiter(limit=5, window=60.0)
    for i in range(MAX_KEYS + 500):
        limiter.check(f"10.0.0.{i}")
    assert len(limiter._hits) <= MAX_KEYS


def test_eviction_drops_the_least_recently_seen_key():
    limiter = RateLimiter(limit=5, window=60.0)
    limiter.check("oldest")
    for i in range(MAX_KEYS):
        limiter.check(f"10.0.0.{i}")
    assert "oldest" not in limiter._hits
    assert "10.0.0.0" in limiter._hits


def test_keys_whose_window_emptied_are_forgotten():
    # window=0 means every recorded hit is already outside the window by the
    # time the next check runs, so the cap sweep should clear essentially all
    # of them rather than evicting one key at a time.
    limiter = RateLimiter(limit=5, window=0.0)
    for i in range(MAX_KEYS + 1):
        limiter.check(f"10.0.0.{i}")
    assert len(limiter._hits) <= 1


def test_limit_still_enforced_within_the_window():
    limiter = RateLimiter(limit=2, window=60.0)
    assert limiter.check("1.2.3.4") is True
    assert limiter.check("1.2.3.4") is True
    assert limiter.check("1.2.3.4") is False
