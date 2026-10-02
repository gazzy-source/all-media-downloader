"""
Running downloads a user can cancel (the ✖ Cancel button, or /cancel).

A job waiting in the queue is simply withdrawn. One already downloading is
stopped at its next chunk or extraction step through a threading.Event the
downloader checks — a worker thread can't be killed, but it can be told to
stop. Once the file is being sent to Telegram it is too late to cancel.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class Job:
    def __init__(self, key: str, owner: int) -> None:
        self.key = key
        self.owner = owner
        self.event = threading.Event()
        self.started = False  # has a download slot (no longer just queued)
        self.cancellable = True  # False once the file is being sent
        self.task: asyncio.Future | None = None
        self.on_cancel: Callable[[], Awaitable[None]] | None = None

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()


_JOBS: dict[str, Job] = {}
_BY_OWNER: dict[int, str] = {}


def start(key: str, owner: int, *, by_owner: bool = False) -> Job:
    job = Job(key, owner)
    _JOBS[key] = job
    if by_owner:  # /cancel finds the user's DM job through this
        _BY_OWNER[owner] = key
    return job


def get(key: str) -> Job | None:
    return _JOBS.get(key)


def drop(job: Job) -> None:
    if _JOBS.get(job.key) is job:
        del _JOBS[job.key]
    if _BY_OWNER.get(job.owner) == job.key:
        del _BY_OWNER[job.owner]


def key_for_owner(owner: int) -> str | None:
    return _BY_OWNER.get(owner)


async def cancel(key: str | None, user_id: int) -> str:
    """'ok' | 'gone' (finished or unknown) | 'not_owner' | 'late' (sending)."""
    job = _JOBS.get(key or "")
    if job is None:
        return "gone"
    if job.owner != user_id:
        return "not_owner"
    if not job.cancellable:
        return "late"
    if not job.cancelled:
        job.event.set()
        if not job.started and job.task is not None:
            job.task.cancel()  # still queued: just leave the queue
        logger.info("job %s cancelled by its owner", key)
        if job.on_cancel is not None:
            try:
                await job.on_cancel()
            except Exception:
                logger.debug("cancel notice failed", exc_info=True)
    return "ok"


async def run_queued(job: Job, queue, factory: Callable[[], Awaitable[Any]], **kw) -> Any:
    """
    queue.run(...) in a task the Cancel button can withdraw while it waits.
    Raises asyncio.CancelledError when cancelled before getting a slot.
    """
    async def go():
        job.started = True
        return await factory()

    job.task = asyncio.ensure_future(queue.run(go, **kw))
    return await job.task


CANCEL_ANSWERS = {
    "ok": "✖ Cancelled",
    "gone": "This one has already finished.",
    "not_owner": "Only the person who started it can cancel it.",
    "late": "Almost done — it's being sent now.",
}
