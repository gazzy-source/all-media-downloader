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
    # (?<![\w.-]): "netflix.com/watch/1" must not yield "x.com/watch/1"
    r"(?<![\w.-])(?:(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com|x\.com|"
    r"facebook\.com|fb\.watch|pinterest\.com|pin\.it|reddit\.com|vimeo\.com|"
    r"soundcloud\.com|twitch\.tv|threads\.net|linkedin\.com)/[^\s<>\"']+)",
    re.IGNORECASE,
)


# Punctuation that ends a sentence, not a URL (ASCII + CJK/typographic).
_TRAILING = ".,;:!?'\"]>…»」』）】，。、！？；："


def _trim_url(url: str) -> str:
    """
    Drop sentence punctuation stuck to the end of a link ("…XcQ!" was read as
    an invalid id). A ")" is kept when it closes a "(" inside the URL, as in
    Wikipedia's Foo_(bar).
    """
    while url:
        if url[-1] in _TRAILING:
            url = url[:-1]
        elif url[-1] == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        else:
            break
    return url


def extract_urls(text: str, *, expand: bool = True) -> list[str]:
    """
    Extract and normalize URLs from user message text.

    `expand=False` skips the short-link HEAD lookups, making the call pure and
    non-blocking — use it from anywhere that must not touch the network.
    """
    if not text:
        return []
    found: list[str] = []
    for match in URL_RE.findall(text):
        url = _trim_url(match)
        if url.lower().startswith("www."):
            url = "https://" + url
        found.append(url)
    if not found:
        for match in BARE_URL_RE.findall(text):
            found.append("https://" + _trim_url(match))
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u not in seen:
            seen.add(u)
            # Only the first few can ever be downloaded (handlers cap batches at
            # 5), so never pay — or let a sender make us pay — for expanding
            # the 150 short links one message can hold.
            out.append(_expand_short_url(u) if expand and len(out) < MAX_EXPAND else u)
    return out


# Links per message worth expanding — matches the 5-link batch cap.
MAX_EXPAND = 5
_SHORT_HOSTS = {"pin.it", "t.co", "bit.ly"}


def _expand_short_url(url: str) -> str:
    """Resolve pin.it / t.co / bit.ly redirects quickly (short timeout).

    NOTE: runs synchronously inside async handlers, so it must stay cheap —
    each attempt is bounded to 2s. yt-dlp follows any remaining redirects
    itself, so a failed expansion is harmless (original URL is returned).
    """
    # Match the real host: a substring test let "http://10.0.0.5/x?bit.ly/"
    # make the bot send a request into the private network.
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return url
    if host not in _SHORT_HOSTS:
        return url
    import urllib.request

    from bot.utils.safe_fetch import open_public

    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url, method=method, headers={"User-Agent": "Mozilla/5.0"}
            )
            # Public addresses only, every redirect hop included.
            with open_public(req, timeout=2) as resp:
                final = resp.geturl()
                if final and final.startswith("http"):
                    return final
        except Exception:
            continue
    return url


def is_likely_url(text: str) -> bool:
    """Cheap syntactic check — never expands short links."""
    return bool(extract_urls(text.strip(), expand=False))


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
        # Label match only: "x.com" in "netflix.com" is not X / Twitter.
        if host.endswith("." + key):
            return name
    return host.split(".")[0].title() if host else "Unknown"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError, OverflowError):  # inf / nan
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
    if v != v or v in (float("inf"), float("-inf")):
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
    name = name[:max_len]
    # Linux caps a filename at 255 BYTES. 80 CJK characters are 240 bytes, and
    # yt-dlp appends ".f30080.mp4.part" — so cap the UTF-8 size too.
    while len(name.encode("utf-8")) > 150:
        name = name[:-1]
    return name.rstrip(" ._") or "media"


def progress_bar(percent: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, percent))
    filled = int(round(width * pct / 100))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:.0f}%"


def analysing_percent(elapsed: float, half_life: float = 8.0) -> float:
    """
    A progress figure for a wait whose true length is unknown.

    The old curve was `min(90, elapsed * 8)`: it hit 90% at 11 seconds and then
    sat there. A 24s analysis therefore showed "90%" for 13 straight seconds,
    which reads as a frozen bot one tick away from finishing — the single most
    reported complaint about the analysing phase.

    An asymptotic curve never stalls and never over-promises: each half_life
    closes half the remaining gap to 95%, so the bar keeps visibly moving for
    as long as the wait lasts and never claims to be nearly done.
    """
    e = max(0.0, float(elapsed))
    return 95.0 * (1.0 - 0.5 ** (e / half_life))


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
