"""URL intake, interactive choice callbacks, and file delivery."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ContextTypes

from bot.config import (
    ADMIN_IDS,
    AUDIO_HOST_HINTS,
    AUTO_DOWNLOAD_ALWAYS,
    AUTO_DOWNLOAD_GROUPS,
    AUTO_QUALITY,
    CHANNEL_REPLACE_LINK,
    DM_FAST_AUTO,
    EXTRACT_TIMEOUT,
    MAX_FILE_SIZE_BYTES,
    QUALITY_MAP,
)
from bot.keyboards.menus import (
    after_download_keyboard,
    audio_format_keyboard,
    main_reply_keyboard,
    mode_keyboard,
    quality_keyboard,
    subtitle_lang_keyboard,
)
from bot.services import activity, inline_cache, jobs, user_prefs
from bot.services.dl_queue import download_queue
from bot.services.upload_gate import UploadCancelled, upload_gate
from bot.utils import redact
from bot.utils.texts import (
    oversized_video_advice,
    rate_limit_text,
    too_big_text,
    upload_failed_text,
)
from bot.utils.progress_view import ProgressView
from bot.services.downloader import PRIVATE_URL_ERROR, download_manager
from bot.services.history import record_download
from bot.services.inflight import add as inflight_add, remove as inflight_remove
from bot.services.media_detect import detect_mode
from bot.services.rate_limit import rate_limiter
from bot.services.session import DownloadSession, sessions
from bot.services.yt_telemetry import classify_failure, emit, emit_terminal, new_job_id
from bot.utils.safe_fetch import UnresolvableURLError, UnsafeURLError, check_public_url
from bot.utils.helpers import (
    extract_urls,
    format_size,
    platform_from_url,
    safe_filename,
    short_id,
)

logger = logging.getLogger(__name__)


async def _extract_with_job_id(url: str, job_id: str):
    """Pass telemetry context when supported; keeps lightweight test doubles valid."""
    fn = download_manager.extract_info
    try:
        params = inspect.signature(fn).parameters.values()
        supports_job_id = any(p.name == "job_id" or p.kind == p.VAR_KEYWORD for p in params)
    except (TypeError, ValueError):
        supports_job_id = False
    return await fn(url, job_id=job_id) if supports_job_id else await fn(url)


def _is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    return chat.type in ("group", "supergroup")


def _is_channel_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "channel")


def _is_auto_chat(update: Update) -> bool:
    """Groups, supergroups, and channels use auto-download (no wizard)."""
    return _is_group_chat(update) or _is_channel_chat(update)


def _actor_id(update: Update) -> int | None:
    """User id, or channel id when the post has no from-user."""
    user = update.effective_user
    if user:
        return user.id
    chat = update.effective_chat
    if chat and chat.type == "channel":
        return chat.id
    return None


def _should_auto_download(update: Update) -> bool:
    if AUTO_DOWNLOAD_ALWAYS:
        return True
    return AUTO_DOWNLOAD_GROUPS and _is_auto_chat(update)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    activity.touch()
    m = update.effective_message
    if m is not None and _is_auto_chat(update) and _stale(m):
        return  # replayed after downtime: the group has moved on
    if not update.effective_message:
        return
    # PTB's message filters also match edits. Fixing a typo in a link message
    # must not download (and in channels, repost) the whole thing again.
    if update.edited_message or update.edited_channel_post:
        return
    # A message sent through our own inline mode is already being handled by
    # the inline flow — auto-download in a group must not fetch it twice.
    via = getattr(update.effective_message, "via_bot", None)
    if via is not None and via.id == context.bot.id:
        return
    # Channel posts often have no effective_user — still process them
    if not update.effective_user and not _is_channel_chat(update):
        return

    # Reply keyboard menus (private UX only — not groups/channels)
    from bot.handlers.start import text_menu_router

    if not _is_auto_chat(update) and await text_menu_router(update, context):
        return

    text = update.effective_message.text or update.effective_message.caption or ""
    # extract_urls() resolves pin.it / t.co / bit.ly with blocking HEAD requests.
    # PTB runs handlers as tasks on one loop, so doing that inline would stall
    # every other user's update for up to a couple of seconds per short link.
    if extract_urls(text, expand=False):
        urls = await asyncio.get_running_loop().run_in_executor(
            None, extract_urls, text
        )
    else:
        urls = []
    if not urls:
        # Groups/channels: stay quiet on normal posts
        if _is_auto_chat(update):
            return
        await update.effective_message.reply_text(
            "🔗 Please send a valid media URL.\n\n"
            "Example: a YouTube, Instagram, TikTok, X, Facebook, or Pinterest link.",
            reply_markup=main_reply_keyboard(),
        )
        return

    # Groups / channels (or DM_FAST_AUTO=1): download immediately in parallel
    if _should_auto_download(update) or (
        DM_FAST_AUTO and not _is_auto_chat(update)
    ):
        batch = urls[:5]
        if len(urls) > 5:
            await update.effective_message.reply_text(
                f"📎 {len(urls)} links — starting first <b>5</b> in parallel.",
                parse_mode=ParseMode.HTML,
            )
        elif len(batch) > 1:
            await update.effective_message.reply_text(
                f"📎 Starting <b>{len(batch)}</b> downloads…",
                parse_mode=ParseMode.HTML,
            )
        # With several links, no single job may remove the shared source post:
        # link A succeeding must not delete link B that failed. Collect every
        # outcome and decide once.
        multi = len(batch) > 1
        outcomes = await asyncio.gather(
            *[
                auto_download_flow(update, context, u, replace_source=not multi)
                for u in batch
            ],
            return_exceptions=True,
        )
        for u, out in zip(batch, outcomes):
            if isinstance(out, BaseException):
                logger.error(
                    "auto download for %s raised", u[:80], exc_info=out
                )
        msg = update.effective_message
        chat = update.effective_chat
        if (
            multi
            and chat is not None
            and chat.type == "channel"
            and CHANNEL_REPLACE_LINK
            and _is_link_only_post(msg)
            and len(urls) == len(batch)  # links past the first 5 were never tried
            and all(out is True for out in outcomes)
        ):
            await _try_delete(context, chat.id, msg.message_id)
        return

    # Private DM (default): mode / quality / format button wizard
    if len(urls) > 1:
        await update.effective_message.reply_text(
            f"📎 Found <b>{len(urls)}</b> links. Starting with the first one.\n"
            f"Send the others again after this finishes — or use a group/channel for multi-link auto.",
            parse_mode=ParseMode.HTML,
        )
    await start_url_flow(update, context, urls[0])


def _is_link_only_post(msg) -> bool:
    """
    A plain text post, the only kind a channel may replace with its media.

    A photo/video whose caption carries a link is content in its own right —
    deleting it to make room for the linked media would destroy what was posted.
    """
    text = getattr(msg, "text", None) if msg is not None else None
    if not text or not getattr(msg, "message_id", None):
        return False
    # Only links (and whitespace): "Great talks: url1 url2" is commentary that
    # would be lost, since the media goes up without a caption.
    rest = text
    for u in extract_urls(text, expand=False):
        rest = rest.replace(u, "")
    return not rest.strip(" \n\t,;|-•")


async def auto_download_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    replace_source: bool = True,
) -> bool:
    """
    Group / channel / instant mode: download immediately at AUTO_QUALITY.
    No mode/quality wizard.

    Returns True once the media was delivered. `replace_source=False` leaves
    the channel's source post alone (the caller decides for a multi-link post).
    """
    chat = update.effective_chat
    msg = update.effective_message
    actor = _actor_id(update)
    if not chat or not msg or actor is None:
        return False
    platform = platform_from_url(url)
    telemetry_id = new_job_id() if "youtube" in platform.lower() else ""

    allowed, retry = rate_limiter.allow(actor)
    if not allowed and actor not in ADMIN_IDS:
        try:
            await msg.reply_text(
                rate_limit_text(retry, actor),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return False
    # Groups stay quiet: a refusal there is just noise for everyone else.
    if await _refuse_private_url(msg, url, quiet=chat.type != "private"):
        return False

    quality = AUTO_QUALITY
    q_label = QUALITY_MAP.get(quality, {}).get("label", quality)
    # Fast path: skip HTML probe for YT/IG/TikTok (always video)
    low_u = url.lower()
    if any(
        x in low_u
        for x in (
            "youtu",
            "instagram.com/reel",
            "instagram.com/tv",
            "tiktok.com",
            "facebook.com",
            "fb.watch",
            "x.com/",
            "twitter.com/",
        )
    ):
        mode = "video"
    elif any(x in low_u for x in ("pinterest.", "pin.it/", "i.pinimg.com")):
        mode = await asyncio.get_running_loop().run_in_executor(None, detect_mode, url)
    else:
        mode = "video"

    # Channels only: music/podcast links become an audio message. Checked after
    # the fast path above because music.youtube.com also matches "youtu" and
    # would otherwise be pulled down as a video stream. Groups and DMs keep
    # their existing behaviour.
    if chat.type == "channel" and any(h in low_u for h in AUDIO_HOST_HINTS):
        mode = "audio"

    if mode == "image":
        kind = "🖼 Image"
    elif mode == "audio":
        kind = "🎵 Audio"
    else:
        kind = f"🎥 {q_label}"
    repeat = inline_cache.repeat_key(mode, quality, "mp3")
    hit = inline_cache.get(url, repeat) if repeat else None
    if hit:
        emit(telemetry_id, "cache", outcome="hit", kind=hit.get("kind", "unknown"),
             cache_hit="yes", delivery="cached_swap", avoided_download="yes")
        is_chan = chat.type == "channel"
        title = _esc((hit.get("title") or "Media")[:100])
        sent = await _send_cached(
            context, chat.id, hit,
            "" if is_chan else f"🎬 <b>{title}</b>\n⚡ Instant · All-Media Downloader",
            reply_markup=None if is_chan else after_download_keyboard(
                url, user_id=actor, private=chat.type == "private"),
            url=url, key=repeat, thread_id=_thread_of(msg),
        )
        if sent is not None:
            if (is_chan and CHANNEL_REPLACE_LINK and replace_source
                    and _is_link_only_post(msg)):
                await _try_delete(context, chat.id, msg.message_id)
            record_download(actor, url, hit.get("title") or "", platform, mode, quality, True)
            emit_terminal(telemetry_id, "success", delivery="cached")
            return True
    emit(telemetry_id, "cache", outcome="miss", cache_hit="no", delivery="fresh_download")

    view = ProgressView()
    head = f"⚡ <b>{kind}</b> · {_esc(platform)}"
    can_cancel = chat.type != "channel"  # a channel post has no person to own it
    status = await msg.reply_text(view.render(head), parse_mode=ParseMode.HTML,
                                  reply_markup=_CANCEL_KB if can_cancel else None)
    job = jobs.start(f"dm:{chat.id}:{status.message_id}", actor)
    if not can_cancel:
        job.cancellable = False
    on_progress, ticker = _progress_driver(
        view, lambda: head, status.edit_text,
        markup=(lambda: _CANCEL_KB if job.cancellable and not job.cancelled else None)
        if can_cancel else None)

    async def _cancelled() -> None:
        await ticker.close(None)
        await _quiet_delete_msg(context, chat.id, status.message_id)

    job.on_cancel = _cancelled

    inflight_add(chat.id, status.message_id)
    try:
        await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_DOCUMENT)
        queue_entered = time.monotonic()
        emit(telemetry_id, "queue", outcome="queue_enter")
        async def _queued_download():
            emit(telemetry_id, "queue", outcome="queue_start",
                 queue_wait_ms=int((time.monotonic() - queue_entered) * 1000))
            return await download_manager.download(
                url=url, mode=mode, quality=quality, title_hint="media",
                progress_cb=on_progress, cancel=job.event, job_id=telemetry_id,
            )
        result = await jobs.run_queued(
            job, download_queue,
            _queued_download,
            on_position=lambda n: on_progress(0, f"Queued — you're #{n} in line"),
            priority=user_prefs.is_premium(actor),
        )
    except asyncio.CancelledError:
        jobs.drop(job)
        if not job.cancelled:
            emit_terminal(telemetry_id, "failure", **{"class": "shutdown"})
            raise
        emit_terminal(telemetry_id, "cancelled", **{"class": "cancelled"})
        if not jobs.SHUTTING_DOWN:  # on shutdown: keep it for the restart notice
            inflight_remove(chat.id, status.message_id)
        return False
    except Exception as e:
        jobs.drop(job)
        ticker.cancel()
        logger.exception("auto download crashed")
        emit_terminal(telemetry_id, "failure", **{"class": classify_failure(str(e))})
        record_download(actor, url, "", platform, mode, quality, False, error=str(e))
        try:
            await status.edit_text(
                f"❌ Download failed:\n{_esc(download_manager._friendly_error(str(e)))}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        inflight_remove(chat.id, status.message_id)
        return False

    # close, not cancel: also waits out a progress edit already in flight,
    # which could otherwise land after (and overwrite) the final text below.
    await ticker.close(None)
    if job.cancelled or not (result.success and result.primary):
        jobs.drop(job)  # nothing will be sent; otherwise kept cancellable until sending
    if job.cancelled:  # its message is already gone (or we are shutting down)
        emit_terminal(telemetry_id, "cancelled", **{"class": "cancelled"})
        if not jobs.SHUTTING_DOWN:
            inflight_remove(chat.id, status.message_id)
        download_manager.cleanup_result_files(result)
        return False
    if not result.success or not result.primary:
        emit_terminal(telemetry_id, "failure", **{"class": classify_failure(result.error or "")})
        record_download(
            actor,
            url,
            result.title or "",
            platform,
            mode,
            quality,
            False,
            error=result.error,
        )
        try:
            await status.edit_text(
                f"❌ <b>Download failed</b>\n\n{_esc(result.error or 'Unknown error')}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        inflight_remove(chat.id, status.message_id)
        download_manager.cleanup_result_files(result)
        return False

    path = result.primary
    size = result.file_size or path.stat().st_size
    delivered = False
    ticker.cancel()
    view.sending(format_size(size))
    try:
        # ✖ Cancel stays while it waits for upload capacity (a tap once the
        # bytes are flowing answers "being sent").
        await status.edit_text(view.render(head), parse_mode=ParseMode.HTML,
                               reply_markup=_CANCEL_KB if can_cancel else None)
    except TelegramError:
        pass

    # Channels: clean media only (no caption, no buttons). Groups keep a short caption.
    is_channel = chat.type == "channel"
    if is_channel:
        caption = ""
        actions = None
    else:
        title = _esc((result.title or "Media")[:100])
        kind = "🖼 Image" if result.is_image else f"📐 {_quality_label(result)}"
        caption = (
            f"🎬 <b>{title}</b>\n"
            f"{kind} · 💾 {format_size(size)} · ⏱ ready in {view.elapsed():.0f}s\n"
            f"⚡ All-Media Downloader"
        )
        actions = after_download_keyboard(url, user_id=actor,
                                          private=chat.type == "private")

    try:
        if size > MAX_FILE_SIZE_BYTES:
            cached_info = download_manager.cached_media_info(url) if mode == "video" else None
            recommendation, recommended_bytes = "none", "none"
            if cached_info:
                from bot.services.downloader import recommend_fitting_quality
                rec = recommend_fitting_quality(cached_info.available_heights,
                                                cached_info.estimated_sizes,
                                                quality, MAX_FILE_SIZE_BYTES)
                if rec:
                    recommendation, recommended_bytes = rec.quality, rec.estimated_bytes
            emit(telemetry_id, "size_guard", outcome="rejected", actual_bytes=size,
                 limit_bytes=MAX_FILE_SIZE_BYTES, recommendation=recommendation,
                 recommended_estimated_bytes=recommended_bytes)
            if is_channel:
                try:
                    await status.delete()
                except TelegramError:
                    pass
            else:
                advice = "Open the bot in private chat to pick a lower quality or 🎵 Audio."
                if mode == "video":
                    cached_info = download_manager.cached_media_info(url)
                    advice = oversized_video_advice(
                        cached_info.available_heights if cached_info else [],
                        cached_info.estimated_sizes if cached_info else {},
                        quality,
                    )
                await status.edit_text(
                    too_big_text(size, advice),
                    parse_mode=ParseMode.HTML,
                )
            record_download(
                actor, url, result.title or "", platform, mode, quality, False,
                file_size=size, error="File too large",
            )
            emit_terminal(telemetry_id, "failure", **{"class": "size_limit"})
            inflight_remove(chat.id, status.message_id)
            return False

        if (
            is_channel
            and CHANNEL_REPLACE_LINK
            and replace_source
            and _is_link_only_post(msg)
        ):
            outcome = await _replace_channel_post(
                context, chat.id, msg.message_id, path, result,
                remember=lambda sent: _remember_upload(
                    url, repeat, sent, result.title or "", result, mode, quality),
            )
            logger.info("Channel post handling: %s", outcome)
        else:
            sent = await _send_media(
                context, chat.id, path, result, caption, reply_markup=actions,
                thread_id=_thread_of(msg),
                cancel=job.event, on_reserved=lambda: setattr(job, "cancellable", False),
                job_id=telemetry_id,
            )
            _remember_upload(url, repeat, sent, result.title or "", result, mode, quality)
        record_download(
            actor, url, result.title or "", platform, mode, quality, True,
            file_size=size,
        )
        delivered = True
        emit_terminal(telemetry_id, "success", total_ms=int(view.elapsed() * 1000))
        try:
            await status.delete()
        except TelegramError:
            if not is_channel:
                try:
                    await status.edit_text(f"✅ Sent · {format_size(size)}")
                except TelegramError:
                    pass
    except UploadCancelled:
        emit_terminal(telemetry_id, "cancelled", **{"class": "cancelled"})
        pass  # cancelled while waiting to send: its message is already gone
    except TelegramError as e:
        logger.exception("auto upload failed")
        emit_terminal(telemetry_id, "failure", **{"class": "upload_error"})
        record_download(
            actor, url, result.title or "", platform, mode, quality, False,
            file_size=size, error=str(e),
        )
        try:
            await status.edit_text(upload_failed_text(e), parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
    finally:
        jobs.drop(job)
        if not (job.cancelled and jobs.SHUTTING_DOWN):  # shutdown: restart says "Interrupted"
            inflight_remove(chat.id, status.message_id)
        download_manager.cleanup_result_files(result)
    return delivered


async def start_url_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    # Groups / channels always auto (safety if called from again: callback)
    if _should_auto_download(update):
        await auto_download_flow(update, context, url)
        return

    allowed, retry = rate_limiter.allow(user.id)
    if not allowed and user.id not in ADMIN_IDS:
        await msg.reply_text(
            rate_limit_text(retry, user.id),
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
        return
    if await _refuse_private_url(msg, url):
        return

    telemetry_id = new_job_id() if "youtube" in platform_from_url(url).lower() else ""
    status = await msg.reply_text("🔍 <b>Reading link…</b>", parse_mode=ParseMode.HTML)
    # Analysis can take tens of seconds on a slow platform. A restart in
    # that window used to leave this message frozen on Analyzing forever,
    # since only the download phase was registered for rescue.
    inflight_add(chat.id, status.message_id)

    # extract_info blocks with no callbacks of its own, so without a heartbeat
    # the message sits unchanged for the whole wait and reads as frozen. It is
    # usually 2-5s, but a retrying platform can stretch that.
    async def _analysing_heartbeat() -> None:
        started = time.time()
        try:
            while True:
                await asyncio.sleep(3)
                elapsed = int(time.time() - started)
                try:
                    await status.edit_text(
                        f"🔍 <b>Reading link…</b> · {elapsed}s",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    return
        except asyncio.CancelledError:
            raise

    heartbeat = asyncio.create_task(_analysing_heartbeat())
    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
        # Bounded: a flaky connection used to leave this running for minutes
        # (254s seen in production) with the user simply waiting.
        info = await asyncio.wait_for(
            _extract_with_job_id(url, telemetry_id), timeout=EXTRACT_TIMEOUT
        )
    except asyncio.TimeoutError:
        emit_terminal(telemetry_id, "failure", **{"class": "timeout"})
        logger.warning("extract_info timed out after %ss: %s", EXTRACT_TIMEOUT, redact.url(url))
        body = (
            "⏱ <b>Took too long to read this link</b>\n\n"
            f"Gave up after {EXTRACT_TIMEOUT}s — the platform is slow or "
            "busy right now.\n\n"
            "Try again, or send a different link."
        )
        try:
            await status.edit_text(body, parse_mode=ParseMode.HTML)
        except TelegramError:
            await msg.reply_text(
                body, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard()
            )
        rate_limiter.refund(user.id)
        return
    except Exception as e:
        emit_terminal(telemetry_id, "failure", **{"class": classify_failure(str(e))})
        logger.exception("extract_info failed")
        rate_limiter.refund(user.id)
        from bot.services.downloader import DownloadManager

        friendly = DownloadManager._friendly_error(str(e))
        body = (
            f"❌ <b>Could not read this link</b>\n\n{_esc(friendly)}"
        )
        # editMessageText accepts an INLINE keyboard only. Passing the
        # persistent reply keyboard here raised BadRequest("Inline keyboard
        # expected"), which escaped to the global handler — so every link the
        # bot could not read answered "Something went wrong" instead of saying
        # why. The reply keyboard is persistent anyway and needs no re-sending;
        # it only goes on the reply_text fallback.
        try:
            await status.edit_text(body, parse_mode=ParseMode.HTML)
        except TelegramError:
            await msg.reply_text(
                body,
                parse_mode=ParseMode.HTML,
                reply_markup=main_reply_keyboard(),
            )
        return
    finally:
        # Must stop on every path, or the ticker keeps overwriting whatever
        # the wizard (or the error branch) just wrote.
        heartbeat.cancel()
        # Analysis is over on every path out of this block - success,
        # timeout or error. The download phase registers itself separately.
        inflight_remove(chat.id, status.message_id)

    if info.is_live:
        emit_terminal(telemetry_id, "failure", **{"class": "other"})
        await status.edit_text(
            "🔴 This looks like a <b>live stream</b>. Live recording is limited.\n"
            "Try again after the stream ends, or send a VOD/clip link.",
            parse_mode=ParseMode.HTML,
        )
        return

    sid = short_id(10)
    session = DownloadSession(
        session_id=sid,
        user_id=user.id,
        chat_id=chat.id,
        url=url,
        telemetry_id=telemetry_id,
        title=info.title,
        platform=info.platform,
        duration=info.duration,
        thumbnail=info.thumbnail,
        uploader=info.uploader,
        view_count=info.view_count,
        description=info.description,
        is_live=info.is_live,
        is_playlist=info.is_playlist,
        playlist_count=info.playlist_count,
        has_video=info.has_video,
        has_audio=info.has_audio,
        has_image=info.has_image,
        has_subtitles=info.has_subtitles,
        subtitle_langs=info.subtitle_langs,
        available_heights=info.available_heights,
        available_image_sizes=info.available_image_sizes,
        estimated_sizes=info.estimated_sizes,
        min_sizes=info.min_sizes,
        extractor=info.extractor,
        raw_info={},  # keep memory light
        status_message_id=status.message_id,
        prompt_message_id=status.message_id,
        link_message_id=_own_message_id(msg, user),
    )
    sessions.put(session)

    choice = _default_choice(session, user_prefs.get(user.id))
    if choice is not None:
        # The user's saved defaults answer every wizard question: go straight
        # to the download (the status message becomes the progress message).
        session.mode, session.quality, session.audio_format = choice
        await execute_download(_StatusQuery(status), context, session)
        return

    body = info.summary_html() + "\n\n<b>Select download type:</b>"
    try:
        await status.edit_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=mode_keyboard(session),
            disable_web_page_preview=False,
        )
    except TelegramError:
        await msg.reply_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=mode_keyboard(session),
        )


class _Ticker:
    """Re-renders every few seconds so the clock moves in steps with no events."""

    def __init__(self, fn) -> None:
        self._fn = fn
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(3)
                await self._fn()
        except asyncio.CancelledError:
            pass

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


def _progress_driver(view: "ProgressView", header, edit, *, start: bool = True,
                     markup=None):
    """
    (on_progress, ticker) for a chat status message. Edits on every step
    change, otherwise at most every 2.5s; the ticker keeps the clock moving
    through steps that report nothing (finding the source, converting).
    `markup()` gives the buttons under it (✖ Cancel), if any.
    ticker.close(text, **kw) makes a final edit that no late progress edit
    can overwrite.
    """
    last = {"t": 0.0, "text": "", "closed": False}
    lock = asyncio.Lock()

    async def render(force: bool = False) -> None:
        async with lock:
            if last["closed"]:
                return
            now = time.monotonic()
            if not force and now - last["t"] < 2.5:
                return
            text = view.render(header())
            if text == last["text"]:
                return  # Telegram refuses identical edits
            last.update(t=now, text=text)
            try:
                kw = {"reply_markup": markup()} if markup is not None else {}
                await edit(text, parse_mode=ParseMode.HTML, **kw)
            except TelegramError:
                pass

    async def on_progress(pct: float, msg: str) -> None:
        changed = view.update(pct, msg)
        await render(force=changed)

    async def tick() -> None:
        if view.stage != "download":  # downloads tick on their own events
            await render()

    ticker = _Ticker(tick)

    async def close(text: str | None, **kw) -> None:
        async with lock:
            last["closed"] = True
            ticker.cancel()
            if text is None:
                return  # just stop editing (the message is about to go)
            try:
                await edit(text, parse_mode=ParseMode.HTML, **kw)
            except TelegramError:
                pass

    ticker.close = close
    if start:
        ticker.start()
    return on_progress, ticker


def _dm_header(session, mode: str, quality: str) -> str:
    what = {"audio": f"🎵 Audio {session.audio_format.upper()}",
            "video_subs": "🎞 Video + subtitles",
            "image": "🖼 Image"}.get(
        mode, f"🎬 Video {QUALITY_MAP.get(quality, {}).get('label', quality)}")
    return f"{_session_header(session)} · {what}"


_LAST_AGAIN: dict[tuple[int, str], float] = {}
_STALE_GROUP_POST = 600  # seconds


def _stale(msg) -> bool:
    """A group/channel post older than 10 min (kept updates replayed at start)."""
    from datetime import datetime, timezone

    date = getattr(msg, "date", None)
    if not isinstance(date, datetime):
        return False
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - date).total_seconds() > _STALE_GROUP_POST


async def _answer(query, text: str, *, alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except TelegramError:
        pass


async def _safe_edit(query, text: str, **kw) -> None:
    """A wizard edit that a double tap or an old message can't turn into an error."""
    try:
        await query.edit_message_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.info("wizard edit refused: %s", e)
    except TelegramError as e:
        logger.info("wizard edit failed: %s", e)


def _thread_of(msg) -> int | None:
    """The forum topic a message was posted in (files must land there too)."""
    if msg is not None and getattr(msg, "is_topic_message", False):
        return getattr(msg, "message_thread_id", None)
    return None


def _own_message_id(msg, user) -> int | None:
    """
    The user's own link message (deleted if they cancel). Not after Download
    Again: there the message is the bot's — the file the user already has.
    """
    sender = getattr(msg, "from_user", None)
    if sender is None or user is None or sender.id != user.id:
        return None
    # Only a message that is nothing but this one link: a photo with a link
    # caption, or a message with several links, is the user's own content.
    if not _is_link_only_post(msg) or len(extract_urls(msg.text, expand=False)) != 1:
        return None
    return getattr(msg, "message_id", None)


async def _quiet_delete_msg(context, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass  # already gone, or too old to delete


_CANCEL_KB = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel", callback_data="dlx")]])


class _StatusQuery:
    """
    Lets execute_download (written for a button tap) drive a plain status
    message, when saved defaults skip the buttons altogether.
    """

    def __init__(self, message) -> None:
        self.message = message

    async def edit_message_text(self, text, **kw):
        return await self.message.edit_text(text, **kw)


def _default_choice(session, prefs: dict) -> tuple[str, str | None, str] | None:
    """(mode, quality, audio_format) from saved defaults, or None to ask."""
    mode = prefs.get("mode")
    if mode == "video" and session.has_video and prefs.get("quality") not in (None, "ask"):
        return "video", prefs["quality"], session.audio_format
    if mode == "audio" and session.has_audio and prefs.get("audio") not in (None, "ask"):
        return "audio", None, prefs["audio"]
    return None


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    activity.touch()
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    if query.data in ("inl:x", "dlx"):
        # ✖ Cancel under an inline card / a DM progress message.
        imid = getattr(query, "inline_message_id", None)
        if query.data == "inl:x":
            key = f"i:{imid}"
        else:
            m = query.message
            key = f"dm:{m.chat.id}:{m.message_id}" if m else None
        outcome = await jobs.cancel(key, update.effective_user.id)
        try:
            await query.answer(jobs.CANCEL_ANSWERS[outcome])
        except TelegramError:
            pass
        return
    if query.data == "inl:wait":
        # The inline card's status button: answer with that same status.
        from bot.handlers.inline import current_status

        now = current_status(getattr(query, "inline_message_id", None))
        try:
            await query.answer(
                f"{now} — the file will replace this card." if now
                else "Working on it — the file will replace this card.")
        except TelegramError:
            pass
        return
    if query.data.startswith(("pref:", "prem:")):
        # Answered first, or Telegram keeps a spinner on the tapped button.
        try:
            await query.answer()
        except TelegramError:
            pass
        from bot.handlers import premium, start

        handler = start.handle_pref_callback if query.data.startswith("pref:") \
            else premium.handle_premium_callback
        await handler(update, context)
        return
    # Answer exactly once, right away — every later query.answer() would raise
    # BadRequest ("query is too old / already answered"). Feedback that used to
    # be an alert is sent as a normal message below instead.
    try:
        await query.answer()
    except TelegramError:
        pass  # stale/old query — still process the action
    data = query.data
    user_id = update.effective_user.id

    if data == "new":
        if not query.message:  # message too old for Telegram to hand back
            return
        await query.message.reply_text(
            "📥 Paste a media link to download.",
            reply_markup=main_reply_keyboard(),
        )
        return

    if data.startswith("again:"):
        from bot.services.url_tokens import get_url

        now = time.monotonic()
        tap = (user_id, data)
        if now - _LAST_AGAIN.get(tap, 0.0) < 3:
            return  # a double tap: the first one is already reading the link
        _LAST_AGAIN[tap] = now
        if len(_LAST_AGAIN) > 5000:
            _LAST_AGAIN.pop(next(iter(_LAST_AGAIN)))

        token = data[6:].strip()
        # Only the person the button was made for (or an admin). callback_data
        # is client-controlled, so the old "raw URL in the token" fallback let
        # a modified client make the bot fetch and post ANY URL into a group.
        chat = update.effective_chat
        private = chat is not None and chat.type == "private"
        # Owner-only in private chats. In a group or channel the button belongs
        # to a link already posted there (and in channels the "owner" is the
        # channel itself, so no member could ever match). The old raw-URL
        # fallback is gone, so a token can only ever name a link the bot saw.
        url = get_url(token, None if (user_id in ADMIN_IDS or not private) else user_id)
        if not url:
            if query.message and private:  # never a public "expired" reply
                await query.message.reply_text(
                    "🔗 Link expired. Please paste the URL again."
                )
            return
        if not query.message:
            return
        await start_url_flow(update, context, url)
        return

    parts = data.split(":")
    action = parts[0]
    if len(parts) < 2:
        return
    sid = parts[1]
    session = sessions.get(sid)
    private = bool(query.message and query.message.chat.type == "private")
    if not session:
        if action == "cancel":
            return  # a second tap on ❌ Cancel: it is already cancelled
        if not private:
            # Not ours to rewrite in a group: someone else's stale menu.
            await _answer(query, "This menu has expired — send the link again.", alert=True)
            return
        # Sessions live in memory, so they also vanish on a bot restart — not
        # only on timeout. Say what to do rather than just what went wrong.
        await _safe_edit(
            query,
            "⌛ <b>This menu has expired</b>\n\n"
            "Menus reset after a while and when the bot restarts. Just send "
            "the link again — it only takes a moment.",
            parse_mode=ParseMode.HTML,
        )
        return
    if session.user_id != user_id and user_id not in ADMIN_IDS:
        # A private pop-up, not a public reply that pings the whole group.
        await _answer(query, "⛔ This isn't your download.", alert=True)
        return

    if action == "cancel":
        sessions.remove(sid)
        rate_limiter.refund(session.user_id)  # nothing was downloaded
        await _safe_edit(query, "❌ Cancelled.")
        return

    if action == "back_mode":
        session.mode = None
        session.quality = None
        await query.edit_message_text(
            _session_header(session) + "\n\n<b>Select download type:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=mode_keyboard(session),
        )
        return

    if action == "back_quality":
        await query.edit_message_text(
            _session_header(session) + "\n\n<b>Select quality:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(session),
        )
        return

    # Every value below arrives in client-controlled callback_data; accept
    # only what our own keyboards can produce.
    if action == "mode":
        mode = parts[2] if len(parts) > 2 else "video"
        if mode not in _MODES:
            return
        session.mode = mode
        if mode == "audio":
            await query.edit_message_text(
                _session_header(session)
                + "\n\n🎵 <b>Audio format:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=audio_format_keyboard(session),
            )
            return
        if mode == "image":
            # Straight to the best image: the old size picker's choice never
            # reached the downloader, so every button fetched the same file.
            await execute_download(query, context, session)
            return
        # video / video_subs → quality
        await query.edit_message_text(
            _session_header(session)
            + f"\n\n🎥 Mode: <b>{_mode_label(mode)}</b>\n"
            f"<b>Select quality:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(session),
        )
        return

    if action == "aformat":
        fmt = parts[2] if len(parts) > 2 else "mp3"
        if fmt not in _AUDIO_FORMATS:
            return
        session.audio_format = fmt
        session.mode = "audio"
        # Start straight away. The format choice is the last thing the user had
        # to decide, so a separate confirm screen only cost another tap and
        # another round-trip before anything began downloading.
        await execute_download(query, context, session)
        return

    if action == "imgsize":
        idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        session.image_index = idx
        session.mode = "image"
        await execute_download(query, context, session)
        return

    if action == "quality":
        q = parts[2] if len(parts) > 2 else "720"
        if q not in QUALITY_MAP:
            return
        session.quality = q
        if session.mode == "video_subs":
            if session.has_subtitles and session.subtitle_langs:
                await query.edit_message_text(
                    _session_header(session)
                    + f"\n\n🎞 Quality: <b>{QUALITY_MAP.get(q, {}).get('label', q)}</b>\n"
                    f"<b>Select subtitle language:</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=subtitle_lang_keyboard(session),
                )
            else:
                # No subtitle list to pick from — nothing left to ask.
                session.subtitle_lang = "en.*"
                await execute_download(query, context, session)
            return
        # plain video — quality was the last decision, so start downloading
        await execute_download(query, context, session)
        return

    if action == "sublang":
        lang = parts[2] if len(parts) > 2 else "en"
        if lang not in ("en", "en.*") and lang not in (session.subtitle_langs or []):
            return  # e.g. "all" would pull every subtitle track there is
        session.subtitle_lang = lang
        await execute_download(query, context, session)
        return

    if action == "go":
        await execute_download(query, context, session)
        return


def _sent_file(msg) -> tuple[str, str] | None:
    """(kind, file_id) of a message we uploaded; animation first (see inline)."""
    for kind in ("animation", "video", "audio", "document"):
        obj = getattr(msg, kind, None)
        if obj is not None and getattr(obj, "file_id", None):
            return kind, obj.file_id
    photos = getattr(msg, "photo", None)
    if photos:
        return "photo", photos[-1].file_id
    return None


def _remember_upload(url: str, key: str | None, sent, title: str, result=None,
                     mode: str = "", quality: str = "") -> None:
    """After a real upload: the next request for this link+quality is instant."""
    found = _sent_file(sent) if key and sent is not None else None
    if found and (result is None or inline_cache.good_enough(mode, quality, result)):
        inline_cache.put(
            url,
            key,
            file_id=found[1],
            kind=found[0],
            title=title or "",
            performer=(getattr(result, "artist", None) or "") if found[0] == "audio" else "",
        )


_DELIVERED_UNCONFIRMED = object()


async def _send_cached(context, chat_id: int, hit: dict, caption: str, reply_markup=None,
                       *, url: str = "", key: str | None = None,
                       thread_id: int | None = None):
    """
    Re-send a file Telegram already has. Returns the Message (or, after a
    timeout, a marker: it probably arrived — never send it twice), or None to
    download normally. Only a refused FILE forgets the cache entry; a chat that
    can't take videos, or a network blip, must not wipe it for everyone.
    """
    field = hit.get("kind") if hit.get("kind") in ("video", "audio", "animation", "photo") else "document"
    send = getattr(context.bot, f"send_{field}")
    kw: dict = {field: hit["file_id"], "reply_markup": reply_markup}
    if field == "video":
        kw["supports_streaming"] = True
    elif field == "audio":
        # Legacy entries may lack presentation metadata; Telegram can still
        # send the cached file_id, so only pass fields known to the cache.
        if hit.get("title"):
            kw["title"] = hit["title"][:64]
        if hit.get("performer"):
            kw["performer"] = hit["performer"][:64]
    if thread_id:
        kw["message_thread_id"] = thread_id
    if caption:
        kw.update(caption=caption[:1024], parse_mode=ParseMode.HTML)
    try:
        sent = await send(chat_id, **kw)
        logger.info("Cached file sent as %s: %s", field, redact.url(url) if url else "inline")
        return sent
    except TimedOut:
        logger.warning(
            "Cached send timed out; delivery is unconfirmed, so skipping duplicate: %s",
            redact.url(url) if url else "inline",
        )
        return _DELIVERED_UNCONFIRMED
    except BadRequest as e:
        text = str(e).lower()
        if url and key and ("file" in text or "wrong type" in text or "identifier" in text):
            logger.info("cached file refused (%s) — forgetting it", e)
            inline_cache.forget(url, key)
        return None
    except TelegramError as e:
        logger.info("cached send failed (%s) — downloading normally", e)
        return None


def _cover_file(result):
    """The square cover Telegram's music player shows (None if there isn't one)."""
    cover = getattr(result, "cover", None)
    if cover is not None and cover.is_file():
        return InputFile(cover.read_bytes(), filename="cover.jpg")
    return None


_MODES = frozenset({"video", "video_subs", "audio", "image"})
_AUDIO_FORMATS = frozenset({"mp3", "m4a", "opus"})


async def _refuse_private_url(msg, url: str, *, quiet: bool = False) -> bool:
    """
    True (and tells the user, unless quiet) when the URL resolves to a
    private / local address. yt-dlp fetches whatever it is given, so without
    this, http://127.0.0.1:9123/health came back to the chat as a file.
    """
    try:
        await asyncio.get_running_loop().run_in_executor(None, check_public_url, url)
        return False
    except UnresolvableURLError:
        return False  # a typo or dead domain is not "private": yt-dlp will say so
    except UnsafeURLError:
        logger.warning("Refused non-public URL %s", redact.url(url))
        if not quiet:
            try:
                await msg.reply_text(f"🚫 {PRIVATE_URL_ERROR}")
            except TelegramError:
                pass
        return True


def _quality_label(result) -> str:
    """
    Requested quality, corrected to what was actually delivered.

    A cookieless YouTube fallback can only serve 360p, so a caption reading
    "1080p" on a 360p file would be plainly wrong.
    """
    requested = QUALITY_MAP.get(result.quality or "", {}).get(
        "label", result.quality or ""
    )
    height = getattr(result, "actual_height", None)
    if not height:
        return requested
    actual = f"{height}p"
    if actual == requested:
        return requested
    if result.quality == "max":
        return actual
    return f"{actual} (best available for {requested})"


def _mode_label(mode: str) -> str:
    return {
        "video": "Video",
        "video_subs": "Video + Subtitles",
        "audio": "Audio",
        "image": "Image",
    }.get(mode, mode)


def _session_header(session: DownloadSession) -> str:
    title = _esc(session.title[:120] if session.title else "Media")
    return (
        f"🎬 <b>{title}</b>\n"
        f"📡 {_esc(session.platform or '')}"
    )


async def execute_download(query, context: ContextTypes.DEFAULT_TYPE, session: DownloadSession) -> None:
    # Guard against a double-tapped button. Handlers run as tasks on one loop,
    # so this check-and-set is atomic between awaits — the second callback sees
    # the flag and returns instead of starting a duplicate download.
    if session.started:
        logger.info("Ignoring duplicate start for session %s", session.session_id)
        return
    session.started = True

    mode = session.mode or "video"
    quality = session.quality or "720"
    chat_id = session.chat_id

    view = ProgressView()
    dm_head = _dm_header(session, mode, quality)

    def _cancel_row():
        return _CANCEL_KB if job.cancellable and not job.cancelled else None

    try:
        await query.edit_message_text(view.render(dm_head), parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

    async def _edit_dm(text: str, **kw) -> None:
        await context.bot.edit_message_text(
            text, chat_id=chat_id, message_id=query.message.message_id, **kw)

    on_progress, ticker = _progress_driver(view, lambda: dm_head, _edit_dm, start=False,
                                           markup=_cancel_row)

    async def _cancelled() -> None:
        # Cancelled = gone: the progress message and the link that started it
        # are removed (the tap's "✖ Cancelled" toast confirms it).
        await ticker.close(None)
        for mid in (query.message.message_id, session.link_message_id):
            if mid:
                await _quiet_delete_msg(context, chat_id, mid)


    # Refuse a doomed download BEFORE spending it, not after. The analysis pass
    # already measured every quality, so when the estimate is over Telegram's
    # limit the outcome is known in advance. Until now the bot downloaded the
    # whole file first and only then said "60.1 MB exceeds the limit", costing
    # the user the entire transfer for a guaranteed failure.
    # Only acts on a confident over-estimate; a missing estimate downloads as
    # before, and the post-download check still backstops an estimate that was
    # too optimistic.
    est = (session.estimated_sizes or {}).get(quality) if mode == "video" else None
    # Refuse only when even the SMALLEST rendition at this height is over —
    # the largest one alone can be a codec yt-dlp would never pick.
    floor = (session.min_sizes or {}).get(quality) or est
    if est and floor and floor > MAX_FILE_SIZE_BYTES:
        emit(session.telemetry_id, "size_guard", outcome="precheck_rejected",
             estimated_bytes=floor, limit_bytes=MAX_FILE_SIZE_BYTES,
             recommendation="none")
        tip = oversized_video_advice(
            session.available_heights,
            session.estimated_sizes or {},
            quality,
        )
        # Keep the session alive and re-armed: the keyboard below is the way
        # out, and removing the session (or leaving `started` set) made every
        # one of its buttons a dead end.
        session.started = False
        session.quality = None
        await query.edit_message_text(
            too_big_text(floor, f"Nothing was downloaded, so you lost no time. {tip}")
            .replace("This file is", f"{QUALITY_MAP.get(quality, {}).get('label', quality)} "
                                     "for this video is at least", 1),
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(session),
        )
        return

    repeat = inline_cache.repeat_key(mode, quality, session.audio_format)
    hit = inline_cache.get(session.url, repeat) if repeat else None
    if hit:
        emit(session.telemetry_id, "cache", outcome="hit", kind=hit.get("kind", "unknown"),
             cache_hit="yes", delivery="cached_swap", avoided_download="yes")
        # Fetched before (by anyone, inline or not) at this exact quality:
        # Telegram already has the file, so this is instant.
        title = _esc((hit.get("title") or session.title or "Media")[:100])
        sent = await _send_cached(
            context, chat_id, hit,
            f"🎬 <b>{title}</b>\n⚡ Instant · All-Media Downloader",
            reply_markup=after_download_keyboard(session.url, user_id=session.user_id),
            url=session.url, key=repeat,
        )
        if sent is not None:
            record_download(session.user_id, session.url, session.title, session.platform,
                            mode, quality, True)
            emit_terminal(session.telemetry_id, "success", delivery="cached")
            sessions.remove(session.session_id)
            try:
                await query.message.delete()
            except TelegramError:
                pass
            return
    emit(session.telemetry_id, "cache", outcome="miss", cache_hit="no", delivery="fresh_download")

    inflight_add(chat_id, query.message.message_id)
    # Registered only now: every return above would otherwise leak the job.
    job = jobs.start(f"dm:{chat_id}:{query.message.message_id}", session.user_id)
    job.on_cancel = _cancelled
    ticker.start()
    try:  # ✖ Cancel straight away, not only after the first progress event
        await _edit_dm(view.render(dm_head), parse_mode=ParseMode.HTML,
                       reply_markup=_CANCEL_KB)
    except TelegramError:
        pass
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        queue_entered = time.monotonic()
        emit(session.telemetry_id, "queue", outcome="queue_enter")
        async def _queued_download():
            emit(session.telemetry_id, "queue", outcome="queue_start",
                 queue_wait_ms=int((time.monotonic() - queue_entered) * 1000))
            return await download_manager.download(
                url=session.url, mode=mode, quality=quality,
                subtitle_lang=session.subtitle_lang, audio_format=session.audio_format,
                title_hint=session.title or "media", progress_cb=on_progress,
                cancel=job.event, job_id=session.telemetry_id,
            )
        result = await jobs.run_queued(
            job, download_queue,
            _queued_download,
            on_position=lambda n: on_progress(0, f"Queued — you're #{n} in line"),
            priority=user_prefs.is_premium(session.user_id),
        )
    except asyncio.CancelledError:
        if not job.cancelled:
            emit_terminal(session.telemetry_id, "failure", **{"class": "shutdown"})
            raise
        result = None
    except Exception as e:
        jobs.drop(job)
        ticker.cancel()
        logger.exception("download crashed")
        emit_terminal(session.telemetry_id, "failure", **{"class": classify_failure(str(e))})
        record_download(
            session.user_id,
            session.url,
            session.title,
            session.platform,
            mode,
            quality,
            False,
            error=str(e),
        )
        inflight_remove(chat_id, query.message.message_id)
        sessions.remove(session.session_id)
        await context.bot.send_message(
            chat_id,
            f"❌ Download failed:\n{_esc(download_manager._friendly_error(str(e)))}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
        return

    # close, not cancel: also waits out a progress edit already in flight,
    # which could otherwise land after (and overwrite) the final text below.
    await ticker.close(None)
    if job.cancelled or not (result.success and result.primary):
        jobs.drop(job)  # nothing will be sent; otherwise kept cancellable until sending
    if job.cancelled:
        emit_terminal(session.telemetry_id, "cancelled", **{"class": "cancelled"})
        # The message is already gone — or the bot is stopping, in which case
        # it stays registered and the restart tells the user "Interrupted".
        if not jobs.SHUTTING_DOWN:
            inflight_remove(chat_id, query.message.message_id)
        sessions.remove(session.session_id)
        if result is not None:
            download_manager.cleanup_result_files(result)
        return
    if not result.success or not result.primary:
        emit_terminal(session.telemetry_id, "failure", **{"class": classify_failure(result.error or "")})
        record_download(
            session.user_id,
            session.url,
            session.title,
            session.platform,
            mode,
            quality,
            False,
            error=result.error,
        )
        # Or the next restart "rescues" this job and overwrites the real error
        # with a false "Interrupted" notice.
        inflight_remove(chat_id, query.message.message_id)
        download_manager.cleanup_result_files(result)
        sessions.remove(session.session_id)
        # Single error message (edit status; don't also send a second one)
        try:
            await query.edit_message_text(
                f"❌ <b>Download failed</b>\n\n{_esc(result.error or 'Unknown error')}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            await context.bot.send_message(
                chat_id,
                f"❌ <b>Download failed</b>\n\n{_esc(result.error or 'Unknown error')}",
                parse_mode=ParseMode.HTML,
                reply_markup=main_reply_keyboard(),
            )
        return

    path = result.primary
    size = result.file_size or path.stat().st_size

    ticker.cancel()
    view.sending(format_size(size))
    try:
        # ✖ Cancel stays while it waits for upload capacity.
        await query.edit_message_text(view.render(dm_head), parse_mode=ParseMode.HTML,
                                      reply_markup=_CANCEL_KB)
    except TelegramError:
        pass

    caption = _build_caption(session, result, size) + f"\n⏱ Ready in {view.elapsed():.0f}s"
    logger.info("dm %s ready in %.1fs (%s) [%s]: %s", mode, view.elapsed(),
                format_size(size), view.phases(), redact.url(session.url))
    # Short token only — full URLs exceed Telegram's 64-byte callback_data limit
    actions = after_download_keyboard(session.url, user_id=session.user_id)

    try:
        if size > MAX_FILE_SIZE_BYTES:
            from bot.services.downloader import recommend_fitting_quality
            rec = recommend_fitting_quality(session.available_heights,
                                            session.estimated_sizes or {}, quality,
                                            MAX_FILE_SIZE_BYTES) if mode == "video" else None
            emit(session.telemetry_id, "size_guard", outcome="rejected",
                 actual_bytes=size, limit_bytes=MAX_FILE_SIZE_BYTES,
                 recommendation=rec.quality if rec else "none",
                 recommended_estimated_bytes=rec.estimated_bytes if rec else "none")
            emit_terminal(session.telemetry_id, "failure", **{"class": "size_limit"})
            advice = (
                oversized_video_advice(
                    session.available_heights,
                    session.estimated_sizes or {},
                    quality,
                )
                if mode in ("video", "video_subs")
                else "Pick a lower quality (480p or 720p) or 🎵 Audio."
            )
            try:
                await query.edit_message_text(
                    too_big_text(size, advice),
                    parse_mode=ParseMode.HTML,
                    reply_markup=actions,
                )
            except TelegramError:
                await context.bot.send_message(
                    chat_id,
                    too_big_text(size, advice),
                    parse_mode=ParseMode.HTML,
                    reply_markup=actions,
                )
            record_download(
                session.user_id,
                session.url,
                session.title,
                session.platform,
                mode,
                quality,
                False,
                file_size=size,
                error="File too large for Telegram",
            )
        else:
            # One output only: media with caption + action buttons
            sent = await _send_media(
                context, chat_id, path, result, caption, reply_markup=actions,
                cancel=job.event, on_reserved=lambda: setattr(job, "cancellable", False),
                job_id=session.telemetry_id,
            )
            _remember_upload(session.url, repeat, sent, result.title or session.title,
                             result, mode, quality)
            if result.subtitle_file and result.subtitle_file.exists() and mode == "video_subs":
                try:
                    await context.bot.send_document(
                        chat_id,
                        document=InputFile(
                            result.subtitle_file.open("rb"),
                            filename=result.subtitle_file.name,
                        ),
                        caption="💬 Subtitle file (also embedded when possible)",
                    )
                except Exception:
                    logger.exception("subtitle send failed")

            record_download(
                session.user_id,
                session.url,
                session.title,
                session.platform,
                mode,
                quality,
                True,
                file_size=size,
            )
            emit_terminal(session.telemetry_id, "success",
                          total_ms=int(view.elapsed() * 1000))
            # Remove progress/status message so chat isn't cluttered with a 2nd "Done"
            try:
                await query.message.delete()
            except TelegramError:
                try:
                    await query.edit_message_text(
                        f"✅ Sent · {format_size(size)}",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass
    except UploadCancelled:
        emit_terminal(session.telemetry_id, "cancelled", **{"class": "cancelled"})
        pass  # cancelled while waiting to send: its messages are already gone
    except TelegramError as e:
        logger.exception("send media failed")
        emit_terminal(session.telemetry_id, "failure", **{"class": "upload_error"})
        record_download(
            session.user_id,
            session.url,
            session.title,
            session.platform,
            mode,
            quality,
            False,
            file_size=size,
            error=str(e),
        )
        failed = upload_failed_text(e)
        try:
            await query.edit_message_text(failed, parse_mode=ParseMode.HTML)
        except TelegramError:
            await context.bot.send_message(
                chat_id,
                failed,
                parse_mode=ParseMode.HTML,
                reply_markup=main_reply_keyboard(),
            )
    finally:
        jobs.drop(job)
        if not (job.cancelled and jobs.SHUTTING_DOWN):  # shutdown: restart says "Interrupted"
            inflight_remove(chat_id, query.message.message_id)
        download_manager.cleanup_result_files(result)
        sessions.remove(session.session_id)


async def _replace_channel_post(
    context, chat_id: int, source_message_id: int, path: Path, result, remember=None
) -> str:
    """
    Post the media as a new message, then remove the link post.

    Send first, delete second, deliberately: if the upload fails the link is
    still sitting in the channel, so a failed download never silently destroys
    what somebody posted. Deleting first would look marginally tidier for a
    second and risk losing the link for nothing.

    Returns "replaced" when the link was removed, "sent" when it could not be
    (the bot needs the "Delete messages" right); the media is delivered either
    way.
    """
    sent = await _send_media(context, chat_id, path, result, caption="", reply_markup=None)
    if remember is not None:
        remember(sent)
    if await _try_delete(context, chat_id, source_message_id):
        return "replaced"
    return "sent"


async def _try_delete(context, chat_id: int, message_id: int) -> bool:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except TelegramError as e:
        logger.info(
            "Could not remove the link post (%s) — grant the bot 'Delete "
            "messages' so it does not have to be removed by hand.",
            e,
        )
        return False


# Per-request timeouts for large media (seconds) — long write for multi-MB uploads
_UPLOAD_KW = {
    "read_timeout": 180,
    "write_timeout": 600,
    "connect_timeout": 30,
    "pool_timeout": 30,
}


async def _send_media(
    context,
    chat_id: int,
    path: Path,
    result,
    caption: str,
    reply_markup=None,
    attempts: int = 3,
    *,
    silent: bool = False,
    thread_id: int | None = None,
    cancel=None,
    on_reserved=None,
    job_id: str = "",
):
    """
    Upload media with long timeouts and retries on TimedOut.

    Returns the sent Message (its file_id is what inline mode re-sends).

    Waits for upload capacity first (upload_gate) and holds it across every
    attempt. `cancel` (the job's event) ends the wait with UploadCancelled;
    `on_reserved()` runs once capacity is granted, right before sending.
    """
    filename = path.name
    if path.stem == "media" and result is not None and getattr(result, "title", None):
        filename = f"{safe_filename(result.title)}{path.suffix}"
    if len(caption) > 1024:
        caption = caption[:1000] + "…"

    upload_bytes = path.stat().st_size
    wait_started = time.monotonic()
    async with upload_gate.reserve(upload_bytes, cancel):
        upload_wait_ms = int((time.monotonic() - wait_started) * 1000)
        send_started = time.monotonic()
        emit(job_id, "upload", outcome="upload_start", upload_wait_ms=upload_wait_ms,
             bytes=upload_bytes, kind=getattr(result, "mode", "media"))
        if on_reserved is not None:
            on_reserved()
        try:
            sent = await _send_media_attempts(
                context, chat_id, path, result, caption, filename, reply_markup,
                attempts, silent=silent, thread_id=thread_id,
            )
        except Exception:
            emit(job_id, "upload", outcome="failure", **{"class": "telegram_refused"},
                 upload_ms=int((time.monotonic() - send_started) * 1000),
                 upload_wait_ms=upload_wait_ms, bytes=upload_bytes,
                 kind=getattr(result, "mode", "media"))
            raise
        emit(job_id, "upload", outcome="success",
             upload_ms=int((time.monotonic() - send_started) * 1000),
             upload_wait_ms=upload_wait_ms, bytes=upload_bytes,
             kind=getattr(result, "mode", "media"))
        return sent


async def _send_media_attempts(context, chat_id, path, result, caption, filename,
                               reply_markup, attempts, *, silent, thread_id):
    """The retry loop, unchanged — run while holding the upload reservation."""
    # Errors are kept WITHOUT tracebacks (see _without_frames): their frames
    # reference the failed attempt's request through the backoff and the retry.
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _send_media_once(
                context, chat_id, path, result, caption, filename, reply_markup,
                silent=silent, thread_id=thread_id,
            )
        except RetryAfter as e:
            last_err = _without_frames(e)
            wait = int(getattr(e, "retry_after", 5)) + 1
            logger.warning("Flood control, waiting %ss (attempt %s)", wait, attempt)
            await _sleep(wait)
        except TimedOut as e:
            last_err = _without_frames(e)
            logger.warning(
                "Upload timed out (attempt %s/%s, size=%s)",
                attempt,
                attempts,
                path.stat().st_size if path.exists() else "?",
            )
            if attempt < attempts:
                await _sleep(2 * attempt)
                # Retry the SAME media send — switching upload type mid-retry
                # would bypass the caller's per-type semantics (streaming,
                # caption handling) and mask the real timeout.
        except NetworkError as e:
            last_err = _without_frames(e)
            logger.warning("Network error on upload (attempt %s): %s", attempt, e)
            if attempt < attempts:
                await _sleep(2 * attempt)
        except TelegramError:
            raise

    if last_err:
        raise last_err


def _without_frames(err: BaseException) -> BaseException:
    """
    `err` with its traceback cleared — and those of the errors it was raised
    from: PTB raises TimedOut *from* httpx's error, and that one's traceback
    alone kept the failed attempt's request and InputFile alive into the retry
    (e.with_traceback(None) was not enough; tests/test_upload_streaming.py).
    """
    todo, seen = [err], set()
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        e.__traceback__ = None
        todo += [e.__cause__, e.__context__]
    return err


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _is_media_format_rejection(error: BadRequest, kind: str) -> bool:
    # Keep this narrow: caption, permission, file-size and chat errors must
    # not silently turn a rejected media send into a document send.
    allowed = {
        "photo": {"wrong file type", "photo_invalid_dimensions",
                  "photo_ext_invalid", "image_process_failed"},
        "video": {"wrong file type", "video_content_type_invalid",
                  "video_file_invalid"},
    }
    return str(error).strip().lower() in allowed[kind]


async def _send_media_once(
    context,
    chat_id: int,
    path: Path,
    result,
    caption: str,
    filename: str,
    reply_markup=None,
    *,
    silent: bool = False,
    thread_id: int | None = None,
):
    kw = dict(_UPLOAD_KW)
    if silent:
        kw["disable_notification"] = True
    if thread_id:
        kw["message_thread_id"] = thread_id
    # Empty caption → omit (clean channel posts)
    cap = (caption or "").strip()
    cap_kw: dict = {}
    if cap:
        cap_kw["caption"] = cap
        cap_kw["parse_mode"] = ParseMode.HTML
    if reply_markup is not None:
        cap_kw["reply_markup"] = reply_markup

    # read_file_handle=False: httpx streams the open file in 64 KB chunks. The
    # default read the whole file into memory, and TLS added two more copies —
    # ~3x the file per upload (5 x 49 MB measured at 584 MB). Each handle stays
    # open (the `with` below) until the request is done, as PTB requires.
    if result.is_audio:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        with path.open("rb") as f:
            return await context.bot.send_audio(
                chat_id,
                audio=InputFile(f, filename=filename, read_file_handle=False),
                title=result.title[:64] if result.title else None,
                performer=(result.artist or "")[:64] or None,
                thumbnail=_cover_file(result),
                **cap_kw,
                **kw,
            )

    if result.is_image:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        with path.open("rb") as f:
            try:
                return await context.bot.send_photo(
                    chat_id,
                    photo=InputFile(f, filename=filename, read_file_handle=False),
                    **cap_kw,
                    **kw,
                )
            except BadRequest as e:
                # BadRequest subclasses NetworkError: classify the media
                # rejection before the transport-error branch below.
                if not _is_media_format_rejection(e, "photo"):
                    raise
                logger.info("send_photo failed (%s), falling back to document", e)
            except (RetryAfter, NetworkError):
                # Flood control and transport failures (TimedOut included) go
                # back to the retry loop. Re-sending as a document right away
                # hit the same flood limit, or posted the image twice when the
                # photo had in fact arrived before the timeout.
                raise
            except TelegramError as e:
                logger.info("send_photo failed (%s), falling back to document", e)
            f.seek(0)
            return await context.bot.send_document(
                chat_id,
                document=InputFile(f, filename=filename, read_file_handle=False),
                **cap_kw,
                **kw,
            )

    if result.is_video:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        with path.open("rb") as f:
            try:
                return await context.bot.send_video(
                    chat_id,
                    video=InputFile(f, filename=filename, read_file_handle=False),
                    supports_streaming=True,
                    **cap_kw,
                    **kw,
                )
            except BadRequest as e:
                if not _is_media_format_rejection(e, "video"):
                    raise
                logger.info("send_video failed (%s), falling back to document", e)
            except RetryAfter:
                # Flood control must be waited out by the retry loop —
                # falling back to document here would hit the same limit again.
                raise
            except NetworkError:  # TimedOut included: the retry loop owns it
                raise
            except TelegramError as e:
                logger.info("send_video failed (%s), falling back to document", e)
            f.seek(0)
            return await context.bot.send_document(
                chat_id,
                document=InputFile(f, filename=filename, read_file_handle=False),
                **cap_kw,
                **kw,
            )

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    with path.open("rb") as f:
        return await context.bot.send_document(
            chat_id,
            document=InputFile(f, filename=filename, read_file_handle=False),
            **cap_kw,
            **kw,
        )


def _build_caption(session: DownloadSession, result, size: int) -> str:
    mode = result.mode or session.mode or ""
    parts = [
        f"🎬 <b>{_esc((result.title or session.title or 'Media')[:100])}</b>",
        f"📡 {_esc(session.platform or '')} · {_mode_label(mode)}",
    ]
    if result.quality and mode in ("video", "video_subs"):
        parts.append(f"📐 {_quality_label(result)}")
    parts.append(f"💾 {format_size(size)}")
    parts.append("⚡ via All-Media Downloader")
    return "\n".join(parts)


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
