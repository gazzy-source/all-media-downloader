"""yt-dlp powered multi-platform media downloader."""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import yt_dlp

from bot.config import (
    BASE_DIR,
    COOKIES_FILE,
    FORMAT_FALLBACK,
    MAX_CONCURRENT_DOWNLOADS,
    META_CACHE_TTL,
    POT_PROVIDER_URL,
    PROXY,
    PROXY_HOSTS,
    QUALITY_MAP,
    SB_GUARD,
    TEMP_DIR,
)
from bot.utils.ffmpeg import ffmpeg_location_dir
from bot.utils.helpers import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    format_duration,
    format_size,
    platform_from_url,
    safe_filename,
    short_id,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], Any]


@dataclass
class MediaInfo:
    url: str
    title: str
    platform: str
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    view_count: int | None = None
    description: str | None = None
    is_live: bool = False
    is_playlist: bool = False
    playlist_count: int = 0
    has_video: bool = False
    has_audio: bool = False
    has_image: bool = False
    has_subtitles: bool = False
    subtitle_langs: list[str] = field(default_factory=list)
    available_heights: list[int] = field(default_factory=list)
    available_image_sizes: list[tuple[int, int]] = field(default_factory=list)
    estimated_sizes: dict[str, int] = field(default_factory=dict)
    extractor: str = ""
    webpage_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def summary_html(self) -> str:
        lines = [
            f"🎬 <b>{_esc(self.title)}</b>",
            f"📡 <b>Platform:</b> {self.platform}",
        ]
        if self.uploader:
            lines.append(f"👤 <b>Uploader:</b> {_esc(self.uploader)}")
        if self.duration:
            lines.append(f"⏱ <b>Duration:</b> {format_duration(self.duration)}")
        if self.view_count is not None:
            from bot.utils.helpers import format_views

            lines.append(f"👁 <b>Views:</b> {format_views(self.view_count)}")
        if self.is_playlist:
            lines.append(f"📑 <b>Playlist:</b> {self.playlist_count} items")
        if self.is_live:
            lines.append("🔴 <b>Live stream detected</b>")
        kinds = []
        if self.has_video:
            kinds.append("Video")
        if self.has_audio:
            kinds.append("Audio")
        if self.has_image:
            kinds.append("Image")
        if kinds:
            lines.append(f"📦 <b>Available:</b> {', '.join(kinds)}")
        if self.has_subtitles and self.subtitle_langs:
            langs = ", ".join(self.subtitle_langs[:8])
            extra = f" +{len(self.subtitle_langs) - 8}" if len(self.subtitle_langs) > 8 else ""
            lines.append(f"💬 <b>Subtitles:</b> {langs}{extra}")
        if self.available_heights:
            qs = ", ".join(f"{h}p" for h in sorted(self.available_heights, reverse=True)[:6])
            lines.append(f"📐 <b>Resolutions:</b> {qs}")
        return "\n".join(lines)


@dataclass
class DownloadResult:
    success: bool
    files: list[Path] = field(default_factory=list)
    primary: Path | None = None
    title: str = ""
    mode: str = ""
    quality: str | None = None
    file_size: int = 0
    error: str | None = None
    is_image: bool = False
    is_audio: bool = False
    is_video: bool = False
    subtitle_file: Path | None = None
    # Height yt-dlp actually delivered. May be below the requested quality when
    # a fallback client was used (e.g. cookieless YouTube capped at 360p), so
    # captions report this rather than the button the user pressed.
    actual_height: int | None = None


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Resolve once per process (avoid logging / path scans every download)
_COOKIE_SOURCE: Path | None = None  # sanitized/source jar (immutable)
_COOKIE_RESOLVED = False
_FFMPEG_DIR: str | None = None
_FFMPEG_RESOLVED = False
_IMPERSONATE = None
_IMPERSONATE_RESOLVED = False

# Sticky YouTube strategy winners — next jobs try the proven path first.
# Metadata and download are tracked separately on purpose: metadata extraction
# succeeds on clients whose media URLs later 403 (no PO token), so a metadata
# win must never pin the download path to a strategy that cannot fetch bytes.
_YT_WINNER_META: int = 0
_YT_WINNER_DL: int = 0
_YT_WINNER_LOCK = threading.Lock()

# PO-token provider reachability (resolved once per process)
_POT_ARGS: dict[str, Any] | None = None
_POT_RESOLVED = False

# Short-lived metadata cache (speeds DM wizard re-analyzes / back buttons)
_META_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_META_CACHE_LOCK = threading.Lock()
_META_CACHE_MAX = 64


def _resolved_cookie_source() -> Path | None:
    """Load/sanitize cookies once. Returns immutable source path (never mutated by yt-dlp)."""
    global _COOKIE_SOURCE, _COOKIE_RESOLVED
    if _COOKIE_RESOLVED:
        return _COOKIE_SOURCE
    _COOKIE_RESOLVED = True
    from bot.utils.cookies import prepare_cookies

    candidates: list[Path] = []
    if COOKIES_FILE:
        candidates.append(Path(COOKIES_FILE))
        candidates.append(BASE_DIR / COOKIES_FILE)
    candidates.append(BASE_DIR / "cookies.txt")
    candidates.append(Path("cookies.txt"))
    dest = BASE_DIR / "data" / "cookies.sanitized.txt"
    filtered = prepare_cookies(candidates, dest)
    source: Path | None = filtered
    if source is None:
        for p in candidates:
            try:
                if p.is_file() and p.stat().st_size > 50:
                    source = p.resolve()
                    break
            except OSError:
                continue
    _COOKIE_SOURCE = source
    if source is not None:
        logger.info("Cookies source ready: %s", source)
    return _COOKIE_SOURCE


def _cookie_jar_for_job() -> Path | None:
    """
    Per-download writable cookie copy.

    Concurrent jobs must NOT share one runtime file — yt-dlp rewrites the jar
    and would race/corrupt LOGIN_INFO across parallel downloads.
    """
    from bot.utils.cookies import make_runtime_cookie_copy

    source = _resolved_cookie_source()
    if source is None:
        return None
    dest = BASE_DIR / "data" / f"cookies.job_{short_id(10)}.txt"
    return make_runtime_cookie_copy(source, dest)


# Back-compat alias used by main.py startup logging
def _resolved_cookie() -> Path | None:
    return _resolved_cookie_source()


def _resolved_ffmpeg_dir() -> str | None:
    global _FFMPEG_DIR, _FFMPEG_RESOLVED
    if _FFMPEG_RESOLVED:
        return _FFMPEG_DIR
    _FFMPEG_RESOLVED = True
    _FFMPEG_DIR = ffmpeg_location_dir()
    return _FFMPEG_DIR


def _merge_extractor_args(
    base: dict[str, Any] | None, extra: dict[str, Any] | None
) -> dict[str, Any]:
    """
    Merge yt-dlp extractor_args one level deep.

    A plain dict.update() would drop the PO-token provider block whenever a
    strategy supplies its own `youtube` args, which silently removes the only
    cookieless path to full-quality YouTube formats.
    """
    merged: dict[str, Any] = {k: dict(v) for k, v in (base or {}).items()}
    for key, val in (extra or {}).items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key].update(val)
        else:
            merged[key] = dict(val) if isinstance(val, dict) else val
    return merged


def _pot_provider_args() -> dict[str, Any]:
    """
    extractor_args for the bgutil PO-token provider, or {} when none is usable.

    YouTube serves full-quality formats to a cookieless client only when it can
    present a PO token. Pointing the plugin at a dead endpoint on every request
    just burns a connect timeout per extraction, so an unset POT_PROVIDER_URL is
    probed once against the plugin's default local endpoint and cached.
    An explicitly configured URL is always trusted — in compose the provider
    container may still be booting when the bot resolves this.
    """
    global _POT_ARGS, _POT_RESOLVED
    if _POT_RESOLVED:
        return dict(_POT_ARGS or {})
    _POT_RESOLVED = True

    configured = bool(POT_PROVIDER_URL)
    base = (POT_PROVIDER_URL or "http://127.0.0.1:4416").rstrip("/")
    reachable = configured
    if not configured:
        import urllib.request

        try:
            with urllib.request.urlopen(f"{base}/ping", timeout=2) as resp:
                reachable = 200 <= resp.status < 400
        except Exception:
            reachable = False

    if reachable:
        _POT_ARGS = {"youtubepot-bgutilhttp": {"base_url": [base]}}
        logger.info("PO-token provider: %s (%s)", base, "configured" if configured else "detected")
    else:
        _POT_ARGS = {}
        logger.info(
            "PO-token provider: none reachable at %s — cookieless YouTube still "
            "works; the top formats may 403 and fall back to the `android` "
            "client (360p). Run bgutil-provider (see docker-compose.yml) to "
            "make every quality reachable.",
            base,
        )
    return dict(_POT_ARGS)


def pot_provider_available() -> bool:
    """True when a PO-token provider is usable (startup logging / diagnostics)."""
    return bool(_pot_provider_args())


def _resolved_impersonate():
    global _IMPERSONATE, _IMPERSONATE_RESOLVED
    if _IMPERSONATE_RESOLVED:
        return _IMPERSONATE
    _IMPERSONATE_RESOLVED = True
    try:
        # Presence check only — importing curl_cffi here just to discard it
        # reads as dead code and trips linters. yt-dlp needs it installed for
        # impersonation to work at all.
        if importlib.util.find_spec("curl_cffi") is None:
            raise ImportError("curl_cffi is not installed")
        from yt_dlp.networking.impersonate import ImpersonateTarget

        _IMPERSONATE = ImpersonateTarget.from_str("chrome")
    except Exception:
        _IMPERSONATE = None
    return _IMPERSONATE


def _platform_flags(host: str) -> dict[str, bool]:
    h = (host or "").lower()
    return {
        "yt": "youtu" in h,
        "ig": "instagram" in h or "instagr.am" in h,
        "tt": "tiktok" in h,
        "x": "twitter." in h or h.endswith("x.com") or ".x.com" in h or h == "x.com",
        "fb": "facebook." in h or "fb.watch" in h or h.endswith("fb.com"),
        "pin": "pinterest." in h or "pin.it" in h or "pinimg." in h,
        "rd": "reddit." in h or "redd.it" in h,
    }


def _base_opts(
    *,
    host: str = "",
    cookiefile: str | Path | None = None,
) -> dict[str, Any]:
    """Lean yt-dlp options tuned for small VPS + all major platforms."""
    flags = _platform_flags(host)
    is_yt = flags["yt"]
    is_ig = flags["ig"]
    is_tt = flags["tt"]
    is_x = flags["x"]
    is_fb = flags["fb"]
    is_pin = flags["pin"]
    is_rd = flags["rd"]

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 18,
        "retries": 3,
        "fragment_retries": 6,
        "file_access_retries": 2,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 256,
        # NOTE: http_chunk_size is NOT set globally. It makes yt-dlp fetch via
        # 10 MB Range requests, which breaks fragmented HLS/DASH downloads — the
        # fragment comes back unusable and yt-dlp aborts with "The downloaded
        # file is empty". Verified live: it was the sole cause of every Reddit
        # and VK download failing. It is applied for YouTube only below, where
        # chunking is the documented mitigation for throttled streams.
        "nocheckcertificate": True,
        "geo_bypass": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "extract_flat": False,
        # Skip extras that only slow extraction/download
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if is_yt:
        # No custom headers for YouTube — vanilla yt-dlp behavior (its own
        # client rotation + UAs) is the proven cookieless path.
        pass
    elif is_ig:
        opts["http_headers"]["Referer"] = "https://www.instagram.com/"
        opts["http_headers"]["Origin"] = "https://www.instagram.com"
        opts["concurrent_fragment_downloads"] = 3
    elif is_tt:
        opts["http_headers"]["Referer"] = "https://www.tiktok.com/"
        opts["concurrent_fragment_downloads"] = 3
    elif is_x:
        opts["http_headers"]["Referer"] = "https://x.com/"
        opts["concurrent_fragment_downloads"] = 3
    elif is_fb:
        opts["http_headers"]["Referer"] = "https://www.facebook.com/"
        opts["concurrent_fragment_downloads"] = 2
    elif is_pin:
        opts["http_headers"]["Referer"] = "https://www.pinterest.com/"
        opts["concurrent_fragment_downloads"] = 2
    elif is_rd:
        opts["http_headers"]["Referer"] = "https://www.reddit.com/"
        opts["concurrent_fragment_downloads"] = 3
    else:
        if host:
            opts["http_headers"]["Referer"] = f"https://{host.split(':')[0]}/"

    # Prefer caller-supplied cookie jar (per-job). Never hand yt-dlp the immutable source.
    cookie: Path | None = None
    if cookiefile is not None:
        cookie = Path(cookiefile) if cookiefile else None
    if cookie and cookie.is_file():
        opts["cookiefile"] = str(cookie)

    # NOTE: no forced YouTube player clients here — see _yt_strategies().
    # Forcing "tv"/"tv_embedded" breaks cookieless extraction on current
    # yt-dlp (client unsupported / "page needs to be reloaded").
    if is_tt:
        # Prefer mobile-friendly extraction when available
        opts.setdefault("extractor_args", {})

    imp = _resolved_impersonate()
    if imp is not None and not is_yt:
        # Chrome impersonation defeats IG/FB bot walls WITHOUT cookies.
        # YouTube extraction is most reliable with vanilla yt-dlp behavior.
        opts["impersonate"] = imp
        # Drop our hardcoded User-Agent: curl_cffi forges a Chrome TLS/JA3
        # fingerprint and sends the UA that matches the version it emulates.
        # Overriding it advertises a different Chrome than the handshake shows,
        # and anti-bot systems fingerprint exactly that mismatch. Measured on
        # Bilibili: impersonate + forced UA succeeded 1/3, either alone 3/3.
        opts["http_headers"].pop("User-Agent", None)

    if is_yt:
        # Chunked ranges keep YouTube from throttling a long single stream, and
        # cap peak RAM on a small VPS. YouTube serves plain HTTP ranges here
        # (progressive + DASH), so the fragment problem above does not apply.
        opts["http_chunk_size"] = 1024 * 1024 * 10

        # YouTube heavily throttles datacenter IPs without PO tokens (403s,
        # "Sign in to confirm you're not a bot") — no cookies needed when a
        # bgutil provider is reachable. Values must be LISTS: the plugin reads
        # them via _configuration_arg(...)[0], so a bare string would resolve to
        # its first character. Empty when no provider is usable.
        pot = _pot_provider_args()
        if pot:
            opts["extractor_args"] = _merge_extractor_args(
                opts.get("extractor_args"), pot
            )

    if PROXY and _should_proxy(host):
        opts["proxy"] = PROXY

    ff = _resolved_ffmpeg_dir()
    if ff:
        opts["ffmpeg_location"] = ff

    return opts


def _should_proxy(host: str) -> bool:
    """
    Whether this host's traffic goes through PROXY.

    With PROXY_HOSTS unset the proxy applies to everything. With it set, only
    matching hosts are proxied — so a metered residential proxy is spent on the
    platforms that actually block the server's IP, not on every video.
    """
    if not PROXY_HOSTS:
        return True
    h = (host or "").lower()
    return any(p in h for p in PROXY_HOSTS)


def _video_format_for_host(host: str, quality: str) -> str:
    """Progressive-first formats tuned per platform (fast + reliable)."""
    q = QUALITY_MAP.get(quality, QUALITY_MAP["1080"])
    h = int(q.get("height") or 1080)
    flags = _platform_flags(host)

    # Every selector below ends in an unrestricted `bv*+ba` merge before the
    # bare fallbacks. "b"/"best" only ever match a format that already carries
    # BOTH tracks, so on a DASH/HLS-only host they match nothing and the whole
    # download fails — see the Reddit branch.
    if flags["yt"]:
        return q["format"]
    if flags["ig"]:
        # Reels are usually single progressive streams
        if h >= 9999:
            return "b/bv*+ba/best"
        return f"b[height<={h}]/best[height<={h}]/bv*+ba/b/best"
    if flags["rd"]:
        # Reddit is DASH/HLS only: every video format is video-only and every
        # audio format is audio-only, so a progressive-first selector matches
        # nothing at all. The merge has to lead here (verified live: "b/best"
        # returned "Requested format is not available" on every v.redd.it post).
        if h >= 9999:
            return "bv*+ba/b/best"
        return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b/best"
    if flags["x"]:
        # X serves a real ladder (270/360/720) as progressive http-* formats,
        # so the requested cap is worth honouring instead of always taking best.
        if h >= 9999:
            return "b/bv*+ba/best"
        return f"b[height<={h}]/bv*[height<={h}]+ba/bv*+ba/b/best"
    if flags["fb"]:
        if h >= 9999:
            return "b/bv*+ba/best"
        return f"b[height<={h}]/bv*[height<={h}]+ba/bv*+ba/best"
    if flags["tt"] or flags["pin"]:
        # Usually a single progressive rendition; merge tail is pure insurance.
        return "b/bv*+ba/best"
    # Generic: progressive under cap, then merge, then anything.
    # SB_GUARD keeps SABR-era storyboard "formats" (mjpeg/mhtml) from winning
    # height-capped selectors; the unrestricted bv*+ba merge tail prefers real
    # video when only storyboards sit under the cap (requires ffmpeg).
    if h >= 9999:
        return "b/bv*+ba/best"
    return f"b[height<={h}]{SB_GUARD}/bv*[height<={h}]{SB_GUARD}+ba/bv*+ba/best"


def _parse_formats(info: dict[str, Any]) -> tuple[bool, bool, bool, list[int], list[tuple[int, int]], dict[str, int]]:
    formats = info.get("formats") or []
    has_video = False
    has_audio = False
    has_image = False
    heights: set[int] = set()
    image_sizes: list[tuple[int, int]] = []
    size_by_quality: dict[str, int] = {}

    # Direct image entries (Instagram photos, Pinterest pins, etc.)
    ext = (info.get("ext") or "").lower()
    if ext in IMAGE_EXTS or info.get("_type") == "url" and ext in IMAGE_EXTS:
        has_image = True

    for f in formats:
        # yt-dlp uses the STRING "none" to mean "this track is definitely
        # absent". A missing key or None means UNKNOWN, which is a completely
        # different claim. Collapsing unknown into "none" (`f.get(...) or
        # "none"`) made real media look empty: Twitch clips and Rumble videos
        # report vcodec=None on the only format they have and were classified
        # image-only, and X/Twitter's progressive http-* formats report
        # acodec=None, so Twitter videos never offered the Audio button.
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        height = f.get("height")
        width = f.get("width")
        fext = (f.get("ext") or "").lower()
        filesize = f.get("filesize") or f.get("filesize_approx") or 0

        # Storyboards are contact sheets, never a download target.
        is_image_fmt = (
            fext in IMAGE_EXTS
            or fext == "mhtml"
            or vcodec == "images"
            or f.get("format_note") == "storyboard"
        )
        if is_image_fmt:
            if fext in IMAGE_EXTS:
                has_image = True
                if width and height:
                    image_sizes.append((int(width), int(height)))
            continue

        # A video container with a codec that is not explicitly "none" is video
        # even when the extractor reports no height — LinkedIn returns three
        # bare mp4 renditions with no dimensions at all, which used to read as
        # an audio-only post. Audio-only formats (m4a/mp3/opus) are excluded by
        # the container check, and true video-less mp4 tracks by vcodec=="none".
        if vcodec != "none" and (height or fext in VIDEO_EXTS):
            has_video = True
            if height:
                heights.add(int(height))
                for qkey, qmeta in QUALITY_MAP.items():
                    if int(height) <= qmeta["height"]:
                        prev = size_by_quality.get(qkey, 0)
                        if filesize and filesize > prev:
                            size_by_quality[qkey] = int(filesize)
        if acodec != "none":
            has_audio = True

    # Single-format extractors return no `formats` list at all — just a
    # top-level url/ext/duration (Snapchat Spotlight, plain direct links).
    # Without this the thumbnail fallback below claimed they were photo posts,
    # so the wizard offered 🖼 Image for a video.
    if not formats and not has_video and not has_audio:
        top_url = str(info.get("url") or "")
        top_ext = ext or top_url.lower().split("?")[0].rsplit(".", 1)[-1]
        if info.get("vcodec") != "none" and (
            top_ext in VIDEO_EXTS
            or (info.get("duration") and top_ext not in IMAGE_EXTS)
        ):
            has_video = True
            has_audio = True
            if info.get("height"):
                heights.add(int(info["height"]))

    # Thumbnails as image fallback for photo posts
    thumbs = info.get("thumbnails") or []
    if not has_video and not has_audio and thumbs:
        has_image = True
        for t in thumbs:
            w, h = t.get("width"), t.get("height")
            if w and h:
                image_sizes.append((int(w), int(h)))

    # Single-image extractors often put url in info
    if not has_video and not has_audio and info.get("url"):
        u = str(info.get("url", ""))
        if any(u.lower().endswith(f".{e}") for e in IMAGE_EXTS) or ext in IMAGE_EXTS:
            has_image = True

    # If extractor says it's an image post
    if info.get("image") or (info.get("width") and info.get("height") and not has_video and ext in IMAGE_EXTS):
        has_image = True
        if info.get("width") and info.get("height"):
            image_sizes.append((int(info["width"]), int(info["height"])))

    # Media with only a video track still "has video". Same rule as the loop:
    # only the literal "none" proves a track is absent.
    if info.get("duration") and formats:
        if any(f.get("vcodec") != "none" and f.get("height") for f in formats):
            has_video = True

    # Audio-only posts (SoundCloud, music)
    if not has_video and any(f.get("acodec") != "none" for f in formats):
        has_audio = True

    # Fallback: if we have a duration and formats, treat as video/audio
    if formats and not has_video and not has_audio and not has_image:
        has_video = True
        has_audio = True

    unique_heights = sorted(heights)
    # Dedupe image sizes
    seen = set()
    uniq_imgs: list[tuple[int, int]] = []
    for wh in sorted(image_sizes, key=lambda x: x[0] * x[1], reverse=True):
        if wh not in seen:
            seen.add(wh)
            uniq_imgs.append(wh)

    return has_video, has_audio, has_image, unique_heights, uniq_imgs, size_by_quality


def _subtitle_langs(info: dict[str, Any]) -> list[str]:
    langs: set[str] = set()
    for key in ("subtitles", "automatic_captions"):
        subs = info.get(key) or {}
        for lang in subs.keys():
            if lang and lang != "live_chat":
                langs.add(lang)
    # Prefer common order
    preferred = ["en", "en-US", "en-GB", "hi", "es", "fr", "de", "pt", "ar", "ru", "ja", "ko", "zh", "zh-Hans", "zh-Hant"]
    ordered = [p for p in preferred if p in langs]
    ordered.extend(sorted(langs - set(ordered)))
    return ordered


def _normalize_info_dict(url: str, info: dict[str, Any]) -> dict[str, Any]:
    """Playlist → first entry preview; otherwise return info as-is."""
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("Playlist is empty or unavailable.")
        first = entries[0]
        first["_playlist_title"] = info.get("title")
        first["_playlist_count"] = len(entries)
        first["_is_playlist"] = True
        first["_playlist_url"] = info.get("webpage_url") or url
        return first
    return info


def _yt_strategies(*, has_cookies: bool) -> list[dict[str, Any]]:
    """
    YouTube strategy ladder — every step works without cookies.

    yt-dlp's own default client rotation carries the full format ladder, but in
    the SABR era its media URLs 403 unless a PO token is attached. Verified live
    against yt-dlp 2026.7.4 with no cookies and no provider: `tv` errors ("page
    needs to be reloaded"); `ios`, `mweb`, `tv_simply`, `web_safari` and
    `web_embedded` return storyboards only; `android_vr` lists format 18 but
    403s on the stream. `android` is the one client that actually delivers
    bytes — capped at progressive 360p, which beats failing outright.

    Order is deliberately quality-first rather than success-first: the default
    rotation leads so a trusted IP or a reachable PO-token provider still gets
    the full ladder, and `android` sits directly behind it as the guaranteed
    floor. The sticky winner promotes whichever one actually worked, so a
    server that always 403s pays the failed first attempt once per process.
    """
    # Bare progressive client: no PO token, no cookies, always returns bytes.
    android = {
        "use_cookies": False,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        "drop_impersonate": True,
    }
    android_vr = {
        "use_cookies": False,
        "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
        "drop_impersonate": True,
    }
    # Default rotation, cookieless — public videos, full format ladder
    ladder: list[dict[str, Any]] = [{"use_cookies": False}]
    if has_cookies:
        # Default rotation + cookies (age-restricted / members-only)
        ladder.append({"use_cookies": True})
    return [*ladder, android, android_vr]


def _remember_yt_strategy(original_index: int, *, download: bool) -> None:
    global _YT_WINNER_META, _YT_WINNER_DL
    with _YT_WINNER_LOCK:
        if download:
            _YT_WINNER_DL = original_index
        else:
            _YT_WINNER_META = original_index


def _yt_winner(*, download: bool) -> int:
    with _YT_WINNER_LOCK:
        return _YT_WINNER_DL if download else _YT_WINNER_META


def _order_yt_strategies(
    base_strats: list[dict[str, Any]], *, download: bool
) -> tuple[list[dict[str, Any]], list[int]]:
    """Put the last known winner first, keeping original indices for recall."""
    wi = _yt_winner(download=download)
    if 0 < wi < len(base_strats):
        ordered = [base_strats[wi], *base_strats[:wi], *base_strats[wi + 1 :]]
        indices = [wi, *range(0, wi), *range(wi + 1, len(base_strats))]
        return ordered, indices
    return list(base_strats), list(range(len(base_strats)))


def _meta_cache_get(url: str) -> dict[str, Any] | None:
    key = url.strip()
    now = time.time()
    with _META_CACHE_LOCK:
        hit = _META_CACHE.get(key)
        if not hit:
            return None
        ts, info = hit
        if now - ts > META_CACHE_TTL:
            _META_CACHE.pop(key, None)
            return None
        return info


def _meta_cache_put(url: str, info: dict[str, Any]) -> None:
    key = url.strip()
    with _META_CACHE_LOCK:
        if len(_META_CACHE) >= _META_CACHE_MAX:
            # Drop oldest ~25%
            items = sorted(_META_CACHE.items(), key=lambda kv: kv[1][0])
            for k, _ in items[: max(1, _META_CACHE_MAX // 4)]:
                _META_CACHE.pop(k, None)
        _META_CACHE[key] = (time.time(), info)


def _extract_info_sync(url: str) -> dict[str, Any]:
    """
    Metadata-only extract for the DM button wizard.
    Cached briefly; cookies + lean strategies on YouTube; single fast pass elsewhere.
    """
    cached = _meta_cache_get(url)
    if cached is not None:
        return cached

    host = urlparse(url).netloc.lower()
    flags = _platform_flags(host)
    is_yt = flags["yt"]
    job_cookies: list[Path] = []
    try:
        # Disposable jar when cookies exist (safe concurrent + never mutates source)
        cookie = _cookie_jar_for_job()
        if cookie:
            job_cookies.append(cookie)

        base = _base_opts(host=host, cookiefile=cookie)
        base.update(
            {
                "skip_download": True,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }
        )

        def _run(opts: dict[str, Any]) -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is None:
                raise RuntimeError("Could not extract media information from this URL.")
            return _normalize_info_dict(url, info)

        if not is_yt:
            # Fast single pass; one plain retry on login walls or a TLS failure
            try:
                info = _run(base)
                _meta_cache_put(url, info)
                return info
            except yt_dlp.utils.DownloadError as e:
                err = str(e).lower()
                wall = cookie and (
                    "login" in err
                    or "private" in err
                    or "cookies" in err
                    or "403" in err
                    or "rate" in err
                )
                # curl_cffi cannot always negotiate TLS with a given host or
                # network; the stdlib client usually can, so it is worth one
                # retry without impersonation even when no cookies are involved.
                transport = base.get("impersonate") is not None and (
                    "sslerror" in err
                    or "connection was reset" in err
                    or "connection reset" in err
                    or "recv failure" in err
                    or "failed to perform" in err
                )
                if wall or transport:
                    opts = dict(base)
                    opts.pop("cookiefile", None)
                    opts.pop("impersonate", None)
                    info = _run(opts)
                    _meta_cache_put(url, info)
                    return info
                raise

        base_strats = _yt_strategies(has_cookies=bool(cookie))
        strats, orig_indices = _order_yt_strategies(base_strats, download=False)
        last_err: Exception | None = None
        for si, strat in enumerate(strats):
            opts = dict(base)
            opts["http_headers"] = dict(base.get("http_headers") or {})
            if strat.get("extractor_args"):
                # Merge, don't replace — the PO-token provider block lives in
                # base and is what makes the full format ladder reachable.
                opts["extractor_args"] = _merge_extractor_args(
                    base.get("extractor_args"), strat["extractor_args"]
                )
            if strat.get("use_cookies") is False:
                opts.pop("cookiefile", None)
            elif strat.get("refresh_cookies"):
                jar = _cookie_jar_for_job()
                if jar is not None:
                    job_cookies.append(jar)
                    opts["cookiefile"] = str(jar)
            if strat.get("drop_impersonate"):
                opts.pop("impersonate", None)
            try:
                info = _run(opts)
                _remember_yt_strategy(orig_indices[si], download=False)
                _meta_cache_put(url, info)
                return info
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                err = str(e).lower()
                if (
                    "video unavailable" in err
                    or "private video" in err
                    or "has been removed" in err
                    or "unsupported url" in err
                ):
                    # Permanent for every player client — fail fast
                    raise
                # Any other client-specific failure (bot wall, page reload,
                # throttling, transient 5xx) is worth a retry with the next
                # strategy — different clients genuinely fail differently.
                logger.warning(
                    "extract_info attempt %s failed: %s",
                    si,
                    str(e).split("\n")[-1][:120],
                )
                continue
        if last_err:
            raise last_err
        raise RuntimeError("Could not extract media information from this URL.")
    finally:
        for jar in job_cookies:
            try:
                if jar.is_file() and "cookies.job_" in jar.name:
                    jar.unlink(missing_ok=True)
            except OSError:
                pass


def build_media_info(url: str, info: dict[str, Any]) -> MediaInfo:
    has_video, has_audio, has_image, heights, img_sizes, sizes = _parse_formats(info)
    subs = _subtitle_langs(info)

    title = info.get("title") or info.get("fulltitle") or "Untitled"
    platform = platform_from_url(url)
    extractor = info.get("extractor_key") or info.get("extractor") or platform

    # Improve platform from extractor
    if extractor:
        platform = extractor.replace("IE", "").replace("_", " ").strip() or platform

    thumb = info.get("thumbnail")
    if not thumb:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumb = thumbs[-1].get("url")

    return MediaInfo(
        url=url,
        title=str(title)[:300],
        platform=platform_from_url(url) if platform_from_url(url) != "Unknown" else str(platform),
        duration=info.get("duration"),
        thumbnail=thumb,
        uploader=info.get("uploader") or info.get("channel") or info.get("creator"),
        view_count=info.get("view_count"),
        description=(info.get("description") or "")[:500] or None,
        is_live=bool(info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")),
        is_playlist=bool(info.get("_is_playlist")),
        playlist_count=int(info.get("_playlist_count") or 0),
        has_video=has_video,
        has_audio=has_audio,
        has_image=has_image,
        has_subtitles=bool(subs),
        subtitle_langs=subs,
        available_heights=heights,
        available_image_sizes=img_sizes,
        estimated_sizes=sizes,
        extractor=str(extractor),
        webpage_url=info.get("webpage_url") or url,
        raw=info,
    )


_INTERNAL_ERROR_MARKERS = (
    "must be str, bytes or bytearray",
    "object is not subscriptable",
    "object has no attribute",
    "traceback (most recent call last)",
    "unhashable type",
    "takes no arguments",
    "nonetype",
    "keyerror",
    "indexerror",
    "typeerror",
    "attributeerror",
)


# yt-dlp appends maintainer-facing boilerplate to many extractor errors. It is
# addressed to whoever runs yt-dlp, not to a Telegram user, and it dominates the
# message when shown verbatim — production showed users the full "please report
# this issue on github ... Confirm you are on the latest version using yt-dlp -U"
# tail on every Substack, TikTok and Facebook failure.
_YTDLP_NOISE = re.compile(
    r"""(?:
          ;?\s*please\ report\ this\ issue\ on\s+https?://\S+.*
        | ,?\s*filling\ out\ the\ appropriate\ issue\ template\.?
        | \s*Confirm\ you\ are\ on\ the\ latest\ version\ using\s+yt-dlp\ -U\.?
        | \s*See\s+https?://github\.com/yt-dlp/\S+[^.]*\.
        | \s*Use\ --cookies(?:-from-browser)?[^.]*\.
    )""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def _clean_extractor_message(msg: str) -> str:
    """Reduce a yt-dlp error to the part a user can actually act on."""
    text = re.sub(r"^\s*ERROR:\s*", "", msg or "", flags=re.IGNORECASE)
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)      # leading "[Substack] "
    # Leading "<video-id>: " — but never an exception class name, or
    # "KeyError: 'formats'" would become "'formats'" and stop being
    # recognisable as an internal crash to mask.
    text = re.sub(r"^(?!\w*(?:Error|Exception)\b)[\w.-]{1,80}:\s+", "", text)
    text = _YTDLP_NOISE.sub("", text)
    return " ".join(text.split()).strip(" ;,")


def _looks_like_internal_error(msg: str) -> bool:
    """A Python-level crash inside an extractor, not a message meant for users."""
    low = (msg or "").lower()
    return any(m in low for m in _INTERNAL_ERROR_MARKERS)


def _delivered_height(info: dict[str, Any] | None) -> int | None:
    """Height of what was actually written, not what the user asked for."""
    if not info:
        return None
    candidates: list[Any] = [info.get("height")]
    for req in info.get("requested_downloads") or []:
        candidates.append(req.get("height"))
    # A merge writes separate video/audio entries; the video one carries height.
    for fmt in info.get("requested_formats") or []:
        candidates.append(fmt.get("height"))
    heights = []
    for c in candidates:
        try:
            h = int(c)
        except (TypeError, ValueError):
            continue
        if h > 0:
            heights.append(h)
    return max(heights) if heights else None


async def _emit_progress(progress_cb: ProgressCallback | None, pct: float, msg: str) -> None:
    if not progress_cb:
        return
    try:
        result = progress_cb(pct, msg)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


class DownloadManager:
    """Async-friendly download manager with concurrency limit."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_DOWNLOADS) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self.active = 0
        self.waiting = 0
        # Dedicated pool so concurrent downloads aren't starved by default executor size
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrent + 2,
            thread_name_prefix="yt-dl",
        )

    @property
    def free_slots(self) -> int:
        return max(0, self.max_concurrent - self.active)

    async def extract_info(self, url: str) -> MediaInfo:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(self._executor, _extract_info_sync, url)
        return build_media_info(url, info)

    async def download(
        self,
        url: str,
        mode: str,
        quality: str = "720",
        subtitle_lang: str | None = None,
        audio_format: str = "mp3",
        title_hint: str = "media",
        progress_cb: ProgressCallback | None = None,
    ) -> DownloadResult:
        self.waiting += 1
        try:
            # Only show "queued" when we would actually wait for a free slot
            if self.active >= self.max_concurrent:
                pos = self.waiting
                await _emit_progress(
                    progress_cb,
                    0,
                    f"Queued · position {pos} ({self.active}/{self.max_concurrent} running)",
                )
            await self._sem.acquire()
        finally:
            self.waiting -= 1

        self.active += 1
        try:
            await _emit_progress(
                progress_cb,
                1,
                f"Starting… ({self.active}/{self.max_concurrent} parallel)",
            )
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor,
                lambda: self._download_sync(
                    url=url,
                    mode=mode,
                    quality=quality,
                    subtitle_lang=subtitle_lang,
                    audio_format=audio_format,
                    title_hint=title_hint,
                    progress_cb=progress_cb,
                    loop=loop,
                ),
            )
            # Auto: video request on image-only posts (Pinterest pins, etc.)
            if (
                not result.success
                and mode == "video"
                and self._looks_like_image_only_error(result.error or "")
            ):
                logger.info("Retrying as image download for %s", url)
                await _emit_progress(progress_cb, 5, "Retrying as image…")
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._download_sync(
                        url=url,
                        mode="image",
                        quality=quality,
                        subtitle_lang=subtitle_lang,
                        audio_format=audio_format,
                        title_hint=title_hint,
                        progress_cb=progress_cb,
                        loop=loop,
                    ),
                )
            return result
        finally:
            self.active -= 1
            self._sem.release()

    @staticmethod
    def _looks_like_image_only_error(err: str) -> bool:
        low = (err or "").lower()
        if "403" in low or "forbidden" in low:
            return False
        return any(
            s in low
            for s in (
                "no video formats",
                "no video on this link",
                "likely an image",
                "only images are available",
                "image-only",
                "there is no video",
                "no video could be found",
                "no image found",
                "pinterest",
            )
        )

    def _download_sync(
        self,
        url: str,
        mode: str,
        quality: str,
        subtitle_lang: str | None,
        audio_format: str,
        title_hint: str,
        progress_cb: ProgressCallback | None,
        loop: asyncio.AbstractEventLoop,
    ) -> DownloadResult:
        work_dir = TEMP_DIR / f"dl_{short_id(12)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(work_dir / f"{safe_filename(title_hint)}.%(ext)s")
        job_cookies: list[Path] = []

        last_pct = {"v": -1.0}
        last_tick = {"t": 0.0}

        def _emit(pct: float, msg: str) -> None:
            if not progress_cb:
                return
            try:
                result = progress_cb(pct, msg)
                if asyncio.iscoroutine(result):
                    asyncio.run_coroutine_threadsafe(result, loop)
            except Exception:
                pass

        def hook(d: dict[str, Any]) -> None:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                speed = d.get("speed")
                speed_s = format_size(speed) + "/s" if speed else "—"
                if not total:
                    # yt-dlp cannot always report a total (some fragmented
                    # streams). A percentage would be invented, so report the
                    # bytes actually fetched, on a timer.
                    now = time.time()
                    if now - last_tick["t"] < 3:
                        return
                    last_tick["t"] = now
                    _emit(0, f"⬇ {format_size(done)} · {speed_s}")
                    return
                pct = done / total * 100
                # Throttle Telegram edits (still update often enough to feel
                # live) — but never swallow the FIRST update. last_pct starts
                # negative, so a download that begins under 8% used to have
                # every early tick dropped and the bar stayed on "Resolving…".
                if last_pct["v"] >= 0 and abs(pct - last_pct["v"]) < 8 and pct < 95:
                    return
                last_pct["v"] = pct
                _emit(pct, f"⬇ {pct:.0f}% · {speed_s}")
            elif status == "finished":
                _emit(100, "⚙️ Finishing…")

        host = urlparse(url).netloc.lower()
        # Disposable jar for every download so concurrent jobs never corrupt cookies
        cookie_path = _cookie_jar_for_job()
        if cookie_path:
            job_cookies.append(cookie_path)

        _emit(2, "Resolving…")
        opts = _base_opts(host=host, cookiefile=cookie_path)
        hooks = [hook] if progress_cb else []
        opts.update(
            {
                "outtmpl": outtmpl,
                "progress_hooks": hooks,
                "noplaylist": True,
                "writethumbnail": False,
            }
        )

        try:
            if mode == "audio":
                opts.update(
                    {
                        "format": "bestaudio[ext=m4a]/bestaudio/best",
                        "postprocessors": [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": audio_format,
                                "preferredquality": "192",
                            },
                        ],
                    }
                )
            elif mode == "image":
                return self._download_image_page(
                    url=url,
                    work_dir=work_dir,
                    title_hint=title_hint,
                    progress_cb=progress_cb,
                    loop=loop,
                )
            elif mode == "video_subs":
                lang = subtitle_lang or "en.*"
                opts.update(
                    {
                        "format": _video_format_for_host(host, quality),
                        "merge_output_format": "mp4",
                        "writesubtitles": True,
                        "writeautomaticsub": True,
                        "subtitleslangs": [lang, "en"],
                        "subtitlesformat": "srt/best",
                        "embedsubtitles": True,
                        "postprocessors": [
                            {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False},
                        ],
                    }
                )
            else:  # video — progressive-first per platform
                opts.update(
                    {
                        "format": _video_format_for_host(host, quality),
                        "merge_output_format": "mp4",
                    }
                )

            info, prepared, title = self._extract_with_format_fallback(
                opts, url, title_hint, job_cookies=job_cookies,
                on_stage=_emit,
            )

            files = sorted(
                [p for p in work_dir.iterdir() if p.is_file() and not p.name.endswith(".part")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

            if not files:
                # Fallback to prepared path
                p = Path(prepared)
                if p.exists():
                    files = [p]
                else:
                    # Try common extensions
                    for ext in ("mp4", "mkv", "webm", "mp3", "m4a", "opus", "jpg", "png", "webp", "gif"):
                        candidate = work_dir / f"{safe_filename(title_hint)}.{ext}"
                        if candidate.exists():
                            files = [candidate]
                            break

            if not files:
                raise RuntimeError("Download finished but no output file was found.")

            # Prefer main media over subtitle sidecar
            primary = files[0]
            sub_file = None
            media_files = [
                f
                for f in files
                if f.suffix.lower()
                not in {".srt", ".vtt", ".ass", ".ttml", ".json3", ".srv1", ".srv2", ".srv3"}
            ]
            if media_files:
                primary = media_files[0]
            for f in files:
                if f.suffix.lower() in {".srt", ".vtt", ".ass"}:
                    sub_file = f
                    break

            # NOTE: mode == "image" never reaches here — it returns early via
            # _download_image_page() above.

            size = primary.stat().st_size if primary.exists() else 0
            ext = primary.suffix.lower().lstrip(".")
            return DownloadResult(
                success=True,
                files=files,
                primary=primary,
                title=title,
                mode=mode,
                quality=quality if mode in ("video", "video_subs") else None,
                file_size=size,
                is_image=ext in IMAGE_EXTS,
                is_audio=ext in {"mp3", "m4a", "opus", "ogg", "flac", "wav", "aac"},
                is_video=ext in {"mp4", "mkv", "webm", "mov", "avi", "m4v", "3gp"},
                subtitle_file=sub_file,
                actual_height=_delivered_height(info),
            )
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).split("\n")[-1][:300]
            # Expected failures: keep logs light for speed/noise
            if self._looks_like_image_only_error(msg) or "403" in msg.lower():
                logger.warning("Download error for %s: %s", url[:80], msg[:160])
            else:
                logger.exception("Download error for %s", url)
            self._cleanup_dir(work_dir)
            return DownloadResult(success=False, error=self._friendly_error(msg), mode=mode, quality=quality)
        except Exception as e:
            logger.exception("Unexpected download failure for %s", url)
            self._cleanup_dir(work_dir)
            raw = str(e).strip() or f"{type(e).__name__}: {e!r}"
            return DownloadResult(
                success=False,
                error=self._friendly_error(raw) if raw else f"{type(e).__name__}",
                mode=mode,
                quality=quality,
            )
        finally:
            for jar in job_cookies:
                try:
                    if jar.is_file() and "cookies.job_" in jar.name:
                        jar.unlink(missing_ok=True)
                except OSError:
                    pass

    def _extract_with_format_fallback(
        self,
        opts: dict[str, Any],
        url: str,
        title_hint: str,
        job_cookies: list[Path] | None = None,
        on_stage: Callable[[float, str], None] | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        """
        Fast path first; fallbacks only on bot-check / format / 403 errors.
        Sticky YouTube strategy winner for subsequent jobs.
        """
        host = urlparse(url).netloc.lower()
        flags = _platform_flags(host)
        is_yt = flags["yt"]
        primary = opts.get("format") or FORMAT_FALLBACK
        tracked = job_cookies if job_cookies is not None else []
        initial_cookie = opts.get("cookiefile")

        if is_yt:
            base_strats = _yt_strategies(has_cookies=bool(initial_cookie))
            strategies, orig_indices = _order_yt_strategies(base_strats, download=True)
        elif flags["ig"] or flags["fb"] or flags["x"]:
            # Cookieless-first: impersonated pass (public posts work without
            # cookies), then without cookies, then without impersonation.
            # With no cookies the first two passes are byte-identical, so the
            # cookie pass is only worth scheduling when a jar actually exists.
            strategies = [
                *([{"use_cookies": True}] if initial_cookie else []),
                {"use_cookies": False},
                {"use_cookies": False, "drop_impersonate": True},
            ]
            orig_indices = list(range(len(strategies)))
        else:
            # Everything else: one plain pass, then one without Chrome
            # impersonation. curl_cffi can fail the TLS handshake outright
            # ("Recv failure: Connection was reset") on networks or hosts it
            # cannot negotiate with, and the stdlib client usually can.
            strategies = [{}, {"drop_impersonate": True}]
            orig_indices = [0, 1]

        last_err: Exception | None = None
        for si, strat in enumerate(strategies):
            # Prefer requested quality; only try b/best if that format fails
            formats_to_try = [primary]
            if primary != FORMAT_FALLBACK and primary != "b/best":
                pass  # b/best added only on format failure
            attempt_base = dict(opts)
            attempt_base["http_headers"] = dict(opts.get("http_headers") or {})

            if strat.get("extractor_args"):
                # Merge so the PO-token provider block survives a client pin.
                attempt_base["extractor_args"] = _merge_extractor_args(
                    opts.get("extractor_args"), strat["extractor_args"]
                )
            if strat.get("use_cookies") is False:
                attempt_base.pop("cookiefile", None)
            elif strat.get("refresh_cookies"):
                jar = _cookie_jar_for_job()
                if jar is not None:
                    tracked.append(jar)
                    attempt_base["cookiefile"] = str(jar)
            elif initial_cookie:
                attempt_base["cookiefile"] = initial_cookie
            if strat.get("drop_impersonate"):
                attempt_base.pop("impersonate", None)

            if on_stage is not None:
                # yt-dlp only drives progress_hooks once bytes are moving, so
                # extraction — 5s on a good day, far longer when strategies
                # retry — showed a frozen "Resolving… 2%". Give each attempt a
                # visible tick so the job never looks hung.
                if si == 0:
                    on_stage(3, "🔎 Resolving media…")
                else:
                    on_stage(
                        min(3 + si, 6),
                        f"🔎 Trying another source… ({si + 1}/{len(strategies)})",
                    )

            fi = 0
            while fi < len(formats_to_try):
                fmt = formats_to_try[fi]
                attempt_opts = dict(attempt_base)
                attempt_opts["format"] = fmt
                try:
                    with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        if info is None:
                            raise RuntimeError("Download returned no data.")
                        prepared = ydl.prepare_filename(info)
                        title = str(info.get("title") or title_hint)[:200]
                        if is_yt:
                            _remember_yt_strategy(orig_indices[si], download=True)
                            if si or fi:
                                logger.info(
                                    "YT strategy ok si=%s fmt=%s cookies=%s",
                                    si,
                                    fmt[:40],
                                    bool(attempt_opts.get("cookiefile")),
                                )
                        return info, prepared, title
                except yt_dlp.utils.DownloadError as e:
                    last_err = e
                    err = str(e).lower()
                    if "no video formats" in err or "only images" in err:
                        if not is_yt:
                            raise
                    format_issue = (
                        "format is not available" in err
                        or "requested format" in err
                    )
                    bot_check = (
                        "not a bot" in err
                        or "sign in to confirm" in err
                        or "cookies are no longer valid" in err
                        or "login required" in err
                    )
                    stream_fail = (
                        "http error 403" in err
                        or "unable to download video data" in err
                    )
                    # Transport-level failures say nothing about the format, so
                    # a plain retry on the next strategy (which may drop Chrome
                    # impersonation) is the only thing worth trying.
                    transport_fail = (
                        "sslerror" in err
                        or "connection was reset" in err
                        or "connection reset" in err
                        or "recv failure" in err
                        or "failed to perform" in err
                        or "connection aborted" in err
                        or "remote end closed" in err
                    )
                    # One format fallback for quality/403 before next strategy
                    if (format_issue or stream_fail) and fi == 0 and primary not in (
                        "b/best",
                        FORMAT_FALLBACK,
                    ):
                        formats_to_try.append("b/best")
                        logger.warning(
                            "Format fallback (si=%s): %s",
                            si,
                            str(e).split("\n")[-1][:100],
                        )
                        fi += 1
                        continue
                    if bot_check or format_issue or stream_fail or transport_fail:
                        logger.warning(
                            "Attempt failed (si=%s fi=%s): %s",
                            si,
                            fi,
                            str(e).split("\n")[-1][:120],
                        )
                        break  # next strategy
                    raise
                fi += 1
        if last_err:
            raise last_err
        raise RuntimeError("Download failed with all format selectors.")

    def _download_image_page(
        self,
        url: str,
        work_dir: Path,
        title_hint: str,
        progress_cb: ProgressCallback | None,
        loop: asyncio.AbstractEventLoop,
    ) -> DownloadResult:
        """Download still images from pins/posts when yt-dlp has no video formats."""
        try:
            if progress_cb:
                try:
                    r = progress_cb(10, "🖼 Looking for image…")
                    if asyncio.iscoroutine(r):
                        asyncio.run_coroutine_threadsafe(r, loop)
                except Exception:
                    pass

            # Reuse media_detect HTML cache when available
            from bot.services.media_detect import _fetch_html_cached

            html = _fetch_html_cached(url) if url else ""
            candidates = self._discover_image_urls(url, html=html or None)
            if not candidates:
                return DownloadResult(
                    success=False,
                    error="No image found on this page.",
                    mode="image",
                )

            # Prefer originals / largest
            def rank(u: str) -> tuple[int, int]:
                score = 0
                lu = u.lower()
                if "originals" in lu:
                    score += 100
                if "/736x/" in lu or "736x" in lu:
                    score += 50
                if "/564x/" in lu:
                    score += 30
                if lu.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    score += 5
                return (score, len(u))

            candidates = sorted(set(candidates), key=rank, reverse=True)
            title = title_hint or "image"
            last_err = None
            for img_url in candidates[:8]:
                ext = self._guess_image_ext(img_url)
                dest = work_dir / f"{safe_filename(title)}.{ext}"
                if progress_cb:
                    try:
                        r = progress_cb(40, "🖼 Downloading image…")
                        if asyncio.iscoroutine(r):
                            asyncio.run_coroutine_threadsafe(r, loop)
                    except Exception:
                        pass
                path = self._fetch_url_file(img_url, dest)
                if path and path.stat().st_size > 500:
                    size = path.stat().st_size
                    return DownloadResult(
                        success=True,
                        files=[path],
                        primary=path,
                        title=title[:200],
                        mode="image",
                        file_size=size,
                        is_image=True,
                    )
                last_err = f"Could not fetch {img_url[:80]}"
            return DownloadResult(
                success=False,
                error=last_err or "Image download failed.",
                mode="image",
            )
        except Exception as e:
            logger.exception("image page download failed")
            return DownloadResult(success=False, error=str(e)[:300], mode="image")

    def _discover_image_urls(
        self, page_url: str, html: str | None = None
    ) -> list[str]:
        """Find direct image URLs from a social/image page (Pinterest, etc.)."""
        found: list[str] = []
        if any(page_url.lower().endswith(f".{e}") for e in IMAGE_EXTS):
            return [page_url]

        if html is None:
            from bot.services.media_detect import _fetch_html_cached

            html = _fetch_html_cached(page_url)
        if not html:
            return found

        patterns = [
            r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
            r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
            r'"url"\s*:\s*"(https://i\.pinimg\.com/originals/[^"]+)"',
            r'(https://i\.pinimg\.com/originals/[a-f0-9/._-]+\.(?:jpg|jpeg|png|webp|gif))',
            r'(https://i\.pinimg\.com/(?:736x|564x)/[a-f0-9/._-]+\.(?:jpg|jpeg|png|webp|gif))',
        ]
        for pat in patterns:
            for m in re.findall(pat, html, flags=re.I):
                u = unquote(m.replace("\\u002F", "/").replace("\\/", "/"))
                u = u.split(")")[0].split("}")[0].rstrip("\\")
                if not u.startswith("http"):
                    continue
                low = u.lower()
                if any(x in low for x in ("favicon", "sprite", "logo", "1x1", "pixel")):
                    continue
                found.append(u)

        pin_orig = [u for u in found if "pinimg.com/originals/" in u]
        if pin_orig:
            return list(dict.fromkeys(pin_orig))
        return list(dict.fromkeys(found))

    @staticmethod
    def _guess_image_ext(url: str) -> str:
        path = urlparse(url).path.lower()
        for e in ("jpg", "jpeg", "png", "webp", "gif"):
            if path.endswith("." + e):
                return "jpg" if e == "jpeg" else e
        return "jpg"

    def _fetch_url_file(self, url: str, dest: Path) -> Path | None:
        try:
            import urllib.request

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.pinterest.com/",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
            return dest if dest.exists() and dest.stat().st_size > 0 else None
        except Exception:
            logger.exception("Failed to fetch image %s", url)
            return None

    @staticmethod
    def _friendly_error(msg: str) -> str:
        msg = _clean_extractor_message(msg)
        low = msg.lower()
        if (
            "sign in to confirm" in low
            or "confirm you're not a bot" in low
            or "confirm you are not a bot" in low
            or "not a bot" in low
            or "cookies are no longer valid" in low
        ):
            # Don't advise running a provider when one is already running — on a
            # bot-walled datacenter IP it mints tokens fine and YouTube still
            # refuses every client, so the only real remedy left is a cleaner IP.
            if pot_provider_available():
                return (
                    "YouTube bot-walled this server on every client it tried, "
                    "even with a PO-token provider running.\n\n"
                    "That means this server's IP is blocked outright — common on "
                    "datacenter/VPS ranges. Cookies are not the issue; public "
                    "videos need none.\n\n"
                    "Fix: set PROXY to a residential/mobile IP (PROXY_HOSTS can "
                    "limit it to just the platforms that need it)."
                )
            return (
                "YouTube bot-walled this server on every client it tried.\n\n"
                "The bot needs no cookies for public videos — it falls back to a "
                "client that works without them. If even that fails, the server's "
                "IP is blocked outright.\n\n"
                "Fixes, easiest first:\n"
                "1. Run a PO-token provider (bgutil) — see docker-compose.yml\n"
                "2. Set PROXY to a residential/clean IP\n"
                "3. Only for private or age-restricted videos: add a cookies.txt"
            )
        if "private" in low or "login required" in low or "sign in" in low:
            return (
                "This content is private, age-restricted, or requires login. "
                "Public posts need no setup; this one needs a cookies.txt on the "
                "server."
            )
        # Must be before generic "not available" (format errors were mislabeled as region)
        if "format is not available" in low or "requested format" in low:
            return (
                "That quality/format isn't offered for this link. "
                "Try Max quality, or Video again — the bot will auto-fallback."
            )
        if "only images are available" in low or "no video formats" in low:
            return (
                "No video on this link (likely an image post). "
                "The bot will retry as image automatically; "
                "in private chat pick 🖼 Image."
            )
        if "geo" in low or "region" in low or "not available in your country" in low:
            return "This media is blocked in the server's region."
        if "unavailable" in low or "has been removed" in low or "video is not available" in low:
            return "This media is unavailable or has been removed."
        if "copyright" in low or "blocked" in low:
            return "This media is blocked due to copyright or platform restrictions."
        # Extractor broken against the live site — nothing the user can change.
        if (
            "unexpected response" in low
            or "cannot parse data" in low
            or "unable to extract" in low
        ):
            return (
                "This platform's extractor is currently failing — the site "
                "changed and yt-dlp needs an update on the server. Nothing you "
                "did wrong; try a different link or try again later."
            )
        if (
            "is not supported" in low  # e.g. Substack: page type "newsletter"
            or "unsupported url" in low
            or "no suitable extractor" in low
            or "no media found" in low
        ):
            return (
                "No downloadable media on this link.\n\n"
                "It looks like an article, newsletter or plain web page rather "
                "than a video, audio or image post. Send the direct link to the "
                "media itself."
            )
        if "ffmpeg" in low or "ffprobe" in low:
            return "FFmpeg is required for this format. Install FFmpeg and try again."
        if "timed out" in low or "timeout" in low:
            return "The download timed out. Please try again."
        if "rate-limit" in low or "rate limit" in low or "too many requests" in low:
            return "The platform rate-limited the bot. Wait a minute and try again."
        if "403" in low or "forbidden" in low:
            return (
                "The platform blocked the media stream (HTTP 403) on every client "
                "the bot tried. For YouTube this usually means the server needs a "
                "PO-token provider (bgutil) or a cleaner IP — cookies are not "
                "required for public videos."
            )
        # Anything left is either an extractor message worth showing verbatim or
        # a raw Python exception from a broken extractor. Leaking the latter
        # ("the JSON object must be str, bytes or bytearray, not dict" — a real
        # yt-dlp OK.ru crash) tells a Telegram user nothing and looks broken.
        if _looks_like_internal_error(msg):
            return (
                "This platform's extractor failed on this link — usually the site "
                "changed and yt-dlp needs an update. Try another link, or update "
                "yt-dlp on the server."
            )
        return msg or "Download failed for an unknown reason."

    @staticmethod
    def _cleanup_dir(path: Path) -> None:
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    @staticmethod
    def cleanup_result_files(result: DownloadResult) -> None:
        dirs: set[Path] = set()
        for f in result.files:
            try:
                if f.exists():
                    dirs.add(f.parent)
                    f.unlink(missing_ok=True)
            except Exception:
                pass
        for d in dirs:
            try:
                if d.exists() and d != TEMP_DIR and not any(d.iterdir()):
                    d.rmdir()
                elif d.exists() and d != TEMP_DIR:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


download_manager = DownloadManager()


def available_qualities_for(heights: list[int]) -> list[str]:
    """Return quality keys that make sense for the media."""
    if not heights:
        return ["max", "1080", "720", "480"]
    max_h = max(heights)
    opts: list[str] = []
    for key, meta in (("480", 480), ("720", 720), ("1080", 1080)):
        # Offer if source has at least something near that tier or higher
        if max_h >= meta * 0.7 or any(h <= meta for h in heights):
            if any(h >= min(meta, max_h) * 0.5 for h in heights) or max_h >= meta:
                opts.append(key)
    # Always offer max
    if "max" not in opts:
        opts.append("max")
    # Filter: only show 1080 if max_h >= 720, etc. more user-friendly
    filtered: list[str] = []
    for k in ("480", "720", "1080"):
        if k in opts and max_h >= int(k) * 0.5:
            filtered.append(k)
    filtered.append("max")
    # Deduplicate
    return list(dict.fromkeys(filtered))


def quality_buttons_meta(heights: list[int], estimated: dict[str, int] | None = None) -> list[dict[str, str]]:
    keys = available_qualities_for(heights)
    estimated = estimated or {}
    out = []
    for k in keys:
        label = QUALITY_MAP[k]["label"]
        size = estimated.get(k)
        if size:
            label = f"{label} (~{format_size(size)})"
        elif heights:
            max_h = max(heights)
            if k != "max":
                h = QUALITY_MAP[k]["height"]
                actual = max([x for x in heights if x <= h], default=None)
                if actual:
                    label = f"{label}"
                elif max_h < h:
                    label = f"{label} (up to {max_h}p)"
            else:
                label = f"Max ({max_h}p)"
        out.append({"key": k, "label": label})
    return out
