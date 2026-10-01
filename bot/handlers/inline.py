"""
Inline mode: type `@bot <link>` in any chat, pick 🎬 Video or 🎵 Audio, and the
sent message turns into the media.

Telegram wants an inline answer within seconds and gives no way to upload a
file into an inline message afterwards — an edit may only reference a file_id.
So the flow is:

1. Answer at once, before any download, with two labelled placeholder photos
   (or, for a link fetched before, the finished file straight from the cache).
2. When the user picks one, Telegram reports the chosen result (inline feedback
   must be enabled in @BotFather). Download in the background, showing progress
   in the placeholder's caption.
3. Upload the file once to a storage chat to obtain a file_id, then swap the
   placeholder for the real media and remember link → file_id.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultsButton,
    InputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import (
    ADMIN_IDS,
    AUTO_QUALITY,
    INLINE_ENABLED,
    INLINE_QUERIES_PER_HOUR,
    MAX_FILE_SIZE_BYTES,
    STORAGE_CHAT_ID,
)
from bot.handlers import download as download_handlers
from bot.services import inline_cache
from bot.services.downloader import PRIVATE_URL_ERROR, download_manager
from bot.services.history import record_download
from bot.services.rate_limit import RateLimiter, rate_limiter
from bot.services.url_tokens import put_url
from bot.utils.helpers import extract_urls, format_size, platform_from_url, progress_bar
from bot.utils.safe_fetch import UnsafeURLError, check_public_url, open_public

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"
MODES = {"v": "video", "a": "audio"}

# Separate from the download limiter: queries arrive per keystroke and must not
# eat the user's download quota. Picking a result DOES count as a download.
inline_query_limiter = RateLimiter(max_per_hour=INLINE_QUERIES_PER_HOUR)

_TITLE_CACHE: dict[str, tuple[float, str]] = {}
_TITLE_TTL = 3600.0


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _result_id(kind: str, url: str) -> str:
    """kind: 'vp'/'ap' placeholder, 'vc'/'ac' cached. Max 64 bytes."""
    return f"{kind}:{hashlib.sha1(url.encode()).hexdigest()[:24]}"


def _storage_chat() -> int | None:
    if STORAGE_CHAT_ID is not None:
        return STORAGE_CHAT_ID
    return min(ADMIN_IDS) if ADMIN_IDS else None


def _fetch_title(url: str) -> str:
    """Best-effort title via oEmbed (YouTube, TikTok) — ~200 ms, never raises."""
    now = time.monotonic()
    hit = _TITLE_CACHE.get(url)
    if hit and now - hit[0] < _TITLE_TTL:
        return hit[1]
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if "youtu" in host:
        endpoint = "https://www.youtube.com/oembed?format=json&url="
    elif "tiktok.com" in host:
        endpoint = "https://www.tiktok.com/oembed?url="
    else:
        return ""
    title = ""
    try:
        req = urllib.request.Request(
            endpoint + urllib.parse.quote(url, safe=""),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with open_public(req, timeout=1.5) as resp:
            title = str(json.loads(resp.read(200_000)).get("title") or "")[:120]
    except Exception:
        pass
    if len(_TITLE_CACHE) > 500:
        _TITLE_CACHE.clear()
    _TITLE_CACHE[url] = (now, title)
    return title


async def _placeholder_file_id(context: ContextTypes.DEFAULT_TYPE, mode: str) -> str | None:
    """Upload the labelled placeholder photo once; reuse its file_id after."""
    # Versioned: a redesigned image needs a fresh upload, not the old file_id.
    name = f"placeholder_{mode}_v2"
    cached = inline_cache.get_meta(name)
    if cached:
        return cached
    chat = _storage_chat()
    if chat is None:
        return None
    try:
        with (ASSETS / f"inline_{mode}.jpg").open("rb") as f:
            msg = await context.bot.send_photo(
                chat, photo=InputFile(f, filename=f"{mode}.jpg"), disable_notification=True
            )
        file_id = msg.photo[-1].file_id
        inline_cache.put_meta(name, file_id)
        if STORAGE_CHAT_ID is None:
            await _quiet_delete(context, chat, msg.message_id)
        return file_id
    except TelegramError:
        logger.exception("could not upload the inline placeholder")
        return None


async def warm_placeholders(app) -> None:
    """Upload both placeholders at startup so no user's first query waits on it."""
    if not INLINE_ENABLED:
        return
    try:
        ok = [m for m in ("video", "audio") if await _placeholder_file_id(app, m)]
        logger.info("Inline mode ready (placeholders: %s)", ", ".join(ok) or "none")
    except Exception:
        logger.exception("Inline placeholder warmup failed")


async def _quiet_delete(context, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


WAIT_CALLBACK = "inl:wait"


def _preparing_markup() -> InlineKeyboardMarkup:
    # An inline keyboard is REQUIRED: without one Telegram omits the
    # inline_message_id, and the message could never be edited into the media.
    # It must NOT lead anywhere while the download runs — an "Open bot" link
    # here started a second, parallel download of the same link in the DM.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏳ Preparing…", callback_data=WAIT_CALLBACK)]]
    )


def _open_bot_markup(bot_username: str, start: str = "inline") -> InlineKeyboardMarkup:
    """Only once the inline attempt is over (failed / too big for Telegram)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🤖 Open bot", url=f"https://t.me/{bot_username}?start={start}")]]
    )


async def _is_public(url: str) -> bool:
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, check_public_url, url), 3
        )
        return True
    except (UnsafeURLError, asyncio.TimeoutError):
        return False


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.inline_query
    if not q or not INLINE_ENABLED:
        return
    hint = InlineQueryResultsButton(text="📥 Paste a media link after the bot's name", start_parameter="inline")
    urls = extract_urls((q.query or "").strip(), expand=False)
    if not urls:
        await _answer(q, [], button=hint, cache_time=3600)
        return
    allowed, _ = inline_query_limiter.allow(q.from_user.id)
    if not allowed and q.from_user.id not in ADMIN_IDS:
        await _answer(q, [], button=InlineQueryResultsButton(
            text="⏳ Too many searches — try again later", start_parameter="inline"))
        return
    url = urls[0]
    if not await _is_public(url):
        await _answer(q, [], button=InlineQueryResultsButton(
            text="🚫 That link can't be downloaded", start_parameter="inline"))
        return

    platform = platform_from_url(url)
    title = await asyncio.get_running_loop().run_in_executor(None, _fetch_title, url)
    label = title or f"{platform} link"
    results = []
    for code, mode in MODES.items():
        hit = inline_cache.get(url, mode)
        if hit:
            results.append(_cached_result(code, mode, url, hit))
            continue
        photo = await _placeholder_file_id(context, mode)
        if not photo:
            continue
        verb = "🎬 Video" if mode == "video" else "🎵 Audio"
        results.append(
            InlineQueryResultCachedPhoto(
                id=_result_id(f"{code}p", url),
                photo_file_id=photo,
                title=f"{verb} · {label}"[:100],
                description=f"{platform} — downloads after you send it",
                caption=f"⏳ <b>Preparing</b> {_esc(label)}…",
                parse_mode=ParseMode.HTML,
                reply_markup=_preparing_markup(),
            )
        )
    if not results:
        await _answer(q, [], button=InlineQueryResultsButton(
            text="Inline mode isn't set up yet — open the bot", start_parameter="inline"))
        return
    # Personal: results depend on what THIS bot has cached, not on the user,
    # but a short cache keeps a fresh upload from hiding behind an old answer.
    await _answer(q, results, cache_time=30)


def _cached_result(code: str, mode: str, url: str, hit: dict):
    rid = _result_id(f"{code}c", url)
    caption = f"<b>{_esc(hit.get('title') or '')}</b>" if hit.get("title") else None
    kind, fid = hit["kind"], hit["file_id"]
    title = f"⚡ {'Video' if mode == 'video' else 'Audio'} · {hit.get('title') or 'ready'}"[:100]
    if kind == "video":
        return InlineQueryResultCachedVideo(id=rid, video_file_id=fid, title=title,
                                            caption=caption, parse_mode=ParseMode.HTML)
    if kind == "audio":
        return InlineQueryResultCachedAudio(id=rid, audio_file_id=fid,
                                            caption=caption, parse_mode=ParseMode.HTML)
    if kind == "photo":
        return InlineQueryResultCachedPhoto(id=rid, photo_file_id=fid, title=title,
                                            caption=caption, parse_mode=ParseMode.HTML)
    return InlineQueryResultCachedDocument(id=rid, document_file_id=fid, title=title,
                                           caption=caption, parse_mode=ParseMode.HTML)


async def _answer(q, results, *, button=None, cache_time: int = 300) -> None:
    try:
        await q.answer(results, cache_time=cache_time, is_personal=True, button=button)
    except TelegramError as e:
        # A query answered too late (slow network) is simply gone; nothing to do.
        logger.info("inline answer failed: %s", e)


def _input_media(kind: str, file_id: str, caption: str):
    common = {"caption": caption, "parse_mode": ParseMode.HTML}
    if kind == "video":
        return InputMediaVideo(file_id, supports_streaming=True, **common)
    if kind == "audio":
        return InputMediaAudio(file_id, **common)
    if kind == "photo":
        return InputMediaPhoto(file_id, **common)
    return InputMediaDocument(file_id, **common)


def _file_of(msg) -> tuple[str, str] | None:
    """(kind, file_id) of an uploaded message."""
    for kind in ("video", "audio", "document"):
        obj = getattr(msg, kind, None)
        if obj is not None:
            return kind, obj.file_id
    if getattr(msg, "photo", None):
        return "photo", msg.photo[-1].file_id
    return None


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ch = update.chosen_inline_result
    if not ch or not ch.inline_message_id or not INLINE_ENABLED:
        return
    code, _, _ = ch.result_id.partition(":")
    if len(code) != 2 or code[1] != "p" or code[0] not in MODES:
        return  # a cached result was sent as finished media already
    mode = MODES[code[0]]
    urls = extract_urls(ch.query or "", expand=False)
    if not urls:
        return
    url, user_id, imid = urls[0], ch.from_user.id, ch.inline_message_id
    me = context.bot.username or ""
    give_up = _open_bot_markup(me, f"dl_{put_url(url, user_id)}")
    last = {"t": 0.0}
    started = time.monotonic()
    logger.info("inline %s chosen by %s: %s", mode, user_id, url[:80])

    async def caption(text: str, *, force: bool = False, final: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last["t"] < 3:
            return  # Telegram limits how often one message may be edited
        last["t"] = now
        try:
            await context.bot.edit_message_caption(
                inline_message_id=imid, caption=text, parse_mode=ParseMode.HTML,
                reply_markup=give_up if final else _preparing_markup(),
            )
        except TelegramError:
            pass

    allowed, retry = rate_limiter.allow(user_id)
    if not allowed and user_id not in ADMIN_IDS:
        await caption(f"⏳ Rate limit reached — try again in {retry}s.", force=True, final=True)
        return
    if not await _is_public(url):
        await caption(f"🚫 {_esc(PRIVATE_URL_ERROR)}", force=True, final=True)
        return

    hit = inline_cache.get(url, mode)
    if hit:  # fetched by someone else while this user was choosing
        await _swap_in(context, imid, hit["kind"], hit["file_id"], hit.get("title", ""))
        return

    async def on_progress(pct: float, msg: str) -> None:
        # The last stages (finishing / converting) must always show — dropped
        # by the throttle, the card sat on an early "7%" for the whole tail.
        await caption(f"{progress_bar(pct)}\n<code>{_esc(msg)}</code>", force=pct >= 99)

    await caption("⏳ <b>Starting…</b>", force=True)
    result = None
    platform = platform_from_url(url)
    try:
        result = await download_manager.download(
            # m4a: YouTube's own AAC stream, remuxed — no minute-long MP3
            # re-encode on a small VPS. Telegram plays it as audio natively.
            url=url, mode=mode, quality=AUTO_QUALITY, audio_format="m4a",
            title_hint="media", progress_cb=on_progress,
        )
        if not result.success or not result.primary:
            record_download(user_id, url, "", platform, mode, AUTO_QUALITY, False, error=result.error)
            await caption(f"❌ <b>Download failed</b>\n{_esc(result.error or 'Unknown error')}", force=True, final=True)
            return
        size = result.file_size or result.primary.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            record_download(user_id, url, result.title or "", platform, mode, AUTO_QUALITY,
                            False, file_size=size, error="File too large")
            await caption(
                f"⚠️ This is <b>{format_size(size)}</b> — over Telegram's "
                f"{format_size(MAX_FILE_SIZE_BYTES)} limit for bots.\n"
                "Tap <b>Open bot</b> to pick a lower quality.",
                force=True, final=True,
            )
            return
        chat = _storage_chat()
        if chat is None:
            await caption("❌ Inline mode isn't set up on this server yet.", force=True, final=True)
            return
        await caption("📤 <b>Uploading…</b>", force=True)
        sent = await download_handlers._send_media(
            context, chat, result.primary, result, caption="", silent=True
        )
        found = _file_of(sent)
        if not found:
            await caption("❌ Upload failed.", force=True, final=True)
            return
        kind, file_id = found
        title = (result.title or "")[:200]
        inline_cache.put(url, mode, file_id=file_id, kind=kind, title=title)
        await _swap_in(context, imid, kind, file_id, title)
        if STORAGE_CHAT_ID is None:
            await _quiet_delete(context, chat, sent.message_id)
        record_download(user_id, url, title, platform, mode, AUTO_QUALITY, True, file_size=size)
        logger.info("inline %s delivered in %.1fs (%s): %s", mode,
                    time.monotonic() - started, format_size(size), url[:80])
    except Exception:
        logger.exception("inline download failed for %s", url[:80])
        await caption("❌ <b>Something went wrong.</b> Please try again.", force=True, final=True)
    finally:
        if result is not None:
            download_manager.cleanup_result_files(result)


async def _swap_in(context, imid: str, kind: str, file_id: str, title: str) -> None:
    cap = f"<b>{_esc(title)}</b>" if title else ""
    try:
        await context.bot.edit_message_media(
            inline_message_id=imid, media=_input_media(kind, file_id, cap)
        )
    except TelegramError:
        logger.exception("could not swap the inline placeholder for the media")
