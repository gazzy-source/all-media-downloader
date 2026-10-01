"""
Link → Telegram file_id cache for inline mode.

A file uploaded once can be re-sent by file_id forever, instantly and without
re-downloading, so every link that was fetched before answers inline queries
with the finished media. Persisted to data/ so it survives restarts.
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

_PATH: Path = DATA_DIR / "inline_cache.json"
_MAX_ENTRIES = 3000
_lock = threading.Lock()
_data: dict[str, dict[str, Any]] | None = None


def repeat_key(mode: str, quality: str = "", audio_format: str = "") -> str | None:
    """
    Cache slot for one deliverable: the same link at the same quality (or
    audio format) is the same file wherever it was first fetched — inline,
    DM or a group. Subtitled and image downloads are not cached.
    """
    if mode == "audio":
        return f"audio@{audio_format or 'mp3'}"
    if mode == "video":
        return f"video@{quality or 'max'}"
    return None


_YT_ID = __import__("re").compile(
    r"^(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
    # Not playlist/channel embeds ("videoseries", "live_stream" are 11 chars
    # too — every playlist embed collapsed into one slot) and exactly 11.
    r"(?!videoseries|live_stream)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
)


def good_enough(mode: str, quality: str, result) -> bool:
    """
    Only a file that IS what the slot promises may be cached. When YouTube
    refuses the top formats the downloader falls back to 360p; caching that
    under video@1080 served 360p to everyone, instantly, with no expiry.
    """
    if mode != "video":
        return True
    from bot.config import QUALITY_MAP

    want = (QUALITY_MAP.get(quality) or {}).get("height") or 0
    got = getattr(result, "actual_height", None)
    if not got:
        return False  # unknown: don't promise anything
    # The fallback client is capped at 360p: anything above that is the real
    # thing (incl. a video whose own best is 480p); at or below 360 it only
    # counts when that is all that was asked for.
    return got > 360 or got >= want


def _norm(url: str) -> str:
    """
    youtu.be/X, watch?v=X&si=…, /shorts/X and m./music. hosts are one video:
    they share a cache slot, so a search result and a pasted link hit the
    same finished file.
    """
    url = (url or "").strip().split("#")[0]
    m = _YT_ID.match(url)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else url


def _key(url: str, mode: str) -> str:
    return f"{mode}|{_norm(url)}"


def _load() -> dict[str, dict[str, Any]]:
    global _data
    if _data is None:
        try:
            _data = json.loads(_PATH.read_text(encoding="utf-8"))
            if not isinstance(_data, dict):
                _data = {}
        except (OSError, ValueError):
            _data = {}
    return _data


def _save(data: dict[str, dict[str, Any]]) -> None:
    tmp = _PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _PATH)
    except OSError:
        logger.exception("could not persist the inline cache")


def get(url: str, mode: str) -> dict[str, Any] | None:
    with _lock:
        return _load().get(_key(url, mode))


def put(url: str, mode: str, *, file_id: str, kind: str, title: str = "") -> None:
    with _lock:
        data = _load()
        data[_key(url, mode)] = {
            "file_id": file_id, "kind": kind, "title": title[:200], "t": time.time(),
        }
        links = [(k, v) for k, v in data.items() if not k.startswith("meta|")]
        if len(links) > _MAX_ENTRIES:
            # Drop the oldest tenth in one go rather than one per write; the
            # bot-level meta entries (placeholders) are never evicted.
            for k, _ in sorted(links, key=lambda kv: kv[1].get("t", 0))[: _MAX_ENTRIES // 10]:
                del data[k]
        _save(data)


def forget(url: str, mode: str) -> None:
    with _lock:
        data = _load()
        if data.pop(_key(url, mode), None) is not None:
            _save(data)


# In-flight inline jobs (inline_message_ids), so a restart can tell those users.
def add_pending(imid: str) -> None:
    with _lock:
        data = _load()
        pend = data.setdefault("meta|pending", {"kind": "meta", "t": time.time(), "ids": []})
        if imid not in pend["ids"]:
            pend["ids"] = (pend["ids"] + [imid])[-200:]
            _save(data)


def drop_pending(imid: str) -> None:
    with _lock:
        data = _load()
        pend = data.get("meta|pending")
        if pend and imid in pend.get("ids", []):
            pend["ids"].remove(imid)
            _save(data)


def drain_pending() -> list[str]:
    with _lock:
        data = _load()
        pend = data.pop("meta|pending", None)
        if pend is not None:
            _save(data)
        return list((pend or {}).get("ids", []))


def get_meta(name: str) -> str | None:
    """Bot-level values (e.g. the placeholder photos' file_ids)."""
    with _lock:
        entry = _load().get(f"meta|{name}")
        return entry.get("file_id") if entry else None


def put_meta(name: str, file_id: str) -> None:
    with _lock:
        data = _load()
        data[f"meta|{name}"] = {"file_id": file_id, "kind": "meta", "t": time.time()}
        _save(data)


def _reset_for_tests(path: Path) -> None:
    global _PATH, _data
    _PATH = path
    _data = None
