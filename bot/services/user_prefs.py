"""
Per-user preferences, persisted in data/user_prefs.json.

Preferences let a user skip the download wizard: with a default type and
quality (or audio format) set, a pasted link downloads straight away.
Paid downloads live in services/quota.py, not here: this file evicts users.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from bot.config import DATA_DIR

logger = logging.getLogger(__name__)

MODES = ("ask", "video", "audio")
QUALITIES = ("ask", "480", "720", "1080", "max")
AUDIO_FORMATS = ("ask", "mp3", "m4a", "opus")
_ALLOWED = {"mode": MODES, "quality": QUALITIES, "audio": AUDIO_FORMATS}
_DEFAULTS = {"mode": "ask", "quality": "ask", "audio": "ask"}
_MAX_USERS = 20000

_PATH: Path = DATA_DIR / "user_prefs.json"
_lock = threading.Lock()
_data: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    """
    A read that fails must never turn into an empty file on the next save: a corrupt file is moved
    aside, and an unreadable one (permissions, too many open files) is not
    cached, so the next call tries again and nothing is overwritten meanwhile.
    """
    global _data
    if _data is None:
        try:
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            _data = raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            _data = {}
        except ValueError:
            aside = _PATH.with_name(f"{_PATH.name}.corrupt-{int(time.time())}")
            logger.error("user_prefs.json is corrupt; moved to %s, starting empty", aside.name)
            try:
                os.replace(_PATH, aside)
            except OSError:
                logger.exception("could not move the corrupt prefs file aside")
                return {}  # not cached: _save refuses while it is still there
            _data = {}
        except OSError:
            logger.exception("could not read user preferences; not caching")
            return {}
    return _data


def _save(data: dict[str, dict[str, Any]]) -> bool:
    if data is not _data:
        logger.error("not saving user preferences: the file could not be read")
        return False
    tmp = _PATH.with_suffix(".tmp")
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())  # Premium records: survive a power cut
        os.replace(tmp, _PATH)
        return True
    except OSError:
        logger.exception("could not persist user preferences")
        return False


def get(user_id: int) -> dict[str, Any]:
    """The user's preferences (defaults filled in) — a copy, safe to read."""
    with _lock:
        row = dict(_load().get(str(user_id), {}))
    return {**_DEFAULTS, **{k: row[k] for k in _DEFAULTS if k in row}}


def set_pref(user_id: int, key: str, value: str) -> bool:
    """Set one preference. Unknown keys/values are refused (callback data is client-controlled)."""
    if key not in _ALLOWED or value not in _ALLOWED[key]:
        return False
    with _lock:
        data = _load()
        row = data.pop(str(user_id), {})  # re-insert last: dict order = recency
        row[key] = value
        data[str(user_id)] = row
        while len(data) > _MAX_USERS:
            data.pop(next(iter(data)))
        _save(data)
    return True


def _reset_for_tests(path: Path) -> None:
    global _PATH, _data
    _PATH = path
    _data = None
