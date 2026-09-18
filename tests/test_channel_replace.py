"""
In a channel the link post should become the media, so no human has to delete it.

Strategy chain (best first): edit the post in place -> post + delete the link ->
post and leave the link. Each step needs a different admin right, so all three
are exercised here.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from telegram.error import BadRequest, Forbidden

import bot.handlers.download as hd
from bot.services.downloader import DownloadResult


def _result(tmp_path: Path, **kw) -> DownloadResult:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v" * 2048)
    base = dict(
        success=True, files=[f], primary=f, title="Clip", mode="video",
        quality="720", file_size=2048, is_video=True,
    )
    base.update(kw)
    return DownloadResult(**base)


class Recorder:
    """Bot double that records edit/send/delete and can fail chosen calls."""

    def __init__(self, fail_delete=False, fail_send=False):
        self.fail_delete = fail_delete
        self.fail_send = fail_send
        self.edited, self.sent, self.deleted = [], [], []

    async def edit_message_media(self, chat_id=None, message_id=None, media=None, **kw):
        # Recorded so a test can assert the link post is never edited.
        self.edited.append((chat_id, message_id, media))
        return True

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        if self.fail_delete:
            raise Forbidden("not enough rights to delete a message")
        self.deleted.append((chat_id, message_id))
        return True

    async def send_chat_action(self, *a, **k):
        return True

    async def send_video(self, chat_id, video=None, **kw):
        if self.fail_send:
            raise BadRequest("upload failed")
        self.sent.append(("video", chat_id))
        return True

    async def send_document(self, chat_id, document=None, **kw):
        if self.fail_send:
            raise BadRequest("upload failed")
        self.sent.append(("document", chat_id))
        return True

    async def send_photo(self, chat_id, photo=None, **kw):
        if self.fail_send:
            raise BadRequest("upload failed")
        self.sent.append(("photo", chat_id))
        return True

    async def send_audio(self, chat_id, audio=None, **kw):
        if self.fail_send:
            raise BadRequest("upload failed")
        self.sent.append(("audio", chat_id))
        return True


class TestReplaceChannelPost:
    """Contract: post the media as a new message, then delete the link post."""

    async def _run(self, tmp_path, bot, result=None):
        class Ctx:
            pass
        ctx = Ctx()
        ctx.bot = bot
        res = result or _result(tmp_path)
        return await hd._replace_channel_post(ctx, -100123, 77, res.primary, res)

    async def test_sends_media_then_deletes_the_link(self, tmp_path):
        bot = Recorder()
        outcome = await self._run(tmp_path, bot)
        assert outcome == "replaced"
        assert bot.sent, "media must be posted as a new message"
        assert bot.deleted == [(-100123, 77)], "the link post must be removed"

    async def test_never_edits_the_link_post(self, tmp_path):
        """Editing the original post is explicitly not wanted."""
        bot = Recorder()
        await self._run(tmp_path, bot)
        assert not bot.edited, "the link post must not be edited"

    async def test_media_is_sent_before_the_link_is_deleted(self, tmp_path):
        """
        Order matters: if the upload fails the link must survive, so a failed
        download never destroys what somebody posted.
        """
        bot = Recorder(fail_send=True)
        with pytest.raises(BadRequest):
            await self._run(tmp_path, bot)
        assert not bot.deleted, "link must survive a failed upload"

    async def test_without_delete_rights_media_still_arrives(self, tmp_path):
        bot = Recorder(fail_delete=True)
        outcome = await self._run(tmp_path, bot)
        assert outcome == "sent"
        assert bot.sent, "media must still be delivered"

    @pytest.mark.parametrize(
        ("kw", "expected_call"),
        [
            (dict(is_video=True), "video"),
            (dict(is_video=False, is_audio=True), "audio"),
            (dict(is_video=False, is_image=True), "photo"),
        ],
    )
    async def test_send_type_matches_the_download(self, tmp_path, kw, expected_call):
        bot = Recorder()
        await self._run(tmp_path, bot, _result(tmp_path, **kw))
        assert bot.sent[0][0] == expected_call


class TestChannelFlowUsesReplacement:
    async def test_channel_replaces_and_group_does_not(self, fx, monkeypatch, tmp_path):
        """
        Telegram does not allow editing another user's message outside channels,
        so groups must keep the plain send behaviour.
        """
        res = _result(tmp_path)

        async def fake_download(**kw):
            return res

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: None)

        calls = []

        async def spy(context, chat_id, mid, path, result):
            calls.append(mid)
            return "replaced"

        monkeypatch.setattr(hd, "_replace_channel_post", spy)

        for chat_type, expect in (("channel", True), ("supergroup", False)):
            calls.clear()
            fx.chat.type = chat_type
            msg = fx.msg("https://youtu.be/x")
            await hd.auto_download_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
            assert bool(calls) is expect, f"{chat_type}: replacement used={bool(calls)}"

    async def test_flag_off_keeps_the_old_behaviour(self, fx, monkeypatch, tmp_path):
        res = _result(tmp_path)

        async def fake_download(**kw):
            return res

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: None)
        monkeypatch.setattr(hd, "CHANNEL_REPLACE_LINK", False)

        calls = []

        async def spy(*a, **k):
            calls.append(1)
            return "replaced"

        monkeypatch.setattr(hd, "_replace_channel_post", spy)
        fx.chat.type = "channel"
        msg = fx.msg("https://youtu.be/x")
        await hd.auto_download_flow(fx.update(msg), fx.ctx, "https://youtu.be/x")
        assert not calls


class TestAudioLinksInChannels:
    """Music links should arrive as an audio message — in channels only."""

    async def _mode_for(self, fx, monkeypatch, url, chat_type):
        seen = {}

        async def fake_download(**kw):
            seen.update(kw)
            return DownloadResult(success=False, error="stop here", mode=kw["mode"])

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        fx.chat.type = chat_type
        msg = fx.msg(url)
        await hd.auto_download_flow(fx.update(msg), fx.ctx, url)
        return seen.get("mode")

    @pytest.mark.parametrize("url", [
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://soundcloud.com/artist/track",
        "https://artist.bandcamp.com/track/song",
        "https://www.mixcloud.com/user/show/",
        "https://podcasts.apple.com/us/podcast/x/id1",
    ])
    async def test_music_links_become_audio_in_channels(self, fx, monkeypatch, url):
        assert await self._mode_for(fx, monkeypatch, url, "channel") == "audio"

    async def test_plain_youtube_stays_video_in_channels(self, fx, monkeypatch):
        """music.youtube.com matches "youtu" too — the plain site must not flip."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert await self._mode_for(fx, monkeypatch, url, "channel") == "video"

    @pytest.mark.parametrize("chat_type", ["supergroup", "group"])
    async def test_groups_keep_previous_behaviour(self, fx, monkeypatch, chat_type):
        """Scoped to channels on purpose — groups were not asked to change."""
        url = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
        assert await self._mode_for(fx, monkeypatch, url, chat_type) == "video"


class TestDmFlowUnchangedButFaster:
    """DM keeps every option; it just stops asking for a redundant confirm tap."""

    def _session(self, **kw):
        from bot.services.session import DownloadSession, sessions
        s = DownloadSession(session_id="sp", user_id=42, chat_id=1,
                            url="https://youtu.be/x", title="t", platform="p", **kw)
        sessions.put(s)
        return s

    async def test_quality_tap_starts_download(self, fx, monkeypatch):
        from tests.conftest import FakeCallbackQuery
        started = []

        async def spy(query, context, session):
            started.append((session.mode, session.quality))

        monkeypatch.setattr(hd, "execute_download", spy)
        s = self._session(mode="video")
        q = FakeCallbackQuery(data="quality:sp:1080")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert started == [("video", "1080")]

    async def test_mode_screen_still_offered(self, fx, monkeypatch):
        """The wizard itself must still exist — only the confirm step is gone."""
        from tests.conftest import FakeCallbackQuery
        self._session()
        q = FakeCallbackQuery(data="mode:sp:video")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert q.edits and "quality" in q.edits[0][0].lower()

    async def test_legacy_go_button_still_works(self, fx, monkeypatch):
        """Old messages still carrying a Download button must not break."""
        from tests.conftest import FakeCallbackQuery
        started = []

        async def spy(query, context, session):
            started.append(session.session_id)

        monkeypatch.setattr(hd, "execute_download", spy)
        self._session(mode="video", quality="720")
        q = FakeCallbackQuery(data="go:sp")
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert started == ["sp"]
