"""Personal defaults, queue positions, music metadata, Premium (Telegram Stars)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bot.handlers.download as hd
import bot.handlers.premium as prem
import bot.handlers.start as start
import bot.services.downloader as dl
from bot.services import user_prefs
from bot.services.dl_queue import DownloadQueue
from bot.services.rate_limit import RateLimiter
from bot.services.session import DownloadSession
from tests.conftest import FakeCallbackQuery, FakeChat, FakeMessage


@pytest.fixture(autouse=True)
def prefs(tmp_path):
    user_prefs._reset_for_tests(tmp_path / "prefs.json")
    yield user_prefs


# ---------------------------------------------------------------- defaults
class TestPreferences:
    def test_defaults_are_ask(self):
        assert user_prefs.get(1) == {"mode": "ask", "quality": "ask", "audio": "ask"}

    def test_set_and_reject_forged_values(self):
        assert user_prefs.set_pref(1, "quality", "1080")
        assert not user_prefs.set_pref(1, "quality", "4320")
        assert not user_prefs.set_pref(1, "evil", "x")
        assert user_prefs.get(1)["quality"] == "1080"

    @pytest.mark.parametrize("pref,has_video,has_audio,expected", [
        ({"mode": "video", "quality": "720", "audio": "ask"}, True, True, ("video", "720", "mp3")),
        ({"mode": "audio", "quality": "ask", "audio": "m4a"}, True, True, ("audio", None, "m4a")),
        ({"mode": "video", "quality": "ask", "audio": "ask"}, True, True, None),   # incomplete
        ({"mode": "video", "quality": "720", "audio": "ask"}, False, True, None),  # no video here
        ({"mode": "ask", "quality": "720", "audio": "mp3"}, True, True, None),
    ])
    def test_default_choice(self, pref, has_video, has_audio, expected):
        s = DownloadSession(session_id="s", user_id=1, chat_id=1, url="u",
                            has_video=has_video, has_audio=has_audio)
        assert hd._default_choice(s, pref) == expected

    async def test_saved_defaults_skip_the_wizard(self, fx, monkeypatch):
        user_prefs.set_pref(fx.user.id, "mode", "audio")
        user_prefs.set_pref(fx.user.id, "audio", "mp3")
        info = SimpleNamespace(
            title="Song", platform="YouTube", duration=200, thumbnail=None, uploader="U",
            view_count=1, description="", is_live=False, is_playlist=False, playlist_count=0,
            has_video=True, has_audio=True, has_image=False, has_subtitles=False,
            subtitle_langs=[], available_heights=[720], available_image_sizes=[],
            estimated_sizes={}, min_sizes={}, extractor="youtube",
            summary_html=lambda: "summary")

        async def fake_extract(url):
            return info

        started = {}

        async def fake_execute(query, context, session):
            started.update(mode=session.mode, fmt=session.audio_format)

        monkeypatch.setattr(hd.download_manager, "extract_info", fake_extract)
        monkeypatch.setattr(hd, "execute_download", fake_execute)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        msg = fx.msg("https://youtu.be/x")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
        assert started == {"mode": "audio", "fmt": "mp3"}

    async def test_settings_buttons_save_and_redraw(self, fx):
        q = FakeCallbackQuery(data="pref:quality:720")
        q.message = FakeMessage(chat=FakeChat(id=1))
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert user_prefs.get(fx.user.id)["quality"] == "720"
        text, kw = q.edits[-1]
        assert "720p" in text
        assert any("✅ 720p" == b.text for row in kw["reply_markup"].inline_keyboard for b in row)

    async def test_settings_command_shows_keyboard(self, fx):
        msg = fx.msg("/settings")
        await start.cmd_settings(fx.update(msg), fx.ctx)
        text, kw = msg.replies[0]
        assert "Settings" in text and kw["reply_markup"].inline_keyboard[0][0].callback_data == "pref:mode:video"


# ------------------------------------------------------------------- queue
class TestQueue:
    async def test_positions_are_reported_and_fifo(self):
        q = DownloadQueue(slots=1)
        gate = asyncio.Event()
        order, positions = [], {}

        gate_b = asyncio.Event()

        async def job(name):
            order.append(name)
            if name == "a":
                await gate.wait()
            if name == "b":
                await gate_b.wait()

        async def pos(name, n):
            positions.setdefault(name, []).append(n)

        ta = asyncio.create_task(q.run(lambda: job("a")))
        await asyncio.sleep(0)
        tb = asyncio.create_task(q.run(lambda: job("b"), on_position=lambda n: pos("b", n)))
        tc = asyncio.create_task(q.run(lambda: job("c"), on_position=lambda n: pos("c", n)))
        await asyncio.sleep(0.05)
        assert positions["b"] == [1] and positions["c"] == [2]
        gate.set()
        await asyncio.sleep(0.05)
        assert positions["c"][-1] == 1  # moved up as b started
        gate_b.set()
        await asyncio.gather(ta, tb, tc)
        assert order == ["a", "b", "c"]

    async def test_premium_goes_first(self):
        q = DownloadQueue(slots=1)
        gate = asyncio.Event()
        order = []

        async def job(name):
            order.append(name)
            if name == "busy":
                await gate.wait()

        t0 = asyncio.create_task(q.run(lambda: job("busy")))
        await asyncio.sleep(0)
        t1 = asyncio.create_task(q.run(lambda: job("free")))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(q.run(lambda: job("vip"), priority=True))
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(t0, t1, t2)
        assert order == ["busy", "vip", "free"]

    async def test_cancelled_waiter_leaves_the_line(self):
        q = DownloadQueue(slots=1)
        gate = asyncio.Event()

        async def hold():
            await gate.wait()

        t0 = asyncio.create_task(q.run(hold))
        await asyncio.sleep(0)
        t1 = asyncio.create_task(q.run(hold))
        await asyncio.sleep(0.02)
        assert q.waiting == 1
        t1.cancel()
        await asyncio.sleep(0.02)
        assert q.waiting == 0
        gate.set()
        await t0
        assert q.running == 0

    async def test_failing_job_frees_its_slot(self):
        q = DownloadQueue(slots=1)

        async def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            await q.run(boom)
        assert q.running == 0
        assert await q.run(lambda: asyncio.sleep(0, result="ok")) == "ok"


# ---------------------------------------------------------------- metadata
class TestMusicMetadata:
    @pytest.mark.parametrize("info,artist", [
        ({"artist": "Maan Panu, Feat X", "uploader": "Label"}, "Maan Panu"),
        ({"uploader": "Arijit Singh - Topic"}, "Arijit Singh"),
        ({"channel": "Lofi Girl"}, "Lofi Girl"),
        ({}, None),
    ])
    def test_artist(self, info, artist):
        assert dl._artist_of(info) == artist

    def test_audio_downloads_tag_and_embed_cover(self, monkeypatch, tmp_path):
        import yt_dlp

        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
        seen = {}

        def capture(self, opts, url, title_hint, **kw):
            seen.update(opts)
            raise yt_dlp.utils.DownloadError("stop")

        monkeypatch.setattr(dl.DownloadManager, "_extract_with_format_fallback", capture)
        dl.DownloadManager()._download_sync(
            url="https://youtu.be/x", mode="audio", quality="720", subtitle_lang=None,
            audio_format="mp3", title_hint="t", progress_cb=None, loop=None)
        keys = [p["key"] for p in seen["postprocessors"]]
        assert keys == ["FFmpegThumbnailsConvertor", "FFmpegExtractAudio",
                        "FFmpegMetadata", "EmbedThumbnail"]
        assert seen["writethumbnail"] is True

    async def test_telegram_shows_the_artist(self, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"a" * 10)
        res = dl.DownloadResult(success=True, files=[f], primary=f, title="Song",
                                mode="audio", is_audio=True, artist="Maan Panu")
        seen = {}

        class Bot:
            async def send_chat_action(self, *a, **k):
                pass

            async def send_audio(self, chat_id, **kw):
                seen.update(kw)

        await hd._send_media_once(SimpleNamespace(bot=Bot()), 1, f, res, "cap", f.name)
        assert seen["performer"] == "Maan Panu" and seen["title"] == "Song"


# ----------------------------------------------------------------- premium
class TestPremiumState:
    def test_grant_extends_and_is_idempotent(self):
        until1 = user_prefs.grant_premium(5, 30, charge_id="c1", stars=100)
        assert user_prefs.is_premium(5) and until1 > time.time() + 29 * 86400
        assert user_prefs.grant_premium(5, 30, charge_id="c1", stars=100) == until1  # same update twice
        until2 = user_prefs.grant_premium(5, 30, charge_id="c2", stars=100)
        assert until2 > until1 + 29 * 86400  # stacked on top

    def test_refund_takes_the_days_back(self):
        user_prefs.grant_premium(6, 30, charge_id="c1", stars=100)
        user_prefs.revoke_charge(6, "c1")
        assert not user_prefs.is_premium(6)
        assert user_prefs.last_charge(6) is None

    def test_premium_raises_the_hourly_limit(self, monkeypatch):
        monkeypatch.setattr(user_prefs, "RATE_LIMIT_PER_HOUR", 2)
        monkeypatch.setattr(user_prefs, "PREMIUM_RATE_MULT", 5)
        lim = RateLimiter(2)
        lim.limit_for = user_prefs.hourly_limit
        assert [lim.allow(7)[0] for _ in range(3)] == [True, True, False]
        user_prefs.grant_premium(8, 30, charge_id="c", stars=1)
        assert all(lim.allow(8)[0] for _ in range(10))


class StarsBot:
    username = "mediabot"

    def __init__(self):
        self.invoices, self.refunds = [], []

    async def send_invoice(self, **kw):
        self.invoices.append(kw)

    async def refund_star_payment(self, **kw):
        self.refunds.append(kw)


class TestPremiumPayments:
    async def test_buy_sends_a_stars_invoice_to_the_buyer(self, fx):
        bot = StarsBot()
        q = FakeCallbackQuery(data="prem:buy")
        q.message = FakeMessage(chat=FakeChat(id=-100, type="supergroup"))
        upd = fx.update(callback_query=q)
        upd.effective_chat = q.message.chat
        await prem.handle_premium_callback(upd, SimpleNamespace(bot=bot))
        inv = bot.invoices[0]
        assert inv["currency"] == "XTR" and inv["provider_token"] == ""
        assert inv["chat_id"] == fx.user.id, "invoices go to the buyer, not the group"
        assert inv["prices"][0].amount == prem.PREMIUM_STARS

    @pytest.mark.parametrize("payload_user,amount,ok", [
        ("self", None, True), ("other", None, False), ("self", 1, False)])
    async def test_precheckout_validates(self, payload_user, amount, ok):
        answers = []

        async def answer(ok, error_message=None):
            answers.append(ok)

        buyer = 42
        uid = buyer if payload_user == "self" else 99
        q = SimpleNamespace(
            invoice_payload=f"premium:{uid}:{prem.PREMIUM_DAYS}:{prem.PREMIUM_STARS}",
            from_user=SimpleNamespace(id=buyer), currency="XTR",
            total_amount=amount or prem.PREMIUM_STARS, answer=answer)
        await prem.handle_precheckout(SimpleNamespace(pre_checkout_query=q), SimpleNamespace())
        assert answers == [ok]

    async def test_successful_payment_grants_premium(self, fx):
        msg = fx.msg("")
        msg.successful_payment = SimpleNamespace(
            currency="XTR", total_amount=prem.PREMIUM_STARS,
            invoice_payload=f"premium:{fx.user.id}:{prem.PREMIUM_DAYS}:{prem.PREMIUM_STARS}",
            telegram_payment_charge_id="tg_charge_1")
        await prem.handle_successful_payment(fx.update(msg), fx.ctx)
        assert user_prefs.is_premium(fx.user.id)
        assert "Premium is active" in msg.replies[0][0]

    async def test_admin_refund(self, fx, monkeypatch):
        monkeypatch.setattr(prem, "ADMIN_IDS", {fx.user.id})
        user_prefs.grant_premium(77, 30, charge_id="tg_c", stars=100)
        bot = StarsBot()
        msg = fx.msg("/refund 77")
        await prem.cmd_refund(fx.update(msg), SimpleNamespace(bot=bot, args=["77"]))
        assert bot.refunds == [{"user_id": 77, "telegram_payment_charge_id": "tg_c"}]
        assert not user_prefs.is_premium(77)

    async def test_refund_is_admin_only(self, fx, monkeypatch):
        monkeypatch.setattr(prem, "ADMIN_IDS", set())
        bot = StarsBot()
        await prem.cmd_refund(fx.update(fx.msg("/refund 77")), SimpleNamespace(bot=bot, args=["77"]))
        assert bot.refunds == []

    async def test_paysupport_exists(self, fx):
        msg = fx.msg("/paysupport")
        await prem.cmd_paysupport(fx.update(msg), fx.ctx)
        assert "Payment support" in msg.replies[0][0]
