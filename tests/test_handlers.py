"""Integration tests for Telegram handler flows — all download/extract calls mocked."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import bot.handlers.download as hd
from bot.config import MAX_FILE_SIZE_BYTES
from bot.keyboards.menus import after_download_keyboard
from bot.services.downloader import DownloadResult
from bot.services.session import DownloadSession, sessions
from tests.conftest import FakeCallbackQuery, FakeMessage


def _ok_result(tmp_path: Path, name="video.mp4", size=1024, is_video=True) -> DownloadResult:
    f = tmp_path / name
    f.write_bytes(b"v" * size)
    return DownloadResult(
        success=True, files=[f], primary=f, title="Test Video",
        mode="video", quality="720", file_size=size,
        is_video=is_video,
    )


@pytest.fixture
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))


# ---------------------------------------------------------------------------
# handle_message routing
# ---------------------------------------------------------------------------
class TestHandleMessage:
    async def test_valid_url_starts_wizard(self, fx, monkeypatch, no_rate_limit):
        called = {}
        async def fake_flow(update, context, url):
            called["url"] = url
        monkeypatch.setattr(hd, "start_url_flow", fake_flow)
        msg = fx.msg("check this https://youtu.be/abc123")
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert called["url"] == "https://youtu.be/abc123"

    async def test_invalid_url_private_gets_hint(self, fx, no_rate_limit):
        msg = fx.msg("hello there, no links")
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert msg.replies and "valid media URL" in msg.replies[0][0]

    async def test_group_auto_downloads(self, fx, monkeypatch, no_rate_limit):
        fx.chat.type = "supergroup"
        results = []
        async def fake_auto(update, context, url):
            results.append(url)
        monkeypatch.setattr(hd, "auto_download_flow", fake_auto)
        msg = fx.msg("https://youtu.be/1 https://youtu.be/2 https://youtu.be/3")
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert results == ["https://youtu.be/1", "https://youtu.be/2", "https://youtu.be/3"]

    async def test_group_caps_at_five_links(self, fx, monkeypatch, no_rate_limit):
        fx.chat.type = "supergroup"
        results = []
        async def fake_auto(update, context, url):
            results.append(url)
        monkeypatch.setattr(hd, "auto_download_flow", fake_auto)
        urls = " ".join(f"https://youtu.be/{i}" for i in range(8))
        msg = fx.msg(urls)
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert len(results) == 5
        assert msg.replies, "must inform about capping"

    async def test_group_quiet_on_non_url(self, fx, no_rate_limit):
        fx.chat.type = "supergroup"
        msg = fx.msg("just chatting")
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert not msg.replies, "groups must stay quiet"

    async def test_channel_post_without_user_still_processed(self, fx, monkeypatch):
        fx.chat.type = "channel"
        called = {}

        async def fake_auto(update, context, url):
            called["url"] = url

        monkeypatch.setattr(hd, "auto_download_flow", fake_auto)
        msg = FakeMessage(text="https://youtu.be/chan", chat=fx.chat)  # no from_user
        upd = fx.update(msg)
        upd.effective_user = None
        await hd.handle_message(upd, fx.ctx)
        assert called["url"] == "https://youtu.be/chan"

    async def test_caption_urls_processed(self, fx, monkeypatch, no_rate_limit):
        called = {}
        async def fake_flow(update, context, url):
            called["url"] = url
        monkeypatch.setattr(hd, "start_url_flow", fake_flow)
        msg = fx.msg("")
        msg.caption = "look: https://tiktok.com/@u/video/9"
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert called["url"] == "https://tiktok.com/@u/video/9"

    async def test_menu_labels_routed(self, fx, no_rate_limit):
        msg = fx.msg("❓ Help")
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert msg.replies and "Help" in msg.replies[0][0]

    async def test_rate_limited_user_blocked(self, fx, monkeypatch):
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (False, 77))
        msg = fx.msg("https://youtu.be/abc")
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert msg.replies and "Rate limit" in msg.replies[0][0]

    async def test_admin_bypasses_rate_limit(self, fx, monkeypatch):
        monkeypatch.setattr(hd, "ADMIN_IDS", {42})
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (False, 77))
        called = {}

        async def fake_flow(update, context, url):
            called["url"] = url

        monkeypatch.setattr(hd, "start_url_flow", fake_flow)
        msg = fx.msg("https://youtu.be/abc")
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert called["url"] == "https://youtu.be/abc"


# ---------------------------------------------------------------------------
# auto_download_flow
# ---------------------------------------------------------------------------
class TestAutoDownloadFlow:
    async def test_success_edits_sends_records_cleans(self, fx, monkeypatch, tmp_path, no_rate_limit):
        res = _ok_result(tmp_path)
        recorded = {}
        monkeypatch.setattr(hd, "record_download",
                            lambda *a, **k: recorded.update({"args": a, **k}))
        monkeypatch.setattr(hd.download_manager, "download",
                            asyncio.coroutine(lambda *a, **k: res) if hasattr(asyncio, "coroutine")
                            else None)
        # python 3.11+: use simple async stub
        async def fake_download(**kw):
            return res
        monkeypatch.setattr(hd.download_manager, "download", fake_download)

        msg = fx.msg("https://youtu.be/abc")
        await hd.auto_download_flow(fx.update(msg), fx.ctx, "https://youtu.be/abc")

        assert recorded["args"][6] is True  # 7th positional arg = success flag
        assert msg.replies, "status message must be posted"
        assert any("Downloading" in r[0] for r in msg.replies)
        assert fx.ctx.bot.chat_actions, "chat action must be sent"
        # status child message: edited to upload notice, then cleaned up
        status = msg.children[0]
        assert any("Uploading" in e[0] for e in status.edits)
        assert status.deleted, "status message must be deleted after send"

    async def test_failure_edits_error_and_records(self, fx, monkeypatch, tmp_path, no_rate_limit):
        async def fake_download(**kw):
            return DownloadResult(success=False, error="The download timed out", mode="video")
        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        recorded = {}
        monkeypatch.setattr(hd, "record_download",
                            lambda *a, **k: recorded.update({"success": a[6], "error": k.get("error")}))
        msg = fx.msg("https://youtu.be/abc")
        await hd.auto_download_flow(fx.update(msg), fx.ctx, "https://youtu.be/abc")
        assert recorded["success"] is False
        assert "timed out" in (recorded["error"] or "")

    async def test_exception_caught_and_reported(self, fx, monkeypatch, no_rate_limit):
        async def boom(**kw):
            raise RuntimeError("disk exploded")
        monkeypatch.setattr(hd.download_manager, "download", boom)
        recorded = {}
        monkeypatch.setattr(hd, "record_download",
                            lambda *a, **k: recorded.update({"error": k.get("error")}))
        msg = fx.msg("https://youtu.be/abc")
        await hd.auto_download_flow(fx.update(msg), fx.ctx, "https://youtu.be/abc")
        assert "disk exploded" in recorded["error"]
        assert msg.replies, "user must see an error message"

    async def test_oversize_file_rejected_and_cleaned(self, fx, monkeypatch, tmp_path, no_rate_limit):
        big = _ok_result(tmp_path, size=MAX_FILE_SIZE_BYTES + 10)
        cleaned = []
        monkeypatch.setattr(hd.download_manager, "download", make_fake(big))
        monkeypatch.setattr(hd.download_manager, "cleanup_result_files",
                            lambda r: cleaned.append(1))
        recorded = {}
        monkeypatch.setattr(hd, "record_download",
                            lambda *a, **k: recorded.update({"success": a[6]}))
        msg = fx.msg("https://youtu.be/abc")
        await hd.auto_download_flow(fx.update(msg), fx.ctx, "https://youtu.be/abc")
        assert recorded["success"] is False
        assert cleaned == [1]
        assert not fx.ctx.bot.sent, "oversize media must not be uploaded"

    async def test_channel_post_gets_no_caption_or_buttons(self, fx, monkeypatch, tmp_path, no_rate_limit):
        fx.chat.type = "channel"
        res = _ok_result(tmp_path)
        monkeypatch.setattr(hd.download_manager, "download", make_fake(res))
        msg = FakeMessage(text="https://youtu.be/abc", chat=fx.chat, from_user=None)
        upd = fx.update(msg)
        await hd.auto_download_flow(upd, fx.ctx, "https://youtu.be/abc")
        # Media is posted as a NEW message and the link post is deleted;
        # the original post is never edited.
        vid_calls = [s for s in fx.ctx.bot.sent if s[0] == "send_video"]
        assert vid_calls, "channel must still receive the media"
        kw = vid_calls[0][2]
        assert not kw.get("caption"), "channel media stays caption-free"
        assert not kw.get("reply_markup"), "channel media carries no buttons"
        assert not fx.ctx.bot.media_edits, "the link post must not be edited"
        assert (fx.chat.id, msg.message_id) in fx.ctx.bot.deletes, (
            "the link post must be deleted"
        )


def make_fake(result):
    async def fake(**kw):
        return result
    return fake


# ---------------------------------------------------------------------------
# handle_callback
# ---------------------------------------------------------------------------
class TestHandleCallback:
    async def test_answers_exactly_once(self, fx):
        """Regression: callback must be answered exactly once (BadRequest otherwise)."""
        q = FakeCallbackQuery(data="new")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert len(q.answers) == 1, f"expected 1 answer, got {len(q.answers)}"

    async def test_expired_again_token_messages_instead_of_answer_alert(self, fx):
        """Regression: expired-token path used a second query.answer(show_alert)."""
        q = FakeCallbackQuery(data="again:deadbeefdead")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert len(q.answers) == 1
        assert q.message.replies and "expired" in q.message.replies[0][0].lower()

    async def test_legacy_raw_url_token_still_works(self, fx, monkeypatch, no_rate_limit):
        called = {}
        async def fake_flow(update, context, url):
            called["url"] = url
        monkeypatch.setattr(hd, "start_url_flow", fake_flow)
        q = FakeCallbackQuery(data="again:https://youtu.be/raw")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert called["url"] == "https://youtu.be/raw"

    async def test_valid_token_triggers_flow(self, fx, monkeypatch, no_rate_limit):
        from bot.services.url_tokens import put_url
        called = {}
        async def fake_flow(update, context, url):
            called["url"] = url
        monkeypatch.setattr(hd, "start_url_flow", fake_flow)
        tok = put_url("https://youtu.be/tok", 42)
        q = FakeCallbackQuery(data=f"again:{tok}")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert called["url"] == "https://youtu.be/tok"

    async def test_unknown_session_reports_expired(self, fx):
        """
        Assert the contract, not the wording: the user must learn the session
        is unusable AND what to do about it. Sessions also vanish on restart,
        not just on timeout, so the message says so.
        """
        q = FakeCallbackQuery(data="mode:ghostsid:video")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert q.edits, "an expired session must still answer the user"
        text = q.edits[0][0].lower()
        assert "no longer active" in text or "expired" in text
        assert "send the link again" in text, "must say how to recover"
        assert "restart" in text, "restarts also clear sessions — say so"

    async def test_foreign_session_rejected_with_message(self, fx):
        s = DownloadSession(session_id="sX", user_id=999, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        q = FakeCallbackQuery(data="mode:sX:video")  # from user 42
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert len(q.answers) == 1
        assert q.message.replies and "isn't your" in q.message.replies[0][0]

    async def test_admin_can_touch_foreign_session(self, fx, monkeypatch):
        monkeypatch.setattr(hd, "ADMIN_IDS", {42})
        s = DownloadSession(session_id="sA", user_id=999, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        q = FakeCallbackQuery(data="mode:sA:video")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert s.mode == "video"

    async def test_cancel_removes_session(self, fx):
        s = DownloadSession(session_id="sC", user_id=42, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        q = FakeCallbackQuery(data="cancel:sC")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert sessions.get("sC") is None
        assert q.edits and "cancelled" in q.edits[0][0].lower()

    async def test_mode_video_asks_quality(self, fx):
        s = DownloadSession(session_id="sQ", user_id=42, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        q = FakeCallbackQuery(data="mode:sQ:video")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert s.mode == "video"
        assert q.edits and "quality" in q.edits[0][0].lower()

    async def test_mode_audio_asks_format(self, fx):
        s = DownloadSession(session_id="sF", user_id=42, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        q = FakeCallbackQuery(data="mode:sF:audio")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert q.edits and "audio format" in q.edits[0][0].lower()

    async def test_aformat_starts_download_immediately(self, fx, monkeypatch):
        """Picking the format is the last decision — no extra confirm tap."""
        started = []
        async def spy(query, context, session):
            started.append(session.audio_format)
        monkeypatch.setattr(hd, "execute_download", spy)
        s = DownloadSession(session_id="sM", user_id=42, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        q = FakeCallbackQuery(data="aformat:sM:opus")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert s.audio_format == "opus" and s.mode == "audio"
        assert started == ["opus"], "download must start on the format tap"

    async def test_imgsize_sets_index(self, fx, monkeypatch):
        s = DownloadSession(session_id="sI", user_id=42, chat_id=1,
                            url="https://x.com/1", title="t", platform="p",
                            available_image_sizes=[(1080, 1920), (720, 1280)])
        sessions.put(s)
        started = []
        async def spy(query, context, session):
            started.append(session.image_index)
        monkeypatch.setattr(hd, "execute_download", spy)
        q = FakeCallbackQuery(data="imgsize:sI:1")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert s.image_index == 1 and s.mode == "image"
        assert started == [1], "download must start on the size tap"

    async def test_video_subs_without_subs_skips_lang_picker(self, fx, monkeypatch):
        s = DownloadSession(session_id="sN", user_id=42, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        q = FakeCallbackQuery(data="mode:sN:video_subs")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert q.edits and "quality" in q.edits[0][0].lower()
        started = []
        async def spy(query, context, session):
            started.append(session.quality)
        monkeypatch.setattr(hd, "execute_download", spy)
        q2 = FakeCallbackQuery(data="quality:sN:720")
        q2.message = q.message
        await hd.handle_callback(fx.update(callback_query=q2), fx.ctx)
        assert s.subtitle_lang == "en.*", "must auto-set subtitle lang"
        assert started == ["720"], "download must start without a confirm tap"


# ---------------------------------------------------------------------------
# execute_download (happy path + failures)
# ---------------------------------------------------------------------------
class TestExecuteDownload:
    def _session(self, sid="sG", chat=100):
        return DownloadSession(session_id=sid, user_id=42, chat_id=chat,
                               url="https://youtu.be/x", title="V", platform="YouTube")

    async def _run(self, fx, session, result, monkeypatch):
        recorded = {}
        monkeypatch.setattr(hd, "record_download",
                            lambda *a, **k: recorded.update({"success": a[6], "size": k.get("file_size")}))
        monkeypatch.setattr(hd.download_manager, "download", make_fake(result))
        q = FakeCallbackQuery(data=f"go:{session.session_id}")
        q.message = FakeMessage(chat=FakeChat2(session.chat_id))
        await hd.execute_download(q, fx.ctx, session)
        return q, recorded

    async def test_success_sends_media_and_cleans(self, fx, monkeypatch, tmp_path):
        s = self._session()
        sessions.put(s)
        res = _ok_result(tmp_path)
        cleaned = []
        monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: cleaned.append(1))
        q, recorded = await self._run(fx, s, res, monkeypatch)
        assert recorded["success"] is True and recorded["size"] == 1024
        assert any(c[0] == "send_video" for c in fx.ctx.bot.sent)
        assert cleaned == [1]
        assert sessions.get(s.session_id) is None, "session removed after finish"
        assert q.message.deleted, "status message cleaned from chat"

    async def test_media_error_falls_back_to_document(self, fx, monkeypatch, tmp_path):
        """send_video failing with generic TelegramError must fall back to send_document."""
        s = self._session()
        sessions.put(s)
        res = _ok_result(tmp_path)
        sent = []

        async def send_video_fail(chat_id, video=None, **kw):
            from telegram.error import TelegramError
            raise TelegramError("bad video")

        async def send_doc_ok(chat_id, document=None, **kw):
            sent.append(("doc", chat_id))
            return FakeMessage()

        fx.ctx.bot.send_video = send_video_fail
        fx.ctx.bot.send_document = send_doc_ok
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(hd.download_manager, "download", make_fake(res))
        q = FakeCallbackQuery(data=f"go:{s.session_id}")
        q.message = FakeMessage(chat=FakeChat2(s.chat_id))
        await hd.execute_download(q, fx.ctx, s)
        assert sent == [("doc", s.chat_id)]

    async def test_download_failure_edits_error(self, fx, monkeypatch):
        s = self._session()
        sessions.put(s)
        res = DownloadResult(success=False, error="Sign in to confirm", mode="video")
        recorded = {}
        monkeypatch.setattr(hd, "record_download",
                            lambda *a, **k: recorded.update({"success": a[6]}))
        monkeypatch.setattr(hd.download_manager, "download", make_fake(res))
        q = FakeCallbackQuery(data=f"go:{s.session_id}")
        q.message = FakeMessage(chat=FakeChat2(s.chat_id))
        await hd.execute_download(q, fx.ctx, s)
        assert recorded["success"] is False
        assert q.edits and any(
            "failed" in e[0].lower() for e in q.edits
        ), f"failure edit expected, got: {[e[0] for e in q.edits]}"
        assert sessions.get(s.session_id) is None

    async def test_crash_sends_failure_message(self, fx, monkeypatch):
        s = self._session()
        sessions.put(s)
        async def boom(**kw):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(hd.download_manager, "download", boom)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        q = FakeCallbackQuery(data=f"go:{s.session_id}")
        q.message = FakeMessage(chat=FakeChat2(s.chat_id))
        await hd.execute_download(q, fx.ctx, s)
        assert any("kaboom" in s[1][1] for s in fx.ctx.bot.sent if s[0] == "send_message")

    async def test_oversize_rejected_with_tip(self, fx, monkeypatch, tmp_path):
        s = self._session()
        sessions.put(s)
        res = _ok_result(tmp_path, size=MAX_FILE_SIZE_BYTES + 5)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        q, recorded = await self._run(fx, s, res, monkeypatch)
        assert not fx.ctx.bot.sent or all(
            c[0] != "send_video" for c in fx.ctx.bot.sent
        ), "oversize must not upload"
        assert q.edits and any(
            "exceeds" in e[0].lower() or "limit" in e[0].lower() for e in q.edits
        ), f"oversize edit expected, got: {[e[0] for e in q.edits]}"


class FakeChat2:
    def __init__(self, chat_id):
        self.id = chat_id
        self.type = "private"


# ---------------------------------------------------------------------------
# _send_media retry logic
# ---------------------------------------------------------------------------
class TestSendMedia:
    async def test_retries_on_timeout_then_succeeds(self, fx, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x" * 100)
        res = DownloadResult(success=True, files=[f], primary=f, is_video=True)
        calls = {"n": 0}

        async def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                from telegram.error import TimedOut
                raise TimedOut()
            return FakeMessage()

        monkeypatch.setattr(fx.ctx.bot, "send_video", flaky)
        monkeypatch.setattr(hd, "_sleep", _nosleep)
        await hd._send_media(fx.ctx, 1, f, res, "cap", attempts=3)
        assert calls["n"] == 3

    async def test_flood_wait_sleeps_then_succeeds(self, fx, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x" * 100)
        res = DownloadResult(success=True, files=[f], primary=f, is_video=True)
        calls = {"n": 0}
        waits = []

        async def flood(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                from telegram.error import RetryAfter
                raise RetryAfter(4)
            return FakeMessage()

        async def fake_sleep(sec):
            waits.append(sec)

        monkeypatch.setattr(fx.ctx.bot, "send_video", flood)
        monkeypatch.setattr(hd, "_sleep", fake_sleep)
        await hd._send_media(fx.ctx, 1, f, res, "cap", attempts=2)
        assert calls["n"] == 2 and waits, "must wait out flood control"

    async def test_gives_up_after_attempts(self, fx, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x" * 100)
        res = DownloadResult(success=True, files=[f], primary=f, is_video=True)
        calls = {"n": 0}

        async def always_timeout(*a, **kw):
            calls["n"] += 1
            from telegram.error import TimedOut
            raise TimedOut()

        monkeypatch.setattr(fx.ctx.bot, "send_video", always_timeout)
        monkeypatch.setattr(hd, "_sleep", _nosleep)
        with pytest.raises(Exception):
            await hd._send_media(fx.ctx, 1, f, res, "cap", attempts=2)
        assert calls["n"] == 2

    async def test_long_caption_truncated(self, fx, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x" * 100)
        res = DownloadResult(success=True, files=[f], primary=f, is_video=True)
        captured = {}

        async def send_video(chat_id, video=None, caption=None, **kw):
            captured["caption"] = caption
            return FakeMessage()

        monkeypatch.setattr(fx.ctx.bot, "send_video", send_video)
        await hd._send_media(fx.ctx, 1, f, res, "x" * 2000, attempts=1)
        assert len(captured["caption"]) <= 1024


async def _nosleep(sec):
    return None


# ---------------------------------------------------------------------------
# start_url_flow
# ---------------------------------------------------------------------------
class TestStartUrlFlow:
    async def test_live_stream_rejected(self, fx, monkeypatch, no_rate_limit):
        from bot.services.downloader import MediaInfo
        info = MediaInfo(url="u", title="LIVE", platform="YouTube", is_live=True)

        class FakeDM:
            async def extract_info(self, url):
                return info

        monkeypatch.setattr(hd.download_manager, "extract_info", FakeDM().extract_info)
        msg = fx.msg("https://youtu.be/live")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/live")
        # handler replies with an Analyzing… status, then edits it with the live notice
        assert msg.children, "status message expected"
        status = msg.children[0]
        assert any("live" in e[0].lower() for e in status.edits), \
            f"live notice expected, got: {[e[0] for e in status.edits]}"

    async def test_extract_error_shows_friendly_tip(self, fx, monkeypatch, no_rate_limit):
        async def boom(url):
            raise RuntimeError("HTTP Error 403: Forbidden")
        monkeypatch.setattr(hd.download_manager, "extract_info", boom)
        msg = fx.msg("https://youtu.be/x")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
        status = msg.children[0]
        err_edits = [e for e in status.edits if "could not read" in e[0].lower()]
        assert err_edits, f"friendly error expected, got: {[e[0] for e in status.edits]}"
        # The edit must carry NO reply keyboard: editMessageText accepts an
        # inline keyboard only, and attaching the persistent one made Telegram
        # answer BadRequest("Inline keyboard expected"), which surfaced to the
        # user as "Something went wrong" on every unreadable link. The old
        # assertion here (`is not None`) encoded that bug.
        from telegram import ReplyKeyboardMarkup

        assert not isinstance(
            err_edits[0][1].get("reply_markup"), ReplyKeyboardMarkup
        )

    async def test_success_creates_session_and_shows_modes(self, fx, monkeypatch, no_rate_limit):
        from bot.services.downloader import MediaInfo
        info = MediaInfo(url="u", title="My Video", platform="YouTube",
                         has_video=True, has_audio=True, available_heights=[720, 1080])

        async def fake_extract(url):
            return info

        monkeypatch.setattr(hd.download_manager, "extract_info", fake_extract)
        msg = fx.msg("https://youtu.be/x")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
        # a session was stored keyed to this user
        s = sessions.get_for_user(42)
        assert s is not None and s.title == "My Video"
        # the wizard status message was edited with mode buttons
        status = msg.children[0]
        assert status.edits and "Select download type" in status.edits[-1][0]
        assert status.edits[-1][1].get("reply_markup") is not None


# ---------------------------------------------------------------------------
# after_download_keyboard token safety
# ---------------------------------------------------------------------------
class TestAfterDownloadKeyboard:
    def test_callback_data_under_64_bytes_with_long_url(self):
        from telegram import InlineKeyboardMarkup
        kb = after_download_keyboard("https://" + "a" * 500 + ".com/x", user_id=1)
        assert isinstance(kb, InlineKeyboardMarkup)
        for row in kb.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64


class TestAnalysingHeartbeat:
    """
    "Getting title, formats & options" never changed while extract_info ran,
    so any wait read as frozen. A ticker fixes that — but it must stop on every
    path or it overwrites whatever the wizard writes next.
    """

    async def _run(self, fx, monkeypatch, extract):
        import asyncio as _a
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        monkeypatch.setattr(hd.download_manager, "extract_info", extract)
        msg = fx.msg("https://youtu.be/x")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
        before = len(msg.children[0].edits) if msg.children else 0
        await _a.sleep(0.35)          # a live ticker would fire again here
        after = len(msg.children[0].edits) if msg.children else 0
        return msg, before, after

    async def test_heartbeat_stops_after_success(self, fx, monkeypatch):
        from bot.services.downloader import MediaInfo

        async def ok(url):
            return MediaInfo(url=url, title="T", platform="YouTube",
                             has_video=True, available_heights=[720])

        monkeypatch.setattr(hd, "_analysing_heartbeat_interval", 0.05, raising=False)
        _, before, after = await self._run(fx, monkeypatch, ok)
        assert before == after, "ticker kept writing after analysis finished"

    async def test_heartbeat_stops_after_failure(self, fx, monkeypatch):
        async def boom(url):
            raise RuntimeError("Video unavailable")

        _, before, after = await self._run(fx, monkeypatch, boom)
        assert before == after, "ticker kept writing after the error was shown"

    async def test_error_message_survives_the_ticker(self, fx, monkeypatch):
        """The last thing on screen must be the error, not a stale tick."""
        async def boom(url):
            raise RuntimeError("Video unavailable")

        msg, *_ = await self._run(fx, monkeypatch, boom)
        last = msg.children[0].edits[-1][0] if msg.children[0].edits else ""
        assert "Could not read this link" in last, last[:120]


class TestAnalysisIsTimeBounded:
    """
    Production showed 'Reading formats · 254s'. yt-dlp's socket_timeout only
    bounds one socket op; retries across strategies can run for minutes and the
    user just waits. The analysis phase now has a hard ceiling.
    """

    async def test_slow_link_gives_up_and_says_so(self, fx, monkeypatch):
        import asyncio as _a
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        monkeypatch.setattr(hd, "EXTRACT_TIMEOUT", 0.2)

        async def never(url):
            await _a.sleep(30)

        monkeypatch.setattr(hd.download_manager, "extract_info", never)
        msg = fx.msg("https://youtu.be/x")
        await _a.wait_for(
            hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/x"),
            timeout=5,
        )
        shown = " ".join(t for t, _ in msg.replies)
        for child in msg.children:
            shown += " " + " ".join(t for t, _ in child.edits)
        assert "Took too long" in shown, shown[:200]
        assert "0.2s" in shown or "Gave up" in shown

    async def test_fast_link_is_unaffected(self, fx, monkeypatch):
        from bot.services.downloader import MediaInfo
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        monkeypatch.setattr(hd, "EXTRACT_TIMEOUT", 30)

        async def quick(url):
            return MediaInfo(url=url, title="Fast", platform="YouTube",
                             has_video=True, available_heights=[720])

        monkeypatch.setattr(hd.download_manager, "extract_info", quick)
        msg = fx.msg("https://youtu.be/x")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
        shown = " ".join(t for c in msg.children for t, _ in c.edits)
        assert "Took too long" not in shown
        assert "Fast" in shown, "the wizard must still appear"
