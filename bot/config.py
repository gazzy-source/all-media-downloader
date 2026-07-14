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
# Private chat: 0 = mode/quality buttons (default). 1 = auto-download like groups.
DM_FAST_AUTO: bool = os.getenv("DM_FAST_AUTO", "0").strip() in (
    "1",
    "true",
    "True",
    "yes",
)
# Metadata extract cache TTL (seconds) — speeds repeated DM analyzes
META_CACHE_TTL: int = int(os.getenv("META_CACHE_TTL", "180"))
AUTO_QUALITY: str = (os.getenv("AUTO_QUALITY", "1080") or "1080").strip().lower()
if AUTO_QUALITY not in ("480", "720", "1080", "max"):
    AUTO_QUALITY = "1080"

TELEGRAM_API_URL: str | None = (os.getenv("TELEGRAM_API_URL") or "").strip() or None
TELEGRAM_LOCAL_MODE: bool = bool(TELEGRAM_API_URL)

# Profile overrides
BOT_NAME_OVERRIDE: str | None = os.getenv("BOT_NAME", "").strip() or None
BOT_DESCRIPTION: str | None = os.getenv("BOT_DESCRIPTION", "").strip() or None
BOT_SHORT_DESCRIPTION: str | None = (
    os.getenv("BOT_SHORT_DESCRIPTION", "").strip() or None
)

TELEGRAM_VIDEO_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_LIMIT = 4096
SESSION_TTL = 600
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
# → faster + less RAM/CPU on small VPS
QUALITY_MAP = {
    "480": {
        "label": "480p",
        "height": 480,
        "format": "b[height<=480]/bv*[height<=480]+ba/b",
    },
    "720": {
        "label": "720p",
        "height": 720,
        "format": "b[height<=720]/bv*[height<=720]+ba/b",
    },
    "1080": {
        "label": "1080p",
        "height": 1080,
        "format": "b[height<=1080]/bv*[height<=1080]+ba/b",
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
