"""yt-dlp powered multi-platform media downloader."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from bot.config import (
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


def _base_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 8,
        "fragment_retries": 15,
        "file_access_retries": 5,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "extract_flat": False,
        # Referer required for many googlevideo CDN URLs
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
        },
        # Prefer web/tv when cookies are used (android/ios ignore cookies → 403)
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "tv", "mweb"],
                "player_skip": ["webpage"],
            },
            "youtubetab": {"skip": ["webpage"]},
        },
        # EJS challenge solver (Deno/Node must be on PATH)
        "remote_components": ["ejs:github"],
    }
    # Browser TLS impersonation (curl_cffi) — must be ImpersonateTarget, not a bare str
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        target = ImpersonateTarget.from_str("chrome")
        opts["impersonate"] = target
    except Exception as e:
        logger.debug("impersonate unavailable: %s", e)

    cookie_path = None
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        cookie_path = Path(COOKIES_FILE)
    elif Path("cookies.txt").is_file():
        cookie_path = Path("cookies.txt").resolve()
    if cookie_path:
        opts["cookiefile"] = str(cookie_path.resolve())
        logger.info("Using cookies file: %s", cookie_path)
    if PROXY:
        opts["proxy"] = PROXY
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


def _extract_info_sync(url: str) -> dict[str, Any]:
    opts = _base_opts()
    opts.update(
        {
            "skip_download": True,
            "noplaylist": True,
        }
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("Could not extract media information from this URL.")
    # Playlist → take first entry for preview (we download single by default)
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


class DownloadManager:
    """Async-friendly download manager with concurrency limit."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_DOWNLOADS) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self.active = 0

    async def extract_info(self, url: str) -> MediaInfo:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _extract_info_sync, url)
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
        async with self._sem:
            self.active += 1
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None,
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
            finally:
                self.active -= 1

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
                # Throttle UI updates
                if abs(pct - last_pct["v"]) < 3 and pct < 99:
                    return
                last_pct["v"] = pct
                speed = d.get("speed")
                eta = d.get("eta")
                speed_s = format_size(speed) + "/s" if speed else "—"
                eta_s = format_duration(eta) if eta else "—"
                _emit(pct, f"⬇ Downloading… {pct:.0f}% · {speed_s} · ETA {eta_s}")
            elif status == "finished":
                _emit(100, "⚙️ Processing / merging…")

        opts = _base_opts()
        opts.update(
            {
                "outtmpl": outtmpl,
                "progress_hooks": [hook],
                "noplaylist": True,
                "merge_output_format": "mp4",
                "writethumbnail": False,
            }
        )

        # Prefer ffmpeg if available (WinGet often installs off-PATH)
        ff_dir = ffmpeg_location_dir()
        if ff_dir:
            opts["ffmpeg_location"] = ff_dir
        elif not find_ffmpeg():
            logger.warning("Proceeding without FFmpeg — merge/audio convert may fail")

        try:
            if mode == "audio":
                opts.update(
                    {
                        "format": f"bestaudio/best/{FORMAT_FALLBACK}",
                        "postprocessors": [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": audio_format,
                                "preferredquality": "192",
                            },
                            {"key": "FFmpegMetadata"},
                        ],
                    }
                )
            elif mode == "image":
                opts.update(
                    {
                        "format": FORMAT_FALLBACK,
                        "writethumbnail": False,
                    }
                )
            elif mode == "video_subs":
                q = QUALITY_MAP.get(quality, QUALITY_MAP["720"])
                lang = subtitle_lang or "en.*"
                opts.update(
                    {
                        "format": q["format"],
                        "writesubtitles": True,
                        "writeautomaticsub": True,
                        "subtitleslangs": [lang, "en", "en-US", "en-GB"],
                        "subtitlesformat": "srt/best",
                        "embedsubtitles": True,
                        "postprocessors": [
                            {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False},
                            {"key": "FFmpegMetadata"},
                        ],
                    }
                )
            else:  # video
                q = QUALITY_MAP.get(quality, QUALITY_MAP["720"])
                opts.update(
                    {
                        "format": q["format"],
                        "postprocessors": [{"key": "FFmpegMetadata"}],
                    }
                )

            info, prepared, title = self._extract_with_format_fallback(
                opts, url, title_hint
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

    def _extract_with_format_fallback(
        self,
        opts: dict[str, Any],
        url: str,
        title_hint: str,
    ) -> tuple[dict[str, Any], str, str]:
        """
        Try preferred format first; on 'format not available' retry with FORMAT_FALLBACK.
        Instagram/Facebook often expose progressive-only formats that miss height filters.
        """
        format_chain = [
            opts.get("format") or FORMAT_FALLBACK,
            FORMAT_FALLBACK,
            "b[ext=mp4]/b",  # progressive only — best 403 antidote
            "18/22/best",  # classic progressive itags
            "worst",
        ]
        formats: list[str] = []
        seen: set[str] = set()
        for f in format_chain:
            if f and f not in seen:
                seen.add(f)
                formats.append(f)

        last_err: Exception | None = None
        for i, fmt in enumerate(formats):
            attempt_opts = dict(opts)
            attempt_opts["format"] = fmt
            # On later attempts, force progressive-friendly YouTube clients
            if i > 0:
                ea = dict(attempt_opts.get("extractor_args") or {})
                yt = dict(ea.get("youtube") or {})
                yt["player_client"] = ["web", "tv", "mweb"]
                ea["youtube"] = yt
                attempt_opts["extractor_args"] = ea
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info is None:
                        raise RuntimeError("Download returned no data.")
                    prepared = ydl.prepare_filename(info)
                    title = str(info.get("title") or title_hint)[:200]
                    if i > 0:
                        logger.info("Download succeeded with fallback format: %s", fmt)
                    return info, prepared, title
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                err = str(e).lower()
                retryable = (
                    "format is not available" in err
                    or "requested format" in err
                    or "no video formats" in err
                    or "only images" in err
                    or "http error 403" in err
                    or "403: forbidden" in err
                    or "unable to download video data" in err
                    or "forbidden" in err
                )
                if retryable and i < len(formats) - 1:
                    logger.warning(
                        "Format %r failed (%s); trying fallback…",
                        fmt[:80],
                        str(e).split("\n")[-1][:120],
                    )
                    continue
                raise
        if last_err:
            raise last_err
        raise RuntimeError("Download failed with all format selectors.")

    def _fetch_url_file(self, url: str, dest: Path) -> Path | None:
        try:
            import urllib.request

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"
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
        ):
            return (
                "YouTube is blocking the server (bot check). "
                "Export browser cookies to cookies.txt on the VPS "
                "(see docs/COOKIES.md) and set COOKIES_FILE=cookies.txt."
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
        if "only images are available" in low:
            return "This post is image-only. Choose the Image download option."
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
