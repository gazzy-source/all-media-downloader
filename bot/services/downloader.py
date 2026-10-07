"""yt-dlp powered multi-platform media downloader."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import importlib.util
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import urllib.parse
from urllib.parse import unquote, urlparse

import yt_dlp

from bot.config import (
    BASE_DIR,
    COOKIES_FILE,
    DATA_DIR,
    DOWNLOAD_ATTEMPT_BUDGET,
    DOWNLOAD_MAX_BYTES,
    DOWNLOAD_REUSE_TTL,
    DOWNLOAD_MAX_SECONDS,
    EXTRACT_TIMEOUT,
    FORMAT_FALLBACK,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_FILE_SIZE_BYTES,
    MAX_MEDIA_DURATION,
    META_CACHE_TTL,
    METADATA_RETRIES,
    METADATA_SOCKET_TIMEOUT,
    POT_PROVIDER_URL,
    PROXY,
    PROXY_HOSTS,
    QUALITY_MAP,
    PROXY_BLIP_BACKOFF,
    PROXY_BLIP_RETRIES,
    SB_GUARD,
    TEMP_DIR,
    WARMUP_URL,
    YT_LEAN_DOWNLOAD,
    YT_LEAN_METADATA,
)
from bot.utils.ffmpeg import ffmpeg_location_dir
from bot.utils.safe_fetch import UnresolvableURLError, UnsafeURLError, check_public_url
from bot.utils import redact
from bot.utils.deadline import DeadlineExceeded, deadline
from bot.utils.warp import rotate_warp_ip
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
    # Smallest plausible size per tier — the bar for refusing up front.
    min_sizes: dict[str, int] = field(default_factory=dict)
    extractor: str = ""
    webpage_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def summary_html(self) -> str:
        lines = [
            f"🎬 <b>{_esc(self.title)}</b>",
            f"📡 <b>Platform:</b> {_esc(self.platform or '')}",
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
            langs = _esc(", ".join(self.subtitle_langs[:8]))
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
    # Performer shown by Telegram's music player (audio only).
    artist: str | None = None
    # Small square cover (<=320px JPEG) for Telegram's audio thumbnail.
    cover: Path | None = None


@dataclass(frozen=True)
class QualityRecommendation:
    """A lower quality supported by current format metadata and its size data."""

    quality: str
    height: int
    estimated_bytes: int
    is_exact: bool = False


_PART_RE = re.compile(r"\.f[\w-]+\.\w+(?:\.part)?$")
_PP_LABELS = {
    "Merger": "🔗 Joining video + audio…",
    "FFmpegMerger": "🔗 Joining video + audio…",
    "ExtractAudio": "🎵 Converting audio…",
    "FFmpegExtractAudio": "🎵 Converting audio…",
    "EmbedThumbnail": "🎨 Adding cover art…",
    "Metadata": "🏷 Adding title & artist…",
    "FFmpegMetadata": "🏷 Adding title & artist…",
    "EmbedSubtitle": "💬 Adding subtitles…",
    "FFmpegEmbedSubtitle": "💬 Adding subtitles…",
}


def _part_kind(d: dict[str, Any]) -> str:
    """
    "video"/"audio" when this file is one half of a video+audio pair (yt-dlp
    names those *.f<format>.<ext>), else "single".
    """
    name = str(d.get("filename") or d.get("tmpfilename") or "")
    if not _PART_RE.search(name):
        return "single"
    vcodec = (d.get("info_dict") or {}).get("vcodec")
    return "audio" if vcodec == "none" else "video"


def _telegram_cover(work_dir: Path) -> Path | None:
    """
    Telegram wants an audio thumbnail as a JPEG of at most 320x320 and 200 KB;
    the YouTube cover is 1280x720. Square-crop and shrink it. Best effort.
    """
    import subprocess

    from bot.utils.ffmpeg import find_ffmpeg

    covers = [p for p in work_dir.glob("*.jpg") if not p.name.startswith("tg_cover")]
    ff = find_ffmpeg()
    if not covers or ff is None:
        return None
    out = work_dir / "tg_cover.jpg"
    try:
        subprocess.run(
            [str(ff), "-loglevel", "error", "-y", "-i", str(covers[0]), "-vf",
             "scale=320:320:force_original_aspect_ratio=increase,crop=320:320",
             "-q:v", "4", str(out)],
            check=True, timeout=20, capture_output=True,
        )
        return out if out.is_file() and out.stat().st_size < 200_000 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _overall_pct(part: str, file_pct: float) -> float:
    """The video half is ~90% of the bytes, the audio ~10%."""
    if part == "video":
        return file_pct * 0.9
    if part == "audio":
        return 90 + file_pct * 0.1
    return file_pct


_META_INFLIGHT = 0  # analyses running right now (the yt-meta pool)
_META_INFLIGHT_LOCK = threading.Lock()


class _MetaFlight:
    """One URL's extraction, shared by every caller asking for it meanwhile."""

    __slots__ = ("cf", "fut", "started", "waiters")

    def __init__(self, cf: concurrent.futures.Future, fut: asyncio.Future,
                 started: dict) -> None:
        self.cf = cf          # the pool job (cancel() only works before it starts)
        self.fut = fut        # its asyncio side, awaited through shield()
        self.started = started
        self.waiters = 0


def _rotation_harmless(own_downloads: int) -> bool:
    """True when a WARP rotation would cut no one else's connection."""
    from bot.services.dl_queue import download_queue

    own_meta = 0 if own_downloads else 1
    return download_queue.running <= own_downloads and _META_INFLIGHT <= own_meta


def _bot_idle() -> bool:
    from bot.services import activity
    from bot.services.dl_queue import download_queue

    return (activity.idle_for() > 90 and download_queue.running == 0
            and download_queue.waiting == 0)


def _operator_hint(text: str) -> None:
    """Server-side fixes belong in the log, never in a user's chat."""
    logger.warning("Operator hint: %s", text)


def _artist_of(info: dict[str, Any] | None) -> str | None:
    """Best available artist: tagged artist, else the channel (minus " - Topic")."""
    if not info:
        return None
    name = info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel")
    if not name:
        return None
    name = str(name).split(",")[0].strip()
    return name[: -len(" - Topic")] if name.endswith(" - Topic") else name


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
# When each pin was set. The pin is an optimisation, not a conclusion: once the
# `android` fallback wins (it always returns bytes, capped at 360p) nothing
# ever re-probes the full ladder, so one transient 403 could hold the whole
# process at 360p until the next restart. Let the pin expire so quality
# recovers on its own.
_YT_WINNER_META_AT: float = 0.0
_YT_WINNER_DL_AT: float = 0.0
_YT_WINNER_TTL = 600.0
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
    dest = DATA_DIR / "cookies.sanitized.txt"
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
    dest = DATA_DIR / f"cookies.job_{short_id(10)}.txt"
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


def pot_provider_mint_check(timeout: float = 25.0) -> tuple[bool, str]:
    """
    Ask the provider to actually mint a token, through the proxy YouTube uses.

    `/ping` only proves the HTTP server is up. The bgutil plugin forwards
    yt-dlp's proxy to the provider, so a provider that answers /ping can still
    fail every real mint — which is exactly what a containerised provider does
    when PROXY points at a host-local SOCKS port it cannot reach from inside
    its own network namespace. That failure is silent: YouTube just starts
    answering "Sign in to confirm you're not a bot" and the startup banner
    still claims the provider is fine.

    Returns (ok, detail). Never raises.
    """
    args = _pot_provider_args()
    if not args:
        return False, "no provider configured"
    base = args["youtubepot-bgutilhttp"]["base_url"][0]

    import json
    import urllib.error
    import urllib.request

    payload: dict[str, Any] = {"content_binding": "startup-selfcheck"}
    # Mirror what a real YouTube extraction sends, proxy included.
    if PROXY and _should_proxy("www.youtube.com"):
        payload["proxy"] = PROXY
    req = urllib.request.Request(
        f"{base}/get_pot",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if e.fp else str(e)
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if body.get("poToken"):
        return True, "minted a token"
    return False, str(body.get("error") or body)[:300]


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
        "x": "twitter." in h or h == "x.com" or h.endswith(".x.com"),
        "fb": "facebook." in h or "fb.watch" in h or h.endswith("fb.com"),
        "pin": "pinterest." in h or "pin.it" in h or "pinimg." in h,
        "rd": "reddit." in h or "redd.it" in h,
    }


def _is_youtube_bot_wall(message: str) -> bool:
    """Recognize YouTube's explicit IP-level anti-bot response."""
    low = (message or "").lower().replace("’", "'")
    return (
        "sign in to confirm" in low and "not a bot" in low
    ) or "confirm you're not a bot" in low or "confirm you are not a bot" in low


def _yt_strategy_label(strategy: dict[str, Any], opts: dict[str, Any]) -> str:
    """Safe, low-cardinality strategy description for operational logs."""
    yt_args = (opts.get("extractor_args") or {}).get("youtube") or {}
    clients = yt_args.get("player_client") or []
    client = "+".join(str(c) for c in clients) if clients else "default"
    if strategy.get("_warp_retry"):
        client += "+warp-retry"
    return client


def _yt_failure_class(message: str) -> str:
    """Classify failures without logging URLs, tokens, or raw extractor text."""
    low = (message or "").lower()
    if _is_youtube_bot_wall(low):
        return "bot_wall"
    if any(x in low for x in ("socks5error", "proxyerror", "proxy error", "connection refused")):
        return "proxy_transport"
    if "http error 403" in low or "unable to download video data" in low:
        return "media_403"
    if "private video" in low or "video unavailable" in low or "has been removed" in low:
        return "content_unavailable"
    if "age" in low and ("confirm" in low or "restricted" in low):
        return "age_restricted"
    if any(x in low for x in ("sslerror", "tls", "connection was reset", "recv failure")):
        return "transport"
    return "extractor_error"


def probe_youtube_media_bytes(info: dict[str, Any], byte_count: int = 1024) -> int:
    """Read a tiny range from an extracted YouTube format using yt-dlp's proxy.

    This checks that metadata URLs are usable without downloading the media.
    The URL and response are intentionally never logged or returned.
    """
    import urllib.request

    formats = [
        fmt for fmt in (info.get("formats") or [])
        if fmt.get("url") and str(fmt.get("protocol") or "").startswith("http")
        and fmt.get("vcodec") != "none"
    ]
    if not formats:
        raise RuntimeError("YouTube metadata had no directly probeable video format")
    # Prefer a progressive/single-file format so this stays one bounded request.
    formats.sort(key=lambda fmt: bool(fmt.get("acodec") == "none"))
    fmt = formats[0]
    headers = dict(info.get("http_headers") or {})
    headers.update(fmt.get("http_headers") or {})
    headers["Range"] = f"bytes=0-{max(0, byte_count - 1)}"
    request = urllib.request.Request(str(fmt["url"]), headers=headers)
    opts = _base_opts(host="www.youtube.com")
    opts["socket_timeout"] = 8
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            response = ydl.urlopen(request)
            try:
                payload = response.read(byte_count)
            finally:
                response.close()
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "code", None)
        category = f"http_{status}" if status else type(exc).__name__
        raise RuntimeError(f"YouTube media range probe failed ({category})") from None
    if not payload:
        raise RuntimeError("YouTube media range probe returned no bytes")
    return len(payload)


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
        # yt-dlp retries the extraction itself 3 times by default, which
        # multiplies on top of the strategy ladder that already provides
        # retry diversity. One pass is enough.
        "extractor_retries": 1,
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
        # Certificates ARE verified. Skipping verification let anyone between
        # the server and a site (or a compromised proxy) read the cookies sent
        # with every request and swap the media.
        "geo_bypass": True,
        "noplaylist": True,
        # `noplaylist` only covers a video that happens to sit IN a playlist.
        # A bare playlist/channel URL still expands, and with extract_flat off
        # yt-dlp fully extracts EVERY entry — minutes of work for a list of a
        # few hundred. _normalize_info_dict then throws all but entries[0]
        # away. Measured: a plain video 5s, a playlist URL >100s. Stopping at
        # the first entry gets the identical result for the cost of one.
        "playlistend": 1,
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
    # Whether the format that currently owns each tier's estimate is video-only
    # (DASH). Those get muxed with a separate audio track, so their size is not
    # the final file's size — see the audio fix-up after the loop.
    tier_is_video_only: dict[str, bool] = {}
    best_audio_size = 0

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
                            tier_is_video_only[qkey] = acodec == "none"
        if acodec != "none":
            has_audio = True
            if vcodec == "none" and filesize:
                best_audio_size = max(best_audio_size, int(filesize))

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

    # Add the audio track to every tier whose estimate came from a video-only
    # (DASH) format. Without this the wizard under-reported every quality above
    # 360p on YouTube: a button labelled "~55 MB" produced a 60.1 MB file that
    # Telegram then refused, after the user had waited out the whole download.
    if best_audio_size:
        for qkey, video_only in tier_is_video_only.items():
            if video_only and size_by_quality.get(qkey):
                size_by_quality[qkey] += best_audio_size

    unique_heights = sorted(heights)
    # Dedupe image sizes
    seen = set()
    uniq_imgs: list[tuple[int, int]] = []
    for wh in sorted(image_sizes, key=lambda x: x[0] * x[1], reverse=True):
        if wh not in seen:
            seen.add(wh)
            uniq_imgs.append(wh)

    return has_video, has_audio, has_image, unique_heights, uniq_imgs, size_by_quality


_INTERNAL_DETAIL_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"  # IPv4 address
    r"|socks5h?://|https?://(?:127\.|localhost|10\.|192\.168\.)"
    r"|\bcurl: \(\d+\)"
    r"|/opt/|/home/|/tmp/|/var/|\b[A-Za-z]:\\",  # filesystem paths
    re.IGNORECASE,
)

class JobRefused(yt_dlp.utils.DownloadError):
    """
    A deliberate refusal from the match_filter (live, too long, private URL).
    A DownloadError so yt-dlp's _match_entry lets it propagate (it swallows
    DownloadCancelled there); its own class so no retry path re-runs the job.
    """


CANCELLED = "Cancelled"


class JobAborted(yt_dlp.utils.DownloadCancelled):
    """
    The byte/time guard tripping mid-transfer. NOT a DownloadError: yt-dlp's
    fragment loop catches DownloadError per fragment and carries on (native
    HLS treats no fragment as fatal), so a DownloadError here produced a
    truncated "successful" file instead of stopping. Reproduced locally.
    """


PRIVATE_URL_ERROR = (
    "That link points to a private or local network address, which this bot "
    "doesn't fetch."
)


def _download_match_filter(info: dict[str, Any], *, incomplete: bool = False) -> None:
    """
    yt-dlp match_filter for real downloads (never the analysis pass).

    Live streams never end, so they'd fill the disk and hold a download slot
    forever — the DM wizard already refused them, but group/channel
    auto-download went straight to download(). An unknown duration passes (direct
    files often have none); the byte/time guard covers those.
    """
    if incomplete:
        return None
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise JobRefused(
            "Live streams can't be downloaded — send the link again once it has ended."
        )
    # The server's cookies belong to the operator: they must not turn into a
    # way for anyone to fetch what only that account may see.
    if info.get("availability") in ("private", "premium_only", "subscriber_only"):
        raise JobRefused("This one is private or members-only, so the bot can't share it.")
    duration = info.get("duration")
    if duration and duration > MAX_MEDIA_DURATION:
        raise JobRefused(
            f"This is longer than the {MAX_MEDIA_DURATION // 3600}h this bot "
            "accepts — it wouldn't fit Telegram's upload size anyway."
        )
    # Where yt-dlp will actually fetch from — a public page can point its
    # media at a private address, which the up-front URL check never sees.
    for key in ("url", "manifest_url"):
        target = info.get(key)
        # URI schemes are case-insensitive. Do not let an extracted uppercase
        # HTTP(S) URL bypass the private-address guard.
        if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
            try:
                check_public_url(target)
            except UnresolvableURLError:
                continue  # the CDN host didn't resolve HERE; yt-dlp may via proxy
            except UnsafeURLError as e:
                raise JobRefused(PRIVATE_URL_ERROR) from e
    return None


def _min_sizes_by_quality(info: dict[str, Any]) -> dict[str, int]:
    """
    The SMALLEST plausible final size per quality tier.

    `_parse_formats` reports the largest format in a tier (the honest number
    for a button label), but that can be a fat avc1 rendition while yt-dlp
    would pick a 40% smaller AV1/VP9 one at the same height. Refusing a
    download up front is only safe when even this floor is over the limit.
    Only formats at the tier's top height count — that is what the selectors
    reach for first. Video-only formats get the smallest audio track added.
    """
    formats = info.get("formats") or []
    smallest_audio = min(
        (
            int(f.get("filesize") or f.get("filesize_approx") or 0)
            for f in formats
            if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
        ),
        default=0,
    )
    smallest_audio = smallest_audio if smallest_audio > 0 else 0
    out: dict[str, int] = {}
    for qkey, qmeta in QUALITY_MAP.items():
        cands = [
            f for f in formats
            if f.get("vcodec") not in (None, "none", "images")
            and f.get("height")
            and int(f["height"]) <= qmeta["height"]
            and f.get("format_note") != "storyboard"
        ]
        if not cands:
            continue
        top = max(int(f["height"]) for f in cands)
        sizes = []
        for f in cands:
            # The selectors lead with b[height<=N] — a progressive format at ANY
            # height <= N can be the pick — then fall back to the top height.
            progressive = f.get("acodec") not in (None, "none")
            if int(f["height"]) != top and not progressive:
                continue
            sz = int(f.get("filesize") or f.get("filesize_approx") or 0)
            if not sz:
                continue
            if f.get("acodec") == "none":
                sz += smallest_audio
            sizes.append(sz)
        if sizes:
            out[qkey] = min(sizes)
    return out


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
        # Prefer the count yt-dlp reports for the whole playlist: we only ever
        # fetch the first entry, so len(entries) is 1 and would under-report.
        first["_playlist_count"] = (
            info.get("playlist_count")
            or info.get("n_entries")
            or len(entries)
        )
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
    # VISIONOS alone first: measured on the server it lists the same formats
    # and qualities as the default client rotation but extracts ~2.5x faster
    # (median 2.6s vs 6.5s, worst 3.0s vs 29.3s) — the rotation also queries
    # WEB clients whose player JS challenge is the slow part on this CPU.
    visionos = {"use_cookies": False,
                "extractor_args": {"youtube": {"player_client": ["visionos"]}}}
    # Then the default rotation, cookieless — public videos, full format ladder
    ladder: list[dict[str, Any]] = [visionos, {"use_cookies": False}]
    if has_cookies:
        # Default rotation + cookies (age-restricted / members-only)
        ladder.append({"use_cookies": True})
    return [*ladder, android, android_vr]


def _remember_yt_strategy(original_index: int, *, download: bool) -> None:
    global _YT_WINNER_META, _YT_WINNER_DL
    global _YT_WINNER_META_AT, _YT_WINNER_DL_AT
    now = time.monotonic()
    with _YT_WINNER_LOCK:
        if download:
            _YT_WINNER_DL = original_index
            _YT_WINNER_DL_AT = now
        else:
            _YT_WINNER_META = original_index
            _YT_WINNER_META_AT = now


def _yt_winner(*, download: bool) -> int:
    """The remembered strategy, or 0 once the pin has gone stale."""
    with _YT_WINNER_LOCK:
        idx = _YT_WINNER_DL if download else _YT_WINNER_META
        at = _YT_WINNER_DL_AT if download else _YT_WINNER_META_AT
    if not idx:
        return 0
    if time.monotonic() - at > _YT_WINNER_TTL:
        # Expired: fall back to the quality-first order so the full ladder gets
        # another chance. Costs one failed attempt at most, and only every TTL.
        return 0
    return idx


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


def _is_web_client_url(fmt: dict[str, Any]) -> bool:
    """A googlevideo URL minted for YouTube's WEB / MWEB player (c=WEB...)."""
    q = urllib.parse.parse_qs(urlparse(str(fmt.get("url") or "")).query)
    client = (q.get("c") or [""])[0].upper()
    return client.startswith(("WEB", "MWEB"))


def _meta_cache_get(url: str, max_age: float | None = None) -> dict[str, Any] | None:
    """
    Cached extraction. `max_age` defaults to META_CACHE_TTL (re-analysis); the
    download passes the longer DOWNLOAD_REUSE_TTL — an entry too old to show
    in the wizard is still good for its media URLs, so it is only dropped
    once it is too old for both.
    """
    key = url.strip()
    now = time.time()
    limit = META_CACHE_TTL if max_age is None else max_age
    with _META_CACHE_LOCK:
        hit = _META_CACHE.get(key)
        if not hit:
            return None
        ts, info = hit
        if now - ts > max(META_CACHE_TTL, DOWNLOAD_REUSE_TTL):
            _META_CACHE.pop(key, None)
            return None
        if now - ts > limit:
            return None
        return info


_HEAVY_KEYS = ("heatmap", "comments")


def _slim(info: dict[str, Any]) -> dict[str, Any]:
    """
    Up to 64 full YouTube info dicts sit in RAM for 30 min on a 950 MB box.
    Drop what nothing reads back (thumbnails/captions are used: cover, subs).
    """
    if not isinstance(info, dict):
        return info
    return {k: v for k, v in info.items() if k not in _HEAVY_KEYS}


def _meta_cache_put(url: str, info: dict[str, Any]) -> None:
    key = url.strip()
    with _META_CACHE_LOCK:
        if len(_META_CACHE) >= _META_CACHE_MAX:
            # Drop oldest ~25%
            items = sorted(_META_CACHE.items(), key=lambda kv: kv[1][0])
            for k, _ in items[: max(1, _META_CACHE_MAX // 4)]:
                _META_CACHE.pop(k, None)
        _META_CACHE[key] = (time.time(), _slim(info))


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
                # Metadata is a handful of small JSON requests with a person
                # watching a spinner, so it wants a short leash. The download
                # defaults (socket_timeout 18, retries 3) are tuned for pushing
                # a 100MB file and are far too patient here: one stalled socket
                # cost 18s of silence before the retry that succeeded, which is
                # exactly the 22s "Reading formats" users reported against an
                # operation that normally takes ~2s.
                "socket_timeout": METADATA_SOCKET_TIMEOUT,
                "retries": METADATA_RETRIES,
            }
        )
        if is_yt and YT_LEAN_METADATA:
            # Analysis only: drop the HLS manifest round trip and the
            # translated-subtitle enumeration. Both are pure duplication for
            # the wizard — see YT_LEAN_METADATA in config for the measurements
            # and the evidence that the offered qualities do not change.
            # Merged, never assigned: the PO-token provider block lives in the
            # same dict and is what unlocks the full format ladder.
            base["extractor_args"] = _merge_extractor_args(
                base.get("extractor_args"),
                {"youtube": {"skip": ["hls", "translated_subs"]}},
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
                # A proxy refusal is retryable no matter what client was in
                # use, so it is checked outside the impersonation condition.
                proxy_blip = (
                    "socks5error" in err
                    or "proxyerror" in err
                    or "proxy error" in err
                    or "connection refused" in err
                )
                transport = proxy_blip or (
                    base.get("impersonate") is not None
                    and (
                        "sslerror" in err
                        or "connection was reset" in err
                        or "connection reset" in err
                        or "recv failure" in err
                        or "failed to perform" in err
                    )
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
        bot_wall_failures = 0
        # Shared across the ladder: a dead relay must not sleep at every rung.
        meta_proxy_retries = 0
        # Self-imposed deadline, slightly inside the caller's asyncio cap.
        # asyncio.wait_for cannot cancel a thread that is already running, so
        # without this an extraction that blew the cap kept its worker busy and
        # starved the pool for the next request. Stop trying more strategies
        # while there is still time to return.
        deadline = time.monotonic() + max(5, EXTRACT_TIMEOUT - 5)
        for si, strat in enumerate(strats):
            if si and time.monotonic() > deadline:
                logger.warning(
                    "Metadata deadline hit after %s strategies for %s",
                    si,
                    url[:80],
                )
                break
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
            attempt_started = time.monotonic()
            if is_yt:
                logger.info(
                    "YT attempt phase=metadata strategy=%s proxy=%s pot=%s cookies=%s",
                    _yt_strategy_label(strat, opts),
                    "warp" if opts.get("proxy") else "off",
                    "yes" if "youtubepot-bgutilhttp" in (opts.get("extractor_args") or {}) else "no",
                    "yes" if opts.get("cookiefile") else "no",
                )
            try:
                info = _run(opts)
                logger.info(
                    "  meta strategy %s ok in %.1fs",
                    orig_indices[si],
                    time.monotonic() - attempt_started,
                )
                _remember_yt_strategy(orig_indices[si], download=False)
                _meta_cache_put(url, info)
                return info
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                err = str(e).lower()
                if is_yt:
                    logger.warning(
                        "YT attempt failed phase=metadata strategy=%s class=%s elapsed=%.1fs",
                        _yt_strategy_label(strat, opts),
                        _yt_failure_class(err),
                        time.monotonic() - attempt_started,
                    )
                if _is_youtube_bot_wall(err):
                    bot_wall_failures += 1
                    if bot_wall_failures >= 2:
                        logger.warning(
                            "YouTube anti-bot refusal persisted across two attempts; "
                            "stopping the metadata strategy ladder"
                        )
                        raise
                else:
                    bot_wall_failures = 0
                if (
                    "video unavailable" in err
                    or "private video" in err
                    or "has been removed" in err
                    or "unsupported url" in err
                ):
                    # Permanent for every player client — fail fast
                    raise
                # The analysis pass is where the user is actually waiting, and
                # a proxy refusal here says nothing about the player client —
                # advancing the ladder just burns every strategy against the
                # same briefly-refusing relay. Wait once and retry this one.
                if (
                    "socks5error" in err
                    or "proxyerror" in err
                    or "proxy error" in err
                    or "connection refused" in err
                ) and meta_proxy_retries < PROXY_BLIP_RETRIES:
                    meta_proxy_retries += 1
                    logger.warning(
                        "Proxy refused during analysis, retry %s/%s after %ss",
                        meta_proxy_retries, PROXY_BLIP_RETRIES, PROXY_BLIP_BACKOFF,
                    )
                    time.sleep(PROXY_BLIP_BACKOFF)
                    strats.insert(si + 1, strat)
                    orig_indices.insert(si + 1, orig_indices[si])
                    continue
                # A bot wall through the proxy: the WARP exit IP is flagged and
                # every remaining strategy shares it. Rotate once and retry this
                # strategy on the fresh IP (the marker keeps it to one rotation).
                # "not a bot" only: "Sign in to confirm your AGE" is a per-video
                # gate, and matching it let anyone flap WARP for every other
                # user by resending an age-restricted link.
                if (
                    "not a bot" in err
                    # From the background warmup only when the bot is truly
                    # idle — no user action for 90s and nothing queued or
                    # downloading — so a walled IP is replaced BEFORE the next
                    # user pays for it, without cutting anyone's transfer.
                    and (_bot_idle() if url == WARMUP_URL else _rotation_harmless(0))
                    and _platform_flags(urlparse(url).netloc.lower())["yt"]
                    and opts.get("proxy")
                    and not any(s.get("_warp_retry") for s in strats)
                    and rotate_warp_ip()
                ):
                    strats.insert(si + 1, {**strat, "_warp_retry": True})
                    orig_indices.insert(si + 1, orig_indices[si])
                    continue
                # Any other client-specific failure (bot wall, page reload,
                # throttling, transient 5xx) is worth a retry with the next
                # strategy — different clients genuinely fail differently.
                if not is_yt:
                    logger.warning(
                        "extract_info attempt %s failed after %.1fs: %s",
                        si,
                        time.monotonic() - attempt_started,
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
        min_sizes=_min_sizes_by_quality(info),
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
        | \s*See\s+https?://github\.com/yt-dlp/\S+[^.]*(?:\.|$)
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
    # @ included so a channel handle ("@BlenderFoundation: …") is stripped too.
    text = re.sub(r"^(?!\w*(?:Error|Exception)\b)[@\w.-]{1,80}:\s+", "", text)
    text = _YTDLP_NOISE.sub("", text)
    # "(caused by <HTTPError 404: Not Found>)" only restates the sentence in
    # Python's words rather than the user's.
    text = re.sub(r"\s*\(caused by [^)]*\)\s*", " ", text)
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
        # Metadata gets its OWN pool, deliberately.
        #
        # Sharing one pool made "Analyzing…" wait behind downloads, and worse:
        # asyncio.wait_for cancels the *await*, never the thread — a
        # run_in_executor task that has started cannot be cancelled. So every
        # extraction that hit the 45s cap kept occupying its worker until
        # yt-dlp finished on its own. A few slow links in a row therefore
        # starved the pool, which made the NEXT extraction queue and time out
        # too: a ratchet, not a one-off. Observed in production as two 45s
        # timeouts on a URL this same code extracts in 2-4s standalone.
        self._meta_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="yt-meta",
        )
        # One extraction per URL at a time (key = the cache key). A caller that
        # times out leaves its thread running; a retry of the same link, or the
        # same link sent four times, joins that work instead of stacking more
        # ladders onto the pool — and with one run per URL an abandoned result
        # can never land on top of a newer one in _META_CACHE.
        self._meta_inflight: dict[str, _MetaFlight] = {}

    @property
    def free_slots(self) -> int:
        return max(0, self.max_concurrent - self.active)

    def cached_media_info(self, url: str) -> MediaInfo | None:
        """Build media estimates from an existing extraction only; never fetch."""
        raw = _meta_cache_get(url, max_age=DOWNLOAD_REUSE_TTL)
        return build_media_info(url, raw) if raw is not None else None

    async def extract_info(self, url: str, limit: float | None = None) -> MediaInfo:
        loop = asyncio.get_running_loop()
        # Timed in three parts so a slow "Analyzing…" can be attributed rather
        # than guessed at: queue = waiting for a pool worker (starvation),
        # work = yt-dlp itself, build = our own parsing.
        queued_at = time.monotonic()
        started = {}

        def _run(u: str):
            started["t"] = time.monotonic()
            global _META_INFLIGHT
            with _META_INFLIGHT_LOCK:
                _META_INFLIGHT += 1
            try:
                # The handler stops waiting at EXTRACT_TIMEOUT; this stops the
                # THREAD soon after, so a dripping server can't keep a worker.
                with deadline(limit or EXTRACT_TIMEOUT + 10):
                    return _extract_info_sync(u)
            finally:
                with _META_INFLIGHT_LOCK:
                    _META_INFLIGHT -= 1

        key = url.strip()
        flight = self._meta_inflight.get(key)
        if flight is None or flight.fut.done() or flight.cf.cancelled():
            cf = self._meta_executor.submit(_run, url)
            flight = _MetaFlight(cf, asyncio.wrap_future(cf, loop=loop), started)
            self._meta_inflight[key] = flight

            def _landed(fut: asyncio.Future, key=key, flight=flight) -> None:
                if self._meta_inflight.get(key) is flight:
                    del self._meta_inflight[key]
                if not fut.cancelled():
                    fut.exception()  # retrieved: nobody may be waiting any more

            flight.fut.add_done_callback(_landed)
        else:
            started = flight.started
        flight.waiters += 1
        try:
            # shield: one caller's timeout must not cancel the shared run.
            info = await asyncio.shield(flight.fut)
        finally:
            flight.waiters -= 1
            if flight.fut.done():
                # Landed: forget it now (the done-callback may not have run yet).
                if self._meta_inflight.get(key) is flight:
                    del self._meta_inflight[key]
            elif flight.waiters == 0:
                # Everyone gave up. Not started yet -> drop it (as plain
                # run_in_executor did). Already running -> it can't be
                # stopped; it finishes under its deadline and caches.
                if flight.cf.cancel() and self._meta_inflight.get(key) is flight:
                    del self._meta_inflight[key]  # a newcomer must start fresh
        done = time.monotonic()
        result = build_media_info(url, info)
        # A caller that joined a run already under way counts from its arrival.
        t0 = max(started.get("t", queued_at), queued_at)
        wait = t0 - queued_at
        work = done - t0
        total = time.monotonic() - queued_at
        level = logger.warning if total > 8 else logger.info
        level(
            "analyze %.1fs (queue %.1fs + work %.1fs + build %.1fs) %s",
            total, wait, work, time.monotonic() - done, url[:90],
        )
        return result

    async def download(
        self,
        url: str,
        mode: str,
        quality: str = "720",
        subtitle_lang: str | None = None,
        audio_format: str = "mp3",
        title_hint: str = "media",
        progress_cb: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> DownloadResult:
        """`cancel`, once set, stops the job at its next step or chunk."""
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
            await _emit_progress(progress_cb, 1, "Starting…")
            loop = asyncio.get_running_loop()
            started = time.monotonic()
            result = await loop.run_in_executor(
                self._executor,
                lambda: self._bounded_download(
                    url=url,
                    mode=mode,
                    quality=quality,
                    subtitle_lang=subtitle_lang,
                    audio_format=audio_format,
                    title_hint=title_hint,
                    progress_cb=progress_cb,
                    loop=loop,
                    cancel=cancel,
                ),
            )
            # Auto: video request on image-only posts (Pinterest pins, etc.)
            # The retry is a second full pass with its own attempt budget, so
            # gate it on the first pass having been quick. An image-only post
            # fails fast ("no video formats"); a pass that burned the whole
            # budget was some other failure and retrying only doubles the wait.
            if (
                not result.success
                and mode == "video"
                and time.monotonic() - started < DOWNLOAD_ATTEMPT_BUDGET
                and not (cancel is not None and cancel.is_set())
                and self._looks_like_image_only_error(result.error or "")
            ):
                logger.info("Retrying as image download for %s", redact.url(url))
                await _emit_progress(progress_cb, 5, "Retrying as image…")
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._bounded_download(
                        url=url,
                        mode="image",
                        quality=quality,
                        subtitle_lang=subtitle_lang,
                        audio_format=audio_format,
                        title_hint=title_hint,
                        progress_cb=progress_cb,
                        loop=loop,
                        cancel=cancel,
                    ),
                )
            return result
        finally:
            self.active -= 1
            self._sem.release()

    def _bounded_download(self, *, cancel=None, **kw) -> DownloadResult:
        """
        _download_sync under a wall clock: the transfer guard only runs on
        progress events, so extraction, image fetches and a dripping server
        had no total limit at all. A cancel also lands mid-read now.
        """
        limit = DOWNLOAD_MAX_SECONDS + 60
        started = time.monotonic()
        try:
            with deadline(limit, cancel=cancel):
                result = self._download_sync(cancel=cancel, **kw)
        except DeadlineExceeded as e:  # raised outside yt-dlp's own handlers
            cancelled = cancel is not None and cancel.is_set()
            return DownloadResult(
                success=False, mode=kw.get("mode", ""),
                error=CANCELLED if cancelled else self._friendly_error(f"timed out: {e}"))
        # yt-dlp can swallow a timed-out read (a skipped fragment) and still
        # report success with a file cut short: never hand that over.
        if result.success and (
            (cancel is not None and cancel.is_set()) or time.monotonic() - started > limit
        ):
            self.cleanup_result_files(result)
            cancelled = cancel is not None and cancel.is_set()
            return DownloadResult(
                success=False, mode=result.mode,
                error=CANCELLED if cancelled else self._friendly_error("timed out"))
        return result

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
        cancel: threading.Event | None = None,
    ) -> DownloadResult:
        if cancel is not None and cancel.is_set():
            return DownloadResult(success=False, error=CANCELLED, mode=mode)
        # The handlers check this too; repeating it here covers every caller.
        # yt-dlp would otherwise fetch http://127.0.0.1:9123/... and hand the
        # body back as a "video" file.
        try:
            check_public_url(url)
        except UnresolvableURLError:
            pass  # a typo/dead domain: let yt-dlp report it (or reach it via proxy)
        except UnsafeURLError:
            return DownloadResult(success=False, error=PRIVATE_URL_ERROR, mode=mode)

        work_dir = TEMP_DIR / f"dl_{short_id(12)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        # A title is DATA: escape %, or "%(formats)s" in a title expands into
        # yt-dlp's whole format table and the filename blows up.
        outtmpl = str(
            work_dir / f"{safe_filename(title_hint).replace('%', '%%')}.%(ext)s"
        )
        job_cookies: list[Path] = []
        job_started = time.monotonic()

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
            part = _part_kind(d)
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
                pct = _overall_pct(part, done / total * 100)
                # One bar for the whole job, forward only: video+audio arrive
                # as two files, and per-file percentages made the bar run to
                # 100%, drop back to 0% and say "Finishing" twice.
                # Capped at 98: the finishing steps (merge, convert, cover)
                # still follow and show at 99 — the bar must not go 100 -> 99.
                pct = min(max(pct, last_pct["v"]), 98.0)
                # Throttle Telegram edits (still update often enough to feel
                # live) — but never swallow the FIRST update. last_pct starts
                # negative, so a download that begins under 8% used to have
                # every early tick dropped and the bar stayed on "Resolving…".
                if last_pct["v"] >= 0 and pct - last_pct["v"] < 8 and pct < 95:
                    return
                last_pct["v"] = pct
                bits = [f"{pct:.0f}%"]
                if part == "single":  # a part's size isn't the job's size
                    bits.append(f"{format_size(done)} of {format_size(total)}")
                bits.append(speed_s)
                eta = d.get("eta")
                if eta and part != "audio":
                    bits.append(f"~{int(eta)}s left")
                _emit(pct, "⬇ " + " · ".join(bits))
            elif status == "finished" and part != "video":
                # A video part finishing is only half the job: its audio follows.
                last_pct["v"] = max(last_pct["v"], 99.0)
                _emit(99, "⚙️ Finishing…")

        def pp_hook(d: dict[str, Any]) -> None:
            """Say what the post-download steps are doing instead of sitting on 100%."""
            if d.get("status") != "started":
                return
            label = _PP_LABELS.get(str(d.get("postprocessor") or ""))
            if label:
                _emit(99, label)

        tripped: dict[str, str] = {}

        def guard(d: dict[str, Any]) -> None:
            """Abort mid-transfer: endless streams and huge files stop HERE."""
            if d.get("status") != "downloading":
                return
            reason = None
            if cancel is not None and cancel.is_set():
                reason = CANCELLED
            elif (d.get("downloaded_bytes") or 0) > DOWNLOAD_MAX_BYTES:
                reason = "File is larger than max-filesize for Telegram"
            elif time.monotonic() - job_started > DOWNLOAD_MAX_SECONDS:
                reason = f"Download timed out after {DOWNLOAD_MAX_SECONDS // 60} min"
            if reason:
                tripped["reason"] = reason
                raise JobAborted(reason)

        host = urlparse(url).netloc.lower()
        # Disposable jar for every download so concurrent jobs never corrupt cookies
        cookie_path = _cookie_jar_for_job()
        if cookie_path:
            job_cookies.append(cookie_path)

        def _stage(pct: float, text: str) -> None:
            """Between extraction attempts: the earliest point a cancel can land."""
            if cancel is not None and cancel.is_set():
                raise JobAborted(CANCELLED)
            _emit(pct, text)

        _emit(2, "Resolving…")
        opts = _base_opts(host=host, cookiefile=cookie_path)
        hooks = [guard, hook] if progress_cb else [guard]
        opts.update(
            {
                "outtmpl": outtmpl,
                "progress_hooks": hooks,
                "postprocessor_hooks": [pp_hook] if progress_cb else [],
                # Refused before a single byte moves. Raising — not returning a
                # reason — makes it an error the user sees, not an empty result.
                "match_filter": _download_match_filter,
                "noplaylist": True,
                "writethumbnail": False,
                # NOTE: max_filesize is deliberately NOT set here.
                #
                # Aborting early looks obviously right — an oversized 1080p job
                # pulls 134.7MB over 48s only to be rejected by the 49MB cap.
                # But max_filesize makes yt-dlp SKIP oversized formats and keep
                # walking the selector chain, and the chain ends in `b`, which
                # happily matches an audio-only format once every video format
                # has been skipped. Measured: a 1080p request came back as a
                # 10.2MB webm containing one opus AUDIO stream and no video,
                # while still reporting h=1080.
                #
                # Silently handing back audio for a video request is worse than
                # wasting bandwidth, so the size guard stays after the download.
                # A safe version needs the selector to refuse audio-only
                # results, and `[vcodec!=none]` is not that guard — yt-dlp
                # filters drop formats whose field is MISSING, which would
                # exclude the real videos that report an unknown vcodec
                # (Twitch, Rumble, X). See _parse_formats for that trap.
            }
        )

        try:
            if mode == "audio":
                opts.update(
                    {
                        "format": "bestaudio[ext=m4a]/bestaudio/best",
                        # Minimal music metadata: title + artist tags and the
                        # cover art, so the file looks right in any player.
                        "writethumbnail": True,
                        "postprocessors": [
                            {
                                "key": "FFmpegThumbnailsConvertor",
                                "format": "jpg",
                                "when": "before_dl",
                            },
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": audio_format,
                                "preferredquality": "192",
                            },
                            {"key": "FFmpegMetadata", "add_metadata": True,
                             "add_chapters": False},
                            # Keep the jpg: Telegram shows an audio's cover only
                            # from a thumbnail sent with it, not from the tags.
                            {"key": "EmbedThumbnail", "already_have_thumbnail": True},
                        ],
                    }
                )
            elif mode == "image":
                res = self._download_image_page(
                    url=url,
                    work_dir=work_dir,
                    title_hint=title_hint,
                    progress_cb=progress_cb,
                    loop=loop,
                )
                if not res.success:
                    # It never raises, so the except-branch cleanup below never
                    # runs for it — and the caller only cleans result.files.
                    self._cleanup_dir(work_dir)
                return res
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
                on_stage=_stage,
            )
            if tripped:
                # The guard fired but something downstream swallowed it: the
                # file on disk is cut short. Never hand that over as a success.
                raise JobAborted(tripped["reason"])

            files = sorted(
                [
                    p for p in work_dir.iterdir()
                    if p.is_file() and not p.name.endswith(".part")
                    # A cover image left behind by the audio metadata step must
                    # never be mistaken for the download itself.
                    and p.suffix.lower().lstrip(".") not in (*IMAGE_EXTS, "webp")
                ],
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
                artist=_artist_of(info),
                cover=_telegram_cover(work_dir) if mode == "audio" else None,
            )
        except (JobRefused, JobAborted) as e:
            # Deliberate refusals / limits: expected, no traceback needed.
            logger.warning("Download refused for %s: %s", redact.url(url), e)
            self._cleanup_dir(work_dir)
            if isinstance(e, JobAborted):
                # Concurrent fragment threads can still be finishing a write
                # when the abort lands; sweep again once they have stopped.
                t = threading.Timer(5.0, self._cleanup_dir, args=(work_dir,))
                t.daemon = True
                t.start()
            return DownloadResult(success=False, error=self._friendly_error(str(e)),
                                  mode=mode, quality=quality)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).split("\n")[-1][:300]
            # Expected failures: keep logs light for speed/noise
            if self._looks_like_image_only_error(msg) or "403" in msg.lower():
                logger.warning("Download error for %s: %s", redact.url(url), msg[:160])
            else:
                logger.exception("Download error for %s", redact.url(url))
            self._cleanup_dir(work_dir)
            return DownloadResult(success=False, error=self._friendly_error(msg), mode=mode, quality=quality)
        except Exception as e:
            if cancel is not None and cancel.is_set():
                # A user's ✖ Cancel surfaces as a failed read here: not a crash.
                logger.info("Download cancelled: %s", redact.url(url))
                self._cleanup_dir(work_dir)
                return DownloadResult(success=False, error=CANCELLED, mode=mode, quality=quality)
            logger.exception("Unexpected download failure for %s", redact.url(url))
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

        reused = self._download_from_analysis(opts, url, title_hint, primary)
        if reused is not None:
            return reused

        last_err: Exception | None = None
        # Budget for transient proxy refusals, shared across the whole ladder so
        # a persistently down relay still fails fast instead of sleeping per step.
        proxy_retries = 0
        warp_rotated = False
        bot_wall_failures = 0
        # No NEW attempt starts past this; an in-flight transfer still finishes.
        attempt_deadline = time.monotonic() + DOWNLOAD_ATTEMPT_BUDGET
        for si, strat in enumerate(strategies):
            if si and time.monotonic() > attempt_deadline:
                logger.warning(
                    "Download attempt budget (%ss) spent after %s strategies: %s",
                    DOWNLOAD_ATTEMPT_BUDGET, si, url[:80],
                )
                break
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
            if is_yt and YT_LEAN_DOWNLOAD and si == 0:
                # First attempt only: no HLS manifest round trip (~10s on this
                # VPS). Every retry below extracts in full, HLS included.
                attempt_base["extractor_args"] = _merge_extractor_args(
                    attempt_base.get("extractor_args"),
                    {"youtube": {"skip": ["hls", "translated_subs"]}},
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

            if is_yt:
                logger.info(
                    "YT attempt phase=download strategy=%s proxy=%s pot=%s cookies=%s",
                    _yt_strategy_label(strat, attempt_base),
                    "warp" if attempt_base.get("proxy") else "off",
                    "yes" if "youtubepot-bgutilhttp" in (attempt_base.get("extractor_args") or {}) else "no",
                    "yes" if attempt_base.get("cookiefile") else "no",
                )

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
                if fi and time.monotonic() > attempt_deadline:
                    logger.warning(
                        "Download attempt budget (%ss) spent mid-ladder: %s",
                        DOWNLOAD_ATTEMPT_BUDGET, url[:80],
                    )
                    break
                fmt = formats_to_try[fi]
                attempt_opts = dict(attempt_base)
                attempt_opts["format"] = fmt
                attempt_started = time.monotonic()
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
                    if is_yt:
                        logger.warning(
                            "YT attempt failed phase=download strategy=%s class=%s elapsed=%.1fs",
                            _yt_strategy_label(strat, attempt_opts),
                            _yt_failure_class(err),
                            time.monotonic() - attempt_started,
                        )
                    if _is_youtube_bot_wall(err):
                        bot_wall_failures += 1
                        if bot_wall_failures >= 2:
                            logger.warning(
                                "YouTube anti-bot refusal persisted across two attempts; "
                                "stopping the download strategy ladder"
                            )
                            raise
                    else:
                        bot_wall_failures = 0
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
                        # A SOCKS proxy that momentarily refuses is the same
                        # class of problem: it says nothing about the format or
                        # the platform. Production lost a whole download to a
                        # single sub-second WARP blip
                        # (Socks5Error(5, 'Connection refused')) because this
                        # matched nothing here and the ladder gave up instead
                        # of retrying.
                        or "socks5error" in err
                        or "proxyerror" in err
                        or "proxy error" in err
                        or "connection refused" in err
                    )
                    # A proxy blip is not a reason to move DOWN the ladder.
                    # Every strategy shares the same proxy, so advancing just
                    # burns them all in milliseconds against a relay that is
                    # briefly refusing; WARP recovers in well under a second
                    # (measured healthy again moments later, 60/60 probes OK).
                    # Wait once and retry the SAME attempt instead.
                    proxy_blip = (
                        "socks5error" in err
                        or "proxyerror" in err
                        or "proxy error" in err
                        or "connection refused" in err
                    )
                    if proxy_blip and proxy_retries < PROXY_BLIP_RETRIES:
                        proxy_retries += 1
                        logger.warning(
                            "Proxy refused (si=%s fi=%s), retry %s/%s after %ss",
                            si, fi, proxy_retries, PROXY_BLIP_RETRIES,
                            PROXY_BLIP_BACKOFF,
                        )
                        time.sleep(PROXY_BLIP_BACKOFF)
                        continue  # same strategy, same format

                    # A bot wall through the proxy means the WARP exit IP is
                    # flagged, and every later strategy shares it. One fresh IP
                    # beats walking the rest of the ladder into the same wall.
                    if (
                        "not a bot" in err  # not the age gate — see the meta pass
                        and is_yt
                        and not warp_rotated
                        and attempt_opts.get("proxy")
                        # Rotating drops every proxied socket: never while
                        # someone else's download or analysis is running.
                        and _rotation_harmless(1)
                        and rotate_warp_ip()
                    ):
                        warp_rotated = True
                        continue  # same strategy, same format, new exit IP

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
                        if not is_yt:
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

    def _download_from_analysis(
        self, opts: dict[str, Any], url: str, title_hint: str, primary: str
    ) -> tuple[dict[str, Any], str, str] | None:
        """
        Download from the analysis pass's extraction instead of repeating it.

        This is yt-dlp's own --load-info-json path: format selection and the
        download run on the cached info dict. Any failure (expired or IP-bound
        URL after a WARP rotation, a format gone) returns None and the normal
        ladder extracts afresh — so this can only make a download faster.
        """
        cached = _meta_cache_get(url, max_age=DOWNLOAD_REUSE_TTL)
        if not cached or not cached.get("formats") or cached.get("_type", "video") != "video":
            return None
        started = time.monotonic()
        reuse_opts = dict(opts)
        reuse_opts["format"] = primary
        try:
            info_in = copy.deepcopy(cached)
            # YouTube's WEB-client URLs need a GVS PO token bound at download
            # time and 403 when replayed from a saved extraction (verified on
            # the server; cookies make no difference). Other clients' URLs
            # (VISIONOS, ANDROID_VR, …) replay fine and carry the same itags.
            info_in["formats"] = [f for f in info_in["formats"] if not _is_web_client_url(f)]
            for stale in ("requested_formats", "requested_downloads", "format_id"):
                info_in.pop(stale, None)
            if not info_in["formats"]:
                return None
            with yt_dlp.YoutubeDL(reuse_opts) as ydl:
                info = ydl.process_ie_result(info_in, download=True)
                if info is None:
                    return None
                prepared = ydl.prepare_filename(info)
            logger.info(
                "download reused the analysis extraction (%.1fs): %s",
                time.monotonic() - started, url[:80],
            )
            return info, prepared, str(info.get("title") or title_hint)[:200]
        except (JobRefused, JobAborted):
            # The answer would be the same after a fresh extraction — and for
            # the size cap, the bot would download up to 3x the limit AGAIN.
            raise
        except Exception as e:
            logger.warning(
                "Reusing the analysis failed (%s) — extracting afresh: %s",
                str(e).split("\n")[-1][:120], url[:80],
            )
            # A half-written file must not be resumed by the fresh attempt.
            outtmpl = opts.get("outtmpl")
            work_dir = Path(outtmpl).parent if isinstance(outtmpl, str) else None
            if work_dir is not None and work_dir.is_dir() and work_dir != TEMP_DIR:
                for f in work_dir.iterdir():
                    try:
                        f.unlink()
                    except OSError:
                        pass
            return None

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
                last_err = "Couldn't download the image from this page."
            return DownloadResult(
                success=False,
                error=last_err or "Image download failed.",
                mode="image",
            )
        except Exception as e:
            logger.exception("image page download failed")
            return DownloadResult(
                success=False, error=self._friendly_error(str(e)), mode="image"
            )

    def _discover_image_urls(
        self, page_url: str, html: str | None = None
    ) -> list[str]:
        """Find direct image URLs from a social/image page (Pinterest, etc.)."""
        found: list[str] = []
        bare = page_url.lower().split("#")[0].split("?")[0]
        if any(bare.endswith(f".{e}") for e in IMAGE_EXTS):
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

            from bot.utils.safe_fetch import open_public

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
            # Public addresses only, and never more than Telegram would accept —
            # an og:image pointing at a huge file must not fill the disk.
            with open_public(req, timeout=60) as resp, open(dest, "wb") as f:
                written = 0
                while chunk := resp.read(256 * 1024):
                    written += len(chunk)
                    if written > MAX_FILE_SIZE_BYTES:
                        raise ValueError("image larger than the upload limit")
                    f.write(chunk)
            if dest.exists() and dest.stat().st_size > 0:
                return dest
        except Exception:
            logger.exception("Failed to fetch image %s", redact.url(url))
        dest.unlink(missing_ok=True)  # never leave a partial file behind
        return None

    @staticmethod
    def _friendly_error(msg: str) -> str:
        # Keep the pre-clean text: _clean_extractor_message strips the
        # "[youtube] <id>:" prefix, which is often the only thing naming the
        # platform. Branches that tailor advice per platform need it.
        raw_low = (msg or "").lower()
        if PRIVATE_URL_ERROR.lower() in raw_low:
            return PRIVATE_URL_ERROR  # not "private video, needs cookies"
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
            # bot-walled host it may mint tokens correctly while YouTube still
            # refuses requests. Do not encourage egress rotation or more retries.
            if pot_provider_available():
                _operator_hint(
                    "YouTube rejected requests from this host despite the PO-token "
                    "provider being available. Treat this as an upstream access "
                    "refusal; stop retries and wait for recovery. Public videos "
                    "do not require cookies.")
            else:
                _operator_hint(
                    "YouTube rejected requests from this host. A PO-token provider "
                    "does not guarantee upstream acceptance; stop retries and wait "
                    "for recovery. Public videos do not require cookies.")
            return (
                "YouTube is refusing the bot right now (its anti-bot check).\n\n"
                "Nothing is wrong with your link — please try again later."
            )
        # Instagram's own wording leaked to users verbatim, truncated mid
        # sentence and telling them to pass --cookies-from-browser — a yt-dlp
        # CLI flag that means nothing in a Telegram chat. Verified against
        # yt-dlp master and through the proxy: neither helps, because the post
        # is served only to signed-in viewers.
        # Gated on Instagram: a bare "empty response" is also what OnDemandKorea
        # and Piksel say, and blaming Instagram for those would be nonsense.
        # Instagram's other login walls ("registered users who follow this
        # account", "redirected to the login page") are the same remedy — the
        # redirect one also says "rate-limit" and would otherwise be told to
        # wait a minute, which never helps an anonymous session.
        if "instagram" in raw_low and (
            "empty media response" in low
            or "registered users" in low
            or "redirected to the login page" in low
        ):
            _operator_hint("Instagram login wall: add or refresh Instagram "
                           "cookies in cookies.txt.")
            return (
                "Instagram only shows this post to signed-in viewers, so the "
                "bot can't read it right now.\n\nPublic posts and reels "
                "usually work — try another link."
            )
        if "private" in low or "login required" in low or "sign in" in low:
            _operator_hint("Login-only content: needs a cookies.txt.")
            return (
                "This one is private, age-restricted or needs a login, so the "
                "bot can't download it. Public posts work fine."
            )
        # Must be before generic "not available" (format errors were mislabeled as region)
        if "format is not available" in low or "requested format" in low:
            return (
                "That quality/format isn't offered for this link. "
                "Pick another quality, or Max."
            )
        if "only images are available" in low or "no video formats" in low:
            return (
                "No video on this link — it looks like an image post. "
                "Pick 🖼 Image instead."
            )
        # Before "not found": yt-dlp says "ffprobe and ffmpeg not found", which
        # would otherwise blame the user's link for a server misconfiguration.
        if "ffmpeg" in low or "ffprobe" in low:
            _operator_hint("FFmpeg/ffprobe missing on the server.")
            return (
                "The bot couldn't convert this file (FFmpeg isn't working on the "
                "server). Try 🎵 M4A or a plain Video instead."
            )
        if "geo" in low or "region" in low or "not available in your country" in low:
            return "This media isn't available in the bot's region."
        if (
            "unavailable" in low
            or "has been removed" in low
            or "video is not available" in low
            or "404" in low
            or "not found" in low
        ):
            return (
                "This media is unavailable, private, or the link is wrong — "
                "the platform returned nothing for it."
            )
        if "copyright" in low or "blocked" in low:
            return "This media is blocked due to copyright or platform restrictions."
        # yt-dlp refused before downloading, because the format is bigger than
        # Telegram will accept. Aborting early is the point — say what to do.
        if "larger than max-filesize" in low or "max-filesize" in low:
            return (
                f"That version is bigger than Telegram's "
                f"{int(MAX_FILE_SIZE_BYTES / 1024 / 1024)} MB limit, so the "
                "download was stopped before wasting your time.\n\n"
                "Pick a lower quality (480p usually fits), or 🎵 Audio."
            )
        # Extractor broken against the live site — nothing the user can change.
        # "Cannot parse data" (Facebook) was verified to fail identically on
        # yt-dlp master, so only that case may claim an update will not help;
        # "unable to extract" / "unexpected response" are often fixed by one.
        if "cannot parse data" in low:
            return (
                "The bot can't read this link — the platform changed its page "
                "format.\n\nNothing you did wrong. If the post also has a "
                "normal video/watch link, try that one."
            )
        if "unexpected response" in low or "unable to extract" in low:
            _operator_hint("Extractor failed (unable to extract/unexpected "
                           "response): try a yt-dlp update.")
            return (
                "The bot can't read this link right now — the platform changed "
                "its page format.\n\nNothing you did wrong; try another link, "
                "or this one again later."
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
        if "timed out" in low or "timeout" in low:
            return "The download timed out. Please try again."
        if "rate-limit" in low or "rate limit" in low or "too many requests" in low:
            return "The platform is limiting the bot. Wait a minute and try again."
        if "403" in low or "forbidden" in low:
            # The PO-token advice is YouTube-specific. Printing it for a Rumble
            # or Bilibili 403 sends the reader after a provider that has nothing
            # to do with the platform that just refused them.
            if "youtu" in low or "youtu" in raw_low:
                _operator_hint("YouTube 403 on every client: PO-token provider "
                               "(bgutil) or a cleaner IP.")
                return (
                    "YouTube refused the download this time.\n\n"
                    "Nothing is wrong with your link — please try again later."
                )
            return (
                "The site refused the bot's request — it may block the bot, or "
                "the post isn't public.\n\n"
                "Try again later, or send a different link."
            )
        # 412 is what Bilibili (and a few others) answer when they refuse the
        # request outright. Left unmapped it reached the user verbatim as
        # "Unable to download webpage: HTTP Error 412: Precondition Failed".
        if "412" in low or "precondition failed" in low:
            return (
                "The site refused the bot's request — it blocks the bot, or the "
                "link needs a region or account the bot doesn't have.\n\n"
                "Try a different link."
            )
        # Transport-level failures surfaced as raw Python repr, e.g. Tumblr's
        # "('Connection aborted.', RemoteDisconnected('Remote end closed
        # connection without response'))". That reads as a crash, not a hiccup.
        # Distinguish OUR relay failing from the platform failing. The generic
        # wording below told a user "the connection to the platform dropped"
        # when in fact the bot's own proxy had refused the connection — which
        # points the reader at the wrong thing entirely.
        if "socks5error" in low or "proxyerror" in low or "proxy error" in low:
            return (
                "The bot's network relay refused the connection for a moment, "
                "so this attempt could not reach the platform.\n\nNothing is "
                "wrong with your link — please try again."
            )
        if (
            "connection aborted" in low
            or "remotedisconnected" in low
            or "remote end closed" in low
            or "connection reset" in low
            or "connection refused" in low
            or "recv failure" in low
            or "sslerror" in low
        ):
            return (
                "The connection to the platform dropped before the bot could "
                "read the page.\n\nThat is usually a temporary network or "
                "anti-bot hiccup — please try again."
            )
        # Anything left is either an extractor message worth showing verbatim or
        # a raw Python exception from a broken extractor. Leaking the latter
        # ("the JSON object must be str, bytes or bytearray, not dict" — a real
        # yt-dlp OK.ru crash) tells a Telegram user nothing and looks broken.
        if _looks_like_internal_error(msg):
            _operator_hint(f"Extractor crashed ({msg[:120]}): try a yt-dlp update.")
            return (
                "The bot can't read this link right now — the site changed.\n\n"
                "Nothing you did wrong; try another link, or this one again later."
            )
        # Verbatim extractor text is often the clearest answer ("This video
        # is only available to Music Premium members"), but not when it carries
        # the server's own plumbing — proxy URLs, internal IPs, curl codes or
        # file paths tell a stranger how the bot is wired.
        if _INTERNAL_DETAIL_RE.search(msg):
            logger.info("Withheld internal error detail from user: %s", msg[:200])
            return (
                "The download failed on the server side. Please try again in a "
                "moment, or send a different link."
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


def recommend_fitting_quality(
    available_heights: list[int],
    estimated_sizes: dict[str, int],
    requested_quality: str | None,
    max_bytes: int,
    *,
    exact_sizes: set[str] | None = None,
) -> QualityRecommendation | None:
    """Return the highest lower available quality with a known size under cap.

    `estimated_sizes` comes from yt-dlp format metadata. A key only qualifies
    when the corresponding choice maps to an actually available height and its
    metadata size is present. Those values remain estimates of final output;
    even exact source-part sizes can change slightly during mux/post-processing.
    `exact_sizes` is reserved for callers that possess an exact final-size
    value for that quality. No arbitrary margin is applied to metadata sizes.
    """
    if requested_quality not in QUALITY_MAP or not available_heights or max_bytes <= 0:
        return None
    heights = sorted({int(h) for h in available_heights if int(h) > 0})
    if not heights:
        return None
    requested_cap = QUALITY_MAP[requested_quality]["height"]
    requested_height = max((h for h in heights if h <= requested_cap), default=None)
    if requested_quality == "max":
        requested_height = max(heights)
    if requested_height is None:
        return None

    exact_sizes = exact_sizes or set()
    candidates: list[tuple[int, int, str, int]] = []
    for key in available_qualities_for(heights):
        cap = QUALITY_MAP[key]["height"]
        actual_height = max((h for h in heights if h <= cap), default=None)
        if key == "max":
            actual_height = max(heights)
        if actual_height is None or actual_height >= requested_height:
            continue
        size = estimated_sizes.get(key)
        if not isinstance(size, int) or size <= 0 or size > max_bytes:
            continue
        # If several menu caps resolve to the same source height, prefer the
        # narrowest cap (e.g. 480p instead of "720p up to 480p").
        candidates.append((actual_height, -cap, key, size))

    if not candidates:
        return None
    height, _, quality, size = max(candidates)
    return QualityRecommendation(
        quality=quality,
        height=height,
        estimated_bytes=size,
        is_exact=quality in exact_sizes,
    )


def quality_buttons_meta(heights: list[int], estimated: dict[str, int] | None = None) -> list[dict[str, str]]:
    keys = available_qualities_for(heights)
    estimated = estimated or {}
    out = []
    for k in keys:
        label = QUALITY_MAP[k]["label"]
        size = estimated.get(k)
        if size:
            # Mark what Telegram will refuse. The size was already shown, but a
            # bare "1080p (~60.1 MB)" reads as a normal choice, so users picked
            # it, waited out the whole download and only then learnt it could
            # not be sent. The marker makes the dead end visible up front.
            over = size > MAX_FILE_SIZE_BYTES
            label = f"{label} (~{format_size(size)}{' ⚠️' if over else ''})"
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
