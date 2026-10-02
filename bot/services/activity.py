"""
When did a user last do something? Lets background maintenance (re-rolling a
bot-walled WARP IP) run only while nobody is waiting on the bot.
"""

from __future__ import annotations

import time

_last = 0.0


def touch() -> None:
    """Call on every user interaction (message, button, inline query/pick)."""
    global _last
    _last = time.monotonic()


def idle_for() -> float:
    """Seconds since the last user interaction (large if there was none yet)."""
    return time.monotonic() - _last if _last else 1e9
