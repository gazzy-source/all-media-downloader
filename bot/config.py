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
# Uploads to Telegram at once, and MB of files in flight (see upload_gate.py).
UPLOAD_MAX_CONCURRENT: int = max(1, int(os.getenv("UPLOAD_MAX_CONCURRENT", "2")))
UPLOAD_BUDGET_BYTES: int = int(float(os.getenv("UPLOAD_BUDGET_MB", "100")) * 1024 * 1024)

DOWNLOAD_DIR: Path = Path(os.getenv("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))
TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", str(BASE_DIR / "temp")))
# Overridable so the test suite can never write into a live deployment's data
# (history, stats, in-flight jobs, inline cache, cookie copies).
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))

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

# Premium (paid with Telegram Stars — no payment provider needed). Price in
# Stars for PREMIUM_DAYS days; premium users get PREMIUM_RATE_MULT x the
# hourly download limit and go ahead of free users in the download queue.
PREMIUM_ENABLED: bool = os.getenv("PREMIUM_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
PREMIUM_STARS: int = max(1, int(os.getenv("PREMIUM_STARS", "100")))
PREMIUM_DAYS: int = max(1, int(os.getenv("PREMIUM_DAYS", "30")))
PREMIUM_RATE_MULT: int = max(1, int(os.getenv("PREMIUM_RATE_MULT", "5")))

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

# Hard ceilings for one download, enforced WHILE it runs. The size check after
# the download protects Telegram, not the server: without these, one link to a
# 24/7 livestream or a 50 GB file (posted in any group the bot sits in) filled
# the disk and pinned a download slot forever.
MAX_MEDIA_DURATION: int = int(os.getenv("MAX_MEDIA_DURATION", str(3 * 3600)))
# Bytes fetched per file before aborting — a multiple of the upload limit so
# an over-estimate that would still fit after merging isn't cut short.
DOWNLOAD_MAX_BYTES: int = int(
    os.getenv("DOWNLOAD_MAX_BYTES", str(3 * MAX_FILE_SIZE_BYTES))
)
DOWNLOAD_MAX_SECONDS: int = int(os.getenv("DOWNLOAD_MAX_SECONDS", "900"))

# Inline mode (@bot <link> in any chat). The finished file is uploaded once to
# a "storage" chat to get a reusable file_id, then swapped into the inline
# message. STORAGE_CHAT_ID: a private channel with the bot as admin (kept as an
# archive). Unset = the first admin's private chat, where each upload is
# deleted again right after (file_ids stay valid after deletion).
INLINE_ENABLED: bool = os.getenv("INLINE_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
STORAGE_CHAT_ID: int | None = (
    int(os.environ["STORAGE_CHAT_ID"]) if os.getenv("STORAGE_CHAT_ID", "").strip() else None
)
# Inline queries arrive per keystroke; this caps answers per user per hour.
INLINE_QUERIES_PER_HOUR: int = int(os.getenv("INLINE_QUERIES_PER_HOUR", "300"))
# Inline search: "@bot lofi beats" lists YouTube results ("@bot audio …" for
# audio). Results are cached per query, and a search only runs once typing
# pauses, so keystrokes cost nothing.
INLINE_SEARCH_ENABLED: bool = os.getenv("INLINE_SEARCH_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
INLINE_SEARCH_PAGE: int = max(1, min(int(os.getenv("INLINE_SEARCH_PAGE", "10")), 20))
INLINE_SEARCH_MAX: int = max(1, min(int(os.getenv("INLINE_SEARCH_MAX", "30")), 50))
# Search lists only media up to this long (seconds). Beyond an hour a video is
# almost always over Telegram's 50 MB bot limit (and slow to fetch), so
# offering it would mostly lead to "too big" — keep the list to what works.
INLINE_SEARCH_MAX_DURATION: int = int(os.getenv("INLINE_SEARCH_MAX_DURATION", "3600"))
# Public URL prefix for the placeholder clip/track that a picked search result
# shows until the real file replaces it (Telegram fetches it by URL). Default:
# this repo's bot/assets via jsDelivr's GitHub CDN (correct MIME types).
INLINE_ASSET_BASE: str = (
    os.getenv("INLINE_ASSET_BASE")
    or "https://cdn.jsdelivr.net/gh/gazzy-source/all-media-downloader@main/bot/assets/"
).rstrip("/") + "/"

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

# Transient SOCKS refusals from the egress proxy (Cloudflare WARP). Seen three
# times in one day in production, each time with WARP healthy again moments
# later — 60/60 sequential and 30/30 concurrent probes pass, so this is a brief
# blip rather than load or an outage. Every strategy in the ladder shares the
# proxy, so advancing through them cannot absorb one; a short wait can.
PROXY_BLIP_RETRIES: int = int(os.getenv("PROXY_BLIP_RETRIES", "2"))
PROXY_BLIP_BACKOFF: float = float(os.getenv("PROXY_BLIP_BACKOFF", "1.5"))

# When YouTube answers "Sign in to confirm you're not a bot" through the proxy,
# the WARP exit IP itself has been flagged — every strategy shares it, so the
# whole ladder fails in seconds (production: 19 of ~80 warmups, plus real user
# links, over 2026-09-29..10-01). A `warp-cli disconnect && connect` hands out
# a fresh exit IP in ~2s. 1 = rotate WARP and retry once (needs warp-cli on
# PATH and PROXY pointing at WARP); 0 = off, the default.
WARP_ROTATE_ON_BOTCHECK: bool = os.getenv(
    "WARP_ROTATE_ON_BOTCHECK", "0"
).strip().lower() in ("1", "true", "yes", "on")
# Minimum seconds between rotations, so a genuinely blocked video can't make
# the bot flap WARP for every other in-flight download.
WARP_ROTATE_COOLDOWN: float = float(os.getenv("WARP_ROTATE_COOLDOWN", "120"))

# Skip YouTube's HLS manifest and translated-subtitle enumeration during the
# ANALYSIS pass only (1 = skip, the default; 0 = restore the old behaviour).
#
# The HLS variant manifest is a separate round trip to manifest.googlevideo.com
# that returns m3u8 duplicates of resolutions already present as https/DASH,
# and translated_subs enumerates hundreds of caption tracks the wizard never
# shows. Benchmarked on the server over 4 videos x 4 rounds:
#
#   baseline            p50 3.82s   p90 14.22s   max 21.32s
#   hls+translated_subs p50 2.76s   p90  3.46s   max 14.32s
#
# The offered qualities are unchanged — identical height lists on every video
# tested, audio still present — only the duplicate format entries go (44 -> 27).
# The p90 is what users feel, and it is the tail this removes.
#
# Deliberately NOT applied to the download pass, which may still want an HLS
# rendition as a fallback when a progressive/DASH URL fails.
YT_LEAN_METADATA: bool = (os.getenv("YT_LEAN_METADATA", "1") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# How often to re-warm the YouTube pipeline, in minutes (0 disables).
#
# Warming once at boot is not enough. The PO token the provider mints carries
# an expiresAt roughly 6 hours out, and yt-dlp's signature-function cache turns
# over whenever YouTube rotates its player. Once either lapses, the NEXT user
# request pays to rebuild it. Measured on the server: a cold mint alone costs
# 12.2s on top of an otherwise 3-7s analysis, which is what produced the 19.7s
# and 42.4s waits in production on a process that had been up for hours.
#
# Re-warming well inside the token lifetime keeps that bill on the bot. The job
# is one small metadata extract, so it is cheap to run often.
WARMUP_INTERVAL_MIN: int = int(os.getenv("WARMUP_INTERVAL_MIN", "45"))

# Metadata extract cache TTL (seconds) — speeds repeated DM analyzes
META_CACHE_TTL: int = int(os.getenv("META_CACHE_TTL", "180"))

# The download reuses the analysis pass's extraction when it is at most this
# old, instead of extracting the same link again. Measured on the 2-core VPS:
# a YouTube extraction is 2-20s (mostly the JS challenge solved in deno), the
# transfer itself 0.3-2s — extracting twice was most of every DM download.
# Media URLs stay valid for hours; if one has gone stale (or WARP changed
# IP) the download falls back to a fresh extraction automatically.
DOWNLOAD_REUSE_TTL: int = int(os.getenv("DOWNLOAD_REUSE_TTL", "1800"))
# The FIRST download attempt on YouTube also skips the HLS manifest (measured
# 21.0s -> 10.7s extraction). Retries still extract in full, so HLS remains
# the fallback the note on YT_LEAN_METADATA asks for.
YT_LEAN_DOWNLOAD: bool = (os.getenv("YT_LEAN_DOWNLOAD", "1") or "1").strip().lower() not in (
    "0", "false", "no",
)
# Inline results default to 720p: phone-sized, roughly half of 1080p's bytes
# to fetch and upload, and far more often under Telegram's 50 MB bot limit.
INLINE_QUALITY: str = (os.getenv("INLINE_QUALITY", "720") or "720").strip().lower()
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
# Defaults are applied on startup ({me} = the bot's @username); set
# BOT_DESCRIPTION=- (or the short one)
# in .env to leave what BotFather has untouched.
_DEFAULT_DESCRIPTION = (
    "🎵 Songs, 🎬 videos and 🖼 images from YouTube, Instagram, TikTok, X, "
    "Facebook, Pinterest and 1000+ sites — fast, in good quality.\n\n"
    "• Paste a link here and pick Video or Audio\n"
    "• In any chat type {me} and a song name — it arrives "
    "right there (add “video” for videos)\n"
    "• Add me to a group: links download automatically\n\n"
    "Live progress, ✖ Cancel any time. Built by Gazzy Labs."
)
_DEFAULT_SHORT_DESCRIPTION = (
    "Songs & videos from YouTube, Instagram, TikTok & 1000+ sites — "
    "paste a link or search in any chat."
)


def _profile_text(name: str, default: str) -> str | None:
    raw = os.getenv(name, "").strip()
    if raw == "-":
        return None
    return raw or default


BOT_DESCRIPTION: str | None = _profile_text("BOT_DESCRIPTION", _DEFAULT_DESCRIPTION)
BOT_SHORT_DESCRIPTION: str | None = _profile_text(
    "BOT_SHORT_DESCRIPTION", _DEFAULT_SHORT_DESCRIPTION)

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
# YouTube selectors lead with the video+audio merge. A progressive "b" first
# matched YouTube's only progressive format, itag 18 — 360p — whenever it was
# listed, so 480/720/1080 and even "Max" came back as 360p (measured on the
# server: a 720p request for dQw4w9WgXcQ picked format 18).
QUALITY_MAP = {
    "480": {
        "label": "480p",
        "height": 480,
        "format": f"bv*[height<=480]{SB_GUARD}+ba/b[height<=480]{SB_GUARD}/bv*+ba/b",
    },
    "720": {
        "label": "720p",
        "height": 720,
        "format": f"bv*[height<=720]{SB_GUARD}+ba/b[height<=720]{SB_GUARD}/bv*+ba/b",
    },
    "1080": {
        "label": "1080p",
        "height": 1080,
        "format": f"bv*[height<=1080]{SB_GUARD}+ba/b[height<=1080]{SB_GUARD}/bv*+ba/b",
    },
    "max": {
        "label": "Max Quality",
        "height": 9999,
        "format": "bv*+ba/b",
    },
}

FORMAT_FALLBACK = "b/best"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
