"""Inline mode: @bot <link> → placeholder → download → swap in the media."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram import (
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InputMediaAudio,
    InputMediaVideo,
)

import bot.handlers.download as hd
import bot.handlers.inline as inl
from bot.services import inline_cache
from bot.services.downloader import DownloadResult


class InlineBot:
    """Bot double for the inline flow."""

    username = "mediabot"
    id = 777

    def __init__(self):
        self.photos, self.deleted, self.captions, self.media_edits = [], [], [], []
        self.markups = []
        self.statuses = []
        self._mid = 100

    async def send_photo(self, chat_id, photo=None, **kw):
        self._mid += 1
        self.photos.append((chat_id, kw))
        return SimpleNamespace(message_id=self._mid,
                               photo=[SimpleNamespace(file_id=f"PH{self._mid}")])

    async def delete_message(self, chat_id=None, message_id=None):
        self.deleted.append((chat_id, message_id))

    async def edit_message_caption(self, inline_message_id=None, caption=None, **kw):
        self.captions.append(caption)
        self.markups.append(kw.get("reply_markup"))

    async def edit_message_reply_markup(self, inline_message_id=None, reply_markup=None, **kw):
        self.markups.append(reply_markup)
        self.statuses.append(reply_markup.inline_keyboard[0][0].text)

    async def edit_message_media(self, inline_message_id=None, media=None, **kw):
        self.media_edits.append((inline_message_id, media))


class FakeInlineQuery:
    _n = 0

    def __init__(self, query, user_id=42, offset=""):
        FakeInlineQuery._n += 1
        self.id = f"q{FakeInlineQuery._n}"
        self.query = query
        self.offset = offset
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, results, **kw):
        self.answers.append((results, kw))


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    inline_cache._reset_for_tests(tmp_path / "inline_cache.json")
    monkeypatch.setattr(inl, "STORAGE_CHAT_ID", None)
    monkeypatch.setattr(inl, "ADMIN_IDS", {1})
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


def _update(**kw):
    base = dict(inline_query=None, chosen_inline_result=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestInlineQuery:
    async def test_no_link_answers_with_hint_only(self, ctx):
        q = FakeInlineQuery("h")  # too short to search
        await inl.handle_inline_query(_update(inline_query=q), ctx)
        results, kw = q.answers[0]
        assert results == [] and kw["button"] is not None

    async def test_link_offers_video_and_audio_placeholders(self, ctx):
        q = FakeInlineQuery("https://youtu.be/abc")
        await inl.handle_inline_query(_update(inline_query=q), ctx)
        results, _ = q.answers[0]
        assert [r.id[:3] for r in results] == ["vp:", "ap:"]
        assert all(isinstance(r, InlineQueryResultCachedPhoto) for r in results)
        # A keyboard is mandatory, or Telegram never reports an inline_message_id.
        assert all(r.reply_markup is not None for r in results)
        assert "Cool Clip" in results[0].title
        # Placeholders were uploaded to the admin's DM once, then deleted there.
        assert [c for c, _ in ctx.bot.photos] == [1, 1]
        assert len(ctx.bot.deleted) == 2

    async def test_placeholders_uploaded_only_once(self, ctx):
        for _ in range(3):
            await inl.handle_inline_query(_update(inline_query=FakeInlineQuery("https://youtu.be/abc")), ctx)
        assert len(ctx.bot.photos) == 2

    async def test_cached_link_answers_with_the_finished_file(self, ctx):
        inline_cache.put("https://youtu.be/abc", inl._key("video"), file_id="VID1", kind="video", title="T")
        q = FakeInlineQuery("https://youtu.be/abc")
        await inl.handle_inline_query(_update(inline_query=q), ctx)
        results, _ = q.answers[0]
        assert isinstance(results[0], InlineQueryResultCachedVideo)
        assert results[0].video_file_id == "VID1" and results[0].id.startswith("vc:")

    async def test_private_link_gets_no_results(self, ctx, monkeypatch):
        def refuse(url):
            raise inl.UnsafeURLError("private")
        monkeypatch.setattr(inl, "check_public_url", refuse)
        q = FakeInlineQuery("http://127.0.0.1:9123/health")
        await inl.handle_inline_query(_update(inline_query=q), ctx)
        assert q.answers[0][0] == []


def _chosen(result_id, query="https://youtu.be/abc", imid="IMID"):
    return SimpleNamespace(result_id=result_id, query=query, inline_message_id=imid,
                           from_user=SimpleNamespace(id=42))


def _result(tmp_path, **kw):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v" * 2048)
    base = dict(success=True, files=[f], primary=f, title="Cool Clip", mode="video",
                file_size=2048, is_video=True, actual_height=720)
    base.update(kw)
    return DownloadResult(**base)


class TestChosenResult:
    async def test_download_upload_and_swap(self, ctx, monkeypatch, tmp_path):
        res = _result(tmp_path)
        cleaned, uploads = [], []

        async def fake_download(**kw):
            await kw["progress_cb"](50, "⬇ 50%")
            return res

        async def fake_send(context, chat_id, path, result, caption="", silent=False, **kw):
            uploads.append((chat_id, silent))
            return SimpleNamespace(message_id=9, video=SimpleNamespace(file_id="NEWVID"),
                                   audio=None, document=None, photo=None)

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: cleaned.append(r))
        monkeypatch.setattr(hd, "_send_media", fake_send)
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ctx)

        assert uploads == [(1, True)], "uploaded silently to the storage chat"
        imid, media = ctx.bot.media_edits[0]
        assert imid == "IMID" and isinstance(media, InputMediaVideo) and media.media == "NEWVID"
        assert inline_cache.get("https://youtu.be/abc", inl._key("video"))["file_id"] == "NEWVID"
        assert (1, 9) in ctx.bot.deleted, "storage copy removed from the admin DM"
        assert cleaned == [res]

    async def test_audio_choice_swaps_in_audio(self, ctx, monkeypatch, tmp_path):
        res = _result(tmp_path, is_video=False, is_audio=True, mode="audio")
        modes = []

        async def fake_download(**kw):
            modes.append(kw["mode"])
            return res

        async def fake_send(*a, **k):
            return SimpleNamespace(message_id=9, video=None, document=None, photo=None,
                                   audio=SimpleNamespace(file_id="AUD"))

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        monkeypatch.setattr(hd, "_send_media", fake_send)
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("ap:x")), ctx)
        assert modes == ["audio"]
        assert isinstance(ctx.bot.media_edits[0][1], InputMediaAudio)

    async def test_cached_choice_needs_no_work(self, ctx, monkeypatch):
        monkeypatch.setattr(inl.download_manager, "download",
                            lambda **k: pytest.fail("must not download"))
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vc:x")), ctx)
        assert ctx.bot.captions == [] and ctx.bot.media_edits == []

    async def test_too_big_offers_the_bot_instead(self, ctx, monkeypatch, tmp_path):
        monkeypatch.setattr(inl, "MAX_FILE_SIZE_BYTES", 1000)

        async def fake_download(**kw):
            return _result(tmp_path)

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        monkeypatch.setattr(hd, "_send_media", lambda *a, **k: pytest.fail("must not upload"))
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ctx)
        assert "Open bot" in ctx.bot.captions[-1] and ctx.bot.media_edits == []
        assert ctx.bot.markups[-1].inline_keyboard[0][0].url.startswith("https://t.me/mediabot?start=dl_")

    async def test_failure_is_shown_in_the_message(self, ctx, monkeypatch):
        async def fake_download(**kw):
            return DownloadResult(success=False, error="Video unavailable", mode="video")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ctx)
        assert "Video unavailable" in ctx.bot.captions[-1]

    async def test_rate_limited(self, ctx, monkeypatch):
        monkeypatch.setattr(inl.rate_limiter, "allow", lambda uid: (False, 60))
        monkeypatch.setattr(inl.download_manager, "download",
                            lambda **k: pytest.fail("must not download"))
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ctx)
        assert "Rate limit" in ctx.bot.captions[-1]

    async def test_private_link_refused(self, ctx, monkeypatch):
        def refuse(url):
            raise inl.UnsafeURLError("private")
        monkeypatch.setattr(inl, "check_public_url", refuse)
        monkeypatch.setattr(inl.download_manager, "download",
                            lambda **k: pytest.fail("must not download"))
        await inl.handle_chosen_inline_result(
            _update(chosen_inline_result=_chosen("vp:x", query="http://10.0.0.5/")), ctx)
        assert "private" in ctx.bot.captions[-1]


class TestDeepLinkAndGroups:
    async def test_open_bot_deep_link_resumes_the_wizard(self, fx, monkeypatch):
        import bot.handlers.start as start
        from bot.services.url_tokens import put_url

        tok = put_url("https://youtu.be/big", fx.user.id)
        seen = {}

        async def fake_flow(update, context, url):
            seen["url"] = url

        monkeypatch.setattr(hd, "start_url_flow", fake_flow)
        fx.ctx.args = [f"dl_{tok}"]
        await start.cmd_start(fx.update(fx.msg("/start")), fx.ctx)
        assert seen["url"] == "https://youtu.be/big"

    async def test_group_ignores_messages_sent_via_our_inline_mode(self, fx, monkeypatch):
        monkeypatch.setattr(hd, "auto_download_flow",
                            lambda *a, **k: pytest.fail("must not auto-download"))
        fx.chat.type = "supergroup"
        fx.ctx.bot.id = 777
        msg = fx.msg("https://youtu.be/abc")
        msg.via_bot = SimpleNamespace(id=777)
        await hd.handle_message(fx.update(msg), fx.ctx)


def test_result_ids_fit_telegram_limit():
    assert len(inl._result_id("vp", "https://example.com/" + "x" * 500)) <= 64


class TestNoDuplicateDownloads:
    async def test_placeholder_button_does_not_open_the_bot(self, ctx):
        q = FakeInlineQuery("https://youtu.be/abc")
        await inl.handle_inline_query(_update(inline_query=q), ctx)
        btn = q.answers[0][0][0].reply_markup.inline_keyboard[0][0]
        assert btn.callback_data == "inl:wait" and btn.url is None

    async def test_progress_keeps_the_wait_button(self, ctx, monkeypatch, tmp_path):
        async def fake_download(**kw):
            await kw["progress_cb"](40, "x")
            return DownloadResult(success=False, error="nope", mode="video")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ctx)
        during = [m.inline_keyboard[0][0] for m in ctx.bot.markups[:-1]]
        assert all(b.callback_data == "inl:wait" for b in during)
        assert ctx.bot.markups[-1].inline_keyboard[0][0].url  # failure offers the bot

    async def test_finishing_stage_is_never_throttled(self, ctx, monkeypatch, tmp_path):
        async def fake_download(**kw):
            await kw["progress_cb"](7, "early")
            await kw["progress_cb"](100, "Finishing")  # within 3s of the last edit
            return DownloadResult(success=False, error="nope", mode="video")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("vp:x")), ctx)
        assert any("Finishing" in (c or "") for c in ctx.bot.captions)

    async def test_inline_audio_skips_the_mp3_reencode(self, ctx, monkeypatch):
        seen = {}

        async def fake_download(**kw):
            seen.update(kw)
            return DownloadResult(success=False, error="nope", mode="audio")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        await inl.handle_chosen_inline_result(_update(chosen_inline_result=_chosen("ap:x")), ctx)
        assert seen["audio_format"] == "m4a"

    async def test_wait_button_just_reassures(self, fx):
        from tests.conftest import FakeCallbackQuery

        q = FakeCallbackQuery(data="inl:wait")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert "replace this card" in q.answers[0][0]


def test_suite_never_touches_real_data():
    """Running the tests on the server used to write into its data/."""
    import os
    from pathlib import Path

    import bot.config as cfg
    import bot.services.history as history
    import bot.services.inflight as inflight

    real = (Path(cfg.BASE_DIR) / "data").resolve()
    for p in (cfg.DATA_DIR, history._HISTORY_FILE, inflight._PATH, inline_cache._PATH):
        assert real not in Path(p).resolve().parents and Path(p).resolve() != real
    assert not os.path.isfile(cfg.COOKIES_FILE or ""), "tests must run without cookies"
