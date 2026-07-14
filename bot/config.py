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

MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
# Soft cap for refusing uploads. Official api.telegram.org is ~50MB unless you
# run a local Bot API server (then set TELEGRAM_API_URL + raise this to e.g. 1900).
MAX_FILE_SIZE_MB: float = float(os.getenv("MAX_FILE_SIZE_MB", "49"))
MAX_FILE_SIZE_BYTES: int = int(MAX_FILE_SIZE_MB * 1024 * 1024)

DOWNLOAD_DIR: Path = Path(os.getenv("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))
TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", str(BASE_DIR / "temp")))
DATA_DIR: Path = BASE_DIR / "data"

# Cookies (Netscape format). Empty env → try ./cookies.txt if present.
_cookies_env = (os.getenv("COOKIES_FILE") or "").strip()
if _cookies_env:
    COOKIES_FILE: str | None = _cookies_env
elif (BASE_DIR / "cookies.txt").is_file():
    COOKIES_FILE = str(BASE_DIR / "cookies.txt")
else:
    COOKIES_FILE = None

PROXY: str | None = os.getenv("PROXY") or None
RATE_LIMIT_PER_HOUR: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))

# Group / auto behaviour
# In groups & supergroups: paste link → download immediately (no wizard)
AUTO_DOWNLOAD_GROUPS: bool = os.getenv("AUTO_DOWNLOAD_GROUPS", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
)
# Force instant download even in private chats
AUTO_DOWNLOAD_ALWAYS: bool = os.getenv("AUTO_DOWNLOAD_ALWAYS", "0").strip() in (
    "1",
    "true",
    "True",
    "yes",
)
# Quality used for auto mode: 480 | 720 | 1080 | max
# Default 1080 — never auto-pick above 1080p (saves bandwidth / Telegram size)
AUTO_QUALITY: str = (os.getenv("AUTO_QUALITY", "1080") or "1080").strip().lower()
if AUTO_QUALITY not in ("480", "720", "1080", "max"):
    AUTO_QUALITY = "1080"

# Local Telegram Bot API (optional) — raises file size limit to ~2GB
# Example: http://127.0.0.1:8081/bot
TELEGRAM_API_URL: str | None = (os.getenv("TELEGRAM_API_URL") or "").strip() or None
TELEGRAM_LOCAL_MODE: bool = bool(TELEGRAM_API_URL)

# Optional profile overrides (leave empty to keep @BotFather settings)
BOT_NAME_OVERRIDE: str | None = os.getenv("BOT_NAME", "").strip() or None
BOT_DESCRIPTION: str | None = os.getenv("BOT_DESCRIPTION", "").strip() or None
BOT_SHORT_DESCRIPTION: str | None = (
    os.getenv("BOT_SHORT_DESCRIPTION", "").strip() or None
)

# Telegram Bot API hard limits
TELEGRAM_VIDEO_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_LIMIT = 4096

# Session TTL for pending download choices (seconds)
SESSION_TTL = 600

# Cleanup temp files older than this (hours)
TEMP_CLEANUP_HOURS = 2

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

# Format chains always end with broad fallbacks — IG/FB/TikTok often only expose
# progressive files without height tags, so strict height filters alone fail.
QUALITY_MAP = {
    "480": {
        "label": "480p",
        "height": 480,
        "format": (
            "bv*[height<=480]+ba/b[height<=480]/"
            "best[height<=480]/bestvideo*+bestaudio/best"
        ),
    },
    "720": {
        "label": "720p",
        "height": 720,
        "format": (
            "bv*[height<=720]+ba/b[height<=720]/"
            "best[height<=720]/bestvideo*+bestaudio/best"
        ),
    },
    "1080": {
        "label": "1080p",
        "height": 1080,
        # Cap at 1080p even if source is 1440p/4K — still fall back if lower only
        "format": (
            "bv*[height<=1080]+ba/b[height<=1080]/"
            "best[height<=1080]/bestvideo*[height<=1080]+bestaudio/"
            "best"
        ),
    },
    "max": {
        "label": "Max Quality",
        "height": 9999,
        "format": "bestvideo*+bestaudio/best/bv*+ba/b",
    },
}

# Ultra-safe fallback when a site rejects the quality selector
FORMAT_FALLBACK = "bestvideo*+bestaudio/best/bestvideo/bestaudio/best"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
