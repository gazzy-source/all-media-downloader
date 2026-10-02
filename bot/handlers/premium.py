"""
Premium, paid with Telegram Stars (currency "XTR" — no payment provider).

Flow: /premium (or 💎 in /settings) → "Get Premium" sends a Stars invoice →
Telegram asks us to confirm (pre_checkout_query: we check the payload, price
and buyer) → on successful_payment the days are added. Each purchase keeps its
telegram_payment_charge_id, so an admin can /refund it.

Telegram requires bots selling digital goods to answer /paysupport.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import (
    ADMIN_IDS,
    PREMIUM_DAYS,
    PREMIUM_ENABLED,
    PREMIUM_RATE_MULT,
    PREMIUM_STARS,
    RATE_LIMIT_PER_HOUR,
)
from bot.services import user_prefs

logger = logging.getLogger(__name__)

CURRENCY = "XTR"  # Telegram Stars


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _payload(user_id: int) -> str:
    return f"premium:{user_id}:{PREMIUM_DAYS}:{PREMIUM_STARS}"


def _parse_payload(payload: str) -> tuple[int, int, int] | None:
    parts = (payload or "").split(":")
    if len(parts) != 4 or parts[0] != "premium":
        return None
    try:
        return int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None


def _date(ts: float) -> str:
    return time.strftime("%d %b %Y", time.gmtime(ts))


def premium_text(user_id: int) -> str:
    until = user_prefs.premium_until(user_id)
    status = (
        f"💎 <b>You're Premium</b> until <b>{_date(until)}</b>. Thank you!\n\n"
        if until > time.time() else ""
    )
    return (
        f"{status}"
        "💎 <b>Premium</b>\n\n"
        f"• <b>{RATE_LIMIT_PER_HOUR * PREMIUM_RATE_MULT}</b> downloads per hour "
        f"(free: {RATE_LIMIT_PER_HOUR})\n"
        "• <b>Priority</b> — you skip ahead of free users in the download queue\n"
        "• Supports keeping the bot fast and online\n\n"
        f"⭐ <b>{PREMIUM_STARS} Stars</b> for <b>{PREMIUM_DAYS} days</b>. Buying again "
        "adds the days on top.\n\n"
        "<i>Payment is handled by Telegram. Help: /paysupport</i>"
    )


def _buy_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⭐ Get Premium — {PREMIUM_STARS} Stars", callback_data="prem:buy")
    ]])


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    if not PREMIUM_ENABLED:
        await msg.reply_text("Premium isn't offered on this bot.")
        return
    await msg.reply_text(premium_text(user.id), parse_mode=ParseMode.HTML,
                         reply_markup=_buy_markup())


async def handle_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """prem:open (from /settings) and prem:buy (sends the Stars invoice)."""
    query, user = update.callback_query, update.effective_user
    if not query or not user or not PREMIUM_ENABLED:
        return
    chat = update.effective_chat
    if query.data == "prem:open":
        if query.message:
            await query.message.reply_text(premium_text(user.id), parse_mode=ParseMode.HTML,
                                           reply_markup=_buy_markup())
        return
    if query.data != "prem:buy":
        return
    # Invoices go to the buyer's private chat, never into a group.
    target = user.id if chat is None or chat.type != "private" else chat.id
    try:
        await context.bot.send_invoice(
            chat_id=target,
            title=f"Premium — {PREMIUM_DAYS} days",
            description=(
                f"{RATE_LIMIT_PER_HOUR * PREMIUM_RATE_MULT} downloads/hour and priority "
                f"in the download queue for {PREMIUM_DAYS} days."
            ),
            payload=_payload(user.id),
            provider_token="",  # Stars: no provider
            currency=CURRENCY,
            prices=[LabeledPrice(f"Premium ({PREMIUM_DAYS} days)", PREMIUM_STARS)],
        )
    except TelegramError as e:
        logger.warning("could not send the premium invoice: %s", e)
        if query.message:
            await query.message.reply_text(
                "Couldn't open the payment. Start a private chat with me and try /premium.")


async def handle_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram asks before charging: approve only OUR invoice, at OUR price, for its buyer."""
    q = update.pre_checkout_query
    if not q:
        return
    parsed = _parse_payload(q.invoice_payload)
    ok = (
        PREMIUM_ENABLED
        and parsed is not None
        and parsed[0] == q.from_user.id
        and parsed[1] == PREMIUM_DAYS
        and parsed[2] == PREMIUM_STARS
        and q.currency == CURRENCY
        and q.total_amount == PREMIUM_STARS
    )
    try:
        if ok:
            await q.answer(ok=True)
        else:
            await q.answer(ok=False, error_message=(
                "This offer has changed. Please open /premium again for the current price."))
    except TelegramError as e:
        logger.warning("pre-checkout answer failed: %s", e)


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    sp = getattr(msg, "successful_payment", None) if msg else None
    if not sp or not user:
        return
    parsed = _parse_payload(sp.invoice_payload)
    if sp.currency != CURRENCY or parsed is None or parsed[0] != user.id:
        logger.error("unexpected payment from %s: %s %s", user.id, sp.currency, sp.invoice_payload)
        return
    try:
        until = user_prefs.grant_premium(
            user.id, parsed[1], charge_id=sp.telegram_payment_charge_id, stars=sp.total_amount)
    except user_prefs.GrantNotSaved:
        # The charge id is the refund trail: it must reach the log.
        logger.error("PREMIUM NOT SAVED for %s: charge %s, %s Stars, %s days",
                     user.id, sp.telegram_payment_charge_id, sp.total_amount, parsed[1])
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin, f"⚠️ Premium payment from {user.id} could not be saved. "
                           f"Charge: {sp.telegram_payment_charge_id} — grant or /refund {user.id}.")
            except TelegramError:
                pass
        await msg.reply_text(
            "⚠️ Your payment went through, but I couldn't activate Premium just "
            "now. The owner has been told and will fix it — or use /paysupport.")
        return
    logger.info("premium bought by %s: %s Stars, until %s", user.id, sp.total_amount, _date(until))
    await msg.reply_text(
        f"💎 <b>Premium is active</b> until <b>{_date(until)}</b>.\n\n"
        f"You now get {user_prefs.hourly_limit(user.id)} downloads per hour and "
        "priority in the queue. Thank you for supporting the bot!",
        parse_mode=ParseMode.HTML,
    )


_SUPPORT_COOLDOWN = 600  # one forwarded request per user per 10 min
_last_support: dict[int, float] = {}
_all_support: list[float] = []
_SUPPORT_PER_MINUTE = 5


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Required by Telegram for bots that sell digital goods. "/paysupport <what
    happened>" reaches the owner; a plain reply in the chat would go nowhere.
    """
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    text = " ".join(getattr(context, "args", None) or []).strip()
    if not text:
        await msg.reply_text(
            "💬 <b>Payment support</b>\n\n"
            "Premium is paid with Telegram Stars. If you were charged but Premium "
            "isn't active, or you want a refund, send:\n\n"
            "<code>/paysupport what happened and the date</code>\n\n"
            "It goes straight to the bot owner. Refunds return to your Stars "
            "balance.",
            parse_mode=ParseMode.HTML,
        )
        return
    now = time.monotonic()
    if now - _last_support.get(user.id, -_SUPPORT_COOLDOWN) < _SUPPORT_COOLDOWN:
        await msg.reply_text("✅ Your earlier request was received — the owner "
                             "will get back to you here.")
        return
    # All accounts together: at most a few forwards a minute to the owner.
    recent = [t for t in _all_support if now - t < 60]
    if len(recent) >= _SUPPORT_PER_MINUTE:
        await msg.reply_text("⏳ Support is busy right now — please try again in a minute.")
        return
    # Claimed BEFORE the awaits below: with concurrent updates, a burst of
    # /paysupport all passed the check and all reached the owner.
    _last_support[user.id] = now
    _all_support[:] = recent + [now]
    if len(_last_support) > 5000:
        for k in [k for k, t in _last_support.items() if now - t > _SUPPORT_COOLDOWN]:
            del _last_support[k]
    who = f"@{user.username}" if getattr(user, "username", None) else "no username"
    until = user_prefs.premium_until(user.id)
    note = (
        f"💬 <b>Payment support</b> from <code>{user.id}</code> ({_html(who)})\n"
        f"Premium: {'active' if until > time.time() else 'not active'}\n\n"
        f"<blockquote>{_html(text[:1500])}</blockquote>\nRefund: <code>/refund {user.id}</code>"
    )
    delivered = False
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, note, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True)
            delivered = True
        except TelegramError as e:
            logger.warning("paysupport forward to %s failed: %s", admin, e)
    if not delivered:
        logger.error("paysupport from %s not delivered (no reachable admin)",
                     user.id)
        _last_support.pop(user.id, None)  # let them try again
        await msg.reply_text("⚠️ Couldn't reach the owner right now — please try "
                             "again in a little while.")
        return
    await msg.reply_text("✅ Sent to the bot owner. You'll get an answer in this "
                         "chat.")


async def cmd_refund(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin only: /refund <user_id> — refunds that user's latest Stars purchase."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or user.id not in ADMIN_IDS:
        return
    args = getattr(context, "args", None) or []
    if len(args) != 1 or not args[0].lstrip("-").isdigit():
        await msg.reply_text("Usage: /refund <user_id>")
        return
    target = int(args[0])
    charge = user_prefs.last_charge(target)
    if not charge:
        await msg.reply_text("No refundable purchase for that user.")
        return
    try:
        await context.bot.refund_star_payment(
            user_id=target, telegram_payment_charge_id=charge["id"])
    except TelegramError as e:
        await msg.reply_text(f"Refund failed: {e}")
        return
    user_prefs.revoke_charge(target, charge["id"])
    logger.info("admin %s refunded %s Stars to %s", user.id, charge.get("stars"), target)
    await msg.reply_text(f"✅ Refunded {charge.get('stars')} Stars to {target}; "
                         "their Premium days for it were removed.")
