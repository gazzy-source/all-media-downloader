"""
Lifetime download allowance, paid with Telegram Stars.

Every user gets FREE_DOWNLOADS deliveries, then buys packs from
DOWNLOAD_PACKS. A chat account (negative id: a channel's own posts, or a
group's anonymous admins posting as the group) is free but capped at
CHAT_DAILY_DOWNLOADS per UTC day; a lifetime allowance per chat was a
loophole, since a new chat costs nothing. Admins (ADMIN_IDS) and accounts
the owner approved with /approvefree are never counted and never blocked.

The balance is derived, never stored:

    remaining = FREE_DOWNLOADS + downloads of non-refunded charges - used

`used` only goes up, one per delivered file set. Charges are keyed by
Telegram's charge id. A payment update that arrives twice, or a refund, can
then never leave a wrong balance behind.

Durable in its own SQLite file (data/quota.sqlite3), separate from history
(capped per user and evicted) and from user_prefs (evicted, rewritten whole).
Nothing here is ever evicted.

Reads come from memory. The bot is one process, so the in-memory copy is the
source of truth while it runs and SQLite is written through. Measured on the
VPS (2026-11-23): a SQLite read per check took 0.6 ms median but up to 88 ms,
on the event loop, on every inline keystroke. A count is an fsync (5 ms
median, 90 ms worst), so counts are written on one background thread, in
order. Payments, refunds and approvals are written synchronously: nobody is
told "done" before it is on disk.

Counting happens after a confirmed delivery; the check happens before a
download starts and counts the account's jobs already in progress, so
starting many links at once can't run past the allowance.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

from bot.config import (
    ADMIN_IDS,
    DATA_DIR,
    DOWNLOAD_PACKS,
    FREE_DOWNLOADS,
    CHAT_DAILY_DOWNLOADS,
    PAID_RATE_MULT,
    PAYWALL_ENABLED,
    RATE_LIMIT_PER_HOUR,
)

logger = logging.getLogger(__name__)

ENABLED = PAYWALL_ENABLED
FREE = FREE_DOWNLOADS
CHAT_DAILY = CHAT_DAILY_DOWNLOADS
PACKS: tuple[tuple[int, int], ...] = DOWNLOAD_PACKS  # (downloads, stars)

_PATH: Path = DATA_DIR / "quota.sqlite3"
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
CREATE INDEX IF NOT EXISTS charges_payer ON charges(payer_id, ts);
CREATE TABLE IF NOT EXISTS chat_daily (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    n INTEGER NOT NULL
);
"""


class NotSaved(RuntimeError):
    """A payment went through but could not be recorded."""


class _Ledger:
    """In-memory copy of the database, loaded once."""

    def __init__(self) -> None:
        self.used: dict[int, int] = {}
        self.approved: set[int] = set()
        self.charges: dict[str, dict[str, Any]] = {}  # charge_id -> row
        self.bought: dict[int, int] = {}  # account -> downloads of live charges
        self.daily: dict[int, tuple[str, int]] = {}  # chat account -> (UTC day, count)


_lock = threading.Lock()  # guards _ledger
_ledger: _Ledger | None = None
# One writer thread: counts stay in order and off the event loop.
_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quota")


def _connect() -> sqlite3.Connection:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")  # paid records: survive a power cut
    con.executescript(_SCHEMA)
    return con


def _load() -> _Ledger:
    """The ledger, read from disk on first use. Caller holds _lock."""
    global _ledger
    if _ledger is None:
        led = _Ledger()
        with closing(_connect()) as con:
            for r in con.execute("SELECT id, used, approved FROM accounts"):
                led.used[r["id"]] = r["used"]
                if r["approved"]:
                    led.approved.add(r["id"])
            for r in con.execute("SELECT id, day, n FROM chat_daily"):
                led.daily[r["id"]] = (r["day"], r["n"])
            for r in con.execute("SELECT * FROM charges"):
                led.charges[r["charge_id"]] = dict(r)
                if not r["refunded"]:
                    led.bought[r["account_id"]] = led.bought.get(r["account_id"], 0) + r["downloads"]
        _ledger = led
    return _ledger


_LOAD_RETRY_S = 30.0
_load_failed_at = 0.0


def _ledger_or_none() -> _Ledger | None:
    """
    The ledger, or None while it can't be read. A failed load is retried at
    most every 30 s: retried on every check, each inline keystroke paid a
    blocking connect (busy timeout 10 s) and logged a traceback.
    """
    global _load_failed_at
    if _ledger is None and time.monotonic() - _load_failed_at < _LOAD_RETRY_S:
        return None
    try:
        with _lock:
            return _load()
    except sqlite3.Error:
        _load_failed_at = time.monotonic()
        logger.exception("quota: could not load the ledger (retrying in 30 s)")
        return None


def is_chat(account: int) -> bool:
    """Negative ids are chats (channels, groups), positive ones people."""
    return account < 0


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _today_count(led: _Ledger, account: int) -> int:
    day, n = led.daily.get(account, ("", 0))
    return n if day == _today() else 0


def pack(downloads: int) -> tuple[int, int] | None:
    """The configured (downloads, stars) pack of that size, if offered."""
    return next((p for p in PACKS if p[0] == downloads), None)


# ------------------------------------------------------------------ reads
def status(account: int) -> dict[str, Any]:
    led = _ledger_or_none() or _Ledger()
    used, bought = led.used.get(account, 0), led.bought.get(account, 0)
    allowance = FREE + bought
    return {
        "used": used,
        "bought": bought,
        "allowance": allowance,
        "remaining": allowance - used,
        "approved": account in led.approved,
        "admin": account in ADMIN_IDS,
        "chat": is_chat(account),
        "today": _today_count(led, account),
        "daily": CHAT_DAILY,
    }


def is_exempt(account: int) -> bool:
    """Lifetime free: an admin, or approved by the owner."""
    if account in ADMIN_IDS:
        return True
    led = _ledger_or_none()
    return led is not None and account in led.approved


def can_download(account: int, active: int = 0) -> bool:
    """
    Whether a new download may start, given `active` downloads of this
    account already in progress (not yet counted). An unreadable ledger lets
    downloads through: a broken disk must not lock every user out.
    """
    if not ENABLED or account in ADMIN_IDS:
        return True
    led = _ledger_or_none()
    if led is None:
        return True
    if account in led.approved:
        return True
    if is_chat(account):
        return _today_count(led, account) + active < CHAT_DAILY
    return FREE + led.bought.get(account, 0) - led.used.get(account, 0) - active > 0


def is_paid(account: int) -> bool:
    """Has a pack that was not refunded (queue priority, higher hourly limit)."""
    led = _ledger_or_none()
    return led is not None and led.bought.get(account, 0) > 0


def hourly_limit(account: int) -> int:
    return RATE_LIMIT_PER_HOUR * (PAID_RATE_MULT if is_paid(account) else 1)


# ----------------------------------------------------------------- writes
def _persist_used(account: int, used: int) -> None:
    try:
        with closing(_connect()) as con, con:
            con.execute(
                "INSERT INTO accounts(id, used, updated) VALUES(?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET used = MAX(used, excluded.used),"
                " updated = excluded.updated", (account, used, time.time()))
    except sqlite3.Error:
        logger.exception("quota: could not save the count for %s", account)


def count(account: int | None):
    """
    One delivered download. Called after the file reached the chat; never
    raises (the user already has the file). Exempt accounts are not counted.
    The balance changes at once; the write happens in the background.
    """
    if account is None or account in ADMIN_IDS:
        return None
    with _lock:
        try:
            led = _load()
        except sqlite3.Error:
            logger.exception("quota: could not count a download for %s", account)
            return None
        if account in led.approved:
            return None
        if is_chat(account):
            day = _today()
            n = _today_count(led, account) + 1
            led.daily[account] = (day, n)
            return _WRITER.submit(_persist_daily, account, day, n)
        led.used[account] = used = led.used.get(account, 0) + 1
    return _WRITER.submit(_persist_used, account, used)


def _persist_daily(account: int, day: str, n: int) -> None:
    try:
        with closing(_connect()) as con, con:
            con.execute(
                "INSERT INTO chat_daily(id, day, n) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE"
                " SET n = CASE WHEN day = excluded.day THEN MAX(n, excluded.n)"
                " ELSE excluded.n END, day = excluded.day", (account, day, n))
    except sqlite3.Error:
        logger.exception("quota: could not save today's count for %s", account)


def flush() -> None:
    """Wait for queued counts to reach the disk (shutdown, tests)."""
    _WRITER.submit(lambda: None).result(timeout=30)


def add_pack(account: int, payer: int, *, charge_id: str, stars: int,
             downloads: int) -> dict[str, Any]:
    """
    Record a paid pack. Idempotent per charge id. Raises NotSaved when it
    could not be written, so nobody is told "added" when nothing was saved.
    """
    row = {"charge_id": charge_id, "account_id": account, "payer_id": payer,
           "stars": stars, "downloads": downloads, "ts": time.time(), "refunded": 0}
    with _lock:
        try:
            led = _load()
            if charge_id not in led.charges:
                with closing(_connect()) as con, con:
                    con.execute(
                        "INSERT OR IGNORE INTO charges(charge_id, account_id, payer_id, stars,"
                        " downloads, ts) VALUES(?,?,?,?,?,?)",
                        (charge_id, account, payer, stars, downloads, row["ts"]))
                led.charges[charge_id] = row
                led.bought[account] = led.bought.get(account, 0) + downloads
        except sqlite3.Error as e:
            raise NotSaved(charge_id) from e
    return status(account)


def last_charge(payer: int) -> dict[str, Any] | None:
    """The payer's latest charge that was not refunded."""
    led = _ledger_or_none()
    if led is None:
        return None
    live = [c for c in led.charges.values() if c["payer_id"] == payer and not c["refunded"]]
    return dict(max(live, key=lambda c: c["ts"])) if live else None


def mark_refunded(charge_id: str) -> None:
    with _lock:
        led = _load()
        c = led.charges.get(charge_id)
        if c is None or c["refunded"]:
            return
        with closing(_connect()) as con, con:
            con.execute("UPDATE charges SET refunded = 1 WHERE charge_id = ?", (charge_id,))
        c["refunded"] = 1
        led.bought[c["account_id"]] = led.bought.get(c["account_id"], 0) - c["downloads"]


def set_approved(account: int, approved: bool, by: int | None = None) -> None:
    with _lock:
        led = _load()
        with closing(_connect()) as con, con:
            con.execute(
                "INSERT INTO accounts(id, approved, approved_by, approved_at, updated)"
                " VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                " approved = excluded.approved, approved_by = excluded.approved_by,"
                " approved_at = excluded.approved_at, updated = excluded.updated",
                (account, 1 if approved else 0, by, time.time() if approved else None,
                 time.time()))
        (led.approved.add if approved else led.approved.discard)(account)


def approved_accounts() -> list[int]:
    led = _ledger_or_none()
    return sorted(led.approved) if led else []


def _reset_for_tests(path: Path, *, enabled: bool = True, free: int = 100,
                     packs: tuple[tuple[int, int], ...] = ((100, 100), (10000, 10000))) -> None:
    global _PATH, ENABLED, FREE, PACKS, _ledger, _load_failed_at
    flush()
    with _lock:
        _PATH, ENABLED, FREE, PACKS, _ledger = path, enabled, free, packs, None
        _load_failed_at = 0.0
