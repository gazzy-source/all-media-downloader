"""Regression tests for the code-review findings on this session's changes."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

import bot.handlers.download as hd
import bot.handlers.inline as inl
import bot.utils.safe_fetch as sf
from bot.services import inline_cache
from bot.services.downloader import DownloadResult
from tests.conftest import FakeCallbackQuery, FakeChat, FakeMessage


# ------------------------------------------------------------- safe_fetch
class TestGuardInputs:
    @pytest.mark.parametrize("url", [
        "http://[::1/x",                       # malformed IPv6 literal
        "http://a..b.com/",                    # empty idna label
        "https://" + "a" * 70 + ".com/x",      # label over 63 chars
    ])
    def test_odd_urls_are_refused_not_crashing(self, url):
        with pytest.raises(sf.UnsafeURLError):
            sf.check_public_url(url)

    @pytest.mark.parametrize("ip", ["64:ff9b::7f00:1", "::127.0.0.1", "fec0::1"])
    def test_embedded_ipv4_and_site_local_refused(self, monkeypatch, ip):
        monkeypatch.setattr(sf.socket, "getaddrinfo",
                            lambda *a, **k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 80, 0, 0))])
        with pytest.raises(sf.UnsafeURLError):
            sf.check_public_url("http://evil.example/")

    def test_peer_check_blocks_a_rebinding_answer(self, monkeypatch):
        """DNS says public, the socket lands on 127.0.0.1: must still refuse."""
        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # pragma: no cover - must never be reached
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"secret")

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            monkeypatch.setattr(sf, "check_public_url", lambda url: None)  # rebinding won
            with pytest.raises(OSError) as exc:  # URLError wrapping UnsafeURLError
                sf.open_public(f"http://127.0.0.1:{srv.server_port}/", timeout=3)
            assert "non-public" in str(exc.value)
        finally:
            srv.shutdown()


# ----------------------------------------------------------------- inline
class InlineBot:
    username = "mediabot"

    def __init__(self, refuse_media=False):
        self.refuse_media = refuse_media
        self.captions, self.media = [], []

    async def edit_message_caption(self, inline_message_id=None, caption=None, **kw):
        self.captions.append((caption, kw.get("reply_markup")))

    async def edit_message_media(self, inline_message_id=None, media=None, **kw):
        if self.refuse_media:
            raise BadRequest("Wrong file identifier/http url specified")
        self.media.append(media)

    async def delete_message(self, **kw):
        pass


@pytest.fixture
def inline_env(monkeypatch, tmp_path):
    inline_cache._reset_for_tests(tmp_path / "inline_cache.json")
    monkeypatch.setattr(inl, "STORAGE_CHAT_ID", None)
    monkeypatch.setattr(inl, "ADMIN_IDS", {1})
    monkeypatch.setattr(inl, "check_public_url", lambda url: None)
    monkeypatch.setattr(inl, "record_download", lambda *a, **k: None)
    monkeypatch.setattr(inl.rate_limiter, "allow", lambda uid: (True, 0))
    monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
    return tmp_path


def _chosen(rid="vp:x", url="https://youtu.be/abc"):
    return SimpleNamespace(chosen_inline_result=SimpleNamespace(
        result_id=rid, query=url, inline_message_id="IM", from_user=SimpleNamespace(id=42)))


def _ok(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v" * 100)
    return DownloadResult(success=True, files=[f], primary=f, title="T", mode="video",
                          file_size=100, is_video=True)


async def _sender(kind="video", file_id="FID"):
    msg = SimpleNamespace(message_id=9, video=None, audio=None, document=None,
                          animation=None, photo=None)
    setattr(msg, kind, SimpleNamespace(file_id=file_id))
    return msg


class TestInlineSwapFailures:
    async def test_refused_swap_is_reported_and_not_cached(self, inline_env, monkeypatch):
        async def fake_download(**kw):
            return _ok(inline_env)

        async def fake_send(*a, **k):
            return await _sender()

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(hd, "_send_media", fake_send)
        bot = InlineBot(refuse_media=True)
        await inl.handle_chosen_inline_result(_chosen(), SimpleNamespace(bot=bot))
        assert inline_cache.get("https://youtu.be/abc", inl._key("video")) is None
        text, markup = bot.captions[-1]
        assert "refused" in text and markup.inline_keyboard[0][0].url

    async def test_silent_clip_is_kept_as_animation(self, inline_env, monkeypatch):
        from telegram import InputMediaAnimation

        async def fake_download(**kw):
            return _ok(inline_env)

        async def fake_send(*a, **k):
            msg = await _sender("animation", "ANIM")
            msg.document = SimpleNamespace(file_id="ANIM")  # Telegram sets both
            return msg

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(hd, "_send_media", fake_send)
        bot = InlineBot()
        await inl.handle_chosen_inline_result(_chosen(), SimpleNamespace(bot=bot))
        assert isinstance(bot.media[0], InputMediaAnimation)
        assert inline_cache.get("https://youtu.be/abc", inl._key("video"))["kind"] == "animation"

    async def test_long_errors_fit_the_caption_limit(self, inline_env, monkeypatch):
        async def fake_download(**kw):
            return DownloadResult(success=False, error="<x>" * 900, mode="video")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        bot = InlineBot()
        await inl.handle_chosen_inline_result(_chosen(), SimpleNamespace(bot=bot))
        assert len(bot.captions[-1][0]) <= 1024
        assert bot.captions[-1][1].inline_keyboard[0][0].url, "terminal state offers the bot"

    async def test_late_progress_never_overwrites_the_final_caption(self, inline_env, monkeypatch):
        holder = {}

        async def fake_download(**kw):
            holder["cb"] = kw["progress_cb"]
            return DownloadResult(success=False, error="nope", mode="video")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        bot = InlineBot()
        await inl.handle_chosen_inline_result(_chosen(), SimpleNamespace(bot=bot))
        before = len(bot.captions)
        await holder["cb"](100, "Finishing")  # a straggler from yt-dlp's thread
        assert len(bot.captions) == before and "nope" in bot.captions[-1][0]

    async def test_restart_rescues_unfinished_inline_jobs(self, inline_env):
        inline_cache.add_pending("IM1")
        inline_cache.add_pending("IM2")
        inline_cache.drop_pending("IM2")
        bot = InlineBot()
        await inl.rescue_interrupted(SimpleNamespace(bot=bot))
        assert len(bot.captions) == 1 and "Interrupted" in bot.captions[0][0]
        assert inline_cache.drain_pending() == []

    async def test_finished_job_leaves_no_pending_entry(self, inline_env, monkeypatch):
        async def fake_download(**kw):
            return DownloadResult(success=False, error="nope", mode="video")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        await inl.handle_chosen_inline_result(_chosen(), SimpleNamespace(bot=InlineBot()))
        assert inline_cache.drain_pending() == []


# --------------------------------------------------------------- handlers
class TestLinkOnlyPosts:
    @pytest.mark.parametrize("text,expected", [
        ("https://youtu.be/a", True),
        ("https://youtu.be/a\nhttps://youtu.be/b", True),
        ("Great talks: https://youtu.be/a https://youtu.be/b", False),
    ])
    def test_only_pure_link_posts_are_replaceable(self, text, expected):
        assert hd._is_link_only_post(FakeMessage(text=text)) is expected


class TestDownloadAgainInGroups:
    async def test_group_member_can_use_the_groups_button_quietly(self, fx, monkeypatch):
        from bot.services.url_tokens import put_url

        tok = put_url("https://youtu.be/g", -100999)  # a channel/group-owned token
        seen = {}

        async def fake_flow(update, context, url):
            seen["url"] = url

        monkeypatch.setattr(hd, "start_url_flow", fake_flow)
        q = FakeCallbackQuery(data=f"again:{tok}")
        q.message = FakeMessage(chat=FakeChat(id=-100999, type="supergroup"))
        upd = fx.update(callback_query=q)
        upd.effective_chat = q.message.chat
        await hd.handle_callback(upd, fx.ctx)
        assert seen["url"] == "https://youtu.be/g"

    async def test_expired_token_never_posts_publicly(self, fx):
        q = FakeCallbackQuery(data="again:deadbeef0000")
        q.message = FakeMessage(chat=FakeChat(id=-100999, type="supergroup"))
        upd = fx.update(callback_query=q)
        upd.effective_chat = q.message.chat
        await hd.handle_callback(upd, fx.ctx)
        assert q.message.replies == []


def test_runtime_cookie_copy_has_a_fresh_mtime(tmp_path):
    import os

    from bot.utils import cookies

    src = tmp_path / "cookies.txt"
    src.write_text("# Netscape HTTP Cookie File\n" + ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n" * 3)
    old = time.time() - 7200
    os.utime(src, (old, old))
    out = cookies.make_runtime_cookie_copy(src, tmp_path / "runtime.txt")
    assert out is not None and out.stat().st_mtime > time.time() - 60


# ------------------------------------------------------------- downloader
import bot.services.downloader as dl  # noqa: E402
import yt_dlp  # noqa: E402


class TestDownloaderReviewFixes:
    def test_guard_trip_that_was_swallowed_never_ships_a_file(self, monkeypatch, tmp_path):
        """Backstop: a fragment downloader that ate the abort returns 'success'."""
        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
        monkeypatch.setattr(dl, "DOWNLOAD_MAX_BYTES", 10)

        def swallowing(self, opts, url, title_hint, **kw):
            try:
                opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 50})
            except dl.JobAborted:
                pass  # what yt-dlp's fragment loop effectively did
            (tmp_path / "half.mp4").write_bytes(b"x")
            return {"title": "t"}, str(tmp_path / "half.mp4"), "t"

        monkeypatch.setattr(dl.DownloadManager, "_extract_with_format_fallback", swallowing)
        res = dl.DownloadManager()._download_sync(
            url="https://example.com/v", mode="video", quality="720", subtitle_lang=None,
            audio_format="mp3", title_hint="t", progress_cb=None, loop=None)
        assert res.success is False and "Telegram" in res.error

    @pytest.mark.parametrize("exc", [dl.JobRefused("Live streams can't be downloaded"),
                                     dl.JobAborted("File is larger than max-filesize")])
    def test_reuse_never_retries_a_refusal_or_limit(self, monkeypatch, tmp_path, exc):
        class Y:
            def __init__(self, p): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def process_ie_result(self, info, download=True): raise exc

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", Y)
        dl._META_CACHE.clear()
        dl._meta_cache_put("https://youtu.be/r", {"id": "r", "formats": [{"format_id": "18"}]})
        with pytest.raises(type(exc)):
            dl.DownloadManager()._download_from_analysis(
                {"outtmpl": str(tmp_path / "t")}, "https://youtu.be/r", "h", "b")
        dl._META_CACHE.clear()

    def test_private_url_refusal_keeps_its_own_message(self):
        msg = dl.DownloadManager._friendly_error(f"ERROR: {dl.PRIVATE_URL_ERROR}")
        assert msg == dl.PRIVATE_URL_ERROR

    def test_unresolvable_host_is_not_called_private(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)

        def unresolvable(url):
            raise dl.UnresolvableURLError("cannot resolve")

        monkeypatch.setattr(dl, "check_public_url", unresolvable)
        seen = {}

        def capture(self, opts, url, title_hint, **kw):
            seen["ran"] = True
            raise yt_dlp.utils.DownloadError("Unable to download webpage: Name or service not known")

        monkeypatch.setattr(dl.DownloadManager, "_extract_with_format_fallback", capture)
        res = dl.DownloadManager()._download_sync(
            url="https://typo-domain.example/v", mode="video", quality="720",
            subtitle_lang=None, audio_format="mp3", title_hint="t", progress_cb=None, loop=None)
        assert seen.get("ran") and dl.PRIVATE_URL_ERROR not in (res.error or "")

    def test_size_floor_counts_a_smaller_progressive_format(self):
        info = {"formats": [
            {"height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 90_000_000},
            {"height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 8_000_000},
        ]}
        assert dl._min_sizes_by_quality(info)["1080"] == 8_000_000

    def test_warmup_never_rotates_warp(self):
        from pathlib import Path
        src = Path(dl.__file__).read_text(encoding="utf-8")
        assert "url != WARMUP_URL" in src
