"""Ensure only one bot process runs at a time (avoids duplicate Telegram replies)."""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_lock_fh = None


def acquire_single_instance(lock_path: Path) -> None:
    """
    Exclusive lock on a file. Exit process if another instance holds it.
    Works on Windows and Unix.
    """
    global _lock_fh
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")

    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                logger.error(
                    "Another bot instance is already running (lock: %s). "
                    "Stop the other process, then start again.",
                    lock_path,
                )
                sys.exit(1)
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                logger.error(
                    "Another bot instance is already running (lock: %s). "
                    "Stop the other process, then start again.",
                    lock_path,
                )
                sys.exit(1)
    except Exception:
        fh.close()
        raise

    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _lock_fh = fh
    atexit.register(_release)
    logger.info("Single-instance lock acquired (pid=%s, file=%s)", os.getpid(), lock_path)


def _release() -> None:
    global _lock_fh
    if _lock_fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            _lock_fh.seek(0)
            try:
                msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _lock_fh.close()
    except Exception:
        pass
    _lock_fh = None
