"""
What the health check reads. The old check asked systemd "is the unit
active?" and pinged the PO-token provider — so a hung event loop, dead
polling, a failing YouTube pipeline or a full disk all reported "ok".

The bot writes data/heartbeat.json every 30s from its own event loop: a stale
file means the loop (or the whole bot) is stuck. It also says whether polling
runs and how the last YouTube warm-up went.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from bot.config import DATA_DIR

logger = logging.getLogger(__name__)

PATH = DATA_DIR / "heartbeat.json"

state: dict[str, Any] = {
    "warmup_ok_at": None,     # last time the YouTube pipeline extracted fine
    "warmup_fail_streak": 0,  # consecutive failed warm-ups
    "warmup_error": None,
}


def warmup_result(ok: bool, error: str | None = None) -> None:
    if ok:
        state.update(warmup_ok_at=time.time(), warmup_fail_streak=0, warmup_error=None)
    else:
        state["warmup_fail_streak"] += 1
        state["warmup_error"] = (error or "")[:200]


def write(polling: bool, running: int, waiting: int) -> None:
    data = {
        "ts": time.time(),
        "pid": os.getpid(),
        "polling": polling,
        "queue_running": running,
        "queue_waiting": waiting,
        **state,
    }
    tmp = PATH.with_suffix(".tmp")
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, PATH)
    except OSError as e:
        logger.warning("could not write the heartbeat: %s", e)
