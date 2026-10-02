"""Start, help, platforms, stats, settings handlers."""

from __future__ import annotations

import asyncio
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import __bot_bio__, __bot_name__
from bot.config import (
    ADMIN_IDS,
    PREMIUM_ENABLED,
    PREMIUM_RATE_MULT,
    RATE_LIMIT_PER_HOUR,
    SUPPORTED_PLATFORMS,
)
from bot.keyboards.menus import main_reply_keyboard, settings_keyboard
from bot.services import user_prefs
from bot.services.history import get_stats, get_user_history
from bot.services.rate_limit import rate_limiter
from bot.utils.helpers import format_size


def welcome_text(bot_username: str) -> str:
    me = f"@{bot_username}" if bot_username else "@bot"
    return f"""
🚀 <b>{__bot_name__}</b>

{__bot_bio__}

<b>📥 Download</b>
• <b>Here:</b> paste a link → pick Video / Audio and quality
• <b>Any chat:</b> type <code>{me} &lt;link&gt;</code> → pick 🎬 or 🎵
• <b>Groups / channels:</b> links download automatically

<b>🔎 Search inline</b>
• <code>{me} kya hua tera wada</code> → videos
• <code>{me} audio arijit singh</code> → songs, with cover art

<b>⚡ Make it yours</b>
• /settings — save a default (e.g. 🎵 M4A) and skip the menu
• /premium — {RATE_LIMIT_PER_HOUR * PREMIUM_RATE_MULT} downloads/hour + priority queue ⭐

YouTube · Instagram · TikTok · X · Facebook · Pinterest &amp; 1000+ sites.
Just paste a link 👇
""".strip()


def help_text(bot_username: str) -> str:
    me = f"@{bot_username}" if bot_username else "@bot"
    return f"""
❓ <b>Help — {__bot_name__}</b>

<b>Commands</b>
/settings — default type, quality &amp; audio format
/premium — more downloads per hour + priority
/history — your recent downloads
/platforms — supported sites
/cancel — cancel what you started
/paysupport — help with a payment

<b>Inline (works in any chat)</b>
• <code>{me} &lt;link&gt;</code> — pick 🎬 Video or 🎵 Audio
• <code>{me} song name</code> — search YouTube
• <code>{me} audio song name</code> — search songs
The card shows the name and live status, then turns into the file.

<b>Progress</b>
Every download shows its steps — Finding source → Downloading (% and
time left) → Finishing → Sending — and the file says how long it took.
Busy? You'll see your place in the queue.

<b>Formats &amp; quality</b>
🎥 Video 480p · 720p · 1080p · Max
🎵 Audio M4A (original, fastest) · MP3 · Opus — with title, artist &amp; cover
🎞 Video + subtitles · 🖼 Images

<b>Limits</b>
• Telegram bots can send files up to ~50 MB — pick a lower quality for long videos
• {RATE_LIMIT_PER_HOUR} downloads/hour ({RATE_LIMIT_PER_HOUR * PREMIUM_RATE_MULT} with /premium)
• Search shows videos up to 1 hour long

Built with ❤️ by <b>Gazzy Labs</b>
""".strip()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    # t.me/<bot>?start=dl_<token>: inline mode's "Open bot" for a file too big
    # to send inline — continue with the normal quality wizard for that link.
    args = getattr(context, "args", None) or []
    if args and args[0].startswith("dl_") and update.effective_user:
        from bot.handlers.download import start_url_flow
        from bot.services.url_tokens import get_url

        url = get_url(args[0][3:], update.effective_user.id)
        if url:
            await start_url_flow(update, context, url)
            return
    await update.effective_message.reply_text(
        welcome_text(getattr(context.bot, "username", "") or ""),
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        help_text(getattr(context.bot, "username", "") or ""),
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    lines = [f"🌐 <b>Supported platforms</b> — {__bot_name__}\n"]
    lines.append("Powered by <b>yt-dlp</b> (1000+ extractors). Highlights:\n")
    for name, domains in SUPPORTED_PLATFORMS:
        lines.append(f"• <b>{name}</b> — <code>{domains}</code>")
    lines.append(
        "\n💡 <i>Most public video/audio/image pages work. "
        "Paste any link and the bot will detect what's available.</i>"
    )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    # File I/O: off the event loop, or one big history.json stalls everyone.
    items = await asyncio.to_thread(get_user_history, update.effective_user.id, 10)
    if not items:
        await update.effective_message.reply_text(
            "🕘 No downloads yet. Send a media link to get started!",
            reply_markup=main_reply_keyboard(),
        )
        return
    lines = ["🕘 <b>Your recent downloads</b>\n"]
    for i, it in enumerate(items, 1):
        status = "✅" if it.get("success") else "❌"
        title = (it.get("title") or "Untitled")[:60]
        platform = it.get("platform") or "?"
        mode = it.get("mode") or "?"
        q = it.get("quality") or "—"
        lines.append(
            f"{i}. {status} <b>{_esc(title)}</b>\n"
            f"    {_esc(platform)} · {_esc(mode)} · {_esc(q)}"
        )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    remaining = rate_limiter.remaining(update.effective_user.id)
    if update.effective_user.id not in ADMIN_IDS:
        # Server-wide numbers (user count, traffic, platforms) are the
        # operator's business; everyone else gets their own quota.
        await update.effective_message.reply_text(
            f"⏱ Your remaining quota this hour: <b>{remaining}</b>/{RATE_LIMIT_PER_HOUR}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
        return
    stats = await asyncio.to_thread(get_stats)
    total = stats.get("total_downloads", 0)
    ok = stats.get("successful", 0)
    fail = stats.get("failed", 0)
    users = stats.get("unique_user_count", 0)
    bytes_served = stats.get("bytes_served", 0)
    by_platform = stats.get("by_platform") or {}
    top = sorted(by_platform.items(), key=lambda x: x[1], reverse=True)[:5]
    top_s = ", ".join(f"{_esc(str(k))} ({v})" for k, v in top) if top else "—"

    text = (
        f"📊 <b>Bot statistics</b>\n\n"
        f"📦 Total downloads: <b>{total}</b>\n"
        f"✅ Successful: <b>{ok}</b>\n"
        f"❌ Failed: <b>{fail}</b>\n"
        f"👥 Unique users: <b>{users}</b>\n"
        f"💾 Data served: <b>{format_size(bytes_served)}</b>\n"
        f"🏆 Top platforms: {top_s}\n\n"
        f"⏱ Your remaining quota this hour: <b>{remaining}</b>/{RATE_LIMIT_PER_HOUR}"
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
    )


def _settings_text(uid: int) -> str:
    prefs = user_prefs.get(uid)
    premium = user_prefs.premium_until(uid)
    labels = {"ask": "ask each time", "video": "🎥 Video", "audio": "🎵 Audio",
              "max": "Max", "mp3": "MP3", "m4a": "M4A", "opus": "Opus"}
    q = prefs["quality"]
    lines = [
        "⚙️ <b>Settings</b>\n",
        f"Default type: <b>{labels.get(prefs['mode'], prefs['mode'])}</b>",
        f"Video quality: <b>{labels.get(q, q + 'p' if q.isdigit() else q)}</b>",
        f"Audio format: <b>{labels.get(prefs['audio'], prefs['audio'])}</b>",
        "",
        f"Downloads this hour: <b>{rate_limiter.remaining(uid)}</b> left "
        f"of {user_prefs.hourly_limit(uid)}",
    ]
    if premium > time.time():
        lines.append(f"💎 Premium until <b>{time.strftime('%d %b %Y', time.gmtime(premium))}</b>")
    if uid in ADMIN_IDS:
        lines.append("🛡 Admin")
    ready = (prefs["mode"] == "video" and q != "ask") or (
        prefs["mode"] == "audio" and prefs["audio"] != "ask")
    lines.append("")
    lines.append(
        "⚡ <i>Links you send download straight away with these defaults.</i>" if ready else
        "<i>Pick a type and its quality/format to skip the menu on every link.</i>"
    )
    return "\n".join(lines)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        _settings_text(uid),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(user_prefs.get(uid), premium_enabled=PREMIUM_ENABLED),
    )


async def handle_pref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """pref:<key>:<value> from the settings keyboard (allowlisted in user_prefs)."""
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3 or not update.effective_user:
        return
    uid = update.effective_user.id
    if not user_prefs.set_pref(uid, parts[1], parts[2]):
        return
    try:
        await query.edit_message_text(
            _settings_text(uid),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(user_prefs.get(uid), premium_enabled=PREMIUM_ENABLED),
        )
    except TelegramError:
        pass  # unchanged ("message is not modified") or too old to edit


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.services.session import sessions

    if not update.effective_message or not update.effective_user:
        return
    s = sessions.get_for_user(update.effective_user.id)
    if s and s.started:
        # Downloads can't be interrupted mid-transfer; don't claim otherwise.
        await update.effective_message.reply_text(
            "⏳ That download is already running and will finish shortly — "
            "it can't be stopped mid-transfer.",
            reply_markup=main_reply_keyboard(),
        )
    elif s:
        sessions.remove(s.session_id)
        await update.effective_message.reply_text(
            "❌ Cancelled. Send a new link whenever you're ready.",
            reply_markup=main_reply_keyboard(),
        )
    else:
        await update.effective_message.reply_text(
            "Nothing to cancel. Paste a media link to start.",
            reply_markup=main_reply_keyboard(),
        )


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def text_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle reply-keyboard menu labels. Returns True if handled."""
    if not update.effective_message or not update.effective_message.text:
        return False
    text = update.effective_message.text.strip()
    mapping = {
        "📥 New Download": (
            "📥 <b>Send a media link</b>\n\n"
            "Paste a URL from YouTube, Instagram, TikTok, X, Facebook, "
            "Pinterest, Reddit, or any supported site."
        ),
        "❓ Help": None,
        "🌐 Platforms": None,
        "🕘 History": None,
        "📊 Stats": None,
        "⚙️ Settings": None,
    }
    if text not in mapping:
        return False
    if text == "❓ Help":
        await cmd_help(update, context)
    elif text == "🌐 Platforms":
        await cmd_platforms(update, context)
    elif text == "🕘 History":
        await cmd_history(update, context)
    elif text == "📊 Stats":
        await cmd_stats(update, context)
    elif text == "⚙️ Settings":
        await cmd_settings(update, context)
    elif text == "📥 New Download":
        await update.effective_message.reply_text(
            mapping[text],
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
    return True
