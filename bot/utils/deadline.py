"""
A wall-clock limit (and a cancel switch) for blocking network work in a
worker thread.

yt-dlp's socket_timeout bounds each READ, not the whole job: a server that
drips one byte every few seconds kept an extraction going for hours, and
asyncio.wait_for in the handlers only stops the await — the thread runs on,
holding a worker. Four such links froze "Reading link…" for every user.

`with deadline(seconds, cancel=event):` makes every socket read in THIS thread
raise DeadlineExceeded once the time is up or the event is set. Other threads
(the event loop, Telegram, other jobs) are untouched: the check is a
thread-local lookup, nothing more.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from contextlib import contextmanager
from typing import Iterator

_local = threading.local()


class DeadlineExceeded(TimeoutError):
    pass


def check() -> None:
    """Raise if this thread's deadline has passed or its job was cancelled."""
    d = getattr(_local, "d", None)
    if d is None:
        return
    until, cancel = d
    if cancel is not None and cancel.is_set():
        raise DeadlineExceeded("Cancelled")
    if time.monotonic() > until:
        raise DeadlineExceeded("timed out (job took too long)")


@contextmanager
def deadline(seconds: float, cancel: threading.Event | None = None) -> Iterator[None]:
    prev = getattr(_local, "d", None)
    until = time.monotonic() + seconds
    if prev is not None:
        until = min(until, prev[0])  # a nested limit can only be tighter
        cancel = cancel or prev[1]
    _local.d = (until, cancel)
    try:
        yield
    finally:
        _local.d = prev


def _guarded(orig):
    def method(self, *args, **kwargs):
        check()
        return orig(self, *args, **kwargs)

    method.__name__ = getattr(orig, "__name__", "recv")
    method._deadline_guard = True
    return method


def install() -> None:
    """Idempotent: wrap the read paths of plain and TLS sockets."""
    for cls in (socket.socket, ssl.SSLSocket):
        for name in ("recv", "recv_into", "connect"):
            orig = cls.__dict__.get(name) or getattr(cls, name)
            if getattr(orig, "_deadline_guard", False):
                continue
            setattr(cls, name, _guarded(orig))


install()
