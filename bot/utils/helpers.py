"""Shared helper utilities."""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

URL_RE = re.compile(
    r"https?://[^\s<>\"']+|www\.[^\s<>\"']+",
    re.IGNORECASE,
)

# Common short-link / social patterns without scheme
BARE_URL_RE = re.compile(
    r"(?:(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com|x\.com|"
    r"facebook\.com|fb\.watch|pinterest\.com|pin\.it|reddit\.com|vimeo\.com|"
    r"soundcloud\.com|twitch\.tv|threads\.net|linkedin\.com)/[^\s<>\"']+)",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Extract and normalize URLs from user message text."""
    if not text:
        return []
    found: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(").,;]'\"")
        if url.lower().startswith("www."):
            url = "https://" + url
        found.append(url)
    if not found:
        for match in BARE_URL_RE.findall(text):
            found.append("https://" + match.rstrip(").,;]'\""))
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(_expand_short_url(u))
    return out


def _expand_short_url(url: str) -> str:
    """Resolve pin.it / tiny redirects quickly (one HEAD/GET, short timeout)."""
    low = url.lower()
    if "pin.it/" not in low and "t.co/" not in low and "bit.ly/" not in low:
        return url
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            final = resp.geturl()
            if final and final.startswith("http"):
                return final
    except Exception:
        try:
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                final = resp.geturl()
                if final and final.startswith("http"):
                    return final
        except Exception:
            pass
    return url


def is_likely_url(text: str) -> bool:
    return bool(extract_urls(text.strip()))


def platform_from_url(url: str) -> str:
    """Guess a friendly platform name from a URL host."""
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return "Unknown"
    mapping = {
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "m.youtube.com": "YouTube",
        "music.youtube.com": "YouTube",
        "instagram.com": "Instagram",
        "instagr.am": "Instagram",
        "tiktok.com": "TikTok",
        "vm.tiktok.com": "TikTok",
        "vt.tiktok.com": "TikTok",
        "twitter.com": "X / Twitter",
        "x.com": "X / Twitter",
        "mobile.twitter.com": "X / Twitter",
        "facebook.com": "Facebook",
        "fb.watch": "Facebook",
        "fb.com": "Facebook",
        "m.facebook.com": "Facebook",
        "pinterest.com": "Pinterest",
        "pin.it": "Pinterest",
        "reddit.com": "Reddit",
        "v.redd.it": "Reddit",
        "old.reddit.com": "Reddit",
        "vimeo.com": "Vimeo",
        "twitch.tv": "Twitch",
        "clips.twitch.tv": "Twitch",
        "soundcloud.com": "SoundCloud",
        "dailymotion.com": "Dailymotion",
        "bilibili.com": "Bilibili",
        "linkedin.com": "LinkedIn",
        "threads.net": "Threads",
        "snapchat.com": "Snapchat",
        "tumblr.com": "Tumblr",
        "bandcamp.com": "Bandcamp",
        "rumble.com": "Rumble",
        "ok.ru": "OK.ru",
        "vk.com": "VK",
        "vk.video": "VK",
    }
    if host in mapping:
        return mapping[host]
    for key, name in mapping.items():
        if host.endswith("." + key) or key in host:
            return name
    return host.split(".")[0].title() if host else "Unknown"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        return "—"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def format_size(num_bytes: int | float | None) -> str:
    if num_bytes is None:
        return "—"
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"


def format_views(n: int | float | None) -> str:
    if n is None:
        return "—"
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(int(v))


def short_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if not name:
        name = "media"
    return name[:max_len]


def progress_bar(percent: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, percent))
    filled = int(round(width * pct / 100))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:.0f}%"


def now_ts() -> float:
    return time.time()


def file_ext(path: Path | str) -> str:
    return Path(path).suffix.lower().lstrip(".")


IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff", "heic", "avif"}
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "3gp", "ts"}
AUDIO_EXTS = {"mp3", "m4a", "opus", "ogg", "flac", "wav", "aac", "wma"}


def media_kind_from_path(path: Path | str) -> str:
    ext = file_ext(path)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "document"
