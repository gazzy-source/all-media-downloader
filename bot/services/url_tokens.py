"""Short-lived URL tokens for Telegram callback_data (max 64 bytes)."""

from __future__ import annotations

import time
from threading import Lock

from bot.utils.helpers import short_id

# token -> (url, user_id, expires_at)
_store: dict[str, tuple[str, int, float]] = {}
_lock = Lock()
_TTL = 7 * 24 * 3600  # 7 days — long enough for "Download Again"


def put_url(url: str, user_id: int) -> str:
    """Store URL and return a short token safe for callback_data."""
    _cleanup()
    token = short_id(12)  # 12 hex chars
    with _lock:
        _store[token] = (url, user_id, time.time() + _TTL)
    return token


def get_url(token: str, user_id: int | None = None) -> str | None:
    with _lock:
        item = _store.get(token)
        if not item:
            return None
        url, owner, exp = item
        if time.time() > exp:
            del _store[token]
            return None
        if user_id is not None and owner != user_id:
            # Still allow same-chat reuse if needed; soft check only
            pass
        return url


def _cleanup() -> None:
    now = time.time()
    with _lock:
        dead = [k for k, v in _store.items() if v[2] < now]
        for k in dead:
            del _store[k]
