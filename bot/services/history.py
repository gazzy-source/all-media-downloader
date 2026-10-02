"""
Download history and usage stats, in SQLite.

Was: two JSON files read and rewritten in full on every download. At a few
thousand users that is tens of MB parsed per download — seconds of work that
holds the GIL and freezes the event loop (polling, progress, ✖ Cancel). Now one
indexed insert. The old JSON files are imported once, then renamed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from bot.config import DATA_DIR

logger = logging.getLogger(__name__)

# The JSON files are only read once, for the migration. The database sits
# next to them (tests point _HISTORY_FILE at a tmp dir to isolate it).
_HISTORY_FILE = DATA_DIR / "history.json"
_STATS_FILE = DATA_DIR / "stats.json"
_MAX_HISTORY_USERS = 5000
_MAX_USER_HISTORY = 50
_lock = threading.Lock()
_init_lock = threading.Lock()
_ready: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    url TEXT, title TEXT, platform TEXT, mode TEXT, quality TEXT,
    success INTEGER NOT NULL, file_size INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS downloads_user ON downloads(user_id, id);
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, last_ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS users_last ON users(last_ts);
CREATE TABLE IF NOT EXISTS seen (user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""


def _db_path() -> Path:
    return _HISTORY_FILE.with_suffix(".sqlite3")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    key = str(path)
    if key not in _ready:
        # Once per process, by one thread: concurrent first uses (a replayed
        # backlog at start) ran the migration two or three times over.
        with _init_lock:
            if key not in _ready:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
                _migrate_json(con)
                _ready.add(key)
    return con


def _bump(con: sqlite3.Connection, key: str, n: int = 1) -> None:
    con.execute(
        "INSERT INTO counters(key, n) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET n = n + excluded.n", (key, n))


def _migrate_json(con: sqlite3.Connection) -> None:
    """One-time import of history.json / stats.json (then renamed, not deleted)."""
    if con.execute("SELECT 1 FROM counters LIMIT 1").fetchone():
        return
    imported = False
    for path, kind in ((_HISTORY_FILE, "history"), (_STATS_FILE, "stats")):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error("history migration: could not read %s (%s); left in place", path, e)
            continue
        with con:
            if kind == "history":
                for uid, items in (data or {}).items():
                    if not str(uid).lstrip("-").isdigit():
                        continue
                    last = 0.0
                    for e in reversed(items or []):  # oldest first, so ids keep order
                        ts = float(e.get("ts") or 0)
                        last = max(last, ts)
                        con.execute(
                            "INSERT INTO downloads(user_id, ts, url, title, platform, mode,"
                            " quality, success, file_size, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (int(uid), ts, e.get("url"), e.get("title"), e.get("platform"),
                             e.get("mode"), e.get("quality"), 1 if e.get("success") else 0,
                             e.get("file_size"), e.get("error")))
                    con.execute("INSERT OR REPLACE INTO users(user_id, last_ts) VALUES(?, ?)",
                                (int(uid), last))
            else:
                for k in ("total_downloads", "successful", "failed", "bytes_served"):
                    _bump(con, k, int(data.get(k) or 0))
                for p, n in (data.get("by_platform") or {}).items():
                    _bump(con, f"platform:{p}", int(n))
                for m, n in (data.get("by_mode") or {}).items():
                    _bump(con, f"mode:{m}", int(n))
                for uid in data.get("unique_users") or []:
                    con.execute("INSERT OR IGNORE INTO seen(user_id) VALUES(?)", (int(uid),))
                extra = int(data.get("unique_user_total") or 0) - len(data.get("unique_users") or [])
                if extra > 0:  # users trimmed from the old list still count
                    _bump(con, "unique_extra", extra)
        try:
            path.rename(path.with_name(path.name + ".migrated"))
        except OSError as e:
            logger.warning("history migration: could not rename %s: %s", path, e)
        imported = True
    _bump(con, "schema", 0)  # marks the database as initialised
    con.commit()
    if imported:
        logger.info("history: imported the old JSON files into %s", _db_path().name)


def record_download(
    user_id: int,
    url: str,
    title: str,
    platform: str,
    mode: str,
    quality: str | None,
    success: bool,
    file_size: int | None = None,
    error: str | None = None,
) -> None:
    """Fire-and-forget disk write so the Telegram upload path isn't blocked."""
    threading.Thread(
        target=_record_download_safe,
        kwargs={
            "user_id": user_id, "url": url, "title": title, "platform": platform,
            "mode": mode, "quality": quality, "success": success,
            "file_size": file_size, "error": error,
        },
        daemon=True,
        name="history-write",
    ).start()


def _record_download_safe(**kw: Any) -> None:
    try:
        _record_download_sync(**kw)
    except Exception:
        logger.exception("history: could not record a download")


def _record_download_sync(
    user_id: int,
    url: str,
    title: str,
    platform: str,
    mode: str,
    quality: str | None,
    success: bool,
    file_size: int | None = None,
    error: str | None = None,
) -> None:
    now = time.time()
    with _lock, closing(_connect()) as con, con:
        con.execute(
            "INSERT INTO downloads(user_id, ts, url, title, platform, mode, quality,"
            " success, file_size, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, now, url, (title or "")[:200], platform, mode, quality,
             1 if success else 0, file_size, (error or "")[:300] if error else None))
        con.execute(
            "DELETE FROM downloads WHERE user_id = ? AND id NOT IN ("
            " SELECT id FROM downloads WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, _MAX_USER_HISTORY))
        con.execute("INSERT OR REPLACE INTO users(user_id, last_ts) VALUES(?, ?)",
                    (user_id, now))
        over = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] - _MAX_HISTORY_USERS
        if over > 0:  # keep the most recently active users only
            old = [r[0] for r in con.execute(
                "SELECT user_id FROM users ORDER BY last_ts ASC LIMIT ?", (over,))]
            con.executemany("DELETE FROM downloads WHERE user_id = ?", [(u,) for u in old])
            con.executemany("DELETE FROM users WHERE user_id = ?", [(u,) for u in old])

        _bump(con, "total_downloads")
        if success:
            _bump(con, "successful")
            if file_size:
                _bump(con, "bytes_served", int(file_size))
        else:
            _bump(con, "failed")
        _bump(con, f"platform:{platform}")
        _bump(con, f"mode:{mode}")
        con.execute("INSERT OR IGNORE INTO seen(user_id) VALUES(?)", (user_id,))


def get_user_history(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT ts, url, title, platform, mode, quality, success, file_size, error"
            " FROM downloads WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)).fetchall()
    return [{**dict(r), "success": bool(r["success"])} for r in rows]


def history_users() -> list[int]:
    """User ids with history, least recently active first."""
    with closing(_connect()) as con:
        return [r[0] for r in con.execute("SELECT user_id FROM users ORDER BY last_ts ASC")]


def get_stats() -> dict[str, Any]:
    with closing(_connect()) as con:
        counters = {r["key"]: r["n"] for r in con.execute("SELECT key, n FROM counters")}
        seen = con.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    out: dict[str, Any] = {
        k: counters.get(k, 0)
        for k in ("total_downloads", "successful", "failed", "bytes_served")
    }
    out["by_platform"] = {k[9:]: v for k, v in counters.items() if k.startswith("platform:")}
    out["by_mode"] = {k[5:]: v for k, v in counters.items() if k.startswith("mode:")}
    out["unique_user_count"] = seen + counters.get("unique_extra", 0)
    return out
