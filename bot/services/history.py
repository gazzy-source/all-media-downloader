"""Lightweight JSON download history and usage stats."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from bot.config import DATA_DIR

_HISTORY_FILE = DATA_DIR / "history.json"
_STATS_FILE = DATA_DIR / "stats.json"
_MAX_HISTORY_USERS = 5000
_lock = threading.Lock()
_MAX_USER_HISTORY = 50


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # compact JSON = less disk I/O on small VPS
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


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
    """Fire-and-forget disk write so Telegram upload path isn't blocked."""
    threading.Thread(
        target=_record_download_sync,
        kwargs={
            "user_id": user_id,
            "url": url,
            "title": title,
            "platform": platform,
            "mode": mode,
            "quality": quality,
            "success": success,
            "file_size": file_size,
            "error": error,
        },
        daemon=True,
        name="history-write",
    ).start()


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
    entry = {
        "ts": time.time(),
        "url": url,
        "title": (title or "")[:200],
        "platform": platform,
        "mode": mode,
        "quality": quality,
        "success": success,
        "file_size": file_size,
        "error": (error or "")[:300] if error else None,
    }
    with _lock:
        hist = _read_json(_HISTORY_FILE, {})
        key = str(user_id)
        items = hist.pop(key, [])  # re-insert last: dict order = recency
        items.insert(0, entry)
        hist[key] = items[:_MAX_USER_HISTORY]
        # Bounded: the whole file is rewritten on every download, so keep the
        # most recently active users only.
        while len(hist) > _MAX_HISTORY_USERS:
            hist.pop(next(iter(hist)))
        _write_json(_HISTORY_FILE, hist)

        stats = _read_json(
            _STATS_FILE,
            {
                "total_downloads": 0,
                "successful": 0,
                "failed": 0,
                "by_platform": {},
                "by_mode": {},
                "bytes_served": 0,
                "unique_users": [],
            },
        )
        stats["total_downloads"] = stats.get("total_downloads", 0) + 1
        if success:
            stats["successful"] = stats.get("successful", 0) + 1
            if file_size:
                stats["bytes_served"] = stats.get("bytes_served", 0) + int(file_size)
        else:
            stats["failed"] = stats.get("failed", 0) + 1

        bp = stats.setdefault("by_platform", {})
        bp[platform] = bp.get(platform, 0) + 1
        bm = stats.setdefault("by_mode", {})
        bm[mode] = bm.get(mode, 0) + 1

        users = stats.get("unique_users", [])
        if user_id not in users:
            users.append(user_id)
            stats["unique_user_total"] = stats.get("unique_user_total", len(users) - 1) + 1
        # Insertion-ordered, trimmed oldest-first (trimming a set dropped an
        # arbitrary user — possibly the one just added). The total keeps
        # counting past the cap.
        stats["unique_users"] = users[-10_000:]
        _write_json(_STATS_FILE, stats)


def get_user_history(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _lock:
        hist = _read_json(_HISTORY_FILE, {})
        return hist.get(str(user_id), [])[:limit]


def get_stats() -> dict[str, Any]:
    with _lock:
        stats = _read_json(_STATS_FILE, {})
        users = stats.get("unique_users", [])
        out = dict(stats)
        out["unique_user_count"] = max(len(users), stats.get("unique_user_total", 0))
        out.pop("unique_users", None)
        return out
