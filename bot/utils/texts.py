"""
User-facing messages that more than one place shows. One wording each, so a
DM, a group and an inline card never describe the same thing three ways.
"""

from __future__ import annotations

import logging
import math

from bot.config import MAX_FILE_SIZE_BYTES, PREMIUM_ENABLED, PREMIUM_RATE_MULT, RATE_LIMIT_PER_HOUR
from bot.utils.helpers import format_size

logger = logging.getLogger(__name__)


def rate_limit_text(retry_s: float, user_id: int | None = None, *, html: bool = True) -> str:
    """Hourly download limit hit: when the next one is allowed, and how to get more."""
    mins = max(1, math.ceil((retry_s or 0) / 60))
    text = f"⏳ Hourly limit reached — next download in {mins} min."
    if PREMIUM_ENABLED and user_id is not None:
        from bot.services import user_prefs

        if not user_prefs.is_premium(user_id):
            more = RATE_LIMIT_PER_HOUR * PREMIUM_RATE_MULT
            text += (f"\n⭐ <b>/premium</b> gives {more} downloads an hour." if html
                     else f" /premium gives {more} downloads an hour.")
    return text


def too_big_text(size: int | None, advice: str) -> str:
    """Over Telegram's upload limit for bots; `advice` is what to do in this place."""
    limit = format_size(MAX_FILE_SIZE_BYTES)
    what = f"This file is <b>{format_size(size)}</b>" if size else "This file is"
    return f"⚠️ {what} — over Telegram's {limit} limit for bots.\n\n{advice}"


def upload_failed_text(error: Exception | str) -> str:
    """Telegram refused the file. The raw error goes to the log, not the chat."""
    raw = str(error)
    logger.warning("Upload to Telegram failed: %s", raw[:300])
    if "timed out" in raw.lower() or "timeout" in raw.lower():
        return ("❌ Couldn't send the file — the upload to Telegram timed out.\n\n"
                "Try again, or pick a lower quality.")
    return "❌ Couldn't send the file to Telegram.\n\nPlease try again."
