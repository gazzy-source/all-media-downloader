"""yt-dlp powered multi-platform media downloader."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import shutil
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
    PROXY,
    QUALITY_MAP,
    TEMP_DIR,
)
from bot.utils.ffmpeg import ffmpeg_location_dir, find_ffmpeg
from bot.utils.helpers import (
    IMAGE_EXTS,
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

# Sticky YouTube strategy winner — next jobs try the fast path first
_YT_WINNER_SI: int = 0
_YT_WINNER_LOCK = __import__("threading").Lock()


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


def _resolved_impersonate():
    global _IMPERSONATE, _IMPERSONATE_RESOLVED
    if _IMPERSONATE_RESOLVED:
        return _IMPERSONATE
    _IMPERSONATE_RESOLVED = True
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        _IMPERSONATE = ImpersonateTarget.from_str("chrome")
    except Exception:
        _IMPERSONATE = None
    return _IMPERSONATE


def _base_opts(
    *,
    host: str = "",
    cookiefile: str | Path | None = None,
) -> dict[str, Any]:
    """Lean yt-dlp options tuned for small VPS + speed."""
    is_yt = "youtu" in host
    is_ig = "instagram" in host or "instagr.am" in host

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 18,
        "retries": 3,
        "fragment_retries": 6,
        "file_access_retries": 2,
        # Parallel DASH fragments — speeds multi-format YT without huge RAM hit
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 256,
        "http_chunk_size": 1024 * 1024 * 10,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "extract_flat": False,
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
        opts["http_headers"]["Referer"] = "https://www.youtube.com/"
        opts["http_headers"]["Origin"] = "https://www.youtube.com"
        # Use local Deno/EJS only — remote github fetch adds multi-second latency
    elif is_ig:
        opts["http_headers"]["Referer"] = "https://www.instagram.com/"
        opts["http_headers"]["Origin"] = "https://www.instagram.com"
        opts["concurrent_fragment_downloads"] = 3
    else:
        if host:
            opts["http_headers"]["Referer"] = f"https://{host.split(':')[0]}/"

    # Prefer caller-supplied cookie jar (per-job). Else shared source for non-YT.
    cookie: Path | None = None
    if cookiefile is not None:
        cookie = Path(cookiefile) if cookiefile else None
    elif is_yt:
        cookie = None  # YT jobs should pass a unique jar from DownloadManager
    else:
        cookie = _resolved_cookie_source()
    if cookie and cookie.is_file():
        opts["cookiefile"] = str(cookie)

    if is_yt:
        if cookie:
            # Single fast client first (tv is usually quickest with cookies)
            opts["extractor_args"] = {
                "youtube": {"player_client": ["tv"]},
            }
        else:
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["tv_embedded", "android"],
                    "player_skip": ["configs", "webpage"],
                },
            }

    imp = _resolved_impersonate()
    if imp is not None:
        opts["impersonate"] = imp

    if PROXY:
        opts["proxy"] = PROXY

    ff = _resolved_ffmpeg_dir()
    if ff:
        opts["ffmpeg_location"] = ff

    return opts


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
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        height = f.get("height")
        width = f.get("width")
        fext = (f.get("ext") or "").lower()
        filesize = f.get("filesize") or f.get("filesize_approx") or 0

        is_image_fmt = fext in IMAGE_EXTS or f.get("format_note") == "storyboard"
        if is_image_fmt and width and height and not (vcodec not in ("none", None) and height and height > 0 and fext in {"mp4", "webm", "mkv"}):
            if fext in IMAGE_EXTS:
                has_image = True
                image_sizes.append((int(width), int(height)))
            continue

        if vcodec != "none" and height:
            has_video = True
            heights.add(int(height))
            for qkey, qmeta in QUALITY_MAP.items():
                if int(height) <= qmeta["height"]:
                    prev = size_by_quality.get(qkey, 0)
                    if filesize and filesize > prev:
                        size_by_quality[qkey] = int(filesize)
        if acodec != "none":
            has_audio = True

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

    # Media with only video track still "has video"
    if info.get("duration") and (info.get("vcodec") or formats):
        if any((f.get("vcodec") or "none") != "none" for f in formats):
            has_video = True

    # Audio-only posts (SoundCloud, music)
    if not has_video and any((f.get("acodec") or "none") != "none" for f in formats):
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
    Lean YouTube attempts: fast path first, few fallbacks.
    Sticky winner is reordered to index 0 by the caller.
    """
    if has_cookies:
        return [
            # 0) Fast path — single client, reuse job cookie jar
            {
                "use_cookies": True,
                "refresh_cookies": False,
                "extractor_args": {"youtube": {"player_client": ["tv"]}},
            },
            # 1) Web clients
            {
                "use_cookies": True,
                "refresh_cookies": True,
                "extractor_args": {
                    "youtube": {"player_client": ["web", "mweb"]}
                },
            },
            # 2) Embedded
            {
                "use_cookies": True,
                "refresh_cookies": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web_embedded", "tv_embedded"]
                    }
                },
            },
            # 3) Cookieless last resort
            {
                "use_cookies": False,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["tv_embedded", "android"],
                        "player_skip": ["webpage"],
                    }
                },
                "drop_impersonate": True,
            },
        ]
    return [
        {
            "use_cookies": False,
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv_embedded", "android", "ios"],
                    "player_skip": ["webpage"],
                }
            },
            "drop_impersonate": True,
        },
    ]


def _ordered_yt_strategies(*, has_cookies: bool) -> list[dict[str, Any]]:
    """Put last successful strategy first for sticky speed."""
    strats = _yt_strategies(has_cookies=has_cookies)
    with _YT_WINNER_LOCK:
        wi = _YT_WINNER_SI
    if 0 < wi < len(strats):
        return [strats[wi], *strats[:wi], *strats[wi + 1 :]]
    return strats


def _remember_yt_strategy(original_index: int) -> None:
    global _YT_WINNER_SI
    with _YT_WINNER_LOCK:
        _YT_WINNER_SI = original_index


def _extract_info_sync(url: str) -> dict[str, Any]:
    """
    Metadata-only extract (wizard / rare sites).
    Uses cookies + lean YT strategies; major platforms usually skip this via DM_FAST_AUTO.
    """
    host = urlparse(url).netloc.lower()
    is_yt = "youtu" in host
    job_cookies: list[Path] = []
    try:
        cookie = _cookie_jar_for_job() if is_yt else _resolved_cookie_source()
        if cookie and is_yt:
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

        if not is_yt:
            with yt_dlp.YoutubeDL(base) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is None:
                raise RuntimeError("Could not extract media information from this URL.")
            return _normalize_info_dict(url, info)

        strats = _ordered_yt_strategies(has_cookies=bool(cookie))
        last_err: Exception | None = None
        for si, strat in enumerate(strats):
            opts = dict(base)
            opts["http_headers"] = dict(base.get("http_headers") or {})
            if strat.get("extractor_args"):
                opts["extractor_args"] = strat["extractor_args"]
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
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info is None:
                    raise RuntimeError(
                        "Could not extract media information from this URL."
                    )
                # Map ordered index back roughly for sticky (best-effort)
                _remember_yt_strategy(0 if si == 0 else si)
                return _normalize_info_dict(url, info)
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                err = str(e).lower()
                if (
                    "not a bot" in err
                    or "sign in to confirm" in err
                    or "cookies are no longer valid" in err
                ):
                    logger.warning(
                        "extract_info attempt failed: %s",
                        str(e).split("\n")[-1][:120],
                    )
                    continue
                raise
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

        last_pct = {"v": -1}

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
                pct = (done / total * 100) if total else 0
                # Throttle Telegram edits (still update often enough to feel live)
                if abs(pct - last_pct["v"]) < 8 and pct < 95:
                    return
                last_pct["v"] = pct
                speed = d.get("speed")
                speed_s = format_size(speed) + "/s" if speed else "—"
                _emit(pct, f"⬇ {pct:.0f}% · {speed_s}")
            elif status == "finished":
                _emit(100, "⚙️ Finishing…")

        host = urlparse(url).netloc.lower()
        is_yt = "youtu" in host
        cookie_path = _cookie_jar_for_job() if is_yt else _resolved_cookie_source()
        if cookie_path and is_yt:
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
                        "format": "bestaudio/best",
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
                q = QUALITY_MAP.get(quality, QUALITY_MAP["1080"])
                lang = subtitle_lang or "en.*"
                opts.update(
                    {
                        "format": q["format"],
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
            else:  # video — progressive-first; merge only when needed
                q = QUALITY_MAP.get(quality, QUALITY_MAP["1080"])
                fmt = q["format"]
                # Instagram: single best under cap (reels rarely need multi-format)
                if "instagram" in host or "instagr.am" in host:
                    h = q.get("height", 1080)
                    if h >= 9999:
                        fmt = "b/best"
                    else:
                        fmt = f"b[height<={h}]/best"
                opts.update(
                    {
                        "format": fmt,
                        "merge_output_format": "mp4",
                    }
                )

            info, prepared, title = self._extract_with_format_fallback(
                opts, url, title_hint, job_cookies=job_cookies
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

            # Special case: image mode may need thumbnail extraction
            if mode == "image":
                img_candidates = [
                    f for f in files if f.suffix.lower().lstrip(".") in IMAGE_EXTS
                ]
                if img_candidates:
                    primary = img_candidates[0]
                elif info and info.get("thumbnail"):
                    # Download thumbnail as image
                    thumb_path = self._fetch_url_file(
                        info["thumbnail"], work_dir / f"{safe_filename(title_hint)}.jpg"
                    )
                    if thumb_path:
                        primary = thumb_path
                        files = [thumb_path]

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
    ) -> tuple[dict[str, Any], str, str]:
        """
        Fast path first; fallbacks only on bot-check / format / 403 errors.
        Sticky YouTube strategy winner for subsequent jobs.
        """
        host = urlparse(url).netloc.lower()
        is_yt = "youtu" in host
        primary = opts.get("format") or FORMAT_FALLBACK
        tracked = job_cookies if job_cookies is not None else []
        initial_cookie = opts.get("cookiefile")

        if is_yt:
            base_strats = _yt_strategies(has_cookies=bool(initial_cookie))
            with _YT_WINNER_LOCK:
                wi = _YT_WINNER_SI
            if 0 < wi < len(base_strats):
                strategies = [base_strats[wi], *base_strats[:wi], *base_strats[wi + 1 :]]
                orig_indices = [wi, *range(0, wi), *range(wi + 1, len(base_strats))]
            else:
                strategies = list(base_strats)
                orig_indices = list(range(len(base_strats)))
        else:
            strategies = [{}]
            orig_indices = [0]

        last_err: Exception | None = None
        for si, strat in enumerate(strategies):
            # Prefer requested quality; only try b/best if that format fails
            formats_to_try = [primary]
            if primary != FORMAT_FALLBACK and primary != "b/best":
                pass  # b/best added only on format failure
            attempt_base = dict(opts)
            attempt_base["http_headers"] = dict(opts.get("http_headers") or {})

            if strat.get("extractor_args"):
                attempt_base["extractor_args"] = strat["extractor_args"]
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
                            _remember_yt_strategy(orig_indices[si])
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
                    if bot_check or format_issue or stream_fail:
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
        low = msg.lower()
        if (
            "sign in to confirm" in low
            or "confirm you're not a bot" in low
            or "confirm you are not a bot" in low
            or "not a bot" in low
            or "cookies are no longer valid" in low
        ):
            return (
                "YouTube blocked this server / cookies expired.\n\n"
                "Fix (takes ~1 min):\n"
                "1. Chrome → open youtube.com (logged in)\n"
                "2. Export cookies with “Get cookies.txt LOCALLY”\n"
                "3. Save as cookies.txt (do NOT open YouTube again after export)\n"
                "4. Upload to VPS and restart the bot\n\n"
                "Note: Google often invalidates cookies when a VPS IP uses them. "
                "Re-export if it breaks again."
            )
        if "private" in low or "login required" in low or "sign in" in low:
            return (
                "This content is private or requires login. "
                "Add a cookies.txt file for authenticated downloads."
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
        if "unsupported url" in low or "no suitable extractor" in low:
            return "This URL is not supported yet. Try another link from a major platform."
        if "ffmpeg" in low:
            return "FFmpeg is required for this format. Install FFmpeg and try again."
        if "timed out" in low or "timeout" in low:
            return "The download timed out. Please try again."
        if "rate-limit" in low or "rate limit" in low or "too many requests" in low:
            return "The platform rate-limited the bot. Wait a minute and try again."
        if "403" in low or "forbidden" in low:
            return (
                "YouTube blocked the media stream (HTTP 403). "
                "Refresh cookies.txt (export while logged into YouTube) "
                "and try again. The bot now retries safer formats automatically."
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
