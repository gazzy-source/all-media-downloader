"""URL intake, interactive choice callbacks, and file delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from telegram import InputFile, Update
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
    image_size_keyboard,
    main_reply_keyboard,
    mode_keyboard,
    quality_keyboard,
    subtitle_lang_keyboard,
)
from bot.services import inline_cache, user_prefs
from bot.services.dl_queue import download_queue
from bot.services.downloader import PRIVATE_URL_ERROR, download_manager
from bot.services.history import record_download
from bot.services.inflight import add as inflight_add, remove as inflight_remove
from bot.services.media_detect import detect_mode
from bot.services.rate_limit import rate_limiter
from bot.services.session import DownloadSession, sessions
from bot.utils.safe_fetch import UnresolvableURLError, UnsafeURLError, check_public_url
from bot.utils.helpers import (
    analysing_percent,
    extract_urls,
    format_size,
    platform_from_url,
    progress_bar,
    short_id,
)

logger = logging.getLogger(__name__)


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

    allowed, retry = rate_limiter.allow(actor)
    if not allowed and actor not in ADMIN_IDS:
        try:
            await msg.reply_text(
                f"⏳ Rate limit — try again in {retry}s.",
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
        is_chan = chat.type == "channel"
        title = _esc((hit.get("title") or "Media")[:100])
        sent = await _send_cached(
            context, chat.id, hit,
            "" if is_chan else f"🎬 <b>{title}</b>\n⚡ Instant · All-Media Downloader · Gazzy Labs",
            reply_markup=None if is_chan else after_download_keyboard(url, user_id=actor),
            url=url, key=repeat,
        )
        if sent is not None:
            if (is_chan and CHANNEL_REPLACE_LINK and replace_source
                    and _is_link_only_post(msg)):
                await _try_delete(context, chat.id, msg.message_id)
            record_download(actor, url, hit.get("title") or "", platform, mode, quality, True)
            return True

    status = await msg.reply_text(
        f"⚡ <b>Downloading</b> · {kind}",
        parse_mode=ParseMode.HTML,
    )

    last_edit = {"t": 0.0}

    async def on_progress(pct: float, text: str) -> None:
        now = time.time()
        is_status = pct < 8 or any(
            k in text for k in ("Queued", "Starting", "Resolving", "Fetching")
        )
        # Light UI updates — avoid Telegram flood while still feeling live
        if not is_status and now - last_edit["t"] < 2.5 and pct < 95:
            return
        last_edit["t"] = now
        try:
            label = "Queued" if "Queued" in text else "Downloading"
            await status.edit_text(
                f"⚡ <b>{label}</b> · {kind}\n"
                f"{progress_bar(pct)}\n"
                f"<code>{_esc(text)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    inflight_add(chat.id, status.message_id)
    try:
        await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_DOCUMENT)
        result = await download_queue.run(
            lambda: download_manager.download(
                url=url,
                mode=mode,
                quality=quality,
                title_hint="media",
                progress_cb=on_progress,
            ),
            on_position=lambda n: on_progress(0, f"Queued — you're #{n} in line"),
            priority=user_prefs.is_premium(actor),
        )
    except Exception as e:
        logger.exception("auto download crashed")
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

    if not result.success or not result.primary:
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
    try:
        await status.edit_text(
            f"📤 Uploading… ({format_size(size)})",
            parse_mode=ParseMode.HTML,
        )
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
            f"{kind} · 💾 {format_size(size)}\n"
            f"⚡ All-Media Downloader · Gazzy Labs"
        )
        actions = after_download_keyboard(url, user_id=actor)

    try:
        if size > MAX_FILE_SIZE_BYTES:
            if is_channel:
                try:
                    await status.delete()
                except TelegramError:
                    pass
            else:
                await status.edit_text(
                    f"⚠️ File is <b>{format_size(size)}</b> (limit "
                    f"~{format_size(MAX_FILE_SIZE_BYTES)}). Try a shorter video.",
                    parse_mode=ParseMode.HTML,
                )
            record_download(
                actor, url, result.title or "", platform, mode, quality, False,
                file_size=size, error="File too large",
            )
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
                context, chat.id, path, result, caption, reply_markup=actions
            )
            _remember_upload(url, repeat, sent, result.title or "", result, mode, quality)
        record_download(
            actor, url, result.title or "", platform, mode, quality, True,
            file_size=size,
        )
        delivered = True
        try:
            await status.delete()
        except TelegramError:
            if not is_channel:
                try:
                    await status.edit_text(f"✅ Sent · {format_size(size)}")
                except TelegramError:
                    pass
    except TelegramError as e:
        logger.exception("auto upload failed")
        record_download(
            actor, url, result.title or "", platform, mode, quality, False,
            file_size=size, error=str(e),
        )
        try:
            await status.edit_text(
                f"❌ Upload failed:\n<code>{_esc(str(e)[:200])}</code>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    finally:
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
            f"⏳ Rate limit reached. Try again in <b>{retry}</b> seconds.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
        return
    if await _refuse_private_url(msg, url):
        return

    status = await msg.reply_text(
        "🔍 <b>Analyzing…</b>\n<code>Getting title, formats &amp; options</code>",
        parse_mode=ParseMode.HTML,
    )
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
                        f"🔍 <b>Analyzing…</b>\n"
                        f"{progress_bar(analysing_percent(elapsed))}\n"
                        f"<code>Reading formats · {elapsed}s</code>",
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
            download_manager.extract_info(url), timeout=EXTRACT_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning("extract_info timed out after %ss: %s", EXTRACT_TIMEOUT, url[:80])
        body = (
            "⏱ <b>Took too long to read this link</b>\n\n"
            f"Gave up after {EXTRACT_TIMEOUT}s — the platform is slow or "
            "blocking the server right now.\n\n"
            "Try again, or send a different link."
        )
        try:
            await status.edit_text(body, parse_mode=ParseMode.HTML)
        except TelegramError:
            await msg.reply_text(
                body, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard()
            )
        return
    except Exception as e:
        logger.exception("extract_info failed")
        from bot.services.downloader import DownloadManager

        friendly = DownloadManager._friendly_error(str(e))
        body = (
            f"❌ <b>Could not read this link</b>\n\n{_esc(friendly)}\n\n"
            "Tips: the post must be public, the link must not be a story/private "
            "account, and some platforms block datacenter IPs."
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
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    if query.data.startswith(("pref:", "prem:")):
        from bot.handlers import premium, start

        handler = start.handle_pref_callback if query.data.startswith("pref:") \
            else premium.handle_premium_callback
        await handler(update, context)
        return
    if query.data == "inl:wait":
        # The inline placeholder's own button: nothing to do but reassure.
        try:
            await query.answer("Still downloading — it will appear right here.")
        except TelegramError:
            pass
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
        await query.message.reply_text(
            f"🔄 Re-analyzing…\n<code>{_esc(url[:100])}</code>",
            parse_mode=ParseMode.HTML,
        )
        await start_url_flow(update, context, url)
        return

    parts = data.split(":")
    action = parts[0]
    if len(parts) < 2:
        return
    sid = parts[1]
    session = sessions.get(sid)
    if not session:
        # Sessions live in memory, so they also vanish on a bot restart — not
        # only on timeout. Say what to do rather than just what went wrong.
        await query.edit_message_text(
            "⌛ <b>This session is no longer active</b>\n\n"
            "Wizard sessions expire after a while, and reset when the bot "
            "restarts.\n\nJust send the link again — it only takes a moment.",
            parse_mode=ParseMode.HTML,
        )
        return
    if session.user_id != user_id and user_id not in ADMIN_IDS:
        if query.message:
            await query.message.reply_text(
                "⛔ This isn't your download session."
            )
        return

    if action == "cancel":
        sessions.remove(sid)
        await query.edit_message_text("❌ Download cancelled.")
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
            await query.edit_message_text(
                _session_header(session)
                + "\n\n🖼 <b>Image resolution:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=image_size_keyboard(session),
            )
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
        inline_cache.put(url, key, file_id=found[1], kind=found[0], title=title or "")


_DELIVERED_UNCONFIRMED = object()


async def _send_cached(context, chat_id: int, hit: dict, caption: str, reply_markup=None,
                       *, url: str = "", key: str | None = None):
    """
    Re-send a file Telegram already has. Returns the Message (or, after a
    timeout, a marker: it probably arrived — never send it twice), or None to
    download normally. Only a refused FILE forgets the cache entry; a chat that
    can't take videos, or a network blip, must not wipe it for everyone.
    """
    field = hit.get("kind") if hit.get("kind") in ("video", "audio", "animation", "photo") else "document"
    send = getattr(context.bot, f"send_{field}")
    kw: dict = {field: hit["file_id"], "reply_markup": reply_markup}
    if caption:
        kw.update(caption=caption[:1024], parse_mode=ParseMode.HTML)
    try:
        return await send(chat_id, **kw)
    except TimedOut:
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
        logger.warning("Refused non-public URL %s", url[:80])
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

    summary = (
        f"{_session_header(session)}\n\n"
        f"⚙️ <b>Starting download…</b>\n"
        f"Mode: <b>{_mode_label(mode)}</b>\n"
    )
    if mode in ("video", "video_subs"):
        summary += f"Quality: <b>{QUALITY_MAP.get(quality, {}).get('label', quality)}</b>\n"
    if mode == "audio":
        summary += f"Format: <b>{session.audio_format.upper()}</b>\n"
    if mode == "video_subs":
        summary += f"Subtitles: <b>{_esc(session.subtitle_lang or 'auto')}</b>\n"
    summary += (
        f"\n{progress_bar(0)}\n"
        f"<code>Starting… ({download_manager.active}/{download_manager.max_concurrent} parallel)</code>"
    )

    try:
        await query.edit_message_text(summary, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

    last_edit = {"t": 0.0}

    async def on_progress(pct: float, msg: str) -> None:
        now = time.time()
        # Always show queue / start messages; throttle mid-download %
        is_status = pct < 5 or "Queued" in msg or "Starting" in msg or "Connecting" in msg
        if not is_status and now - last_edit["t"] < 2.0 and pct < 99:
            return
        last_edit["t"] = now
        text = (
            f"{_session_header(session)}\n\n"
            f"{progress_bar(pct)}\n"
            f"<code>{_esc(msg)}</code>"
        )
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=query.message.message_id,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

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
        fits = [
            (q, sz) for q, sz in (session.estimated_sizes or {}).items()
            if sz and sz <= MAX_FILE_SIZE_BYTES
        ]
        best = max(fits, key=lambda qs: qs[1])[0] if fits else None
        tip = (
            f"Pick <b>{QUALITY_MAP[best]['label']}</b> — it fits."
            if best and best in QUALITY_MAP
            else "Try 🎵 Audio instead."
        )
        # Keep the session alive and re-armed: the keyboard below is the way
        # out, and removing the session (or leaving `started` set) made every
        # one of its buttons a dead end.
        session.started = False
        session.quality = None
        await query.edit_message_text(
            f"⚠️ <b>{QUALITY_MAP.get(quality, {}).get('label', quality)}</b> for this "
            f"video is at least <b>{format_size(floor)}</b>, over Telegram's "
            f"{format_size(MAX_FILE_SIZE_BYTES)} limit for bots.\n\n"
            f"Nothing was downloaded, so you lost no time. {tip}",
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(session),
        )
        return

    repeat = inline_cache.repeat_key(mode, quality, session.audio_format)
    hit = inline_cache.get(session.url, repeat) if repeat else None
    if hit:
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
            sessions.remove(session.session_id)
            try:
                await query.message.delete()
            except TelegramError:
                pass
            return

    inflight_add(chat_id, query.message.message_id)
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        result = await download_queue.run(
            lambda: download_manager.download(
                url=session.url,
                mode=mode,
                quality=quality,
                subtitle_lang=session.subtitle_lang,
                audio_format=session.audio_format,
                title_hint=session.title or "media",
                progress_cb=on_progress,
            ),
            on_position=lambda n: on_progress(
                0, f"Queued — you're #{n} in line"
                + (" (premium: ahead of free users)" if user_prefs.is_premium(session.user_id) else "")
            ),
            priority=user_prefs.is_premium(session.user_id),
        )
    except Exception as e:
        logger.exception("download crashed")
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

    if not result.success or not result.primary:
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

    try:
        await query.edit_message_text(
            f"{_session_header(session)}\n\n"
            f"{progress_bar(100)}\n"
            f"📤 Uploading to Telegram… ({format_size(size)})",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

    caption = _build_caption(session, result, size)
    # Short token only — full URLs exceed Telegram's 64-byte callback_data limit
    actions = after_download_keyboard(session.url, user_id=session.user_id)

    try:
        if size > MAX_FILE_SIZE_BYTES:
            try:
                await query.edit_message_text(
                    f"⚠️ File is <b>{format_size(size)}</b>, which exceeds the "
                    f"Telegram bot upload limit (~{format_size(MAX_FILE_SIZE_BYTES)}).\n\n"
                    f"Tips: pick a lower quality (480p/720p) or audio-only.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=actions,
                )
            except TelegramError:
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ File is <b>{format_size(size)}</b>, which exceeds the "
                    f"Telegram bot upload limit (~{format_size(MAX_FILE_SIZE_BYTES)}).\n\n"
                    f"Tips: pick a lower quality (480p/720p) or audio-only.",
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
                context, chat_id, path, result, caption, reply_markup=actions
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
    except TelegramError as e:
        logger.exception("send media failed")
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
        hint = ""
        if "timed out" in str(e).lower():
            hint = (
                "\n\n💡 <i>Upload timed out — often slow network or large file. "
                "Try 480p/720p, or send again.</i>"
            )
        try:
            await query.edit_message_text(
                f"❌ Upload failed:\n<code>{_esc(str(e)[:200])}</code>{hint}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            await context.bot.send_message(
                chat_id,
                f"❌ Upload failed:\n<code>{_esc(str(e)[:200])}</code>{hint}",
                parse_mode=ParseMode.HTML,
                reply_markup=main_reply_keyboard(),
            )
    finally:
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
):
    """
    Upload media with long timeouts and retries on TimedOut.

    Returns the sent Message (its file_id is what inline mode re-sends).
    """
    filename = path.name
    if len(caption) > 1024:
        caption = caption[:1000] + "…"

    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _send_media_once(
                context, chat_id, path, result, caption, filename, reply_markup,
                silent=silent,
            )
        except RetryAfter as e:
            last_err = e
            wait = int(getattr(e, "retry_after", 5)) + 1
            logger.warning("Flood control, waiting %ss (attempt %s)", wait, attempt)
            await _sleep(wait)
        except TimedOut as e:
            last_err = e
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
            last_err = e
            logger.warning("Network error on upload (attempt %s): %s", attempt, e)
            if attempt < attempts:
                await _sleep(2 * attempt)
        except TelegramError:
            raise

    if last_err:
        raise last_err


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


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
):
    kw = dict(_UPLOAD_KW)
    if silent:
        kw["disable_notification"] = True
    # Empty caption → omit (clean channel posts)
    cap = (caption or "").strip()
    cap_kw: dict = {}
    if cap:
        cap_kw["caption"] = cap
        cap_kw["parse_mode"] = ParseMode.HTML
    if reply_markup is not None:
        cap_kw["reply_markup"] = reply_markup

    if result.is_audio:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        with path.open("rb") as f:
            return await context.bot.send_audio(
                chat_id,
                audio=InputFile(f, filename=filename),
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
                    photo=InputFile(f, filename=filename),
                    **cap_kw,
                    **kw,
                )
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
                    document=InputFile(f, filename=filename),
                    **cap_kw,
                    **kw,
                )

    if result.is_video:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        with path.open("rb") as f:
            try:
                return await context.bot.send_video(
                    chat_id,
                    video=InputFile(f, filename=filename),
                    supports_streaming=True,
                    **cap_kw,
                    **kw,
                )
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
                    document=InputFile(f, filename=filename),
                    **cap_kw,
                    **kw,
                )

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    with path.open("rb") as f:
        return await context.bot.send_document(
            chat_id,
            document=InputFile(f, filename=filename),
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
    parts.append("⚡ via All-Media Downloader Bot · Gazzy Labs")
    return "\n".join(parts)


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
