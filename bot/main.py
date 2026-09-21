"""Entry point for All-Media Downloader Bot."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

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
    TELEGRAM_API_URL,
    WARMUP_INTERVAL_MIN,
    WARMUP_ON_START,
    WARMUP_URL,
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
    # Duck-typed access: works for Update and any object carrying effective_message
    msg = getattr(update, "effective_message", None)
    if msg is not None:
        try:
            await msg.reply_text(
                "⚠️ Something went wrong. Please try again or /cancel."
            )
        except Exception:
            pass


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic temp file + session cleanup (keep disk/RAM free on small VPS)."""
    removed_sessions = sessions.cleanup_expired()
    cutoff = time.time() - TEMP_CLEANUP_HOURS * 3600
    removed_files = 0
    try:
        for p in sorted(TEMP_DIR.rglob("*"), reverse=True):
            try:
                # Never sweep away dotfiles: temp/.gitkeep is tracked in git, so
                # deleting it leaves every deployment with a dirty working tree
                # and makes the next `git pull` refuse to fast-forward.
                if p.name.startswith("."):
                    continue
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    removed_files += 1
                elif p.is_dir():
                    try:
                        next(p.iterdir())
                    except StopIteration:
                        p.rmdir()
            except Exception:
                pass
    except Exception:
        logger.exception("temp cleanup failed")
    if removed_sessions or removed_files:
        logger.info("Cleanup: %s sessions, %s temp files", removed_sessions, removed_files)


async def warmup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the YouTube pipeline warm so no user request pays to rebuild it."""
    await _warm_youtube_pipeline()


async def _rescue_interrupted_jobs(app: Application) -> None:
    """
    Tell anyone whose download died with the previous process.

    Jobs are in-memory, so a deploy, crash or reboot kills them mid-flight and
    nothing ever updates the "Downloading…" message again — it sits frozen on
    whatever it last showed, which reads as the bot hanging. The registry is on
    disk, so this also covers a SIGKILL that no shutdown hook would catch.
    """
    from bot.services.inflight import drain

    rows = drain()
    if not rows:
        return
    logger.info("Found %s download(s) interrupted by the last restart", len(rows))
    for row in rows:
        try:
            await app.bot.edit_message_text(
                "⚠️ <b>Interrupted</b>\n\n"
                "The bot restarted while this was downloading, so the job was "
                "lost.\n\nSend the link again to retry.",
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                parse_mode="HTML",
            )
        except Exception as e:  # message deleted, too old, no rights — never fatal
            logger.debug("Could not flag interrupted job: %s", e)


async def _warm_youtube_pipeline() -> None:
    """
    Pay the first-request costs at boot instead of charging them to a user.

    A fresh process must spawn deno, solve and cache YouTube's signature
    function, mint its first PO token and open the first proxy connection
    before it can answer anything. Production measured 24.3s for the first
    link after a restart versus ~3s once warm, and the user watching that
    reasonably read it as the bot hanging.

    Strictly best effort: metadata only, never fatal, and it holds nothing the
    request path needs. A failure here says nothing about the bot's health —
    the link may simply be gone — so it logs at debug and moves on.
    """
    if not WARMUP_ON_START:
        return
    from bot.services.downloader import download_manager

    started = time.time()
    try:
        await asyncio.wait_for(
            download_manager.extract_info(WARMUP_URL), timeout=120
        )
        logger.info("Warmed the YouTube pipeline in %.1fs", time.time() - started)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(
            "Warmup did not complete after %.1fs (harmless): %s",
            time.time() - started,
            e,
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
    from bot.services.downloader import (
        _resolved_cookie,
        pot_provider_available,
        pot_provider_mint_check,
    )

    ck = _resolved_cookie()
    pot = pot_provider_available()
    logger.info("Cookies: %s (optional — public posts need none)", ck if ck else "none")
    if pot:
        # Reachable != able to mint. A containerised provider answers /ping
        # from its own namespace but cannot reach a host-local SOCKS proxy, so
        # every real mint fails while the banner claims all is well. Ask it for
        # a token the same way an extraction would, proxy included. Off the
        # event loop: the call is blocking and can sit on a proxy timeout.
        ok, detail = await asyncio.get_running_loop().run_in_executor(
            None, pot_provider_mint_check
        )
        if ok:
            # Still not a promise of service: YouTube can bot-wall the host's
            # IP even with a valid token (seen on a datacenter VPS).
            logger.info(
                "YouTube: PO-token provider working (%s) — top formats unlocked "
                "unless this server's IP is itself bot-walled",
                detail,
            )
        else:
            logger.warning(
                "YouTube: PO-token provider answers but CANNOT MINT (%s). "
                "Expect 'Sign in to confirm you're not a bot' on YouTube. If "
                "PROXY is set, the provider must be able to reach it too — a "
                "container on a bridge network cannot reach a host-local "
                "127.0.0.1 proxy; run it with host networking.",
                detail,
            )
    else:
        logger.info(
            "YouTube: no PO-token provider — public videos still download "
            "cookielessly, but the highest formats may 403 and fall back to the "
            "`android` client (360p). Start bgutil-provider or set "
            "POT_PROVIDER_URL to make every quality reachable."
        )
    logger.info("Telegram API: %s", TELEGRAM_API_URL or "https://api.telegram.org (default)")
    logger.info("=" * 50)

    await _rescue_interrupted_jobs(app)

    # Background, so polling starts immediately: the whole point is that no
    # user waits on this. Held on the Application so it is not garbage
    # collected mid-flight, which asyncio permits for bare tasks.
    app.bot_data["_warmup_task"] = asyncio.create_task(_warm_youtube_pipeline())

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
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .connect_timeout(30.0)
        .read_timeout(180.0)
        .write_timeout(600.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(20.0)
        .get_updates_read_timeout(50.0)
        .get_updates_write_timeout(30.0)
        .get_updates_pool_timeout(20.0)
        .media_write_timeout(600.0)
        .connection_pool_size(16)
    )
    # Local Bot API server (optional) for larger uploads (~2GB)
    if TELEGRAM_API_URL:
        # e.g. http://127.0.0.1:8081/bot  → base_url http://127.0.0.1:8081/bot
        # PTB expects base_url ending with /bot
        base = TELEGRAM_API_URL.rstrip("/")
        if not base.endswith("/bot"):
            base = base + "/bot"
        builder = builder.base_url(base + "/")
        builder = builder.base_file_url(base.replace("/bot", "/file/bot") + "/")
        logger.info("Using custom Telegram API base: %s", base)

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("platforms", cmd_platforms))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(handle_callback))
    # Private + group messages
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    # Captions with links on media
    app.add_handler(
        MessageHandler(filters.CAPTION & ~filters.COMMAND, handle_message)
    )
    # Channel posts (bot must be admin with Post messages)
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS & filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS & filters.CAPTION & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(on_error)

    if app.job_queue:
        # More frequent temp cleanup on small VPS
        app.job_queue.run_repeating(cleanup_job, interval=600, first=30)
        # Re-warm well inside the PO token's ~6h life. Without this the token
        # lapses mid-day and the next user waits out a 12s re-mint.
        if WARMUP_ON_START and WARMUP_INTERVAL_MIN > 0:
            app.job_queue.run_repeating(
                warmup_job,
                interval=WARMUP_INTERVAL_MIN * 60,
                first=WARMUP_INTERVAL_MIN * 60,
            )

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
