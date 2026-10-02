"""
One fair download queue for every entry point (DM, groups/channels, inline).

The downloader's own semaphore made waiting jobs wait silently and in no
particular order. This queue hands out the same number of slots first-come
first-served (premium ahead of free), and tells each waiting job its place in
line whenever it changes, so users see "#3 in line" instead of a frozen bar.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Awaitable, Callable

from bot.config import MAX_CONCURRENT_DOWNLOADS

logger = logging.getLogger(__name__)

PositionCallback = Callable[[int], Awaitable[Any]]


class DownloadQueue:
    def __init__(self, slots: int) -> None:
        self.slots = max(1, slots)
        self.running = 0
        self._waiting: list[tuple[int, int]] = []  # (priority rank, ticket), sorted
        self._tickets = itertools.count()
        self._changed = asyncio.Event()

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    def _position(self, entry: tuple[int, int]) -> int:
        return self._waiting.index(entry) + 1

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def run(
        self,
        job: Callable[[], Awaitable[Any]],
        *,
        on_position: PositionCallback | None = None,
        priority: bool = False,
    ) -> Any:
        entry = (0 if priority else 1, next(self._tickets))
        self._waiting.append(entry)
        self._waiting.sort()
        self._wake()  # positions behind a priority job just moved
        told = None
        try:
            while not (self._waiting and self._waiting[0] == entry and self.running < self.slots):
                pos = self._position(entry)
                if on_position is not None and pos != told:
                    told = pos
                    try:
                        await on_position(pos)
                    except Exception:  # a status edit failing must not lose the job
                        logger.debug("queue position update failed", exc_info=True)
                    continue  # state may have changed while we were editing
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
        self._wake()
        try:
            return await job()
        finally:
            self.running -= 1
            self._wake()


download_queue = DownloadQueue(MAX_CONCURRENT_DOWNLOADS)
