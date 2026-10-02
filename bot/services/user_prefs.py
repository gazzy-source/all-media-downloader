"""
Per-user preferences and premium status, persisted in data/user_prefs.json.

Preferences let a user skip the download wizard: with a default type and
quality (or audio format) set, a pasted link downloads straight away.
Premium is bought with Telegram Stars; each purchase is recorded with its
telegram_payment_charge_id so it can be refunded.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from bot.config import DATA_DIR, PREMIUM_RATE_MULT, RATE_LIMIT_PER_HOUR

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
    This file holds paid Premium and refund records. A read that fails must
    never turn into an empty file on the next save: a corrupt file is moved
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
            oldest = next(iter(data))
            if _premium_until_unlocked(data[oldest]) > time.time():
                data[oldest] = data.pop(oldest)  # never evict a paying user
                break
            data.pop(oldest)
        _save(data)
    return True


# ------------------------------------------------------------------ premium
def _premium_until_unlocked(row: dict[str, Any]) -> float:
    try:
        return float(row.get("premium_until") or 0)
    except (TypeError, ValueError):
        return 0.0


def premium_until(user_id: int) -> float:
    """Unix time premium ends (0 = never had it)."""
    with _lock:
        return _premium_until_unlocked(_load().get(str(user_id), {}))


def is_premium(user_id: int) -> bool:
    return premium_until(user_id) > time.time()


class GrantNotSaved(RuntimeError):
    """The payment went through but the Premium record could not be written."""


def grant_premium(user_id: int, days: int, *, charge_id: str, stars: int) -> float:
    """
    Extend premium by `days` from whichever is later: now or the current end.
    Idempotent per charge id — Telegram may deliver a payment update twice.
    """
    with _lock:
        data = _load()
        row = data.setdefault(str(user_id), {})
        charges = row.setdefault("charges", [])
        if any(c.get("id") == charge_id for c in charges):
            return _premium_until_unlocked(row)
        start = max(time.time(), _premium_until_unlocked(row))
        before = (row.get("premium_until"), list(charges))
        row["premium_until"] = start + days * 86400
        charges.append({"id": charge_id, "stars": stars, "days": days, "t": time.time()})
        row["charges"] = charges[-20:]
        if not _save(data):
            # Never tell a paying user "active" when nothing was recorded.
            row["premium_until"], row["charges"] = before
            raise GrantNotSaved(charge_id)
        return row["premium_until"]


def last_charge(user_id: int) -> dict[str, Any] | None:
    with _lock:
        charges = _load().get(str(user_id), {}).get("charges") or []
        live = [c for c in charges if not c.get("refunded")]
        return dict(live[-1]) if live else None


def revoke_charge(user_id: int, charge_id: str) -> None:
    """After a refund: take back that purchase's days (never below now)."""
    with _lock:
        data = _load()
        row = data.get(str(user_id))
        if not row:
            return
        for c in row.get("charges") or []:
            if c.get("id") == charge_id and not c.get("refunded"):
                c["refunded"] = True
                until = _premium_until_unlocked(row)
                if until > time.time():
                    row["premium_until"] = max(time.time(), until - c.get("days", 0) * 86400)
                _save(data)
                return


def hourly_limit(user_id: int) -> int:
    """Downloads per hour for this user (premium gets PREMIUM_RATE_MULT x)."""
    return RATE_LIMIT_PER_HOUR * (PREMIUM_RATE_MULT if is_premium(user_id) else 1)


def _reset_for_tests(path: Path) -> None:
    global _PATH, _data
    _PATH = path
    _data = None
