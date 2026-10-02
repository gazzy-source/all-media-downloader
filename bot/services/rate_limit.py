"""Per-user rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable

from bot.config import RATE_LIMIT_PER_HOUR


class RateLimiter:
    def __init__(self, max_per_hour: int = RATE_LIMIT_PER_HOUR) -> None:
        self.max_per_hour = max_per_hour
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        # Optional per-user limit (premium users get more); set at startup.
        self.limit_for: Callable[[int], int] | None = None

    def _limit(self, user_id: int) -> int:
        if self.limit_for is not None:
            try:
                return int(self.limit_for(user_id))
            except Exception:
                pass
        return self.max_per_hour

    def allow(self, user_id: int) -> tuple[bool, int]:
        """Return (allowed, seconds_until_reset)."""
        limit = self._limit(user_id)
        if limit <= 0:
            return True, 0  # 0 / negative = unlimited (it used to IndexError)
        now = time.time()
        window = 3600.0
        q = self._hits[user_id]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            retry = int(window - (now - q[0])) + 1
            return False, max(retry, 1)
        q.append(now)
        self._evict_idle(now)
        return True, 0

    def refund(self, user_id: int) -> None:
        """Give back the last download: the request cost the user nothing."""
        q = self._hits.get(user_id)
        if q:
            q.pop()

    def remaining(self, user_id: int) -> int:
        now = time.time()
        # Plain .get(): a read must not create an entry for an unknown user,
        # which would let /settings-style lookups grow the map without bound.
        limit = self._limit(user_id)
        q = self._hits.get(user_id)
        if q is None:
            return limit
        while q and now - q[0] > 3600:
            q.popleft()
        return max(0, limit - len(q))

    def _evict_idle(self, now: float) -> None:
        """Drop users whose whole window has aged out (long-running bot)."""
        if len(self._hits) < 512:
            return
        stale = [uid for uid, q in self._hits.items() if not q or now - q[-1] > 3600]
        for uid in stale:
            del self._hits[uid]


rate_limiter = RateLimiter()
