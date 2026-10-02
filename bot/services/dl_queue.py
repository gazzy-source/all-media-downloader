"""
One fair download queue for every entry point (DM, groups/channels, inline).

The downloader's own semaphore made waiting jobs wait silently and in no
particular order. This queue hands out the same number of slots first-come
first-served (premium ahead of free), and tells each waiting job its place in
line whenever it changes, so users see "#3 in line" instead of a frozen bar.

Fair between people too: one user runs at most PER_USER jobs at a time (a
5-link group message, or a premium user pasting links, used to take every
slot); their next job waits while other users' jobs behind it go first.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import Counter
from typing import Any, Awaitable, Callable

from bot.config import MAX_CONCURRENT_DOWNLOADS

logger = logging.getLogger(__name__)

PositionCallback = Callable[[int], Awaitable[Any]]
PER_USER = 2


class DownloadQueue:
    def __init__(self, slots: int, per_user: int = PER_USER) -> None:
        self.slots = max(1, slots)
        self.per_user = max(1, per_user)
        self.running = 0
        self._running_by: Counter = Counter()
        # (priority rank, ticket, owner), kept sorted
        self._waiting: list[tuple[int, int, Any]] = []
        self._tickets = itertools.count()
        self._changed = asyncio.Event()
        self._notes: set[asyncio.Task] = set()

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    def _position(self, entry) -> int:
        return self._waiting.index(entry) + 1

    def _next(self):
        """The first waiting entry whose owner isn't already at their cap."""
        if self.running >= self.slots:
            return None
        for e in self._waiting:
            if e[2] is None or self._running_by[e[2]] < self.per_user:
                return e
        return None

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _tell(self, on_position: PositionCallback, pos: int) -> None:
        # Fired, not awaited: a slow Telegram edit must never hold this job
        # (or the free slot it is about to take) in line.
        async def note():
            try:
                await on_position(pos)
            except Exception:  # a status edit failing must not lose the job
                logger.debug("queue position update failed", exc_info=True)

        task = asyncio.ensure_future(note())
        self._notes.add(task)
        task.add_done_callback(self._notes.discard)

    async def run(
        self,
        job: Callable[[], Awaitable[Any]],
        *,
        on_position: PositionCallback | None = None,
        priority: bool = False,
        owner: Any = None,
    ) -> Any:
        entry = (0 if priority else 1, next(self._tickets), owner)
        self._waiting.append(entry)
        self._waiting.sort(key=lambda e: (e[0], e[1]))
        self._wake()  # positions behind a priority job just moved
        told = None
        try:
            while self._next() != entry:
                pos = self._position(entry)
                if on_position is not None and pos != told:
                    told = pos
                    self._tell(on_position, pos)
                changed = self._changed
                try:
                    await asyncio.wait_for(changed.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        except BaseException:
            if entry in self._waiting:
                self._waiting.remove(entry)
                self._wake()
            raise
        self._waiting.remove(entry)
        self.running += 1
        if owner is not None:
            self._running_by[owner] += 1
        self._wake()
        try:
            return await job()
        finally:
            self.running -= 1
            if owner is not None:
                self._running_by[owner] -= 1
                if self._running_by[owner] <= 0:
                    del self._running_by[owner]
            self._wake()


download_queue = DownloadQueue(MAX_CONCURRENT_DOWNLOADS)
