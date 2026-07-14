"""Fast media-type detection (video vs image) without full downloads."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from urllib.request import Request, urlopen

from bot.utils.helpers import IMAGE_EXTS

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Strong video signals only (avoid generic page JS false positives)
_VIDEO_RE = re.compile(
    r"(?:"
    r"pinimg\.com/videos/"
    r"|v\.pinimg\.com/"
    r'|"video_list"\s*:\s*\{'
    r'|"V_(?:HLS|720P|540P|480P|360P)"\s*:\s*\{'
    r'|isVideo"\s*:\s*true'
    r'|"@type"\s*:\s*"VideoObject"'
    r'|property=["\']og:video(?::secure_url|:url)?["\']\s+content=["\']https?://'
    r"|https?://[^\"'\s>]+\.mp4(?:\?|\"|')"
    r")",
    re.I,
)

_IMAGE_PIN_RE = re.compile(
    r"i\.pinimg\.com/(?:originals|736x|564x|474x|236x)/[a-f0-9/._-]+\.(?:jpg|jpeg|png|webp|gif)",
    re.I,
)

# Platforms that are almost always video — skip HTML probe
_VIDEO_HOST_HINTS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com/reel",
    "instagram.com/tv",
    "facebook.com",
    "fb.watch",
    "x.com/",
    "twitter.com/",
    "vimeo.com",
    "reddit.com",
    "v.redd.it",
    "twitch.tv",
    "dailymotion.com",
    "streamable.com",
)

_IMAGE_HOST_HINTS = (
    "i.pinimg.com/",
    "i.imgur.com/",
    "pbs.twimg.com/media",
)


def detect_mode(url: str) -> str:
    """
    Return 'video' or 'image' for auto-download.
    Uses HTML signals + yt-dlp probe for ambiguous Pinterest pins.
    """
    if not url:
        return "video"
    low = url.lower().split("?")[0]

    for e in IMAGE_EXTS:
        if low.endswith("." + e):
            return "image"
    for e in ("mp4", "webm", "mkv", "mov", "m4v"):
        if low.endswith("." + e):
            return "video"

    if any(h in low for h in _IMAGE_HOST_HINTS):
        return "image"

    if any(h in low for h in _VIDEO_HOST_HINTS):
        return "video"

    # Pinterest / pin.it — reliable detect
    if "pinterest." in low or "pin.it/" in low:
        return _detect_pinterest(url)

    # Instagram: reels/TV are video; /p/ may be photo — try video first (fast),
    # image fallback in downloader handles photo posts without a second probe.
    if "instagram.com/" in low:
        return "video"

    return "video"


def _detect_pinterest(url: str) -> str:
    html = _fetch_html_cached(url)
    has_video = bool(html and _VIDEO_RE.search(html))
    has_image = bool(html and _IMAGE_PIN_RE.search(html))

    if has_video:
        logger.info("Pinterest VIDEO (html): %s", url[:80])
        return "video"
    if has_image and not has_video:
        logger.info("Pinterest IMAGE (html): %s", url[:80])
        return "image"

    # HTML shell often has no pin media — ask yt-dlp (skip_download)
    return _detect_via_ytdlp(url)


def _detect_via_ytdlp(url: str) -> str:
    """
    One lightweight extract_info(download=False).
    No formats → image. Formats with vcodec → video.
    """
    try:
        import yt_dlp

        from bot.services.downloader import _base_opts

        from urllib.parse import urlparse as _up

        opts = _base_opts(host=_up(url).netloc.lower())
        opts.update(
            {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": True,
            }
        )
        opts.pop("progress_hooks", None)
        # Don't need format selection for type probe
        opts.pop("format", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            logger.info("probe empty → image: %s", url[:80])
            return "image"
        formats = info.get("formats") or []
        has_vid = any(
            (f.get("vcodec") or "none") != "none" and (f.get("height") or f.get("url"))
            for f in formats
        )
        # duration / ext hints
        if info.get("duration") or has_vid:
            logger.info("probe VIDEO: %s", url[:80])
            return "video"
        ext = (info.get("ext") or "").lower()
        if ext in IMAGE_EXTS or info.get("thumbnail"):
            logger.info("probe IMAGE: %s", url[:80])
            return "image"
        # No usable video formats
        if not formats or not has_vid:
            logger.info("probe no formats → IMAGE: %s", url[:80])
            return "image"
        return "video"
    except Exception as e:
        err = str(e).lower()
        if "no video formats" in err or "only images" in err:
            logger.info("probe exception → IMAGE: %s", url[:80])
            return "image"
        logger.warning("probe failed (%s) → video default: %s", e, url[:80])
        return "video"


@lru_cache(maxsize=128)
def _fetch_html_cached(url: str) -> str:
    """Cache HTML probes (full page up to 1.5MB)."""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(req, timeout=15) as resp:
            data = resp.read(1_500_000)
        return data.decode("utf-8", "replace")
    except Exception as e:
        logger.warning("media probe failed for %s: %s", url[:80], e)
        return ""


def is_direct_image_url(url: str) -> bool:
    low = (url or "").lower().split("?")[0]
    return any(low.endswith("." + e) for e in IMAGE_EXTS)
