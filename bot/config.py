"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

# Parallel user jobs (5 is fine if MemoryMax ~700M+ and progressive formats)
MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))
MAX_FILE_SIZE_MB: float = float(os.getenv("MAX_FILE_SIZE_MB", "49"))
MAX_FILE_SIZE_BYTES: int = int(MAX_FILE_SIZE_MB * 1024 * 1024)

DOWNLOAD_DIR: Path = Path(os.getenv("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))
TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", str(BASE_DIR / "temp")))
DATA_DIR: Path = BASE_DIR / "data"

_cookies_env = (os.getenv("COOKIES_FILE") or "").strip()
if _cookies_env:
    COOKIES_FILE: str | None = _cookies_env
elif (BASE_DIR / "cookies.txt").is_file():
    COOKIES_FILE = str(BASE_DIR / "cookies.txt")
else:
    COOKIES_FILE = None

PROXY: str | None = os.getenv("PROXY") or None
# Optional allowlist: route ONLY these hosts through PROXY (substring match on
# the URL host). Residential proxies bill per GB and video is heavy, so on a
# datacenter VPS you usually want to proxy just the platforms that block the
# server's IP and let everything else go out directly.
# Empty/unset = proxy everything (previous behaviour).
def parse_proxy_hosts(raw: str | None) -> tuple[str, ...]:
    """Split a PROXY_HOSTS value into a normalised tuple of host fragments."""
    return tuple(h.strip().lower() for h in (raw or "").split(",") if h.strip())


PROXY_HOSTS: tuple[str, ...] = parse_proxy_hosts(os.getenv("PROXY_HOSTS"))
RATE_LIMIT_PER_HOUR: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))

AUTO_DOWNLOAD_GROUPS: bool = os.getenv("AUTO_DOWNLOAD_GROUPS", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
)
AUTO_DOWNLOAD_ALWAYS: bool = os.getenv("AUTO_DOWNLOAD_ALWAYS", "0").strip() in (
    "1",
    "true",
    "True",
    "yes",
)
# Channels: post the downloaded media as a new message and delete the original
# link post, so nobody has to remove it by hand. Needs the "Delete messages"
# admin right; without it the media is still posted and the link is left.
# Groups and DMs are unaffected.
CHANNEL_REPLACE_LINK: bool = os.getenv("CHANNEL_REPLACE_LINK", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
)

# Links that are music/podcast by nature: auto-download delivers them as an
# audio message rather than a video file. music.youtube.com in particular serves
# the full video stream, so without this a song arrives as an mp4.
# NOTE: Spotify is absent on purpose — yt-dlp has no Spotify extractor because
# its tracks are DRM-protected. Those links fall through to the normal
# "no downloadable media" reply.
AUDIO_HOST_HINTS: tuple[str, ...] = (
    "music.youtube.com",
    "soundcloud.com",
    "bandcamp.com",
    "mixcloud.com",
    "audiomack.com",
    "podcasts.apple.com",
)

# Private chat: 0 = mode/quality buttons (default). 1 = auto-download like groups.
DM_FAST_AUTO: bool = os.getenv("DM_FAST_AUTO", "0").strip() in (
    "1",
    "true",
    "True",
    "yes",
)
# Hard ceiling on the "Analyzing…" phase. yt-dlp's socket_timeout only
# bounds a single socket operation; with retries across several
# strategies a flaky connection can stretch to minutes, and the user just
# waits. Observed in production: 254s on one link. Fail fast instead.
EXTRACT_TIMEOUT: int = int(os.getenv("EXTRACT_TIMEOUT", "45"))
# Per-request leash for the metadata pass only. Downloads keep the more
# patient values, where waiting out a slow socket beats restarting a
# large transfer.
METADATA_SOCKET_TIMEOUT: int = int(os.getenv("METADATA_SOCKET_TIMEOUT", "8"))
METADATA_RETRIES: int = int(os.getenv("METADATA_RETRIES", "1"))
# Ceiling on the download RESOLVE loop: no NEW (strategy, format) attempt
# starts past this. A transfer already in flight is allowed to finish, so
# this bounds thrashing rather than cutting off a legitimately large file.
# Without it the ladder multiplies out to ~19 minutes (38 with the
# image-only retry) while holding a concurrency slot and a pool thread.
DOWNLOAD_ATTEMPT_BUDGET: int = int(os.getenv("DOWNLOAD_ATTEMPT_BUDGET", "420"))

# Warm the YouTube pipeline in the background right after startup.
#
# The first YouTube analysis in a fresh process pays costs no later one does:
# spawning deno, solving and caching YouTube's signature function
# (~/.cache/yt-dlp/youtube-sigfuncs), the first PO-token mint and the first
# proxy TLS handshake. Measured in production: 24.3s for the first request
# after a restart against ~3s once warm. That bill landed on whichever user
# happened to send the first link. Paying it ourselves at boot, off the
# request path, keeps it away from every user.
#
# It also re-warms after YouTube rotates its player, because that invalidates
# the cached signature function — the same 24s, otherwise charged to a user.
WARMUP_ON_START: bool = (os.getenv("WARMUP_ON_START", "1") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
# "Me at the zoo": the oldest, most stable public video on YouTube — 19s long
# and metadata-only here, so warming costs a few small JSON requests.
WARMUP_URL: str = (
    os.getenv("WARMUP_URL") or "https://www.youtube.com/watch?v=jNQXAC9IVRw"
).strip()

# Metadata extract cache TTL (seconds) — speeds repeated DM analyzes
META_CACHE_TTL: int = int(os.getenv("META_CACHE_TTL", "180"))
AUTO_QUALITY: str = (os.getenv("AUTO_QUALITY", "1080") or "1080").strip().lower()
if AUTO_QUALITY not in ("480", "720", "1080", "max"):
    AUTO_QUALITY = "1080"

TELEGRAM_API_URL: str | None = (os.getenv("TELEGRAM_API_URL") or "").strip() or None
TELEGRAM_LOCAL_MODE: bool = bool(TELEGRAM_API_URL)

# YouTube PO-token provider (bgutil) — lets yt-dlp download WITHOUT cookies.
# Leave empty to use the default local endpoint (http://127.0.0.1:4416).
# In docker-compose set POT_PROVIDER_URL=http://bgutil-provider:4416
POT_PROVIDER_URL: str | None = (os.getenv("POT_PROVIDER_URL") or "").strip() or None

# Profile overrides
BOT_NAME_OVERRIDE: str | None = os.getenv("BOT_NAME", "").strip() or None
BOT_DESCRIPTION: str | None = os.getenv("BOT_DESCRIPTION", "").strip() or None
BOT_SHORT_DESCRIPTION: str | None = (
    os.getenv("BOT_SHORT_DESCRIPTION", "").strip() or None
)

TELEGRAM_VIDEO_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_LIMIT = 4096
# How long a DM wizard stays usable. 10 minutes was tight: pick a link,
# get distracted, come back to "This session expired". Sessions are a few
# hundred bytes each and the cleanup job sweeps them, so a longer window
# costs nothing. (They still die on restart — they live in memory only.)
SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))
TEMP_CLEANUP_HOURS = 1

BOT_NAME = "All-Media Downloader Bot"
BOT_BIO = (
    "Download videos, audio, and images from any platform instantly. "
    "Built by Gazzy Labs."
)

SUPPORTED_PLATFORMS = [
    ("YouTube", "youtube.com, youtu.be, music.youtube.com"),
    ("Instagram", "instagram.com, instagr.am"),
    ("TikTok", "tiktok.com, vm.tiktok.com"),
    ("X / Twitter", "x.com, twitter.com"),
    ("Facebook", "facebook.com, fb.watch, fb.com"),
    ("Pinterest", "pinterest.com, pin.it"),
    ("Reddit", "reddit.com, v.redd.it"),
    ("Vimeo", "vimeo.com"),
    ("Twitch", "twitch.tv, clips.twitch.tv"),
    ("SoundCloud", "soundcloud.com"),
    ("Dailymotion", "dailymotion.com"),
    ("Bilibili", "bilibili.com"),
    ("LinkedIn", "linkedin.com"),
    ("Threads", "threads.net"),
    ("Snapchat", "snapchat.com"),
    ("Tumblr", "tumblr.com"),
    ("Bandcamp", "bandcamp.com"),
    ("Rumble", "rumble.com"),
    ("OK.ru", "ok.ru"),
    ("VK", "vk.com, vk.video"),
]

# Progressive-first format strings: one file, no ffmpeg merge when possible
# → faster + less RAM/CPU on small VPS.
# Storyboard guard: YouTube's SABR responses include mjpeg/mhtml storyboard
# "formats" with real heights — a capped selector can otherwise pick a
# 0-second image frame as "video" (verified live: 15.9 KB "480p" file).
# yt-dlp caveat: filters referencing a missing field silently exclude the
# format (e.g. format_note!=storyboard drops formats WITHOUT that key), and
# a trailing b[height<=N] re-matches the storyboard when only storyboards sit
# under the cap. Chain: guarded segments first, then an UNRESTRICTED merge
# bv*+ba (requires ffmpeg, which the bot ships/requires) — verified to prefer
# real video over storyboards in every format-list shape.
SB_GUARD = "[vcodec!^=mjpeg][ext!=mhtml]"
QUALITY_MAP = {
    "480": {
        "label": "480p",
        "height": 480,
        "format": f"b[height<=480]{SB_GUARD}/bv*[height<=480]{SB_GUARD}+ba/bv*+ba/b",
    },
    "720": {
        "label": "720p",
        "height": 720,
        "format": f"b[height<=720]{SB_GUARD}/bv*[height<=720]{SB_GUARD}+ba/bv*+ba/b",
    },
    "1080": {
        "label": "1080p",
        "height": 1080,
        "format": f"b[height<=1080]{SB_GUARD}/bv*[height<=1080]{SB_GUARD}+ba/bv*+ba/b",
    },
    "max": {
        "label": "Max Quality",
        "height": 9999,
        "format": "b/bv*+ba/b",
    },
}

FORMAT_FALLBACK = "b/best"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
