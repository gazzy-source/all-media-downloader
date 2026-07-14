"""Start, help, platforms, stats, settings handlers."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import __bot_bio__, __bot_name__, __version__
from bot.config import ADMIN_IDS, RATE_LIMIT_PER_HOUR, SUPPORTED_PLATFORMS
from bot.keyboards.menus import main_reply_keyboard
from bot.services.history import get_stats, get_user_history
from bot.services.rate_limit import rate_limiter
from bot.utils.helpers import format_size


WELCOME = f"""
🚀 <b>{__bot_name__}</b>
<code>v{__version__}</code>

{__bot_bio__}

<b>How to use</b>
• Paste a link → downloads instantly (video, up to 1080p)
• Works the same in <b>DM</b> and <b>groups</b>
• Rare sites may still open a short quality wizard

<b>Pro tips</b>
• YouTube / IG / TikTok / X / Facebook / Pinterest &amp; 1000+ sites
• Audio / subtitles: use the wizard on non-auto sites, or ask admin
• Bot needs to see group messages (@BotFather → /setprivacy → Disable)

Just paste a link 👇
""".strip()


HELP_TEXT = f"""
❓ <b>Help — {__bot_name__}</b>

<b>Commands</b>
/start — Welcome &amp; main menu
/help — This guide
/platforms — Supported sites
/history — Your recent downloads
/stats — Usage statistics
/settings — Preferences &amp; limits
/cancel — Cancel current operation

<b>Download modes</b>
🎥 <b>Video</b> — Best merged video+audio at your quality
🎞 <b>Video + Subtitles</b> — Same, with soft/hard subtitles
🎵 <b>Audio</b> — Extract soundtrack (MP3/M4A/Opus)
🖼 <b>Image</b> — Photos, pins, thumbnails at best size

<b>Quality options</b>
• 480p — small &amp; fast
• 720p — balanced (default)
• 1080p — Full HD
• Max — highest available (up to 4K+)

<b>Limits</b>
• Telegram bots can send up to ~50 MB per file
• Rate limit: {RATE_LIMIT_PER_HOUR} downloads / hour / user
• Very large files are sent as documents when possible

<b>Troubleshooting</b>
• Private / login-walled posts need a cookies.txt on the server
• Install FFmpeg for audio conversion &amp; merging
• Age-restricted YouTube may need cookies

Built with ❤️ by <b>Gazzy Labs</b>
""".strip()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        WELCOME,
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        HELP_TEXT,
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
    items = get_user_history(update.effective_user.id, limit=10)
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
            f"    {platform} · {mode} · {q}"
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
    stats = get_stats()
    remaining = rate_limiter.remaining(update.effective_user.id)
    total = stats.get("total_downloads", 0)
    ok = stats.get("successful", 0)
    fail = stats.get("failed", 0)
    users = stats.get("unique_user_count", 0)
    bytes_served = stats.get("bytes_served", 0)
    by_platform = stats.get("by_platform") or {}
    top = sorted(by_platform.items(), key=lambda x: x[1], reverse=True)[:5]
    top_s = ", ".join(f"{k} ({v})" for k, v in top) if top else "—"

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


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    uid = update.effective_user.id
    remaining = rate_limiter.remaining(uid)
    is_admin = uid in ADMIN_IDS
    text = (
        f"⚙️ <b>Settings</b>\n\n"
        f"• Rate limit: <b>{RATE_LIMIT_PER_HOUR}</b> downloads / hour\n"
        f"• Remaining this hour: <b>{remaining}</b>\n"
        f"• Default video quality preference: <b>720p</b> (choose per download)\n"
        f"• Admin: <b>{'Yes' if is_admin else 'No'}</b>\n\n"
        f"<i>More preferences (default quality, audio format) can be chosen "
        f"interactively on every download for maximum control.</i>"
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.services.session import sessions

    if not update.effective_message or not update.effective_user:
        return
    s = sessions.get_for_user(update.effective_user.id)
    if s:
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
