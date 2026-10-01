"""
YouTube search for inline mode.

A *flat* search: yt-dlp reads YouTube's search results page and returns the
listing (id, title, channel, duration, views) without resolving any video —
no player JS, no PO token, no format list. That is what makes it ~1-2s on the
small VPS instead of the 2-16s a full extraction costs. Results are cached per
query so the same search from anyone is instant for a while.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import yt_dlp

from bot.config import INLINE_SEARCH_MAX, INLINE_SEARCH_MAX_DURATION, MAX_MEDIA_DURATION, PROXY

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, list["SearchHit"]]] = {}
_CACHE_LOCK = threading.Lock()
# Searches running right now, by query: an identical query arriving meanwhile
# (two users, or one user's client re-asking) waits for that result instead of
# hitting YouTube a second time.
_RUNNING: dict[str, threading.Event] = {}
# Own pool: a slow YouTube/WARP must not tie up the default executor that
# link checks, short-link expansion and title lookups share.
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytsearch")
_CACHE_TTL = 900.0
_CACHE_MAX = 300


@dataclass(frozen=True)
class SearchHit:
    id: str
    title: str
    channel: str
    duration: int | None
    views: int | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def thumbnail(self) -> str:
        # Always JPEG and always present — what Telegram's thumbnail_url needs.
        return f"https://i.ytimg.com/vi/{self.id}/hqdefault.jpg"


def normalize_query(text: str) -> str:
    return " ".join((text or "").split())[:100]


def _opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 8,
        "extractor_retries": 1,
        "playlistend": INLINE_SEARCH_MAX,
    }
    from bot.services.downloader import _should_proxy  # same routing as downloads

    if PROXY and _should_proxy("www.youtube.com"):
        opts["proxy"] = PROXY
    return opts


def _hit(entry: dict[str, Any]) -> SearchHit | None:
    vid = entry.get("id")
    if not vid or entry.get("ie_key") not in (None, "Youtube"):
        return None  # channels/playlists in the listing
    if entry.get("live_status") in ("is_live", "is_upcoming") or entry.get("is_live"):
        return None  # would be refused at download time anyway
    duration = entry.get("duration")
    if not duration:
        return None  # live streams / premieres list without one — refused anyway
    if duration > min(INLINE_SEARCH_MAX_DURATION, MAX_MEDIA_DURATION):
        return None  # long mixes/compilations: usually too big for Telegram
    return SearchHit(
        id=str(vid),
        title=str(entry.get("title") or "Untitled")[:200],
        channel=str(entry.get("channel") or entry.get("uploader") or "")[:80],
        duration=int(duration) if duration else None,
        views=int(entry["view_count"]) if entry.get("view_count") else None,
    )


def search(query: str) -> list[SearchHit]:
    """Up to INLINE_SEARCH_MAX hits for `query`. Blocking — call from a thread."""
    key = normalize_query(query).lower()
    if not key:
        return []
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
        running = _RUNNING.get(key)
        if running is None:
            _RUNNING[key] = threading.Event()
    if running is not None:
        running.wait(timeout=25)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit:
            return hit[1]
        raise RuntimeError("the same search failed a moment ago")
    try:
        started = time.monotonic()
        with yt_dlp.YoutubeDL(_opts()) as ydl:
            info = ydl.extract_info(f"ytsearch{INLINE_SEARCH_MAX}:{key}", download=False)
        hits, seen = [], set()
        for h in (_hit(e) for e in (info or {}).get("entries") or []):
            # yt-dlp does not dedupe across result pages, and one duplicate id
            # makes Telegram reject the whole answer (RESULT_ID_DUPLICATE).
            if h and h.id not in seen:
                seen.add(h.id)
                hits.append(h)
        logger.info("inline search %r: %s hits in %.1fs", key[:40], len(hits),
                    time.monotonic() - started)
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 4]:
                    _CACHE.pop(k, None)
            _CACHE[key] = (time.monotonic(), hits)
        return hits
    finally:
        with _CACHE_LOCK:
            ev = _RUNNING.pop(key, None)
        if ev is not None:
            ev.set()


def cached(query: str) -> list[SearchHit] | None:
    """Results if this query was searched recently — lets typing skip the debounce."""
    key = normalize_query(query).lower()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
    return None


def human_views(n: int | None) -> str:
    if not n:
        return ""
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix + " views"
    return f"{n} views"


def human_duration(sec: int | None) -> str:
    if not sec:
        return ""
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
