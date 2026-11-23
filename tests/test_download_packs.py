"""
Download allowance: 100 free per account, then Stars packs; the owner's
/approvefree list is never counted. One count per delivery, wherever it
happened: private chat, group, channel or inline.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.handlers.download as hd
import bot.handlers.inline as inl
import bot.handlers.premium as prem
from bot.services import inline_cache, quota
from bot.services.downloader import DownloadResult
from bot.services.session import DownloadSession
from tests.conftest import FakeCallbackQuery, FakeChat, FakeMessage
from tests.test_inline import FakeInlineQuery, InlineBot, _chosen, _update

OWNER = 1
USER = 42


@pytest.fixture(autouse=True)
def q(tmp_path, monkeypatch):
    quota._reset_for_tests(tmp_path / "quota.sqlite3", enabled=True, free=100,
                           packs=((100, 100), (10000, 10000)))
    monkeypatch.setattr(quota, "ADMIN_IDS", {OWNER})
    monkeypatch.setattr(prem, "ADMIN_IDS", {OWNER})
    hd._last_quota_notice.clear()
    yield quota
    drain()
    quota._reset_for_tests(tmp_path / "unused.sqlite3", enabled=False)


def drain() -> None:
    """Wait for counts queued with consume_soon."""
    quota._WRITER.submit(lambda: None).result(timeout=10)


def use_up(account: int, n: int = 100) -> None:
    for _ in range(n):
        quota.consume(account)


# ------------------------------------------------------------- the ledger
class TestLedger:
    def test_free_allowance_then_blocked(self):
        assert quota.status(USER)["remaining"] == 100
        use_up(USER, 99)
        assert quota.can_download(USER)
        quota.consume(USER)
        st = quota.status(USER)
        assert st["used"] == 100 and st["remaining"] == 0
        assert not quota.can_download(USER)

    def test_owner_and_approved_are_never_counted(self):
        use_up(OWNER, 150)
        assert quota.status(OWNER)["used"] == 0 and quota.can_download(OWNER)
        quota.set_approved(7, True, by=OWNER)
        use_up(7, 150)
        assert quota.status(7)["used"] == 0 and quota.can_download(7)

    def test_revoked_account_resumes_where_it_was(self):
        use_up(8, 40)
        quota.set_approved(8, True, by=OWNER)
        use_up(8, 500)
        quota.set_approved(8, False, by=OWNER)
        assert quota.status(8)["remaining"] == 60

    def test_pack_adds_once_per_charge_and_refund_takes_it_back(self):
        use_up(USER)
        quota.add_pack(USER, USER, charge_id="c1", stars=100, downloads=100)
        quota.add_pack(USER, USER, charge_id="c1", stars=100, downloads=100)  # delivered twice
        assert quota.status(USER)["remaining"] == 100
        assert quota.is_paid(USER)
        quota.add_pack(USER, USER, charge_id="c2", stars=10000, downloads=10000)
        assert quota.status(USER)["remaining"] == 10100
        quota.mark_refunded("c2")
        assert quota.status(USER)["remaining"] == 100

    def test_disabled_paywall_counts_but_never_blocks(self, monkeypatch):
        monkeypatch.setattr(quota, "ENABLED", False)
        use_up(USER, 120)
        assert quota.status(USER)["used"] == 120 and quota.can_download(USER)

    def test_storage_error_does_not_lock_everyone_out(self, monkeypatch):
        import sqlite3

        def broken():
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(quota, "_connect", broken)
        assert quota.can_download(USER)
        quota.consume(USER)  # never raises: the file was already delivered


# ------------------------------------------------------- who is counted
class TestAccount:
    def test_sender_chat_is_the_account(self, fx):
        # Anonymous admins / "send as channel": the stand-in user is shared by
        # everyone, so the chat they posted as is the account.
        fx.chat.type, fx.chat.id = "supergroup", -100200
        anon = fx.msg("x")
        anon.sender_chat = SimpleNamespace(id=-100200)  # anonymous admin
        assert hd._actor_id(fx.update(anon)) == -100200
        fwd = fx.msg("x")
        fwd.sender_chat, fwd.is_automatic_forward = SimpleNamespace(id=-100555), True
        assert hd._actor_id(fx.update(fwd)) == -100555, "the linked channel's post"
        assert hd._actor_id(fx.update(fx.msg("x"))) == fx.user.id

    def test_posting_as_your_own_channel_is_not_a_fresh_allowance(self, fx):
        fx.chat.type, fx.chat.id = "supergroup", -100200
        for throwaway in (-1001, -1002, -1003):
            m = fx.msg("x")
            m.sender_chat = SimpleNamespace(id=throwaway)
            assert hd._actor_id(fx.update(m)) is None, "no account: not the group's either"

    async def test_post_as_own_channel_is_not_downloaded(self, fx, monkeypatch):
        started = []
        monkeypatch.setattr(hd.download_manager, "download", lambda **kw: started.append(1))
        fx.chat.type, fx.chat.id = "supergroup", -100200
        quota.set_approved(-100200, True, by=OWNER)  # the group's approval doesn't extend
        m = fx.msg("https://youtu.be/x")
        m.sender_chat = SimpleNamespace(id=-1001)
        assert not await hd.auto_download_flow(fx.update(m), fx.ctx, "https://youtu.be/x")
        assert started == [] and m.replies == []


def _res(tmp_path, **kw):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v" * 2048)
    base = dict(success=True, files=[f], primary=f, title="Clip", mode="video",
                quality="720", file_size=2048, is_video=True, actual_height=720)
    base.update(kw)
    return DownloadResult(**base)


class _Flow:
    @pytest.fixture
    def flow(self, fx, monkeypatch, tmp_path):
        downloads = []

        async def fake_download(**kw):
            downloads.append(kw["url"])
            return _res(tmp_path)

        async def fake_send(*a, **k):
            return SimpleNamespace(message_id=9, video=SimpleNamespace(file_id="V"),
                                   audio=None, document=None, photo=None)

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: None)
        monkeypatch.setattr(hd, "_send_media", fake_send)
        monkeypatch.setattr(hd, "_replace_channel_post", lambda *a, **k: _done("posted"))
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(hd.inline_cache, "get", lambda url, key: None)
        monkeypatch.setattr(hd.inline_cache, "put", lambda *a, **k: None)
        monkeypatch.setattr(hd, "check_public_url", lambda url: None, raising=False)

        async def public(*a, **k):
            return False

        monkeypatch.setattr(hd, "_refuse_private_url", public)
        allowed = []
        monkeypatch.setattr(hd.rate_limiter, "allow",
                            lambda uid: (allowed.append(uid), (True, 0))[1])
        fx.ctx.bot.username = "mediabot"
        return SimpleNamespace(downloads=downloads, rate_hits=allowed)


async def _done(v):
    return v


def _channel_post(fx):
    """A channel post as Telegram sends it: no from-user, sender_chat = the channel."""
    fx.chat.type, fx.chat.id = "channel", -100300
    msg = fx.msg("https://youtu.be/x")
    msg.sender_chat = SimpleNamespace(id=-100300)
    upd = fx.update(msg)
    msg.from_user = None
    upd.effective_user = None
    return upd


class TestGroupAndChannel(_Flow):
    async def test_group_delivery_is_counted(self, fx, flow):
        fx.chat.type = "supergroup"
        assert await hd.auto_download_flow(fx.update(fx.msg("https://youtu.be/x")), fx.ctx,
                                           "https://youtu.be/x")
        drain()
        assert quota.status(USER)["used"] == 1

    async def test_group_user_out_of_downloads_is_refused_once(self, fx, flow):
        fx.chat.type = "supergroup"
        fx.chat.id = -100200
        use_up(USER)
        msgs = [fx.msg("https://youtu.be/x") for _ in range(3)]
        for m in msgs:
            assert not await hd.auto_download_flow(fx.update(m), fx.ctx, "https://youtu.be/x")
        assert flow.downloads == [] and flow.rate_hits == [], "no download, no hourly slot"
        notices = [r for m in msgs for r in m.replies]
        assert len(notices) == 1, "one notice per window, not one per link"
        text, kw = notices[0]
        assert "100 downloads" in text
        assert kw["reply_markup"].inline_keyboard[0][0].url == "https://t.me/mediabot?start=buy"

    async def test_channel_is_its_own_account(self, fx, flow):
        upd = _channel_post(fx)
        assert await hd.auto_download_flow(upd, fx.ctx, "https://youtu.be/x")
        drain()
        assert quota.status(-100300)["used"] == 1

    async def test_channel_out_of_downloads_names_its_id(self, fx, flow):
        use_up(-100300)
        upd = _channel_post(fx)
        msg = upd.effective_message
        assert not await hd.auto_download_flow(upd, fx.ctx, "https://youtu.be/x")
        assert flow.downloads == []
        text, kw = msg.replies[0]
        assert "/buy -100300" in text
        assert kw["reply_markup"].inline_keyboard[0][0].url.endswith("start=buy_-100300")

    async def test_approved_channel_is_free(self, fx, flow):
        use_up(-100300)
        quota.set_approved(-100300, True, by=OWNER)
        upd = _channel_post(fx)
        assert await hd.auto_download_flow(upd, fx.ctx, "https://youtu.be/x")
        drain()
        assert quota.status(-100300)["used"] == 100, "approved: not counted"

    async def test_failed_download_is_not_counted(self, fx, flow, monkeypatch):
        async def fail(**kw):
            return DownloadResult(success=False, error="boom", mode="video")

        monkeypatch.setattr(hd.download_manager, "download", fail)
        fx.chat.type = "supergroup"
        await hd.auto_download_flow(fx.update(fx.msg("https://youtu.be/x")), fx.ctx,
                                    "https://youtu.be/x")
        drain()
        assert quota.status(USER)["used"] == 0

    async def test_cached_resend_is_counted(self, fx, flow, monkeypatch):
        monkeypatch.setattr(hd.inline_cache, "get",
                            lambda url, key: {"file_id": "F", "kind": "video", "title": "C"})
        monkeypatch.setattr(hd, "_send_cached", lambda *a, **k: _done(object()))
        fx.chat.type = "supergroup"
        assert await hd.auto_download_flow(fx.update(fx.msg("https://youtu.be/x")), fx.ctx,
                                           "https://youtu.be/x")
        drain()
        assert quota.status(USER)["used"] == 1


class TestPrivateChat(_Flow):
    async def test_dm_refusal_offers_both_packs(self, fx, flow):
        use_up(USER)
        msg = fx.msg("https://youtu.be/x")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
        text, kw = msg.replies[0]
        assert "used all <b>100</b>" in text
        buttons = [r[0].callback_data for r in kw["reply_markup"].inline_keyboard]
        assert buttons == ["pay:100", "pay:10000"]
        assert flow.rate_hits == []

    async def test_menu_opened_earlier_stops_at_the_limit(self, fx, flow):
        use_up(USER)
        s = DownloadSession(session_id="s1", user_id=USER, chat_id=100,
                            url="https://youtu.be/x", mode="video", quality="720")
        query = FakeCallbackQuery(data="q:s1:720")
        edits = []

        async def edit(text, **kw):
            edits.append(text)

        query.edit_message_text = edit
        await hd.execute_download(query, fx.ctx, s)
        assert flow.downloads == [] and "used all" in edits[0]
        assert s.started is False, "after buying, the same menu works"


# ------------------------------------------------------------------ inline
@pytest.fixture
def ictx(monkeypatch, tmp_path):
    inline_cache._reset_for_tests(tmp_path / "inline_cache.json")
    monkeypatch.setattr(inl, "STORAGE_CHAT_ID", None)
    monkeypatch.setattr(inl, "ADMIN_IDS", {OWNER})
    monkeypatch.setattr(inl, "INLINE_ENABLED", True)
    monkeypatch.setattr(inl, "check_public_url", lambda url: None)
    monkeypatch.setattr(inl, "_fetch_title", lambda url: "Cool Clip")
    monkeypatch.setattr(inl, "record_download", lambda *a, **k: None)
    monkeypatch.setattr(inl.rate_limiter, "allow", lambda uid: (True, 0))
    monkeypatch.setattr(inl.inline_query_limiter, "allow", lambda uid: (True, 0))
    prefetched = []

    async def fake_prefetch(url):
        prefetched.append(url)

    monkeypatch.setattr(inl, "_prefetch", fake_prefetch)
    return SimpleNamespace(bot=InlineBot(), prefetched=prefetched)


class TestInline:
    async def test_out_of_downloads_gets_the_buy_button_not_tiles(self, ictx):
        use_up(USER)
        query = FakeInlineQuery("https://youtu.be/abc")
        await inl.handle_inline_query(_update(inline_query=query), ictx)
        results, kw = query.answers[0]
        assert results == [] and kw["button"].start_parameter == "buy"
        assert ictx.prefetched == [], "no extraction for a download that can't start"

    async def test_search_too(self, ictx, monkeypatch):
        use_up(USER)
        monkeypatch.setattr(inl, "INLINE_SEARCH_ENABLED", True)
        query = FakeInlineQuery("arijit singh")
        await inl.handle_inline_query(_update(inline_query=query), ictx)
        results, kw = query.answers[0]
        assert results == [] and kw["button"].start_parameter == "buy"

    async def test_chosen_result_is_refused_and_not_downloaded(self, ictx, monkeypatch):
        use_up(USER)
        started = []
        monkeypatch.setattr(inl.download_manager, "download",
                            lambda **kw: started.append(1))
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ictx)
        assert started == [] and "used your 100 downloads" in ictx.bot.captions[-1]

    async def test_fresh_inline_delivery_is_counted(self, ictx, monkeypatch, tmp_path):
        async def fake_send(*a, **k):
            return SimpleNamespace(message_id=9, video=SimpleNamespace(file_id="NEWVID"),
                                   audio=None, document=None, photo=None)

        monkeypatch.setattr(inl.download_manager, "download", lambda **kw: _done(_res(tmp_path)))
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        monkeypatch.setattr(hd, "_send_media", fake_send)
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ictx)
        drain()
        assert quota.status(USER)["used"] == 1

    async def test_cached_inline_swap_is_counted(self, ictx):
        # Was a free bypass: a cached swap recorded nothing at all.
        inline_cache.put("https://youtu.be/abc", inl._key("video"), file_id="V", kind="video")
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ictx)
        assert ictx.bot.media_edits, "swapped in from the cache"
        drain()
        assert quota.status(USER)["used"] == 1


# ---------------------------------------------------------------- payments
class StarsBot:
    username = "mediabot"

    def __init__(self, admin_of=()):
        self.invoices, self.refunds, self.sent = [], [], []
        self.admin_of = set(admin_of)

    async def send_invoice(self, **kw):
        self.invoices.append(kw)

    async def refund_star_payment(self, **kw):
        self.refunds.append(kw)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id, user_id):
        ok = (chat_id, user_id) in self.admin_of
        return SimpleNamespace(status="administrator" if ok else "member")


def _cb(fx, data, chat_type="private"):
    query = FakeCallbackQuery(data=data)
    query.message = FakeMessage(chat=FakeChat(id=-5 if chat_type != "private" else USER,
                                              type=chat_type))
    upd = fx.update(callback_query=query)
    upd.effective_chat = query.message.chat
    return upd


class TestBuying:
    @pytest.mark.parametrize("n,stars", [(100, 100), (10000, 10000)])
    async def test_pack_invoice(self, fx, n, stars):
        bot = StarsBot()
        await prem.handle_premium_callback(_cb(fx, f"pay:{n}", "supergroup"),
                                           SimpleNamespace(bot=bot, args=[]))
        inv = bot.invoices[0]
        assert inv["currency"] == "XTR" and inv["provider_token"] == ""
        assert inv["chat_id"] == USER, "invoices go to the buyer, never into a group"
        assert inv["prices"][0].amount == stars
        assert inv["payload"] == f"dl:{USER}:{USER}:{n}:{stars}"

    async def test_forged_pack_is_refused(self, fx):
        bot = StarsBot()
        await prem.handle_premium_callback(_cb(fx, "pay:5"), SimpleNamespace(bot=bot, args=[]))
        assert bot.invoices == []

    async def test_channel_pack_needs_a_channel_admin(self, fx):
        bot = StarsBot()
        await prem.handle_premium_callback(_cb(fx, "pay:100:-100300"),
                                           SimpleNamespace(bot=bot, args=[]))
        assert bot.invoices == []
        bot = StarsBot(admin_of={(-100300, USER)})
        await prem.handle_premium_callback(_cb(fx, "pay:100:-100300"),
                                           SimpleNamespace(bot=bot, args=[]))
        assert bot.invoices[0]["payload"] == f"dl:-100300:{USER}:100:100"

    @pytest.mark.parametrize("payload,amount,ok", [
        (f"dl:{USER}:{USER}:100:100", 100, True),
        (f"dl:{USER}:{USER}:10000:10000", 10000, True),
        (f"dl:{USER}:99:100:100", 100, False),         # someone else's invoice
        (f"dl:{USER}:{USER}:100:1", 1, False),          # price edited
        (f"dl:{USER}:{USER}:100:100", 1, False),        # amount differs
        (f"dl:{USER}:{USER}:500:500", 500, False),      # pack not offered
        (f"premium:{USER}:30:100", 100, False),         # the retired 30-day plan
    ])
    async def test_precheckout(self, payload, amount, ok):
        answers = []

        async def answer(ok, error_message=None):
            answers.append(ok)

        pcq = SimpleNamespace(invoice_payload=payload, from_user=SimpleNamespace(id=USER),
                              currency="XTR", total_amount=amount, answer=answer)
        await prem.handle_precheckout(SimpleNamespace(pre_checkout_query=pcq),
                                      SimpleNamespace(bot=StarsBot()))
        assert answers == [ok]

    async def test_precheckout_rechecks_channel_admin(self):
        answers = []

        async def answer(ok, error_message=None):
            answers.append(ok)

        pcq = SimpleNamespace(invoice_payload=f"dl:-100300:{USER}:100:100",
                              from_user=SimpleNamespace(id=USER), currency="XTR",
                              total_amount=100, answer=answer)
        await prem.handle_precheckout(SimpleNamespace(pre_checkout_query=pcq),
                                      SimpleNamespace(bot=StarsBot()))
        assert answers == [False], "no longer an admin there"

    def _paid(self, fx, payload, amount, charge="tg_1"):
        msg = fx.msg("")
        msg.successful_payment = SimpleNamespace(
            currency="XTR", total_amount=amount, invoice_payload=payload,
            telegram_payment_charge_id=charge)
        return msg

    async def test_payment_adds_the_pack_once(self, fx):
        use_up(USER)
        bot = StarsBot()
        for _ in range(2):  # Telegram may deliver the update twice
            msg = self._paid(fx, f"dl:{USER}:{USER}:100:100", 100)
            await prem.handle_successful_payment(fx.update(msg), SimpleNamespace(bot=bot))
        assert quota.status(USER)["remaining"] == 100
        assert "100 downloads added" in msg.replies[0][0]
        assert any("bought 100 downloads" in t for _, t in bot.sent), "owner is told"

    async def test_unsaved_payment_is_never_announced(self, fx, monkeypatch):
        def fail(*a, **k):
            raise quota.NotSaved("tg_X")

        monkeypatch.setattr(quota, "add_pack", fail)
        bot = StarsBot()
        msg = self._paid(fx, f"dl:{USER}:{USER}:100:100", 100, charge="tg_X")
        await prem.handle_successful_payment(fx.update(msg), SimpleNamespace(bot=bot))
        assert "couldn't add" in msg.replies[0][0]
        assert any("tg_X" in t for _, t in bot.sent), "the owner has the charge id"

    async def test_refund_returns_stars_and_takes_the_pack_back(self, fx):
        quota.add_pack(USER, USER, charge_id="tg_r", stars=100, downloads=100)
        bot = StarsBot()
        fx.user.id = OWNER
        msg = fx.msg(f"/refund {USER}")
        await prem.cmd_refund(fx.update(msg), SimpleNamespace(bot=bot, args=[str(USER)]))
        assert bot.refunds == [{"user_id": USER, "telegram_payment_charge_id": "tg_r"}]
        assert quota.status(USER)["allowance"] == 100 and not quota.is_paid(USER)

    async def test_refund_is_owner_only(self, fx):
        quota.add_pack(USER, USER, charge_id="tg_r", stars=100, downloads=100)
        bot = StarsBot()
        await prem.cmd_refund(fx.update(fx.msg("/refund 42")), SimpleNamespace(bot=bot, args=["42"]))
        assert bot.refunds == []


class TestOwnerCommands:
    async def test_approvefree_and_revokefree(self, fx):
        fx.user.id = OWNER
        ctx = SimpleNamespace(bot=StarsBot(), args=["555"])
        msg = fx.msg("/approvefree 555")
        await prem.cmd_approvefree(fx.update(msg), ctx)
        assert quota.is_exempt(555) and "approved" in msg.replies[0][0]
        msg = fx.msg("/revokefree 555")
        await prem.cmd_revokefree(fx.update(msg), ctx)
        assert not quota.is_exempt(555)

    async def test_channel_ids_are_accepted(self, fx):
        fx.user.id = OWNER
        await prem.cmd_approvefree(fx.update(fx.msg("/approvefree -100300")),
                                   SimpleNamespace(bot=StarsBot(), args=["-100300"]))
        assert quota.is_exempt(-100300)

    async def test_reply_to_a_message_approves_its_sender(self, fx):
        fx.user.id = OWNER
        msg = fx.msg("/approvefree")
        msg.reply_to_message = SimpleNamespace(sender_chat=None,
                                               from_user=SimpleNamespace(id=777))
        await prem.cmd_approvefree(fx.update(msg), SimpleNamespace(bot=StarsBot(), args=[]))
        assert quota.is_exempt(777)

    async def test_nobody_else_can_approve(self, fx):
        await prem.cmd_approvefree(fx.update(fx.msg("/approvefree 42")),
                                   SimpleNamespace(bot=StarsBot(), args=["42"]))
        assert not quota.is_exempt(42)

    async def test_no_argument_lists_approved(self, fx):
        quota.set_approved(555, True, by=OWNER)
        fx.user.id = OWNER
        msg = fx.msg("/approvefree")
        msg.reply_to_message = None
        await prem.cmd_approvefree(fx.update(msg), SimpleNamespace(bot=StarsBot(), args=[]))
        assert "555" in msg.replies[0][0]


class TestBalanceShown:
    async def test_premium_command_shows_what_is_left(self, fx):
        use_up(USER, 37)
        msg = fx.msg("/premium")
        await prem.cmd_premium(fx.update(msg), SimpleNamespace(bot=StarsBot(), args=[]))
        assert "<b>63</b> downloads left" in msg.replies[0][0]

    async def test_buy_for_a_channel_you_do_not_run(self, fx):
        msg = fx.msg("/buy -100300")
        await prem.cmd_premium(fx.update(msg), SimpleNamespace(bot=StarsBot(), args=["-100300"]))
        assert "Only an admin" in msg.replies[0][0]
