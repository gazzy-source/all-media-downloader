"""Entry point for All-Media Downloader Bot."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import __bot_bio__, __bot_name__, __version__
from bot.config import (
    ADMIN_IDS,
    BASE_DIR,
    BOT_TOKEN,
    TEMP_DIR,
    TEMP_CLEANUP_HOURS,
)
from bot.handlers.download import handle_callback, handle_message
from bot.handlers.start import (
    cmd_cancel,
    cmd_help,
    cmd_history,
    cmd_platforms,
    cmd_settings,
    cmd_start,
    cmd_stats,
)
from bot.services.session import sessions
from bot.utils.instance_lock import acquire_single_instance

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("all-media-bot")

# Quieter libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again or /cancel."
            )
        except Exception:
            pass


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic temp file + session cleanup."""
    removed_sessions = sessions.cleanup_expired()
    cutoff = time.time() - TEMP_CLEANUP_HOURS * 3600
    removed_files = 0
    try:
        for p in TEMP_DIR.rglob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    removed_files += 1
                elif p.is_dir():
                    # remove empty dirs
                    try:
                        next(p.iterdir())
                    except StopIteration:
                        p.rmdir()
            except Exception:
                pass
    except Exception:
        logger.exception("temp cleanup failed")
    if removed_sessions or removed_files:
        logger.info(
            "Cleanup: %s sessions, %s temp files", removed_sessions, removed_files
        )


async def post_init(app: Application) -> None:
    from bot.utils.ffmpeg import find_ffmpeg

    me = await app.bot.get_me()
    ff = find_ffmpeg()
    logger.info("=" * 50)
    logger.info("%s v%s", __bot_name__, __version__)
    logger.info("Bio: %s", __bot_bio__)
    logger.info("Logged in as @%s (id=%s)", me.username, me.id)
    logger.info("Admins: %s", ADMIN_IDS or "(none)")
    logger.info("FFmpeg: %s", ff if ff else "NOT FOUND")
    logger.info("=" * 50)

    # Command menu only — do NOT overwrite name/description/about from BotFather
    # unless explicitly set in .env (BOT_NAME / BOT_DESCRIPTION / BOT_SHORT_DESCRIPTION).
    from telegram import BotCommand

    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "How to use"),
            BotCommand("platforms", "Supported platforms"),
            BotCommand("history", "Your recent downloads"),
            BotCommand("stats", "Usage statistics"),
            BotCommand("settings", "Preferences & limits"),
            BotCommand("cancel", "Cancel current download"),
        ]
    )

    from bot.config import BOT_DESCRIPTION, BOT_NAME_OVERRIDE, BOT_SHORT_DESCRIPTION

    try:
        if BOT_NAME_OVERRIDE:
            await app.bot.set_my_name(BOT_NAME_OVERRIDE)
            logger.info("Applied BOT_NAME from .env")
        if BOT_DESCRIPTION:
            await app.bot.set_my_description(BOT_DESCRIPTION)
            logger.info("Applied BOT_DESCRIPTION from .env")
        if BOT_SHORT_DESCRIPTION:
            await app.bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
            logger.info("Applied BOT_SHORT_DESCRIPTION from .env")
        if not any((BOT_NAME_OVERRIDE, BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION)):
            logger.info(
                "Profile left unchanged (edit via @BotFather, or set BOT_* in .env)"
            )
    except Exception as e:
        logger.warning("Could not update bot profile fields: %s", e)


def build_app() -> Application:
    if not BOT_TOKEN:
        logger.error(
            "BOT_TOKEN is missing. Copy .env.example to .env and set your token from @BotFather."
        )
        sys.exit(1)

    # Long write/read timeouts — media uploads (10–50 MB) often need minutes
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .connect_timeout(60.0)
        .read_timeout(300.0)
        .write_timeout(300.0)
        .pool_timeout(60.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(60.0)
        .get_updates_write_timeout(30.0)
        .get_updates_pool_timeout(30.0)
        .media_write_timeout(300.0)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("platforms", cmd_platforms))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    # Also accept captions with links on forwarded media
    app.add_handler(
        MessageHandler(filters.CAPTION & ~filters.COMMAND, handle_message)
    )

    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_repeating(cleanup_job, interval=900, first=60)

    return app


def main() -> None:
    # Prevent multiple polling processes (causes duplicate replies)
    acquire_single_instance(BASE_DIR / "data" / "bot.lock")

    app = build_app()
    logger.info("Starting polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
