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
    until = user_prefs.grant_premium(
        user.id, parsed[1], charge_id=sp.telegram_payment_charge_id, stars=sp.total_amount)
    logger.info("premium bought by %s: %s Stars, until %s", user.id, sp.total_amount, _date(until))
    await msg.reply_text(
        f"💎 <b>Premium is active</b> until <b>{_date(until)}</b>.\n\n"
        f"You now get {user_prefs.hourly_limit(user.id)} downloads per hour and "
        "priority in the queue. Thank you for supporting the bot!",
        parse_mode=ParseMode.HTML,
    )


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Required by Telegram for bots that sell digital goods."""
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "💬 <b>Payment support</b>\n\n"
        "Premium is a digital service paid with Telegram Stars. If something went "
        "wrong with a purchase — you were charged but Premium isn't active, or you "
        "want a refund — reply here describing the issue, including the date of "
        "the payment. Refunds are handled by the bot owner and returned to your "
        "Stars balance.",
        parse_mode=ParseMode.HTML,
    )


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
