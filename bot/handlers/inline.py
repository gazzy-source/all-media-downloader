"""
Inline mode: type `@bot <link>` in any chat, pick 🎬 Video or 🎵 Audio, and the
sent message turns into the media.

Telegram wants an inline answer within seconds and gives no way to upload a
file into an inline message afterwards — an edit may only reference a file_id.
So the flow is:

1. Answer at once, before any download, with two labelled placeholder photos.
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
    InlineQueryResultCachedPhoto,
    InlineQueryResultAudio,
    InlineQueryResultVideo,
    InlineQueryResultsButton,
    InputFile,
    InputMediaAnimation,
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
    INLINE_ASSET_BASE,
    INLINE_ENABLED,
    INLINE_SEARCH_ENABLED,
    INLINE_SEARCH_PAGE,
    INLINE_QUALITY,
    INLINE_QUERIES_PER_HOUR,
    MAX_FILE_SIZE_BYTES,
    STORAGE_CHAT_ID,
)
from bot.handlers import download as download_handlers
from bot.services import activity, inline_cache, jobs, user_prefs, yt_search
from bot.services.dl_queue import download_queue
from bot.services.upload_gate import UploadCancelled
from bot.services.downloader import PRIVATE_URL_ERROR, download_manager
from bot.services.history import record_download
from bot.services.rate_limit import RateLimiter, rate_limiter
from bot.utils import redact
from bot.utils.texts import (
    oversized_video_advice,
    rate_limit_text,
    too_big_text,
    upload_failed_text,
)
from bot.services.url_tokens import put_url
from bot.services.yt_telemetry import classify_failure, emit, emit_terminal, new_job_id
from bot.utils.helpers import extract_urls, format_size, platform_from_url
from bot.utils.progress_view import ProgressView
from bot.utils.safe_fetch import (
    UnresolvableURLError,
    UnsafeURLError,
    check_public_url,
    open_public,
)

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"
MODES = {"v": "video", "a": "audio"}

# Separate from the download limiter: queries arrive per keystroke and must not
# eat the user's download quota. Picking a result DOES count as a download.
inline_query_limiter = RateLimiter(max_per_hour=INLINE_QUERIES_PER_HOUR)

_TITLE_CACHE: dict[str, tuple[float, str]] = {}
_TITLE_TTL = 3600.0

# Links being pre-extracted while the user picks Video or Audio. Choosing takes
# a second or two — enough to finish most of the 2-20s extraction before the
# download even starts, which then reuses it (DOWNLOAD_REUSE_TTL).
_PREFETCHING: set[str] = set()
_BACKGROUND: set[asyncio.Task] = set()  # strong refs: tasks must not be GC'd
_PREFETCH_SLOTS = asyncio.Semaphore(2)


async def _prefetch(url: str) -> None:
    if url in _PREFETCHING or len(_PREFETCHING) >= 8:
        return
    _PREFETCHING.add(url)
    try:
        async with _PREFETCH_SLOTS:
            await asyncio.wait_for(download_manager.extract_info(url), 90)
    except Exception as e:  # best effort: the download extracts by itself
        logger.debug("inline prefetch skipped for %s (%s)", redact.url(url), type(e).__name__)
    finally:
        _PREFETCHING.discard(url)


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _key(mode: str) -> str:
    """Cache slot shared with DM/group downloads of the same link+quality."""
    return inline_cache.repeat_key(mode, INLINE_QUALITY, "m4a")


def _result_id(kind: str, url: str) -> str:
    """kind: 'vp'/'ap' placeholder. Max 64 bytes."""
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
CANCEL_CALLBACK = "inl:x"


# Live status per inline message (what its button says), so tapping the
# button can answer with the same thing. Bounded; dropped when a job ends.
_STATUS: dict[str, str] = {}


def _status_markup(label: str) -> InlineKeyboardMarkup:
    # An inline keyboard is REQUIRED: without one Telegram omits the
    # inline_message_id, and the message could never be edited into the media.
    # The button IS the live status ("⬇ 45%"); it must not lead anywhere while
    # the download runs — an "Open bot" link here started a second, parallel
    # download of the same link in the DM.
    return InlineKeyboardMarkup([[InlineKeyboardButton(label[:60], callback_data=WAIT_CALLBACK)]])


def _preparing_markup() -> InlineKeyboardMarkup:
    """Working… (tap: the live status) | ✖ Cancel (stops the download)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏳ Working…", callback_data=WAIT_CALLBACK),
        InlineKeyboardButton("✖ Cancel", callback_data=CANCEL_CALLBACK),
    ]])


def current_status(inline_message_id: str | None) -> str | None:
    return _STATUS.get(inline_message_id or "")


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
    except UnresolvableURLError:
        return True  # a typo/dead domain: the download will say so itself
    except (UnsafeURLError, asyncio.TimeoutError):
        return False


_IGNORE_UNTIL = 0.0  # set at startup: queries replayed from before it are dead


def ignore_backlog(seconds: float = 15) -> None:
    global _IGNORE_UNTIL
    _IGNORE_UNTIL = time.monotonic() + seconds


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    activity.touch()
    q = update.inline_query
    if not q or not INLINE_ENABLED:
        return
    if time.monotonic() < _IGNORE_UNTIL:
        # Kept across the restart, these queries expired long ago (Telegram
        # waits ~10s for an answer); searching for them is wasted work.
        try:
            await q.answer([], cache_time=0)
        except TelegramError:
            pass
        return
    hint = InlineQueryResultsButton(
        text="🎵 Type a song — “video …” for videos, or paste a link",
        start_parameter="inline")
    urls = extract_urls((q.query or "").strip(), expand=False)
    if not urls:
        text = yt_search.normalize_query(q.query or "")
        if INLINE_SEARCH_ENABLED and len(text) >= 2:
            await _search(q, text)
        else:
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
    # A cache hit will be swapped in after selection. Avoid eagerly extracting
    # the same URL just because the other media mode has not been cached yet.
    if not (inline_cache.get(url, _key("video")) or inline_cache.get(url, _key("audio"))):
        task = asyncio.create_task(_prefetch(url))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    title = await asyncio.get_running_loop().run_in_executor(None, _fetch_title, url)
    label = title or f"{platform} link"
    results = []
    for code, mode in MODES.items():
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
                caption=f"<b>{_esc(label)}</b>",
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


async def _answer(
    q, results, *, button=None, cache_time: int = 300, next_offset: str | None = None,
    personal: bool = True,
) -> None:
    try:
        await q.answer(results, cache_time=cache_time, is_personal=personal, button=button,
                       next_offset=next_offset)
    except TelegramError as e:
        # A query answered too late (slow network) is simply gone; nothing to do.
        logger.info("inline answer failed: %s", e)


_VIDEO_ID = __import__("re").compile(r"[A-Za-z0-9_-]{11}")
_AUDIO_PREFIXES = ("audio ", "mp3 ", "music ", "song ", "🎵")
_VIDEO_PREFIXES = ("video ", "videos ", "vid ", "🎬", "🎥")
# Per user: the newest query id. A search starts only once typing pauses, so a
# word typed letter by letter costs one YouTube search, not one per keystroke.
_LATEST: dict[int, str] = {}
# Measured in production: people pause 0.5-1.7s between words, so 0.4s still
# searched "meet me on the l", "…la", "…lab" separately.
_DEBOUNCE = 0.5  # searches cost ~0.9s now (one API call), so react sooner
_SEARCH_SLOTS = asyncio.Semaphore(3)


def _split_mode(text: str) -> tuple[str, str]:
    """
    Songs by default: 'lofi beats' -> ('audio', 'lofi beats');
    'video lofi beats' -> ('video', 'lofi beats'). "audio …" still works.
    """
    low = text.lower()
    word = low.strip()
    if len(word) >= 3 and any(m.startswith(word) for m in ("video", "videos")):
        # "vid", "vide", "video": the mode word alone or still being typed —
        # nothing to search for yet.
        return "video", ""
    if word in ("audio", "music", "mp3", "song"):
        return "audio", ""
    for p in _VIDEO_PREFIXES:
        if low.startswith(p):
            return "video", text[len(p):].strip()
    for p in _AUDIO_PREFIXES:
        if low.startswith(p):
            return "audio", text[len(p):].strip()
    return "audio", text


async def _search(q, text: str) -> None:
    mode, terms = _split_mode(text)
    if len(terms) < 3:
        # One or two letters match everything: don't spend a YouTube search on it.
        await _answer(q, [], button=InlineQueryResultsButton(
            text="🎬 Now type a video name" if mode == "video" else "🎵 Keep typing a song name…",
            start_parameter="inline"), cache_time=60)
        return
    uid = q.from_user.id
    # Every query (cached or not) becomes the user's latest, so an older one
    # still waiting out the debounce stands down.
    _LATEST.pop(uid, None)
    _LATEST[uid] = q.id
    while len(_LATEST) > 5000:
        _LATEST.pop(next(iter(_LATEST)))  # oldest first, not everyone at once
    hits = yt_search.cached(terms)
    if hits is None:
        await asyncio.sleep(_DEBOUNCE)
        if _LATEST.get(uid) != q.id:
            return  # still typing: a newer query will search (Telegram drops this one)
        allowed, _ = inline_query_limiter.allow(uid)
        if not allowed and uid not in ADMIN_IDS:
            await _answer(q, [], button=InlineQueryResultsButton(
                text="⏳ Too many searches — try again later", start_parameter="inline"))
            return
        try:
            async with _SEARCH_SLOTS:
                # 20s: the first search after a restart also pays yt-dlp's cold
                # start (two timed out at 12s in production). The thread keeps
                # running past a timeout and still fills the cache for the retry.
                # 8s: Telegram drops an inline answer after ~10s ("query is
                # too old"); 15-21s searches were answered into the void. The
                # thread keeps running and fills the cache for the retry.
                hits = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        yt_search.EXECUTOR, yt_search.search, terms), 8)
        except Exception as e:
            logger.warning("inline search failed (%s chars): %r", len(terms), e)
            await _answer(q, [], button=InlineQueryResultsButton(
                text="⚠️ Search is unavailable right now — paste a link instead",
                start_parameter="inline"), cache_time=5)
            return
    try:
        start = max(0, int(q.offset or 0))
    except ValueError:
        start = 0
    page = hits[start:start + INLINE_SEARCH_PAGE]
    if not page and start == 0:
        await _answer(q, [], button=InlineQueryResultsButton(
            text=f"No results for “{terms[:30]}”", start_parameter="inline"), cache_time=60)
        return
    results = [_search_result(mode, h) for h in page]
    if start == 0 and page and not inline_cache.get(page[0].url, _key(mode)):
        task = asyncio.create_task(_prefetch_if_still_latest(uid, q.id, page[0].url))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    nxt = start + INLINE_SEARCH_PAGE
    # Not personal: everyone typing the same search gets the same list, so
    # Telegram can answer repeats from its own cache without asking the bot.
    await _answer(q, results, cache_time=300, personal=False,
                  next_offset=str(nxt) if nxt < len(hits) else None,
                  button=None if start else InlineQueryResultsButton(
                      text=("🎵 Songs — start with “video” for videos"
                            if mode == "audio" else "🎬 Videos — type without “video” for songs"),
                      start_parameter="inline"))


async def _prefetch_if_still_latest(uid: int, query_id: str, url: str) -> None:
    """
    Most picks are the top result: once the user has stopped typing and is
    looking at the list, extract it ahead of time so a pick starts warm. Not
    while they are still typing — every word would cost an extraction.
    """
    await asyncio.sleep(1.5)
    if _LATEST.get(uid) == query_id:
        await _prefetch(url)


def _search_result(mode: str, h: "yt_search.SearchHit"):
    meta = " · ".join(x for x in (h.channel, yt_search.human_duration(h.duration),
                                  yt_search.human_views(h.views)) if x)
    caption = f"<b>{_esc(h.title[:200])}</b>"
    if mode == "audio":
        # An audio placeholder swaps cleanly into the real track.
        return InlineQueryResultAudio(
            # Per-result URL: Telegram caches a file by URL WITH the title it
            # was first sent with, so one shared URL showed the previous
            # song's name on every later pick while it downloaded. The file
            # carries ID3 tags ("Arrives here in a moment" / "All-Media
            # Downloader"): untagged, Telegram showed its file name.
            id=f"sa:{h.id}", audio_url=f"{INLINE_ASSET_BASE}preparing_audio_v3.mp3?v={h.id}",
            title=h.title[:100], performer=h.channel[:60] or None,
            audio_duration=h.duration, caption=caption, parse_mode=ParseMode.HTML,
            reply_markup=_preparing_markup(),
        )
    # A video result shows the real thumbnail, title and stats in the list; the
    # sent message is a short placeholder clip that becomes the real video.
    return InlineQueryResultVideo(
        id=f"sv:{h.id}", video_url=f"{INLINE_ASSET_BASE}placeholder_v1.mp4?v={h.id}",
        mime_type="video/mp4", thumbnail_url=h.thumbnail, title=h.title[:100],
        description=meta[:200] or None, caption=caption, parse_mode=ParseMode.HTML,
        video_duration=h.duration, reply_markup=_preparing_markup(),
    )


def _input_media(kind: str, file_id: str, caption: str):
    common = {"caption": caption, "parse_mode": ParseMode.HTML}
    if kind == "video":
        return InputMediaVideo(file_id, supports_streaming=True, **common)
    if kind == "audio":
        return InputMediaAudio(file_id, **common)
    if kind == "photo":
        return InputMediaPhoto(file_id, **common)
    if kind == "animation":
        return InputMediaAnimation(file_id, **common)
    return InputMediaDocument(file_id, **common)


def _file_of(msg) -> tuple[str, str] | None:
    """(kind, file_id) of an uploaded message."""
    # animation first: Telegram turns a soundless MP4 sent via sendVideo into
    # an animation, and such a message carries BOTH .animation and .document.
    for kind in ("animation", "video", "audio", "document"):
        obj = getattr(msg, kind, None)
        if obj is not None:
            return kind, obj.file_id
    if getattr(msg, "photo", None):
        return "photo", msg.photo[-1].file_id
    return None


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    activity.touch()
    ch = update.chosen_inline_result
    if not ch or not ch.inline_message_id or not INLINE_ENABLED:
        return
    code, _, ref = ch.result_id.partition(":")
    if code in ("sv", "sa") and _VIDEO_ID.fullmatch(ref):
        # A search result: the id IS the video; the query was search text.
        mode = "video" if code == "sv" else "audio"
        url = f"https://www.youtube.com/watch?v={ref}"
    elif len(code) == 2 and code[1] == "p" and code[0] in MODES:
        mode = MODES[code[0]]
        urls = extract_urls(ch.query or "", expand=False)
        if not urls:
            return
        url = urls[0]
    else:
        return  # unknown/stale result id; placeholders use vp/ap or sv/sa
    user_id, imid = ch.from_user.id, ch.inline_message_id
    telemetry_id = new_job_id() if "youtube" in platform_from_url(url).lower() else ""
    me = context.bot.username or ""
    give_up = _open_bot_markup(me, f"dl_{put_url(url, user_id)}")
    last = {"t": 0.0}
    # Progress edits are fired from yt-dlp's thread without waiting, so a late
    # "Finishing…" could land AFTER the final caption and leave the card stuck
    # on Preparing. One lock orders the edits; once a stage is terminal (or the
    # upload began), progress stops editing for good.
    edits = asyncio.Lock()
    state = {"done": False}
    started = time.monotonic()
    logger.info("inline %s chosen by %s: %s", mode, redact.uid(user_id), redact.url(url))

    title = _known_title(url, code, ref)
    job = jobs.start(f"i:{imid}", user_id)

    async def cancelled_card() -> None:
        async with edits:
            state["done"] = True
            _STATUS.pop(imid, None)
            # Telegram lets no bot delete a message sent via inline mode (it
            # is the user's own); the closest is an empty card: no name, no
            # buttons, one word.
            try:
                await context.bot.edit_message_caption(
                    inline_message_id=imid, caption="✖ Cancelled",
                    parse_mode=ParseMode.HTML, reply_markup=None)
            except TelegramError as e:
                logger.info("inline cancel edit failed: %s", e)

    job.on_cancel = cancelled_card

    async def status(label: str, *, force: bool = False, markup=None) -> None:
        """
        One status line under the name; the button stays "⏳ Working…" (tap it
        for the same status). Button-only edits never showed up in Telegram's
        apps — the card sat on its first status until the file arrived.
        """
        async with edits:
            if state["done"] or label == state.get("label"):
                return  # identical edits are refused ("message is not modified")
            now = time.monotonic()
            if not force and now - last["t"] < 2:
                return  # Telegram limits how often one message may be edited
            last["t"] = now
            state["label"] = label
            _STATUS[imid] = label
            if len(_STATUS) > 2000:
                _STATUS.pop(next(iter(_STATUS)))
            head = f"<b>{_esc(title)}</b>\n" if title else ""
            try:
                await context.bot.edit_message_caption(
                    inline_message_id=imid, caption=f"{head}{_esc(label)}"[:1024],
                    parse_mode=ParseMode.HTML, reply_markup=markup or _preparing_markup())
            except TelegramError as e:
                logger.info("inline status edit failed: %s", e)
                if "message_id_invalid" in str(e).lower():
                    # The card no longer exists: nobody is waiting for it.
                    logger.info("inline card gone — stopping its download")
                    job.event.set()
                    if job.task is not None and not job.started:
                        job.task.cancel()

    async def finish(text: str) -> None:
        """Terminal state: the reason in the card, and the way out (Open bot)."""
        async with edits:
            state["done"] = True
            _STATUS.pop(imid, None)
            head = f"<b>{_esc(title)}</b>\n" if title else ""
            try:
                await context.bot.edit_message_caption(
                    inline_message_id=imid, caption=(head + text)[:1024],
                    parse_mode=ParseMode.HTML, reply_markup=give_up)
            except TelegramError as e:
                logger.info("inline final edit failed: %s", e)

    try:
        await _deliver(context, job, mode, url, user_id, imid, title, started,
                       status, finish, state, telemetry_id)
    finally:
        jobs.drop(job)


async def _deliver(context, job, mode, url, user_id, imid, title, started,
                   status, finish, state, telemetry_id: str = "") -> None:
    allowed, retry = rate_limiter.allow(user_id)
    if not allowed and user_id not in ADMIN_IDS:
        emit_terminal(telemetry_id, "failure", **{"class": "other"})
        await finish(rate_limit_text(retry, user_id))
        return
    if not await _is_public(url):
        emit_terminal(telemetry_id, "failure", **{"class": "private"})
        await finish(f"🚫 {_esc(PRIVATE_URL_ERROR)}")
        return

    if job.cancelled:
        emit_terminal(telemetry_id, "cancelled", **{"class": "cancelled"})
        return
    hit = inline_cache.get(url, _key(mode))
    if hit:  # fetched before or by someone else while this user was choosing
        emit(telemetry_id, "cache", outcome="hit", kind=hit.get("kind", "unknown"),
             cache_hit="yes", delivery="cached_swap", avoided_download="yes")
        logger.info("inline cache hit — swapping cached %s", mode)
        if await _swap_in(context, imid, hit["kind"], hit["file_id"], hit.get("title", "")):
            emit_terminal(telemetry_id, "success", delivery="cached")
            return
        inline_cache.forget(url, _key(mode))  # Telegram refused it: fetch afresh below
    logger.info("inline cache miss — downloading %s", mode)
    emit(telemetry_id, "cache", outcome="miss", cache_hit="no", delivery="fresh_download")
    if job.cancelled:
        return

    view = ProgressView()

    async def on_progress(pct: float, msg: str) -> None:
        # One short line on the button ("⬇ 45% · ~2s left"); a step change
        # always shows — throttled away, the card looked stuck.
        changed = view.update(pct, msg)
        await status(view.short(), force=changed)

    async def tick() -> None:
        # Keep the clock moving through steps that report nothing.
        while not state["done"]:
            await asyncio.sleep(3)
            if view.stage != "download":
                await status(view.short())

    ticker = asyncio.create_task(tick())
    _STATUS[imid] = "🔎 Finding source"
    inline_cache.add_pending(imid)
    result = None
    platform = platform_from_url(url)
    try:
        try:
            queue_entered = time.monotonic()
            emit(telemetry_id, "queue", outcome="queue_enter")
            async def _queued_download():
                emit(telemetry_id, "queue", outcome="queue_start",
                     queue_wait_ms=int((time.monotonic() - queue_entered) * 1000))
                return await download_manager.download(
                    url=url, mode=mode, quality=INLINE_QUALITY, audio_format="m4a",
                    title_hint=title or "media", progress_cb=on_progress,
                    cancel=job.event, job_id=telemetry_id,
                )
            result = await jobs.run_queued(
                job, download_queue,
                _queued_download,
                on_position=lambda n: on_progress(0, f"Queued — you're #{n} in line"),
                priority=user_prefs.is_premium(user_id),
            )
        except asyncio.CancelledError:
            if job.cancelled:
                return  # withdrawn from the queue; the card already says so
            raise
        if job.cancelled:
            return  # the card already says "Cancelled"
        if not result.success or not result.primary:
            emit_terminal(telemetry_id, "failure", **{"class": classify_failure(result.error or "")})
            logger.info("inline %s failed after %.1fs [%s]: %s — %s", mode,
                        time.monotonic() - started, view.phases(), redact.url(url),
                        (result.error or "")[:120])
            record_download(user_id, url, "", platform, mode, INLINE_QUALITY, False, error=result.error)
            error = (result.error or "Unknown error")[:700]
            await finish(f"❌ {_esc(error)}")
            return
        size = result.file_size or result.primary.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            from bot.services.downloader import recommend_fitting_quality
            cached_info = download_manager.cached_media_info(url) if mode == "video" else None
            rec = recommend_fitting_quality(cached_info.available_heights,
                                            cached_info.estimated_sizes,
                                            INLINE_QUALITY, MAX_FILE_SIZE_BYTES) if cached_info else None
            emit(telemetry_id, "size_guard", outcome="rejected", actual_bytes=size,
                 limit_bytes=MAX_FILE_SIZE_BYTES,
                 recommendation=rec.quality if rec else "none",
                 recommended_estimated_bytes=rec.estimated_bytes if rec else "none")
            emit_terminal(telemetry_id, "failure", **{"class": "size_limit"})
            record_download(user_id, url, result.title or "", platform, mode, INLINE_QUALITY,
                            False, file_size=size, error="File too large")
            advice = "Tap <b>Open bot</b> to pick a lower quality or 🎵 Audio."
            if mode == "video":
                cached_info = download_manager.cached_media_info(url)
                advice = oversized_video_advice(
                    cached_info.available_heights if cached_info else [],
                    cached_info.estimated_sizes if cached_info else {},
                    INLINE_QUALITY,
                    inline=True,
                )
            await finish(too_big_text(
                size, advice))
            return
        chat = _storage_chat()
        if chat is None:
            emit_terminal(telemetry_id, "failure", **{"class": "other"})
            await finish("❌ Inline mode isn't set up on this server yet.")
            return
        if job.cancelled:
            return
        view.sending()
        # ✖ Cancel stays while it waits for upload capacity; once the bytes
        # are flowing a tap answers "being sent".
        await status(view.short(), force=True)
        state["done"] = True  # from here the card only changes into the file
        try:
            sent = await download_handlers._send_media(
                context, chat, result.primary, result, caption="", silent=True,
                cancel=job.event, on_reserved=lambda: setattr(job, "cancellable", False),
                job_id=telemetry_id,
            )
        except UploadCancelled:
            emit_terminal(telemetry_id, "cancelled", **{"class": "cancelled"})
            return  # the card already says "Cancelled"
        found = _file_of(sent)
        if not found:
            state["done"] = False
            emit_terminal(telemetry_id, "failure", **{"class": "upload_error"})
            await finish(upload_failed_text("inline storage upload returned no file"))
            return
        kind, file_id = found
        title = (result.title or title or "")[:200]
        if not await _swap_in(context, imid, kind, file_id, title):
            state["done"] = False
            emit_terminal(telemetry_id, "failure", **{"class": "telegram_refused"})
            await finish("❌ Telegram refused this file here. Tap <b>Open bot</b> to get it.")
            return
        # Cached only once Telegram has accepted it in a message (an invalid
        # file_id would make every later inline answer for this link fail as
        # a whole) — and only if it IS the promised quality, not a fallback.
        if inline_cache.good_enough(mode, INLINE_QUALITY, result):
            inline_cache.put(
                url,
                _key(mode),
                file_id=file_id,
                kind=kind,
                title=title,
                performer=(getattr(result, "artist", None) or "") if kind == "audio" else "",
            )
        if STORAGE_CHAT_ID is None:
            await _quiet_delete(context, chat, sent.message_id)
        record_download(user_id, url, title, platform, mode, INLINE_QUALITY, True, file_size=size)
        emit_terminal(telemetry_id, "success", total_ms=int((time.monotonic() - started) * 1000))
        logger.info("inline %s delivered in %.1fs (%s) [%s]: %s", mode,
                    time.monotonic() - started, format_size(size), view.phases(),
                    redact.url(url))
    except Exception:
        logger.exception("inline download failed for %s", redact.url(url))
        emit_terminal(telemetry_id, "failure", **{"class": "unknown"})
        if job.cancelled:
            return
        state["done"] = False
        await finish("❌ Something went wrong. Please try again.")
    finally:
        ticker.cancel()
        _STATUS.pop(imid, None)
        if not jobs.SHUTTING_DOWN:  # on shutdown the restart rescues the card
            inline_cache.drop_pending(imid)
        if result is not None:
            download_manager.cleanup_result_files(result)


async def _swap_in(context, imid: str, kind: str, file_id: str, title: str) -> bool:
    cap = f"<b>{_esc(title)}</b>" if title else ""
    try:
        await context.bot.edit_message_media(
            inline_message_id=imid, media=_input_media(kind, file_id, cap)
        )
        return True
    except TelegramError:
        logger.exception("could not swap the inline placeholder for the media")
        return False


def _known_title(url: str, code: str, ref: str) -> str:
    """The name the card already shows (search hit or link title), for final captions."""
    if code in ("sv", "sa"):
        return yt_search.title_for(ref) or ""
    hit = _TITLE_CACHE.get(url)
    return hit[1] if hit else ""


async def rescue_interrupted(app) -> None:
    """
    Inline jobs live in memory. After a restart their cards would sit on
    "Preparing…" forever (and the button would keep saying "still
    downloading"), so tell those users what happened.
    """
    me = app.bot.username or ""
    for imid in inline_cache.drain_pending():
        try:
            await app.bot.edit_message_caption(
                inline_message_id=imid,
                caption="⚠️ <b>Interrupted</b> — the bot restarted. Send the link again.",
                parse_mode=ParseMode.HTML,
                reply_markup=_open_bot_markup(me),
            )
        except TelegramError as e:
            logger.debug("could not flag interrupted inline job: %s", e)
