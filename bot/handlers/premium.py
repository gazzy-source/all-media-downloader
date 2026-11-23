"""
Download packs, paid with Telegram Stars (currency "XTR", no payment provider).

Each account gets FREE_DOWNLOADS free downloads (see services/quota.py), then:

  /premium (also /buy, ⭐ in the menu) → pick a pack → Stars invoice in the
  buyer's private chat → pre_checkout_query (we re-check account, buyer, pack
  and price) → successful_payment adds the pack. Every charge keeps its
  telegram_payment_charge_id, so the owner can /refund it.

Channels are their own account (a post has no person behind it). An admin of
the channel buys for it with "/buy <channel id>"; the refund goes to that admin.

Owner commands: /approvefree <id> (lifetime free, never counted),
/revokefree <id>, /refund <user id>.

Telegram requires bots selling digital goods to answer /paysupport.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import ADMIN_IDS
from bot.services import quota

logger = logging.getLogger(__name__)

CURRENCY = "XTR"  # Telegram Stars
_ADMIN_STATUSES = ("creator", "administrator")


def _html(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _n(x: int) -> str:
    return f"{x:,}"


# ----------------------------------------------------------------- payload
def _payload(account: int, payer: int, downloads: int, stars: int) -> str:
    return f"dl:{account}:{payer}:{downloads}:{stars}"


def _parse_payload(payload: str) -> tuple[int, int, int, int] | None:
    parts = (payload or "").split(":")
    if len(parts) != 5 or parts[0] != "dl":
        return None
    try:
        return int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None


# ------------------------------------------------------------------- texts
def _who(account: int, user_id: int) -> str:
    return "You have" if account == user_id else f"<code>{account}</code> has"


def quota_text(account: int, user_id: int) -> str:
    """The account's balance and the packs on offer."""
    st = quota.status(account)
    who = _who(account, user_id)
    if st["admin"] or st["approved"]:
        head = f"♾ <b>{who} unlimited free downloads</b> (approved by the owner).\n\n"
    elif st["remaining"] > 0:
        head = (f"📥 {who} <b>{_n(st['remaining'])}</b> downloads left "
                f"({_n(st['used'])} of {_n(st['allowance'])} used).\n\n")
    else:
        head = (f"🚫 {who} used all <b>{_n(st['allowance'])}</b> downloads "
                f"({_n(quota.FREE)} free" + (f" + {_n(st['bought'])} bought" if st["bought"] else "")
                + ").\n\n")
    packs = "\n".join(f"• ⭐ <b>{_n(s)} Stars</b> → <b>{_n(n)}</b> more downloads"
                      for n, s in quota.PACKS)
    return (
        f"{head}⭐ <b>Get more downloads</b>\n\n"
        f"Everyone gets {_n(quota.FREE)} free downloads: in this chat, inline, in "
        "groups and in channels, all counted together. Then:\n\n"
        f"{packs}\n\n"
        "Packs never expire and stack. Buyers also skip ahead in the queue.\n\n"
        "<i>Payment is handled by Telegram. Help: /paysupport</i>"
    )


def _packs_markup(account: int, user_id: int) -> InlineKeyboardMarkup:
    suffix = "" if account == user_id else f":{account}"
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"⭐ {_n(n)} downloads — {_n(s)} Stars", callback_data=f"pay:{n}{suffix}")]
        for n, s in quota.PACKS])


def refusal(account: int, *, user_id: int | None, private: bool, bot_username: str,
            channel: bool = False) -> tuple[str, InlineKeyboardMarkup | None]:
    """What to say when an account is out of downloads, and the way to buy."""
    st = quota.status(account)
    used = _n(st["allowance"])
    if private and user_id == account:
        return quota_text(account, user_id), _packs_markup(account, user_id)
    if channel:
        text = (f"🚫 This channel has used its {used} downloads. A channel admin can add "
                f"more: open @{bot_username} and send <code>/buy {account}</code>.")
        url = f"https://t.me/{bot_username}?start=buy_{account}"
    else:
        text = f"🚫 You've used your {used} downloads. Get more in a private chat with me ⭐"
        url = f"https://t.me/{bot_username}?start=buy"
    if not bot_username:
        return text, None
    return text, InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Get more downloads", url=url)]])


async def _is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return getattr(member, "status", None) in _ADMIN_STATUSES


async def _account_for(update: Update, context, raw: str | None) -> int | None:
    """
    The account a buyer is looking at: themselves, or a chat they administer.
    Returns None (and says why) when they may not buy for it.
    """
    user, msg = update.effective_user, update.effective_message
    if raw is None or raw == str(user.id):
        return user.id
    if not raw.lstrip("-").isdigit() or not raw.startswith("-"):
        if msg:
            await msg.reply_text("Usage: /buy, or /buy <channel id> for a channel you run.")
        return None
    account = int(raw)
    if user.id not in ADMIN_IDS and not await _is_chat_admin(context.bot, account, user.id):
        if msg:
            await msg.reply_text("⛔ Only an admin of that channel can buy downloads for it "
                                 "(and I have to be in it).")
        return None
    return account


# ---------------------------------------------------------------- commands
async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/premium, /buy and /buy <channel id>: the balance and the packs."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    chat = update.effective_chat
    if chat is not None and chat.type != "private":
        me = getattr(context.bot, "username", "") or ""
        await msg.reply_text(
            "⭐ Downloads are bought in a private chat with me.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "⭐ Open", url=f"https://t.me/{me}?start=buy")]]) if me else None)
        return
    args = getattr(context, "args", None) or []
    account = await _account_for(update, context, args[0] if args else None)
    if account is None:
        return
    await msg.reply_text(quota_text(account, user.id), parse_mode=ParseMode.HTML,
                         reply_markup=_packs_markup(account, user.id))


async def handle_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """pay:<downloads>[:<account>] sends the invoice; prem:* (older menus) shows the packs."""
    query, user = update.callback_query, update.effective_user
    if not query or not user:
        return
    data = query.data or ""
    if data.startswith("prem:"):
        if query.message:
            await query.message.reply_text(quota_text(user.id, user.id),
                                           parse_mode=ParseMode.HTML,
                                           reply_markup=_packs_markup(user.id, user.id))
        return
    parts = data.split(":")
    if parts[0] != "pay" or len(parts) not in (2, 3) or not parts[1].isdigit():
        return
    offer = quota.pack(int(parts[1]))
    if offer is None:
        if query.message:
            await query.message.reply_text("That offer has changed — open /premium again.")
        return
    account = user.id
    if len(parts) == 3:
        # callback_data is client-controlled: check the right to buy again.
        account = await _account_for(update, context, parts[2])
        if account is None:
            return
    downloads, stars = offer
    chat = update.effective_chat
    target = user.id if chat is None or chat.type != "private" else chat.id
    what = "" if account == user.id else f" for {account}"
    try:
        await context.bot.send_invoice(
            chat_id=target,
            title=f"{_n(downloads)} downloads",
            description=f"{_n(downloads)} more downloads{what}. Never expire.",
            payload=_payload(account, user.id, downloads, stars),
            provider_token="",  # Stars: no provider
            currency=CURRENCY,
            prices=[LabeledPrice(f"{_n(downloads)} downloads", stars)],
        )
    except TelegramError as e:
        logger.warning("could not send the %s-download invoice: %s", downloads, e)
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
        parsed is not None
        and parsed[1] == q.from_user.id
        and quota.pack(parsed[2]) == (parsed[2], parsed[3])
        and q.currency == CURRENCY
        and q.total_amount == parsed[3]
    )
    if ok and parsed[0] != parsed[1] and q.from_user.id not in ADMIN_IDS:
        ok = await _is_chat_admin(context.bot, parsed[0], q.from_user.id)
    try:
        if ok:
            await q.answer(ok=True)
        else:
            await q.answer(ok=False, error_message=(
                "This offer has changed. Please open /premium again for the current price."))
    except TelegramError as e:
        logger.warning("pre-checkout answer failed: %s", e)


async def _tell_admins(context, text: str) -> None:
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    sp = getattr(msg, "successful_payment", None) if msg else None
    if not sp or not user:
        return
    parsed = _parse_payload(sp.invoice_payload)
    charge = sp.telegram_payment_charge_id
    if sp.currency != CURRENCY or parsed is None or parsed[1] != user.id:
        logger.error("unexpected payment from %s: %s %s (charge %s)",
                     user.id, sp.currency, sp.invoice_payload, charge)
        await _tell_admins(context, f"⚠️ Unexpected payment from <code>{user.id}</code>: "
                                    f"{_html(sp.invoice_payload)}. Charge: <code>{_html(charge)}</code>")
        return
    account, _, downloads, _ = parsed
    try:
        st = quota.add_pack(account, user.id, charge_id=charge, stars=sp.total_amount,
                            downloads=downloads)
    except quota.NotSaved:
        # The charge id is the refund trail: it must reach the log and the owner.
        logger.error("PACK NOT SAVED for %s (payer %s): charge %s, %s Stars, %s downloads",
                     account, user.id, charge, sp.total_amount, downloads)
        await _tell_admins(context, f"⚠️ Payment from <code>{user.id}</code> could not be saved. "
                                    f"Charge: <code>{_html(charge)}</code> ({downloads} downloads "
                                    f"for {account}) — /refund {user.id}.")
        await msg.reply_text(
            "⚠️ Your payment went through, but I couldn't add the downloads just "
            "now. The owner has been told and will fix it — or use /paysupport.")
        return
    logger.info("pack bought: %s downloads for %s by %s, %s Stars", downloads, account,
                user.id, sp.total_amount)
    await _tell_admins(context, f"⭐ <code>{user.id}</code> bought {_n(downloads)} downloads"
                                f"{'' if account == user.id else f' for {account}'} "
                                f"({sp.total_amount} Stars).")
    who = "You now have" if account == user.id else f"<code>{account}</code> now has"
    await msg.reply_text(
        f"✅ <b>{_n(downloads)} downloads added.</b>\n\n"
        f"{who} <b>{_n(max(0, st['remaining']))}</b> downloads left. Thank you for "
        "supporting the bot!",
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
            "Downloads are paid with Telegram Stars. If you were charged but the "
            "downloads weren't added, or you want a refund, send:\n\n"
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
    try:
        st = quota.status(user.id)
        bal = f"{st['used']} used of {st['allowance']}" + (" (approved)" if st["approved"] else "")
    except Exception:  # noqa: BLE001 — the request must still reach the owner
        bal = "unknown"
    note = (
        f"💬 <b>Payment support</b> from <code>{user.id}</code> ({_html(who)})\n"
        f"Downloads: {bal}\n\n"
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
    if len(args) != 1 or not args[0].isdigit():
        await msg.reply_text("Usage: /refund <user_id> (the person who paid)")
        return
    payer = int(args[0])
    charge = quota.last_charge(payer)
    if not charge:
        await msg.reply_text("No refundable purchase for that user.")
        return
    try:
        await context.bot.refund_star_payment(
            user_id=payer, telegram_payment_charge_id=charge["charge_id"])
    except TelegramError as e:
        await msg.reply_text(f"Refund failed: {e}")
        return
    quota.mark_refunded(charge["charge_id"])
    logger.info("admin %s refunded %s Stars to %s", user.id, charge["stars"], payer)
    await msg.reply_text(f"✅ Refunded {charge['stars']} Stars to {payer}; the "
                         f"{charge['downloads']} downloads were taken back from "
                         f"{charge['account_id']}.")


def _target_id(update: Update, context) -> int | None:
    """An id argument (negative for a channel/group), or the replied-to sender."""
    args = getattr(context, "args", None) or []
    if args:
        raw = args[0].strip()
        return int(raw) if raw.lstrip("-").isdigit() and raw != "-" else None
    reply = getattr(update.effective_message, "reply_to_message", None)
    if reply is not None:
        sender_chat = getattr(reply, "sender_chat", None)
        if sender_chat is not None:
            return sender_chat.id
        if getattr(reply, "from_user", None) is not None:
            return reply.from_user.id
    return None


async def cmd_approvefree(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: /approvefree <id> — lifetime free, never counted."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or user.id not in ADMIN_IDS:
        return
    target = _target_id(update, context)
    if target is None:
        ids = quota.approved_accounts()
        listing = ", ".join(f"<code>{i}</code>" for i in ids) if ids else "none yet"
        await msg.reply_text(
            "Usage: <code>/approvefree &lt;user or channel id&gt;</code> (or reply to "
            "their message). Undo with /revokefree.\n\n"
            f"Approved: {listing}", parse_mode=ParseMode.HTML)
        return
    quota.set_approved(target, True, by=user.id)
    logger.info("admin %s approved %s as free", user.id, target)
    await msg.reply_text(f"♾ <code>{target}</code> is approved: unlimited downloads, never "
                         "counted. Undo with /revokefree.", parse_mode=ParseMode.HTML)


async def cmd_revokefree(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: /revokefree <id> — back to the normal allowance."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or user.id not in ADMIN_IDS:
        return
    target = _target_id(update, context)
    if target is None:
        await msg.reply_text("Usage: <code>/revokefree &lt;user or channel id&gt;</code> "
                             "(or reply to their message).", parse_mode=ParseMode.HTML)
        return
    quota.set_approved(target, False, by=user.id)
    st = quota.status(target)
    logger.info("admin %s revoked free access of %s", user.id, target)
    await msg.reply_text(
        f"↩️ <code>{target}</code> is back on the normal allowance: "
        f"{_n(max(0, st['remaining']))} of {_n(st['allowance'])} downloads left "
        "(downloads made while approved were not counted).", parse_mode=ParseMode.HTML)
