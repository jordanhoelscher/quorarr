"""In-memory sliding-window rate limiter.

Single-process, in-memory by design: this app is documented to run as one
container with no shared cache, and the only thing being rate-limited is the
login endpoint. Running it with more than one worker splits the window per
worker and multiplies the effective limit -- don't.

The key is a client IP, which is only as trustworthy as the proxy chain in
front of it -- so the number of tracked keys is capped rather than assumed
bounded. Without the cap, anyone who can vary the key (a spoofed
``X-Forwarded-For``) grows this dict forever from an unauthenticated
endpoint.
"""

import time

#: Maximum distinct keys tracked at once, past which ``_evict`` runs.
MAX_KEYS = 5000


class RateLimiter:
    """Sliding-window rate limiter keyed by an arbitrary string (e.g. client IP)."""

    def __init__(self, limit: int = 10, window: float = 60.0) -> None:
        """Configure the limiter.

        Args:
            limit: Maximum allowed hits per key within ``window`` seconds.
            window: Sliding window size in seconds.
        """
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """Record a hit for ``key`` and report whether it's still within limit.

        Args:
            key: Identifier to rate-limit on (e.g. client IP).

        Returns:
            True if this hit keeps ``key`` within the configured limit,
            False if it exceeds it.
        """
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > MAX_KEYS:
            self._evict(now)
        return len(hits) <= self.limit

    def _evict(self, now: float) -> None:
        """Bring ``_hits`` back to at most ``MAX_KEYS`` entries.

        First drop every key whose window has emptied -- those carry no
        information at all. If that isn't enough (a genuine flood of live
        keys), drop whichever key was seen least recently until we're back
        under the cap.
        """
        self._hits = {
            k: hits for k, hits in self._hits.items()
            if hits and now - hits[-1] < self.window
        }
        while len(self._hits) > MAX_KEYS:
            del self._hits[min(self._hits, key=lambda k: self._hits[k][-1])]


auth_limiter = RateLimiter()

#: Requests filed from the Discover tab, keyed by Plex account id (not IP --
#: this endpoint is session-gated, so the account is the honest identity).
#: A cap rather than a throttle: 20 titles an hour is far past normal use and
#: well short of what it takes to fill a disk by accident or by malice.
request_limiter = RateLimiter(limit=20, window=3600.0)
