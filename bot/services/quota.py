"""
Lifetime download allowance, paid with Telegram Stars.

Every account gets FREE_DOWNLOADS deliveries. An account is a user, or a
channel for the channel's own posts, since a channel post has no person
behind it. After that it buys packs from DOWNLOAD_PACKS. Admins (ADMIN_IDS)
and accounts the owner approved with /approvefree are never counted and
never blocked.

The balance is derived, never stored:

    remaining = FREE_DOWNLOADS + downloads of non-refunded charges - used

`used` only goes up, one per delivered file set. Charges are keyed by
Telegram's charge id. A payment update that arrives twice, or a refund, can
then never leave a wrong balance behind.

Kept in its own SQLite file, separate from history (capped per user and
evicted) and from user_prefs (evicted, rewritten whole). Nothing here is ever
evicted.

Counting happens after a confirmed delivery and the check happens before a
download starts. So an account at its last download that starts several
links at once can go a few over (one per job it can run at the same time).
Those were all delivered; the next pack simply starts from there.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

from bot.config import (
    ADMIN_IDS,
    DATA_DIR,
    DOWNLOAD_PACKS,
    FREE_DOWNLOADS,
    PAID_RATE_MULT,
    PAYWALL_ENABLED,
    RATE_LIMIT_PER_HOUR,
)

logger = logging.getLogger(__name__)

ENABLED = PAYWALL_ENABLED
FREE = FREE_DOWNLOADS
PACKS: tuple[tuple[int, int], ...] = DOWNLOAD_PACKS  # (downloads, stars)

_PATH: Path = DATA_DIR / "quota.sqlite3"
_lock = threading.Lock()
_ready: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0,
    approved_by INTEGER, approved_at REAL,
    updated REAL
);
CREATE TABLE IF NOT EXISTS charges (
    charge_id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    payer_id INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    downloads INTEGER NOT NULL,
    ts REAL NOT NULL,
    refunded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS charges_account ON charges(account_id);
CREATE INDEX IF NOT EXISTS charges_payer ON charges(payer_id, ts);
"""


class NotSaved(RuntimeError):
    """A payment went through but could not be recorded."""


def _connect() -> sqlite3.Connection:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA synchronous=FULL")  # paid records: survive a power cut
    key = str(_PATH)
    if key not in _ready:
        with _lock:
            if key not in _ready:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
                _ready.add(key)
    return con


def pack(downloads: int) -> tuple[int, int] | None:
    """The configured (downloads, stars) pack of that size, if offered."""
    return next((p for p in PACKS if p[0] == downloads), None)


# ------------------------------------------------------------------ reads
def status(account: int) -> dict[str, Any]:
    with closing(_connect()) as con:
        row = con.execute("SELECT used, approved FROM accounts WHERE id = ?",
                          (account,)).fetchone()
        bought = con.execute(
            "SELECT COALESCE(SUM(downloads), 0) FROM charges"
            " WHERE account_id = ? AND refunded = 0", (account,)).fetchone()[0]
    used = int(row["used"]) if row else 0
    allowance = FREE + int(bought)
    return {
        "used": used,
        "bought": int(bought),
        "allowance": allowance,
        "remaining": allowance - used,
        "approved": bool(row and row["approved"]),
        "admin": account in ADMIN_IDS,
    }


def is_exempt(account: int) -> bool:
    """Lifetime free: an admin, or approved by the owner."""
    if account in ADMIN_IDS:
        return True
    try:
        with closing(_connect()) as con:
            row = con.execute("SELECT approved FROM accounts WHERE id = ?",
                              (account,)).fetchone()
    except sqlite3.Error:
        logger.exception("quota: could not read approval for %s", account)
        return False
    return bool(row and row["approved"])


def can_download(account: int) -> bool:
    """
    Whether a new download may start. A storage error lets the download
    through: a broken disk must not lock every user out of the bot.
    """
    if not ENABLED or account in ADMIN_IDS:
        return True
    try:
        st = status(account)
    except sqlite3.Error:
        logger.exception("quota: could not read %s; allowing", account)
        return True
    return st["approved"] or st["remaining"] > 0


def is_paid(account: int) -> bool:
    """Has a pack that was not refunded (queue priority, higher hourly limit)."""
    try:
        with closing(_connect()) as con:
            return con.execute(
                "SELECT 1 FROM charges WHERE account_id = ? AND refunded = 0 LIMIT 1",
                (account,)).fetchone() is not None
    except sqlite3.Error:
        return False


def hourly_limit(account: int) -> int:
    return RATE_LIMIT_PER_HOUR * (PAID_RATE_MULT if is_paid(account) else 1)


# ----------------------------------------------------------------- writes
def consume(account: int | None) -> None:
    """
    One delivered download. Called after the file reached the chat; never
    raises (the user already has the file). Exempt accounts are not counted.
    """
    if account is None:
        return
    try:
        if is_exempt(account):
            return
        with closing(_connect()) as con, con:
            con.execute(
                "INSERT INTO accounts(id, used, updated) VALUES(?, 1, ?)"
                " ON CONFLICT(id) DO UPDATE SET used = used + 1, updated = excluded.updated",
                (account, time.time()))
    except sqlite3.Error:
        logger.exception("quota: could not count a download for %s", account)


# One writer thread: the count is an fsync'd write (tens of ms on the VPS
# boot volume) and must not run on the event loop.
_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quota")


def consume_soon(account: int | None) -> Future:
    """consume() off the event loop; in order with other counts."""
    return _WRITER.submit(consume, account)


def add_pack(account: int, payer: int, *, charge_id: str, stars: int,
             downloads: int) -> dict[str, Any]:
    """
    Record a paid pack. Idempotent per charge id. Raises NotSaved when it
    could not be written, so nobody is told "added" when nothing was saved.
    """
    try:
        with closing(_connect()) as con, con:
            con.execute(
                "INSERT OR IGNORE INTO charges(charge_id, account_id, payer_id, stars,"
                " downloads, ts) VALUES(?,?,?,?,?,?)",
                (charge_id, account, payer, stars, downloads, time.time()))
        return status(account)
    except sqlite3.Error as e:
        raise NotSaved(charge_id) from e


def last_charge(payer: int) -> dict[str, Any] | None:
    """The payer's latest charge that was not refunded."""
    with closing(_connect()) as con:
        row = con.execute(
            "SELECT * FROM charges WHERE payer_id = ? AND refunded = 0"
            " ORDER BY ts DESC LIMIT 1", (payer,)).fetchone()
    return dict(row) if row else None


def mark_refunded(charge_id: str) -> None:
    with closing(_connect()) as con, con:
        con.execute("UPDATE charges SET refunded = 1 WHERE charge_id = ?", (charge_id,))


def set_approved(account: int, approved: bool, by: int | None = None) -> None:
    with closing(_connect()) as con, con:
        con.execute(
            "INSERT INTO accounts(id, approved, approved_by, approved_at, updated)"
            " VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET approved = excluded.approved,"
            " approved_by = excluded.approved_by, approved_at = excluded.approved_at,"
            " updated = excluded.updated",
            (account, 1 if approved else 0, by, time.time() if approved else None, time.time()))


def approved_accounts() -> list[int]:
    with closing(_connect()) as con:
        return [r[0] for r in con.execute(
            "SELECT id FROM accounts WHERE approved = 1 ORDER BY approved_at")]


def _reset_for_tests(path: Path, *, enabled: bool = True, free: int = 100,
                     packs: tuple[tuple[int, int], ...] = ((100, 100), (10000, 10000))) -> None:
    global _PATH, ENABLED, FREE, PACKS
    _PATH, ENABLED, FREE, PACKS = path, enabled, free, packs
