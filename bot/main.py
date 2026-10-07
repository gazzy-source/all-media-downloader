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
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from bot import __bot_bio__, __bot_name__, __version__
from bot.config import (
    POT_PROVIDER_URL,
    ADMIN_IDS,
    DATA_DIR,
    BOT_TOKEN,
    TEMP_DIR,
    TEMP_CLEANUP_HOURS,
    TELEGRAM_API_URL,
    WARMUP_INTERVAL_MIN,
    WARMUP_ON_START,
    WARMUP_URL,
)
from bot.handlers import premium
from bot.handlers.download import handle_callback, handle_message
from bot.handlers.inline import (
    handle_chosen_inline_result,
    handle_inline_query,
    rescue_interrupted,
    warm_placeholders,
)
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
from bot.services import heartbeat  # noqa: E402

logger = logging.getLogger("all-media-bot")

# Quieter libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
# "Running job heartbeat_job…" twice every 30s buried the real log lines.
logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _who(update: object) -> str:
    """update id + hashed user, for following one failure through the log."""
    from bot.utils import redact

    uid = getattr(getattr(update, "effective_user", None), "id", None)
    return (f"update={getattr(update, 'update_id', '?')} "
            f"user={redact.uid(uid) if uid is not None else '-'}")


_BENIGN = (
    "message is not modified",
    "query is too old",
    "query id is invalid",
    "message to edit not found",
    "message to delete not found",
    "message can't be edited",
)


def _benign(err: object) -> bool:
    from telegram.error import BadRequest

    return isinstance(err, BadRequest) and any(b in str(err).lower() for b in _BENIGN)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import BadRequest, NetworkError

    if isinstance(context.error, NetworkError) and not isinstance(context.error, BadRequest):
        # Telegram's own hiccups (502 Bad Gateway, timeouts): PTB retries
        # polling by itself, and a reply that hit one is not "our" bug — the
        # user was told "Something went wrong" for a Telegram 502 before.
        logger.warning("Telegram network hiccup%s: %s",
                       "" if update is None else f" ({_who(update)})", context.error)
        return
    if _benign(context.error):
        # A double tap re-sends an identical edit ("message is not modified"),
        # a stale button's query has expired, a message was deleted meanwhile:
        # nothing failed from the user's point of view.
        logger.info("Ignored benign Telegram refusal: %s", context.error)
        return
    # exc_info from the error itself: logger.exception outside an except
    # block printed "NoneType: None" instead of the traceback.
    logger.error("Unhandled error (%s): %s", _who(update), context.error,
                 exc_info=context.error)
    # Duck-typed access: works for Update and any object carrying effective_message
    msg = getattr(update, "effective_message", None)
    if msg is not None:
        try:
            # Private chats only: in a group it is noise for everyone else.
            chat = getattr(update, "effective_chat", None)
            if chat is not None and getattr(chat, "type", "private") != "private":
                return
            await msg.reply_text("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    is_cmd = bool(msg.text and msg.text.startswith("/"))
    await msg.reply_text(
        ("🤔 I don't know that command — see /help.\n\n" if is_cmd else "")
        + "📥 Send me a link (YouTube, Instagram, TikTok, X…) and I'll download it.",
    )


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
                elif p.is_dir() and p.stat().st_mtime < cutoff:
                    # Old empty dirs only: a running job's work dir is empty
                    # during extraction, and removing it broke the job's write.
                    try:
                        next(p.iterdir())
                    except StopIteration:
                        p.rmdir()
            except Exception:
                pass
    except Exception:
        logger.exception("temp cleanup failed")
    # Per-job cookie copies are deleted when the job ends, but a crash or a
    # kill -9 mid-job leaves a full copy of the operator's login cookies in
    # data/. None legitimately lives longer than a download.
    try:
        from bot.config import DATA_DIR

        jar_cutoff = time.time() - 3600
        for jar in DATA_DIR.glob("cookies.job_*.txt"):
            try:
                if jar.stat().st_mtime < jar_cutoff:
                    jar.unlink(missing_ok=True)
                    removed_files += 1
            except OSError:
                pass
    except Exception:
        logger.exception("cookie jar sweep failed")
    if removed_sessions or removed_files:
        logger.info("Cleanup: %s sessions, %s temp files", removed_sessions, removed_files)


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """data/heartbeat.json for the health check (stale = event loop stuck)."""
    from bot.services.dl_queue import download_queue

    from bot.services import url_tokens

    updater = getattr(context.application, "updater", None)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, heartbeat.write, bool(updater and updater.running),
        download_queue.running, download_queue.waiting)
    await loop.run_in_executor(None, url_tokens.flush)


class BotApplication(Application):
    async def stop(self) -> None:
        """
        Stop downloads FIRST. Application.stop() waits for every running
        handler — i.e. every download, and every queued one that would still
        start a fresh transfer — so a post_stop hook ran only once they had all
        finished (or systemd killed us). Their users are told "Interrupted"
        on the next start (their messages stay registered).
        """
        from bot.services import jobs, url_tokens

        stopped = jobs.shutdown_all()
        if stopped:
            logger.info("Shutdown: stopping %s running/queued download(s)", stopped)
        url_tokens.flush()
        await super().stop()


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

    Best effort and never fatal to startup. YouTube warm-up checks both
    metadata and a 1 KiB media range, so metadata-only success cannot hide a
    broken token, route, or media URL. Failures update the warm-up signal.
    """
    if not WARMUP_ON_START:
        return
    from bot.services.downloader import download_manager

    started = time.time()
    try:
        info = await asyncio.wait_for(
            download_manager.extract_info(WARMUP_URL, limit=120), timeout=120
        )
        from bot.services.downloader import probe_youtube_media_bytes

        await asyncio.wait_for(
            asyncio.to_thread(probe_youtube_media_bytes, info.raw), timeout=15
        )
        logger.info(
            "YouTube warm-up passed metadata and media-byte probe in %.1fs",
            time.time() - started,
        )
        heartbeat.warmup_result(True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        heartbeat.warmup_result(False, f"{type(e).__name__}: {e}")
        # The canary for YouTube: users hit the same wall until it recovers.
        logger.warning(
            "YouTube warm-up failed after %.1fs (%s in a row): %s",
            time.time() - started, heartbeat.state["warmup_fail_streak"],
            str(e).split("\n")[-1][:160],
        )


async def _recheck_pot() -> None:
    """
    The provider container often finishes booting after the bot (seen after
    reboots and package upgrades). Re-check for a few minutes so the log says
    when it recovered — and so the first YouTube requests are not blamed on it.
    """
    from bot.services.downloader import pot_provider_mint_check

    for _ in range(10):
        await asyncio.sleep(30)
        ok, detail = await asyncio.get_running_loop().run_in_executor(
            None, pot_provider_mint_check
        )
        if ok:
            logger.info("YouTube: PO-token provider recovered (%s)", detail)
            return
    logger.error("YouTube: PO-token provider still cannot mint after 5 minutes")


async def post_init(app: Application) -> None:
    from bot.handlers.inline import ignore_backlog
    from bot.utils.ffmpeg import find_ffmpeg

    # Updates are kept across restarts now; inline queries among them expired.
    ignore_backlog(15)

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
            app.bot_data["_pot_recheck_task"] = asyncio.create_task(_recheck_pot())
            logger.warning(
                "YouTube: PO-token provider answers but CANNOT MINT (%s). "
                "Expect 'Sign in to confirm you're not a bot' on YouTube. If "
                "PROXY is set, the provider must be able to reach it too — a "
                "container on a bridge network cannot reach a host-local "
                "127.0.0.1 proxy; run it with host networking.",
                detail,
            )
    else:
        if POT_PROVIDER_URL:
            # Configured but not answering yet: the provider container often
            # finishes booting after the bot. Keep checking for a while.
            app.bot_data["_pot_recheck_task"] = asyncio.create_task(_recheck_pot())
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
    # Inline placeholders: best effort, never blocks startup.
    app.bot_data["_inline_warm_task"] = asyncio.create_task(warm_placeholders(app))
    app.bot_data["_inline_rescue_task"] = asyncio.create_task(rescue_interrupted(app))

    # Command menu only — do NOT overwrite name/description/about from BotFather
    # unless explicitly set in .env (BOT_NAME / BOT_DESCRIPTION / BOT_SHORT_DESCRIPTION).
    from telegram import BotCommand

    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start & how to use"),
            BotCommand("settings", "Save default quality/format — skip the menu"),
            BotCommand("premium", "More downloads + priority queue"),
            BotCommand("history", "Your recent downloads"),
            BotCommand("help", "All features & limits"),
            BotCommand("platforms", "Supported sites"),
            BotCommand("cancel", "Stop the current download"),
            BotCommand("paysupport", "Help with a payment"),
        ]
    )

    from bot.config import BOT_DESCRIPTION, BOT_NAME_OVERRIDE, BOT_SHORT_DESCRIPTION

    try:
        if BOT_NAME_OVERRIDE:
            await app.bot.set_my_name(BOT_NAME_OVERRIDE)
            logger.info("Applied BOT_NAME from .env")
        if BOT_DESCRIPTION:
            me = f"@{app.bot.username}" if app.bot.username else "the bot"
            await app.bot.set_my_description(BOT_DESCRIPTION.replace("{me}", me)[:512])
            logger.info("Applied bot description")
        if BOT_SHORT_DESCRIPTION:
            await app.bot.set_my_short_description(BOT_SHORT_DESCRIPTION[:120])
            logger.info("Applied bot short description")
        if not any((BOT_NAME_OVERRIDE, BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION)):
            logger.info(
                "Profile left unchanged (edit via @BotFather, or set BOT_* in .env)"
            )
    except Exception as e:
        logger.warning("Could not update bot profile fields: %s", e)


def build_app() -> Application:
    # Premium users get a higher hourly limit; the limiter asks per user.
    from bot.services import user_prefs
    from bot.services.rate_limit import rate_limiter

    rate_limiter.limit_for = user_prefs.hourly_limit

    if not BOT_TOKEN:
        logger.error(
            "BOT_TOKEN is missing. Copy .env.example to .env and set your token from @BotFather."
        )
        sys.exit(1)

    # Long write/read timeouts — media uploads (10–50 MB) often need minutes
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .application_class(BotApplication)
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
        # Replace only the trailing "/bot": a host like http://botapi:8081
        # contains "//bot" too, and a blanket replace mangled the host.
        builder = builder.base_file_url(base[: -len("/bot")] + "/file/bot/")
        logger.info("Using custom Telegram API base: %s", base)

    app = builder.build()

    # New messages only: CommandHandler also matches EDITED messages, so
    # editing "/paysupport …" or "/start dl_…" ran the command a second time.
    def command(name, fn):
        app.add_handler(CommandHandler(name, fn, filters=filters.UpdateType.MESSAGE))

    command("start", cmd_start)
    command("help", cmd_help)
    command("platforms", cmd_platforms)
    command("history", cmd_history)
    command("stats", cmd_stats)
    command("settings", cmd_settings)
    command("cancel", cmd_cancel)
    # Premium (Telegram Stars)
    command("premium", premium.cmd_premium)
    command("paysupport", premium.cmd_paysupport)
    command("refund", premium.cmd_refund)
    app.add_handler(PreCheckoutQueryHandler(premium.handle_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, premium.handle_successful_payment))

    app.add_handler(CallbackQueryHandler(handle_callback))
    # Inline mode: @bot <link> in any chat
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(ChosenInlineResultHandler(handle_chosen_inline_result))
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

    # Private chat, anything else (an unknown /command, a sticker, a voice
    # note, a photo with no caption): say what the bot does instead of silence.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
            & (filters.COMMAND
               | ((filters.ATTACHMENT | filters.Sticker.ALL | filters.LOCATION
                   | filters.CONTACT) & ~filters.CAPTION & ~filters.SUCCESSFUL_PAYMENT)),
            handle_unknown,
        )
    )

    app.add_error_handler(on_error)

    if app.job_queue:
        # More frequent temp cleanup on small VPS
        app.job_queue.run_repeating(cleanup_job, interval=600, first=30)
        app.job_queue.run_repeating(heartbeat_job, interval=30, first=5)
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
    acquire_single_instance(DATA_DIR / "bot.lock")

    app = build_app()
    logger.info("Starting polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        # Keep what arrived during a restart/deploy: a successful_payment
        # dropped here is a user charged with no Premium and no record; a link
        # sent mid-deploy would get no answer. Handlers are safe to replay.
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
