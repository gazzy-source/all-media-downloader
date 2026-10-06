"""Regression tests for the production audit (flows, errors, privacy)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError

import bot.handlers.download as hd
import bot.main as main_mod
from bot.handlers import start
from bot.services.rate_limit import RateLimiter
from bot.utils import redact
from tests.conftest import FakeCallbackQuery, FakeChat


class TestErrors:
    @pytest.mark.parametrize("err", [
        BadRequest("Message is not modified: specified new message content is the same"),
        BadRequest("Query is too old and response timeout expired"),
        NetworkError("Bad Gateway"),
    ])
    async def test_benign_errors_never_tell_the_user_something_broke(self, fx, err):
        msg = fx.msg("x")
        await main_mod.on_error(fx.update(msg), SimpleNamespace(error=err))
        assert not msg.replies

    async def test_real_errors_still_answer_in_private(self, fx, caplog):
        msg = fx.msg("x")
        await main_mod.on_error(fx.update(msg), SimpleNamespace(error=RuntimeError("boom")))
        assert msg.replies and "went wrong" in msg.replies[0][0]
        assert "NoneType: None" not in caplog.text  # the real traceback is logged

    async def test_double_tap_on_cancel_keeps_the_cancelled_text(self, fx):
        q = FakeCallbackQuery(data="cancel:gone-session")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert not q.edits  # no "session no longer active" over "Cancelled"

    async def test_stale_menu_in_a_group_is_a_private_popup(self, fx):
        q = FakeCallbackQuery(data="mode:gone:video")
        q.message.chat = FakeChat(id=-5, type="supergroup")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert not q.edits and any(kw.get("show_alert") for _, kw in q.answers)


class TestPrivacyAndGroups:
    async def test_settings_in_a_group_point_to_private_chat(self, fx):
        fx.chat.type = "supergroup"
        msg = fx.msg("/settings")
        await start.cmd_settings(fx.update(msg), fx.ctx)
        assert "private" in msg.replies[0][0]
        assert "Downloads this hour" not in msg.replies[0][0]

    async def test_expired_deep_link_says_so(self, fx):
        fx.ctx.args = ["dl_doesnotexist"]
        msg = fx.msg("/start dl_doesnotexist")
        await start.cmd_start(fx.update(msg), fx.ctx)
        assert "expired" in msg.replies[0][0]

    async def test_unknown_command_gets_a_hint(self, fx):
        msg = fx.msg("/foo")
        await main_mod.handle_unknown(fx.update(msg), fx.ctx)
        assert "/help" in msg.replies[0][0]

    def test_logs_hash_users_and_drop_query_strings(self):
        assert redact.uid(123) == redact.uid(123) and "123" not in redact.uid(123)
        assert redact.url("https://youtu.be/x?si=TRACK&t=1") == "https://youtu.be/x"
        safe = redact.url("https://user:secret@example.org/media?sig=private")
        assert safe == "https://example.org/media"
        assert "user" not in safe and "secret" not in safe and "private" not in safe


def test_unreadable_link_gives_the_download_back():
    rl = RateLimiter(max_per_hour=2)
    assert rl.allow(1)[0]
    rl.refund(1)
    assert rl.remaining(1) == 2


def test_forum_topic_is_kept_for_the_file():
    topic = SimpleNamespace(is_topic_message=True, message_thread_id=77)
    assert hd._thread_of(topic) == 77
    assert hd._thread_of(SimpleNamespace(is_topic_message=False, message_thread_id=77)) is None


async def test_paysupport_burst_reaches_the_owner_once(fx, monkeypatch, tmp_path):
    import asyncio

    import bot.handlers.premium as prem
    from bot.services import user_prefs

    user_prefs._reset_for_tests(tmp_path / "p.json")
    monkeypatch.setattr(prem, "ADMIN_IDS", {999})
    prem._last_support.clear()
    prem._all_support.clear()
    fx.ctx.args = ["help"]
    await asyncio.gather(*[prem.cmd_paysupport(fx.update(fx.msg("/paysupport help")), fx.ctx)
                           for _ in range(20)])
    assert len(fx.ctx.bot.sent) == 1


def test_history_migrates_once_under_concurrent_first_use(tmp_path, monkeypatch):
    import json
    import threading

    import bot.services.history as hist

    h = tmp_path / "h.json"
    h.write_text(json.dumps({str(u): [{"ts": 1, "url": "u", "title": "t", "platform": "p",
                                       "mode": "video", "quality": None, "success": True,
                                       "file_size": 1, "error": None}] for u in range(300)}))
    monkeypatch.setattr(hist, "_HISTORY_FILE", h)
    monkeypatch.setattr(hist, "_STATS_FILE", tmp_path / "s.json")
    errors = []

    def first_use():
        try:
            hist.get_user_history(1)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=first_use) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(hist.history_users()) == 300
    assert len(hist.get_user_history(1, limit=50)) == 1  # imported exactly once


async def test_unsaved_premium_is_never_announced_as_active(fx, monkeypatch, tmp_path):
    import bot.handlers.premium as prem
    from bot.services import user_prefs

    user_prefs._reset_for_tests(tmp_path / "p.json")
    monkeypatch.setattr(user_prefs, "_save", lambda data: False)  # disk full
    monkeypatch.setattr(prem, "ADMIN_IDS", {999})
    payload = prem._payload(fx.user.id)
    msg = fx.msg("")
    msg.successful_payment = SimpleNamespace(
        invoice_payload=payload, currency="XTR", telegram_payment_charge_id="CH1",
        total_amount=prem.PREMIUM_STARS)
    await prem.handle_successful_payment(fx.update(msg), fx.ctx)
    assert "couldn't activate" in msg.replies[0][0]
    assert not user_prefs.is_premium(fx.user.id)
    assert any("CH1" in a[1][1] for a in fx.ctx.bot.sent)  # the owner has the charge id


async def test_shutdown_stops_jobs_but_keeps_them_for_the_restart_notice(monkeypatch):
    from bot.services import jobs

    monkeypatch.setattr(jobs, "SHUTTING_DOWN", False)
    job = jobs.start("dm:1:2", 1)
    try:
        assert jobs.shutdown_all() >= 1
        assert job.cancelled and jobs.SHUTTING_DOWN
    finally:
        jobs.drop(job)
