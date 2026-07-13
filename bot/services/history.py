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
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
    entry = {
        "ts": time.time(),
        "url": url,
        "title": title[:200],
        "platform": platform,
        "mode": mode,
        "quality": quality,
        "success": success,
        "file_size": file_size,
        "error": error,
    }
    with _lock:
        hist = _read_json(_HISTORY_FILE, {})
        key = str(user_id)
        items = hist.get(key, [])
        items.insert(0, entry)
        hist[key] = items[:_MAX_USER_HISTORY]
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

        users = set(stats.get("unique_users", []))
        users.add(user_id)
        # store as list for JSON
        stats["unique_users"] = list(users)[-10_000:]
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
        out["unique_user_count"] = len(users)
        out.pop("unique_users", None)
        return out
