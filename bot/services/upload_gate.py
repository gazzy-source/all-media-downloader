"""
At most UPLOAD_MAX_CONCURRENT uploads to Telegram at once, and at most
UPLOAD_BUDGET_MB of files in flight.

Uploads stream from disk (see _send_media_once), so this guards bandwidth, the
16-connection pool to Telegram and flood limits rather than RAM. It is taken
only AFTER a job's download slot is released, so waiting to send never holds
download capacity, and it is held across the whole retry loop, so a retry
never loses its place to newer uploads.

    async with upload_gate.reserve(size, cancel_event):
        ...send, with retries...

Strict FIFO: only the head of the line is ever admitted, so a big upload can't
be overtaken forever by small ones. A file bigger than the budget costs the
whole budget, i.e. it runs alone instead of never running.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque

from bot.config import UPLOAD_BUDGET_BYTES, UPLOAD_MAX_CONCURRENT

logger = logging.getLogger(__name__)

_CANCEL_POLL = 0.25  # seconds: how soon a waiter notices its job was cancelled


class UploadCancelled(Exception):
    """The job was cancelled while waiting for upload capacity (nothing was sent)."""


class _Waiter:
    __slots__ = ("cost", "future")

    def __init__(self, cost: int, future: asyncio.Future) -> None:
        self.cost = cost
        self.future = future


class UploadGate:
    def __init__(self, max_concurrent: int = UPLOAD_MAX_CONCURRENT,
                 budget_bytes: int = UPLOAD_BUDGET_BYTES) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.budget_bytes = max(1, int(budget_bytes))
        self.running_uploads = 0
        self.in_flight_bytes = 0
        self._waiters: deque[_Waiter] = deque()

    @property
    def waiters(self) -> int:
        return len(self._waiters)

    def cost_of(self, size: int | None) -> int:
        return max(0, min(int(size or 0), self.budget_bytes))

    def _fits(self, cost: int) -> bool:
        return (self.running_uploads < self.max_concurrent
                and self.in_flight_bytes + cost <= self.budget_bytes)

    def _admit(self, cost: int) -> None:
        self.running_uploads += 1
        self.in_flight_bytes += cost

    def _release(self, cost: int) -> None:
        self.running_uploads -= 1
        self.in_flight_bytes -= cost
        self._wake()

    def _wake(self) -> None:
        """Admit waiters from the head of the line while they fit (FIFO)."""
        while self._waiters and self._fits(self._waiters[0].cost):
            w = self._waiters.popleft()
            if w.future.done():  # its task was cancelled meanwhile
                continue
            self._admit(w.cost)
            w.future.set_result(None)

    def reserve(self, size: int | None,
                cancel: threading.Event | None = None) -> "_Reservation":
        return _Reservation(self, self.cost_of(size), cancel)


class _Reservation:
    """One upload's place in the gate; released exactly once in __aexit__."""

    def __init__(self, gate: UploadGate, cost: int, cancel: threading.Event | None) -> None:
        self._gate = gate
        self.cost = cost
        self._cancel = cancel
        self._held = False

    async def __aenter__(self) -> "_Reservation":
        gate = self._gate
        if self._cancel is not None and self._cancel.is_set():
            raise UploadCancelled
        # Straight in only when nobody is waiting: no overtaking the line.
        if not gate._waiters and gate._fits(self.cost):
            gate._admit(self.cost)
            self._held = True
            return self
        waiter = _Waiter(self.cost, asyncio.get_running_loop().create_future())
        gate._waiters.append(waiter)
        logger.info("upload waiting for capacity (running=%s, in flight=%.0f MB, ahead=%s)",
                    gate.running_uploads, gate.in_flight_bytes / 2**20, len(gate._waiters) - 1)
        try:
            while True:
                # Checked before done(): a job cancelled in the instant it was
                # admitted must not start sending (the except gives the slot back).
                if self._cancel is not None and self._cancel.is_set():
                    raise UploadCancelled
                if waiter.future.done():
                    break
                # asyncio.wait never cancels the future itself on timeout.
                await asyncio.wait({waiter.future}, timeout=_CANCEL_POLL)
        except BaseException:
            if waiter.future.done() and not waiter.future.cancelled():
                # Admitted in the same instant it was cancelled: give it back.
                gate._release(self.cost)
            else:
                waiter.future.cancel()
                try:
                    gate._waiters.remove(waiter)
                except ValueError:
                    pass
                gate._wake()  # it may have been the head blocking the others
            raise
        self._held = True
        return self

    async def __aexit__(self, *exc) -> None:
        if self._held:
            self._held = False
            self._gate._release(self.cost)


upload_gate = UploadGate()
