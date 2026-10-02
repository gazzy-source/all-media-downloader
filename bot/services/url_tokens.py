"""
Short URL tokens for Telegram callback_data (max 64 bytes).

Saved to data/url_tokens.json: in memory only, every restart (each deploy,
the weekly yt-dlp update) turned every "Download Again" and "Open bot"
button sent before it into "Link expired". Bounded by count and by age.
"""

from __future__ import annotations

import json
import logging
import os
import time
from threading import Lock

from bot.config import DATA_DIR
from bot.utils.helpers import short_id

logger = logging.getLogger(__name__)

_PATH = DATA_DIR / "url_tokens.json"
_TTL = 7 * 24 * 3600  # 7 days — long enough for "Download Again"
_MAX = 5000

# token -> (url, user_id, expires_at); insertion order = age
_store: dict[str, tuple[str, int, float]] | None = None
_lock = Lock()
_dirty = False


def _loaded() -> dict[str, tuple[str, int, float]]:
    global _store
    if _store is None:
        _store = {}
        try:
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            now = time.time()
            for k, v in (raw or {}).items():
                if isinstance(v, list) and len(v) == 3 and v[2] > now:
                    _store[str(k)] = (str(v[0]), int(v[1]), float(v[2]))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logger.warning("url tokens not loaded (%s) — old buttons will say expired", e)
    return _store


def _save_locked() -> None:
    tmp = _PATH.with_suffix(".tmp")
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({k: list(v) for k, v in _loaded().items()}), encoding="utf-8")
        os.replace(tmp, _PATH)
    except OSError as e:
        logger.warning("could not save url tokens: %s", e)


def put_url(url: str, user_id: int) -> str:
    """Store URL and return a short token safe for callback_data."""
    token = short_id(12)  # 12 hex chars
    with _lock:
        store = _loaded()
        now = time.time()
        while store:  # oldest first: drop expired, and keep the count bounded
            k = next(iter(store))
            if store[k][2] < now or len(store) >= _MAX:
                del store[k]
            else:
                break
        store[token] = (url, user_id, now + _TTL)
        global _dirty
        _dirty = True  # saved by flush() from the heartbeat job, off the event loop
    return token


def flush() -> None:
    """Write new tokens to disk (every 30s from a worker thread, and at stop)."""
    global _dirty
    with _lock:
        if _dirty:
            _save_locked()
            _dirty = False


def get_url(token: str, user_id: int | None = None) -> str | None:
    with _lock:
        store = _loaded()
        item = store.get(token)
        if not item:
            return None
        url, owner, exp = item
        if time.time() > exp:
            del store[token]
            return None
        # Pass user_id=None only for admins. Anyone else gets the URL only
        # for a button that was made for them.
        if user_id is not None and owner != user_id:
            return None
        return url


def _reset_for_tests(path) -> None:
    global _PATH, _store
    _PATH, _store = path, None
