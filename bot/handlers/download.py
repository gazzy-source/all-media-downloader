"""URL intake, interactive choice callbacks, and file delivery."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from telegram import InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ContextTypes

from bot.config import ADMIN_IDS, MAX_FILE_SIZE_BYTES, QUALITY_MAP
from bot.keyboards.menus import (
    after_download_keyboard,
    audio_format_keyboard,
    confirm_keyboard,
    image_size_keyboard,
    main_reply_keyboard,
    mode_keyboard,
    quality_keyboard,
    subtitle_lang_keyboard,
)
from bot.services.downloader import download_manager
from bot.services.history import record_download
from bot.services.rate_limit import rate_limiter
from bot.services.session import DownloadSession, sessions
from bot.utils.helpers import (
    extract_urls,
    format_size,
    progress_bar,
    short_id,
)

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return

    # Reply keyboard menus
    from bot.handlers.start import text_menu_router

    if await text_menu_router(update, context):
        return

    text = update.effective_message.text or update.effective_message.caption or ""
    urls = extract_urls(text)
    if not urls:
        await update.effective_message.reply_text(
            "🔗 Please send a valid media URL.\n\n"
            "Example: a YouTube, Instagram, TikTok, X, Facebook, or Pinterest link.",
            reply_markup=main_reply_keyboard(),
        )
        return

    # Process first URL fully; queue note for extras
    if len(urls) > 1:
        await update.effective_message.reply_text(
            f"📎 Found <b>{len(urls)}</b> links. Starting with the first one.\n"
            f"Send the others again after this download finishes.",
            parse_mode=ParseMode.HTML,
        )

    await start_url_flow(update, context, urls[0])


async def start_url_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    allowed, retry = rate_limiter.allow(user.id)
    if not allowed and user.id not in ADMIN_IDS:
        await msg.reply_text(
            f"⏳ Rate limit reached. Try again in <b>{retry}</b> seconds.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
        return

    status = await msg.reply_text(
        "🔍 Analyzing link…\n<code>Fetching metadata &amp; available formats</code>",
        parse_mode=ParseMode.HTML,
    )

    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
        info = await download_manager.extract_info(url)
    except Exception as e:
        logger.exception("extract_info failed")
        err = str(e)[:250]
        await status.edit_text(
            f"❌ <b>Could not read this link</b>\n\n<code>{_esc(err)}</code>\n\n"
            "Tips: ensure the post is public, try another URL, or update yt-dlp.",
            parse_mode=ParseMode.HTML,
        )
        return

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
        extractor=info.extractor,
        raw_info={},  # keep memory light
        status_message_id=status.message_id,
        prompt_message_id=status.message_id,
    )
    sessions.put(session)

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


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "new":
        await query.message.reply_text(  # type: ignore[union-attr]
            "📥 Paste a media link to download.",
            reply_markup=main_reply_keyboard(),
        )
        return

    if data.startswith("again:"):
        url = data[6:]
        # Fabricate a mini update flow
        class _Fake:
            pass

        # Re-use message
        await query.message.reply_text(  # type: ignore[union-attr]
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
        await query.edit_message_text(
            "⌛ This session expired. Please send the link again.",
        )
        return
    if session.user_id != user_id and user_id not in ADMIN_IDS:
        await query.answer("This isn't your download session.", show_alert=True)
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

    if action == "mode":
        mode = parts[2] if len(parts) > 2 else "video"
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
        session.audio_format = fmt
        session.mode = "audio"
        await query.edit_message_text(
            _session_header(session)
            + f"\n\n🎵 Audio · <b>{fmt.upper()}</b>\n\n"
            f"Ready when you are:",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_keyboard(session),
        )
        return

    if action == "imgsize":
        idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        session.image_index = idx
        session.mode = "image"
        size_label = "Best available"
        if session.available_image_sizes and idx < len(session.available_image_sizes):
            w, h = session.available_image_sizes[idx]
            size_label = f"{w}×{h}"
        await query.edit_message_text(
            _session_header(session)
            + f"\n\n🖼 Image · <b>{size_label}</b>\n\n"
            f"Ready when you are:",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_keyboard(session),
        )
        return

    if action == "quality":
        q = parts[2] if len(parts) > 2 else "720"
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
                session.subtitle_lang = "en.*"
                await query.edit_message_text(
                    _session_header(session)
                    + f"\n\n🎞 Quality: <b>{QUALITY_MAP.get(q, {}).get('label', q)}</b>\n"
                    f"💬 Subtitles: <i>auto (if available)</i>\n\n"
                    f"Ready when you are:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=confirm_keyboard(session),
                )
            return
        # plain video
        await query.edit_message_text(
            _session_header(session)
            + f"\n\n🎥 Quality: <b>{QUALITY_MAP.get(q, {}).get('label', q)}</b>\n\n"
            f"Ready when you are:",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_keyboard(session),
        )
        return

    if action == "sublang":
        lang = parts[2] if len(parts) > 2 else "en"
        session.subtitle_lang = lang
        qlabel = QUALITY_MAP.get(session.quality or "720", {}).get("label", session.quality)
        await query.edit_message_text(
            _session_header(session)
            + f"\n\n🎞 Quality: <b>{qlabel}</b>\n"
            f"💬 Subtitles: <b>{_esc(lang)}</b>\n\n"
            f"Ready when you are:",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_keyboard(session),
        )
        return

    if action == "go":
        await execute_download(query, context, session)
        return


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
        f"📡 {session.platform}"
    )


async def execute_download(query, context: ContextTypes.DEFAULT_TYPE, session: DownloadSession) -> None:
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
    summary += f"\n{progress_bar(0)}\n<code>Queued…</code>"

    try:
        await query.edit_message_text(summary, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

    last_edit = {"t": 0.0}

    async def on_progress(pct: float, msg: str) -> None:
        now = time.time()
        if now - last_edit["t"] < 2.0 and pct < 99:
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

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        result = await download_manager.download(
            url=session.url,
            mode=mode,
            quality=quality,
            subtitle_lang=session.subtitle_lang,
            audio_format=session.audio_format,
            title_hint=session.title or "media",
            progress_cb=on_progress,
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
        sessions.remove(session.session_id)
        await context.bot.send_message(
            chat_id,
            f"❌ Download failed:\n<code>{_esc(str(e)[:300])}</code>",
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
    actions = after_download_keyboard(session.url)

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
            await _send_media(
                context, chat_id, path, result, caption, reply_markup=actions
            )
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
        download_manager.cleanup_result_files(result)
        sessions.remove(session.session_id)


# Per-request timeouts for large media (seconds)
_UPLOAD_KW = {
    "read_timeout": 300,
    "write_timeout": 300,
    "connect_timeout": 60,
    "pool_timeout": 60,
}


async def _send_media(
    context,
    chat_id: int,
    path: Path,
    result,
    caption: str,
    reply_markup=None,
    attempts: int = 3,
) -> None:
    """Upload media with long timeouts and retries on TimedOut."""
    from telegram.error import TimedOut, NetworkError, RetryAfter

    filename = path.name
    if len(caption) > 1024:
        caption = caption[:1000] + "…"

    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await _send_media_once(
                context, chat_id, path, result, caption, filename, reply_markup
            )
            return
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
                # Next attempt: force document (often more reliable than send_video)
                result.is_video = False
                result.is_audio = result.is_audio  # keep audio as-is
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
    import asyncio

    await asyncio.sleep(seconds)


async def _send_media_once(
    context,
    chat_id: int,
    path: Path,
    result,
    caption: str,
    filename: str,
    reply_markup=None,
) -> None:
    kw = dict(_UPLOAD_KW)

    if result.is_audio:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        with path.open("rb") as f:
            await context.bot.send_audio(
                chat_id,
                audio=InputFile(f, filename=filename),
                caption=caption,
                parse_mode=ParseMode.HTML,
                title=result.title[:64] if result.title else None,
                performer="All-Media Downloader Bot",
                reply_markup=reply_markup,
                **kw,
            )
        return

    if result.is_image:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        with path.open("rb") as f:
            try:
                await context.bot.send_photo(
                    chat_id,
                    photo=InputFile(f, filename=filename),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    **kw,
                )
            except TelegramError:
                f.seek(0)
                await context.bot.send_document(
                    chat_id,
                    document=InputFile(f, filename=filename),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    **kw,
                )
        return

    if result.is_video:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        with path.open("rb") as f:
            try:
                await context.bot.send_video(
                    chat_id,
                    video=InputFile(f, filename=filename),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    reply_markup=reply_markup,
                    **kw,
                )
                return
            except TimedOut:
                raise
            except TelegramError as e:
                logger.info("send_video failed (%s), falling back to document", e)
                f.seek(0)
                await context.bot.send_document(
                    chat_id,
                    document=InputFile(f, filename=filename),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    **kw,
                )
                return

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    with path.open("rb") as f:
        await context.bot.send_document(
            chat_id,
            document=InputFile(f, filename=filename),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            **kw,
        )


def _build_caption(session: DownloadSession, result, size: int) -> str:
    mode = result.mode or session.mode or ""
    parts = [
        f"🎬 <b>{_esc((result.title or session.title or 'Media')[:100])}</b>",
        f"📡 {session.platform} · {_mode_label(mode)}",
    ]
    if result.quality and mode in ("video", "video_subs"):
        parts.append(
            f"📐 {QUALITY_MAP.get(result.quality, {}).get('label', result.quality)}"
        )
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
