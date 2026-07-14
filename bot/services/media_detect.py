"""Fast media-type detection (video vs image) without full downloads."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from bot.utils.helpers import IMAGE_EXTS

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Strong video signals in HTML / JSON embeds
_VIDEO_RE = re.compile(
    r"(?:pinimg\.com/videos|v\.pinimg\.com|\.mp4(?:\?|\")|"
    r'"video_list"|V_\d+P|isVideo["\']?\s*:\s*true|'
    r'"@type"\s*:\s*"VideoObject"|property=["\']og:video|'
    r'content=["\']video["\']|video/mp4|m3u8)',
    re.I,
)

# Direct video hosts / paths
_VIDEO_HOST_HINTS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com/reel",
    "instagram.com/p/",  # can be carousel; still try video first
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
    "cdninstagram.com",
)


def detect_mode(url: str) -> str:
    """
    Return 'video' or 'image' for auto-download.
    Prefers video when unsure (except clear image URLs).
    """
    if not url:
        return "video"
    low = url.lower().split("?")[0]

    # Direct file extensions win
    for e in IMAGE_EXTS:
        if low.endswith("." + e):
            return "image"
    for e in ("mp4", "webm", "mkv", "mov", "m4v"):
        if low.endswith("." + e):
            return "video"

    # Clear image CDNs
    if any(h in low for h in _IMAGE_HOST_HINTS):
        return "image"

    # Known video platforms
    if any(h in low for h in _VIDEO_HOST_HINTS):
        return "video"

    # Pinterest / pin.it — inspect page (video pins exist)
    if "pinterest." in low or "pin.it/" in low:
        return _detect_pinterest(url)

    # Default: try video first (image fallback still exists in downloader)
    return "video"


def _detect_pinterest(url: str) -> str:
    html = _fetch_html_cached(url)
    if not html:
        # Unknown — video first; downloader falls back to image if needed
        return "video"
    if _VIDEO_RE.search(html):
        logger.info("Pinterest pin detected as VIDEO: %s", url[:80])
        return "video"
    # pinimg originals without video signals → image
    if re.search(r"i\.pinimg\.com/(?:originals|736x|564x)/", html, re.I):
        logger.info("Pinterest pin detected as IMAGE: %s", url[:80])
        return "image"
    return "video"


@lru_cache(maxsize=64)
def _fetch_html_cached(url: str) -> str:
    """Cache short HTML probes within process lifetime."""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(req, timeout=12) as resp:
            # Only need first ~400KB for embedded JSON
            data = resp.read(400_000)
        return data.decode("utf-8", "replace")
    except Exception as e:
        logger.warning("media probe failed for %s: %s", url[:80], e)
        return ""


def is_direct_image_url(url: str) -> bool:
    low = (url or "").lower().split("?")[0]
    return any(low.endswith("." + e) for e in IMAGE_EXTS)
