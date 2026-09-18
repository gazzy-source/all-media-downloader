"""
Track downloads that are in progress, so a restart doesn't strand the user.

Jobs live in memory, so a restart (deploy, crash, OOM, reboot) kills them
mid-flight. Nothing then updates the "Downloading…" message the user is
watching, and it sits frozen forever on whatever it last showed — the bot looks
hung when it is simply gone.

The registry is written to disk rather than kept in memory precisely because
the interesting case is the process dying: a graceful-shutdown hook would miss
a SIGKILL or a power loss. On the next start the bot drains the file and tells
each of those chats what happened.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from bot.config import DATA_DIR

logger = logging.getLogger(__name__)

_PATH = Path(DATA_DIR) / "inflight.json"
_LOCK = threading.Lock()


def _read() -> list[dict]:
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, ValueError):
        return []


def _write(rows: list[dict]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a truncated file
        # that would throw away every other tracked job.
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        tmp.replace(_PATH)
    except OSError as e:
        logger.warning("Cannot persist in-flight jobs: %s", e)


def add(chat_id: int, message_id: int) -> None:
    """Record a status message that a running job owns."""
    with _LOCK:
        rows = [r for r in _read()
                if not (r.get("chat_id") == chat_id and r.get("message_id") == message_id)]
        rows.append({"chat_id": chat_id, "message_id": message_id})
        _write(rows[-200:])  # bound the file; oldest entries are least useful


def remove(chat_id: int, message_id: int) -> None:
    """Job finished (or failed cleanly) — it no longer needs rescuing."""
    with _LOCK:
        rows = [r for r in _read()
                if not (r.get("chat_id") == chat_id and r.get("message_id") == message_id)]
        _write(rows)


def drain() -> list[dict]:
    """Return everything that was still running when the process died, and clear it."""
    with _LOCK:
        rows = _read()
        _write([])
        return rows
