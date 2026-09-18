"""Per-user rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from bot.config import RATE_LIMIT_PER_HOUR


class RateLimiter:
    def __init__(self, max_per_hour: int = RATE_LIMIT_PER_HOUR) -> None:
        self.max_per_hour = max_per_hour
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> tuple[bool, int]:
        """Return (allowed, seconds_until_reset)."""
        now = time.time()
        window = 3600.0
        q = self._hits[user_id]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= self.max_per_hour:
            retry = int(window - (now - q[0])) + 1
            return False, max(retry, 1)
        q.append(now)
        self._evict_idle(now)
        return True, 0

    def remaining(self, user_id: int) -> int:
        now = time.time()
        # Plain .get(): a read must not create an entry for an unknown user,
        # which would let /settings-style lookups grow the map without bound.
        q = self._hits.get(user_id)
        if q is None:
            return self.max_per_hour
        while q and now - q[0] > 3600:
            q.popleft()
        return max(0, self.max_per_hour - len(q))

    def _evict_idle(self, now: float) -> None:
        """Drop users whose whole window has aged out (long-running bot)."""
        if len(self._hits) < 512:
            return
        stale = [uid for uid, q in self._hits.items() if not q or now - q[-1] > 3600]
        for uid in stale:
            del self._hits[uid]


rate_limiter = RateLimiter()
