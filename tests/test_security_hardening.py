"""Regression tests for the 2026-10 security audit fixes."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp

import bot.handlers.download as hd
import bot.handlers.start as start_handlers
import bot.services.downloader as dl
from bot.services import url_tokens
from bot.services.downloader import DownloadManager
from bot.services.session import DownloadSession, sessions
from bot.utils import helpers as helpers_mod
from tests.conftest import FakeCallbackQuery, FakeChat, FakeMessage


class TestDownloadMatchFilter:
    def test_live_stream_refused(self):
        with pytest.raises(yt_dlp.utils.DownloadError, match="Live streams"):
            dl._download_match_filter({"is_live": True})
        with pytest.raises(yt_dlp.utils.DownloadError):
            dl._download_match_filter({"live_status": "is_upcoming"})

    def test_overlong_media_refused(self, monkeypatch):
        monkeypatch.setattr(dl, "MAX_MEDIA_DURATION", 3600)
        with pytest.raises(yt_dlp.utils.DownloadError, match="longer than"):
            dl._download_match_filter({"duration": 7200})
        assert dl._download_match_filter({"duration": 600}) is None
        assert dl._download_match_filter({}) is None  # unknown duration passes

    def test_media_url_pointing_inward_refused(self, monkeypatch):
        def guard(url):
            if "127.0.0.1" in url:
                raise dl.UnsafeURLError("private")

        monkeypatch.setattr(dl, "check_public_url", guard)
        with pytest.raises(yt_dlp.utils.DownloadError, match="private"):
            dl._download_match_filter({"url": "http://127.0.0.1:9123/health"})
        assert dl._download_match_filter({"url": "https://cdn.example/v.mp4"}) is None


class TestDownloadSyncGuards:
    @staticmethod
    def _run(url="https://example.com/v", title="t"):
        return DownloadManager()._download_sync(
            url=url, mode="video", quality="720", subtitle_lang=None,
            audio_format="mp3", title_hint=title, progress_cb=None, loop=None,
        )

    def test_private_url_never_reaches_ytdlp(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)

        def guard(url):
            raise dl.UnsafeURLError("private")

        monkeypatch.setattr(dl, "check_public_url", guard)

        def must_not_run(*a, **k):
            pytest.fail("yt-dlp must not run")

        monkeypatch.setattr(DownloadManager, "_extract_with_format_fallback", must_not_run)
        res = self._run("http://127.0.0.1:9123/health")
        assert res.success is False and "private" in res.error
        assert list(tmp_path.iterdir()) == []

    def test_opts_carry_filter_guard_and_escaped_title(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
        seen: dict = {}

        def capture(self, opts, url, title_hint, **kw):
            seen.update(opts)
            raise yt_dlp.utils.DownloadError("stop here")

        monkeypatch.setattr(DownloadManager, "_extract_with_format_fallback", capture)
        self._run(title="100%(formats)s")
        assert seen["match_filter"] is dl._download_match_filter
        assert seen["progress_hooks"], "the size/time guard must always run"
        assert "%%(formats)s" in seen["outtmpl"]
        assert "nocheckcertificate" not in seen

    def test_guard_hook_aborts_oversized_and_slow_transfers(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
        monkeypatch.setattr(dl, "DOWNLOAD_MAX_BYTES", 1000)
        hooks: dict = {}

        def capture(self, opts, url, title_hint, **kw):
            hooks["guard"] = opts["progress_hooks"][0]
            raise yt_dlp.utils.DownloadError("stop here")

        monkeypatch.setattr(DownloadManager, "_extract_with_format_fallback", capture)
        self._run()
        guard = hooks["guard"]
        guard({"status": "downloading", "downloaded_bytes": 999})
        # A cancel, not a DownloadError: yt-dlp's fragment loop swallows the latter.
        with pytest.raises(dl.JobAborted, match="max-filesize"):
            guard({"status": "downloading", "downloaded_bytes": 1001})
        assert not isinstance(dl.JobAborted("x"), yt_dlp.utils.DownloadError)
        monkeypatch.setattr(dl, "DOWNLOAD_MAX_SECONDS", -1)
        with pytest.raises(dl.JobAborted, match="timed out"):
            guard({"status": "downloading", "downloaded_bytes": 1})


def _refuse_all(url):
    raise hd.UnsafeURLError("private")


class TestHandlerPrivateUrlRefusal:
    async def test_dm_flow_refuses_before_analysis(self, fx, monkeypatch):
        monkeypatch.setattr(hd, "check_public_url", _refuse_all)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))

        async def must_not_analyse(url):
            pytest.fail("must not analyse")

        monkeypatch.setattr(hd.download_manager, "extract_info", must_not_analyse)
        msg = fx.msg("http://127.0.0.1:9123/health")
        await hd.start_url_flow(fx.update(msg), fx.ctx, "http://127.0.0.1:9123/health")
        assert any("private" in r[0] for r in msg.replies)

    async def test_group_flow_refuses_quietly(self, fx, monkeypatch):
        monkeypatch.setattr(hd, "check_public_url", _refuse_all)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))

        async def must_not_download(**k):
            pytest.fail("must not download")

        monkeypatch.setattr(hd.download_manager, "download", must_not_download)
        fx.chat.type = "supergroup"
        msg = fx.msg("x")
        assert await hd.auto_download_flow(fx.update(msg), fx.ctx, "http://10.0.0.5/") is False
        assert msg.replies == []


class TestCallbackHardening:
    @pytest.mark.parametrize("data", [
        "mode:sCB:evil", "aformat:sCB:wav", "quality:sCB:9999", "sublang:sCB:all",
    ])
    async def test_forged_values_are_ignored(self, fx, monkeypatch, data):
        s = DownloadSession(session_id="sCB", user_id=fx.user.id, chat_id=100,
                            url="https://youtu.be/x", title="V", platform="YouTube",
                            subtitle_langs=["en", "de"], has_subtitles=True)
        sessions.put(s)

        async def must_not_start(*a, **k):
            pytest.fail("forged value started a job")

        monkeypatch.setattr(hd, "execute_download", must_not_start)
        q = FakeCallbackQuery(data=data)
        q.message = FakeMessage(chat=FakeChat(id=100))
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert s.mode is None and s.quality is None and s.subtitle_lang in (None, "")

    def test_url_token_is_owner_only(self):
        tok = url_tokens.put_url("https://youtu.be/a", 42)
        assert url_tokens.get_url(tok, 42) == "https://youtu.be/a"
        assert url_tokens.get_url(tok, 99) is None
        assert url_tokens.get_url(tok, None) == "https://youtu.be/a"  # admin path


class TestShortLinkExpansion:
    def test_only_real_short_hosts_are_expanded(self, monkeypatch):
        def must_not_fetch(*a, **k):
            pytest.fail("must not fetch")

        monkeypatch.setattr("bot.utils.safe_fetch.open_public", must_not_fetch)
        u = "http://10.0.0.5:8080/admin/delete?x=bit.ly/"
        assert helpers_mod._expand_short_url(u) == u

    def test_expansion_capped_per_message(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(helpers_mod, "_expand_short_url",
                            lambda u: calls.append(u) or u)
        text = " ".join(f"https://t.co/{i}" for i in range(40))
        assert len(helpers_mod.extract_urls(text)) == 40
        assert len(calls) == helpers_mod.MAX_EXPAND


class TestErrorRedaction:
    @pytest.mark.parametrize("raw", [
        "ERROR: [generic] Failed to perform, curl: (7) Failed to connect to 10.0.0.5 port 80",
        "Unable to open /opt/all-media-downloader/temp/x",
        "ProxyError socks5://127.0.0.1:40000 refused",
    ])
    def test_internal_details_withheld(self, raw):
        msg = DownloadManager._friendly_error(raw)
        assert "10.0.0.5" not in msg and "/opt/" not in msg and "127.0.0.1" not in msg

    def test_useful_platform_message_kept(self):
        msg = DownloadManager._friendly_error(
            "ERROR: [youtube] x: This video is only available to Music Premium members"
        )
        assert "Music Premium" in msg


class TestStatsPrivacy:
    async def test_non_admin_sees_only_own_quota(self, fx, monkeypatch):
        monkeypatch.setattr(start_handlers, "ADMIN_IDS", set())

        def must_not_load():
            pytest.fail("global stats must not load")

        monkeypatch.setattr(start_handlers, "get_stats", must_not_load)
        msg = fx.msg("/stats")
        await start_handlers.cmd_stats(fx.update(msg), fx.ctx)
        assert "left of" in msg.replies[0][0]


class TestCookieJarSweep:
    async def test_stale_job_jars_removed(self, monkeypatch, tmp_path):
        import bot.config as cfg
        import bot.main as main_mod

        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
        old = tmp_path / "cookies.job_old.txt"
        new = tmp_path / "cookies.job_new.txt"
        keep = tmp_path / "cookies.sanitized.txt"
        for f in (old, new, keep):
            f.write_text("x")
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(keep, (past, past))
        await main_mod.cleanup_job(SimpleNamespace())
        assert not old.exists() and new.exists() and keep.exists()


def test_ignore_files_cover_compose_data_and_keys():
    root = Path(__file__).resolve().parents[1]
    gi = (root / ".gitignore").read_text().splitlines()
    di = (root / ".dockerignore").read_text().splitlines()
    assert "compose-data/" in gi and "compose-data/" in di
    assert "*.key" in gi and "cookies*.txt" in gi
