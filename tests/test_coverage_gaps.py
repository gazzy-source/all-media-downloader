"""Coverage for bot/handlers/start.py, bot/services/media_detect.py, bot/utils/ffmpeg.py.

All network / yt-dlp / filesystem-probe interactions are stubbed — no test
touches the real network or scans the real disk for binaries.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

import bot.handlers.start as start
import bot.services.media_detect as md
import bot.utils.ffmpeg as ff
from tests.conftest import FakeMessage

# ---------------------------------------------------------------------------
# bot/handlers/start.py
# ---------------------------------------------------------------------------


class TestStartCommands:
    async def test_cmd_start_replies_welcome(self, fx):
        msg = fx.msg("/start")
        await start.cmd_start(fx.update(msg), fx.ctx)
        assert msg.replies and "How to use" in msg.replies[0][0]
        assert msg.replies[0][1]["parse_mode"] is not None

    async def test_cmd_start_no_message_noop(self, fx):
        upd = fx.update(fx.msg(""))
        upd.effective_message = None
        await start.cmd_start(upd, fx.ctx)  # must not raise

    async def test_cmd_help_mentions_limits(self, fx):
        msg = fx.msg("/help")
        await start.cmd_help(fx.update(msg), fx.ctx)
        text = msg.replies[0][0]
        assert "Troubleshooting" in text
        assert "downloads / hour" in text

    async def test_cmd_platforms_lists_supported(self, fx):
        msg = fx.msg("/platforms")
        await start.cmd_platforms(fx.update(msg), fx.ctx)
        text = msg.replies[0][0]
        assert "Supported platforms" in text
        assert "YouTube" in text

    async def test_cmd_history_empty(self, fx, monkeypatch):
        monkeypatch.setattr(start, "get_user_history", lambda uid, limit=10: [])
        msg = fx.msg("/history")
        await start.cmd_history(fx.update(msg), fx.ctx)
        assert "No downloads yet" in msg.replies[0][0]

    async def test_cmd_history_entries_escaped(self, fx, monkeypatch):
        items = [
            {"success": True, "title": "a&b<c>", "platform": "YouTube",
             "mode": "video", "quality": "720"},
            {"success": False, "title": None, "platform": None,
             "mode": None, "quality": None},
        ]
        monkeypatch.setattr(start, "get_user_history", lambda uid, limit=10: items)
        msg = fx.msg("/history")
        await start.cmd_history(fx.update(msg), fx.ctx)
        text = msg.replies[0][0]
        assert "✅" in text and "❌" in text
        assert "a&amp;b&lt;c&gt;" in text, "HTML must be escaped"
        assert "Untitled" in text and "—" in text  # None fallbacks

    async def test_cmd_stats_formats_numbers(self, fx, monkeypatch):
        monkeypatch.setattr(start, "get_stats", lambda: {
            "total_downloads": 10, "successful": 8, "failed": 2,
            "unique_user_count": 3, "bytes_served": 1536,
            "by_platform": {"YouTube": 6, "TikTok": 4},
        })
        msg = fx.msg("/stats")
        await start.cmd_stats(fx.update(msg), fx.ctx)
        text = msg.replies[0][0]
        assert "Total downloads: <b>10</b>" in text
        assert "YouTube (6), TikTok (4)" in text
        assert "1.5 KB" in text or "1536 B" in text

    async def test_cmd_stats_empty_platforms(self, fx, monkeypatch):
        monkeypatch.setattr(start, "get_stats", lambda: {})
        msg = fx.msg("/stats")
        await start.cmd_stats(fx.update(msg), fx.ctx)
        assert "Top platforms: —" in msg.replies[0][0]

    async def test_cmd_settings_admin_flag(self, fx, monkeypatch):
        monkeypatch.setattr(start, "ADMIN_IDS", {42})
        msg = fx.msg("/settings")
        await start.cmd_settings(fx.update(msg), fx.ctx)
        assert "Admin: <b>Yes</b>" in msg.replies[0][0]

    async def test_cmd_settings_non_admin(self, fx, monkeypatch):
        monkeypatch.setattr(start, "ADMIN_IDS", set())
        msg = fx.msg("/settings")
        await start.cmd_settings(fx.update(msg), fx.ctx)
        assert "Admin: <b>No</b>" in msg.replies[0][0]

    async def test_cmd_cancel_removes_active_session(self, fx):
        from bot.services.session import DownloadSession, sessions

        s = DownloadSession(session_id="sZ", user_id=42, chat_id=1,
                            url="https://x.com/1", title="t", platform="p")
        sessions.put(s)
        msg = fx.msg("/cancel")
        await start.cmd_cancel(fx.update(msg), fx.ctx)
        assert sessions.get("sZ") is None
        assert "Cancelled" in msg.replies[0][0]

    async def test_cmd_cancel_nothing_active(self, fx):
        msg = fx.msg("/cancel")
        await start.cmd_cancel(fx.update(msg), fx.ctx)
        assert "Nothing to cancel" in msg.replies[0][0]


class TestCommandGuards:
    """Early-return guards: commands must no-op without message/user, not crash."""

    @pytest.mark.parametrize("fn", ["cmd_start", "cmd_help", "cmd_platforms"])
    async def test_no_message_noop(self, fx, fn):
        upd = fx.update(fx.msg("x"))
        upd.effective_message = None
        await getattr(start, fn)(upd, fx.ctx)  # must not raise

    @pytest.mark.parametrize("fn", ["cmd_history", "cmd_stats", "cmd_settings", "cmd_cancel"])
    async def test_no_user_noop(self, fx, fn):
        upd = fx.update(fx.msg("x"))
        upd.effective_user = None
        await getattr(start, fn)(upd, fx.ctx)  # must not raise


class TestTextMenuRouter:
    async def test_non_menu_text_returns_false(self, fx):
        assert await start.text_menu_router(fx.update(fx.msg("random text")), fx.ctx) is False

    async def test_no_text_returns_false(self, fx):
        msg = fx.msg("")
        msg.text = None
        assert await start.text_menu_router(fx.update(msg), fx.ctx) is False

    async def test_no_message_returns_false(self, fx):
        upd = fx.update(fx.msg("x"))
        upd.effective_message = None
        assert await start.text_menu_router(upd, fx.ctx) is False

    @pytest.mark.parametrize("label,fragment", [
        ("📥 New Download", "Send a media link"),
        ("❓ Help", "Troubleshooting"),
        ("🌐 Platforms", "Supported platforms"),
        ("🕘 History", "No downloads yet"),
        ("📊 Stats", "Bot statistics"),
        ("⚙️ Settings", "Settings"),
    ])
    async def test_each_menu_label_handled(self, fx, monkeypatch, label, fragment):
        if label == "🕘 History":
            monkeypatch.setattr(start, "get_user_history", lambda uid, limit=10: [])
        msg = fx.msg(f"  {label}  ")  # whitespace-tolerant
        handled = await start.text_menu_router(fx.update(msg), fx.ctx)
        assert handled is True
        assert any(fragment in r[0] for r in msg.replies), \
            f"{label!r} expected reply containing {fragment!r}: {msg.replies}"

    def test_esc(self):
        assert start._esc("a&b<c>d") == "a&amp;b&lt;c&gt;d"


# ---------------------------------------------------------------------------
# bot/services/media_detect.py
# ---------------------------------------------------------------------------


class TestDetectModeRouting:
    def test_direct_video_extensions(self):
        for ext in ("mp4", "webm", "mkv", "mov", "m4v"):
            assert md.detect_mode(f"https://host/f.{ext}") == "video"

    def test_query_string_stripped_for_ext(self):
        assert md.detect_mode("https://host/pic.jpg?w=200") == "image"
        assert md.detect_mode("https://host/clip.mp4?dl=1") == "video"

    def test_image_host_hints(self):
        assert md.detect_mode("https://i.pinimg.com/736x/a/b.jpg") == "image"
        assert md.detect_mode("https://i.imgur.com/x.gif") == "image"
        assert md.detect_mode("https://pbs.twimg.com/media/abc") == "image"

    def test_video_host_hints(self):
        for url in (
            "https://vimeo.com/123", "https://reddit.com/r/x/1",
            "https://v.redd.it/abc", "https://twitch.tv/x",
            "https://dailymotion.com/x", "https://streamable.com/x",
            "https://www.facebook.com/watch/1", "https://fb.watch/abc",
        ):
            assert md.detect_mode(url) == "video", url

    def test_instagram_defaults_video(self):
        assert md.detect_mode("https://instagram.com/p/abc") == "video"

    def test_unknown_host_defaults_video(self):
        assert md.detect_mode("https://some-random.site/watch") == "video"

    def test_pinterest_routes_to_probe(self, monkeypatch):
        monkeypatch.setattr(md, "_fetch_html_cached", lambda url: "")
        called = {}
        monkeypatch.setattr(md, "_detect_via_ytdlp",
                            lambda url: called.setdefault("url", url) and "image")
        assert md.detect_mode("https://pin.it/abc123") == "image"
        assert called["url"] == "https://pin.it/abc123"

    def test_is_direct_image_url_none(self):
        assert md.is_direct_image_url(None) is False


class TestDetectPinterest:
    def test_video_signal_wins_over_image(self, monkeypatch):
        html = ('<a href="https://i.pinimg.com/originals/x.jpg">'
                '"video_list": {')
        monkeypatch.setattr(md, "_fetch_html_cached", lambda url: html)
        assert md._detect_pinterest("https://pinterest.com/pin/1") == "video"

    def test_image_only(self, monkeypatch):
        html = '<img src="https://i.pinimg.com/564x/ab/cd/abcdef.jpg" />'
        monkeypatch.setattr(md, "_fetch_html_cached", lambda url: html)
        assert md._detect_pinterest("https://pinterest.com/pin/2") == "image"

    def test_og_video_tag_detected(self, monkeypatch):
        html = ('<meta property="og:video:secure_url" '
                'content="https://v.pinimg.com/videos/x.mp4" />')
        monkeypatch.setattr(md, "_fetch_html_cached", lambda url: html)
        assert md._detect_pinterest("https://pinterest.com/pin/3") == "video"

    def test_isVideo_true_signal(self, monkeypatch):
        monkeypatch.setattr(md, "_fetch_html_cached",
                            lambda url: '{"isVideo": true, "x": 1}')
        assert md._detect_pinterest("https://pinterest.com/pin/4") == "video"


class _FakeYDL:
    """Context-manager stub for yt_dlp.YoutubeDL."""

    info: dict | None = {}
    error: Exception | None = None

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if _FakeYDL.error:
            raise _FakeYDL.error
        return _FakeYDL.info


def _install_fake_ydl(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYDL))
    import bot.services.downloader as dl

    monkeypatch.setattr(dl, "_resolved_ffmpeg_dir", lambda: None)


class TestDetectViaYtdlp:
    def test_video_formats(self, monkeypatch):
        _install_fake_ydl(monkeypatch)
        _FakeYDL.info = {"formats": [{"vcodec": "h264", "height": 720, "url": "u"}]}
        _FakeYDL.error = None
        assert md._detect_via_ytdlp("https://pinterest.com/pin/v") == "video"

    def test_image_by_thumbnail(self, monkeypatch):
        _install_fake_ydl(monkeypatch)
        _FakeYDL.info = {"formats": [], "thumbnail": "https://i.pinimg.com/t.jpg"}
        _FakeYDL.error = None
        assert md._detect_via_ytdlp("https://pinterest.com/pin/i") == "image"

    def test_no_info_is_image(self, monkeypatch):
        _install_fake_ydl(monkeypatch)
        _FakeYDL.info = None
        _FakeYDL.error = None
        assert md._detect_via_ytdlp("https://pinterest.com/pin/n") == "image"

    def test_empty_formats_is_image(self, monkeypatch):
        _install_fake_ydl(monkeypatch)
        _FakeYDL.info = {}
        _FakeYDL.error = None
        assert md._detect_via_ytdlp("https://pinterest.com/pin/e") == "image"

    def test_truthy_info_without_media_is_image(self, monkeypatch):
        """Truthy info dict, but no formats/duration/thumbnail → image."""
        _install_fake_ydl(monkeypatch)
        _FakeYDL.info = {"uploader": "someone"}
        _FakeYDL.error = None
        assert md._detect_via_ytdlp("https://pinterest.com/pin/z") == "image"

    def test_image_exception_is_image(self, monkeypatch):
        _install_fake_ydl(monkeypatch)
        _FakeYDL.error = Exception("Only images available, no video formats found")
        assert md._detect_via_ytdlp("https://pinterest.com/pin/x") == "image"

    def test_generic_exception_defaults_video(self, monkeypatch):
        _install_fake_ydl(monkeypatch)
        _FakeYDL.error = Exception("Connection refused")
        assert md._detect_via_ytdlp("https://pinterest.com/pin/y") == "video"


class TestFetchHtmlCached:
    def test_network_error_returns_empty_string(self, monkeypatch):
        def boom(req, timeout=0):
            raise OSError("no route to host")

        monkeypatch.setattr(md, "urlopen", boom)
        # unique URL avoids lru_cache collisions with other tests
        assert md._fetch_html_cached("https://pinterest.com/pin/err-1") == ""

    def test_success_reads_html(self, monkeypatch):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                return "<html>pin page</html>".encode()

        monkeypatch.setattr(md, "urlopen", lambda req, timeout=0: Resp())
        assert "pin page" in md._fetch_html_cached("https://pinterest.com/pin/ok-1")


# ---------------------------------------------------------------------------
# bot/utils/ffmpeg.py
# ---------------------------------------------------------------------------


def _clear_ffmpeg_cache() -> None:
    """Some tests monkeypatch find_ffmpeg with a plain callable, and teardown
    can run before monkeypatch undoes that — so never assume the lru_cache."""
    clear = getattr(ff.find_ffmpeg, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _ffmpeg_cache_clean():
    """find_ffmpeg is lru_cached — isolate every test from real disk state."""
    _clear_ffmpeg_cache()
    yield
    _clear_ffmpeg_cache()


class TestEnsurePath:
    def test_prepends_once(self, monkeypatch):
        monkeypatch.setenv("PATH", os.pathsep.join(["/a", "/b"]))
        ff._ensure_path(Path("/added"))
        parts = os.environ["PATH"].split(os.pathsep)
        assert parts[0] == str(Path("/added"))
        ff._ensure_path(Path("/added"))
        assert os.environ["PATH"].split(os.pathsep).count(str(Path("/added"))) == 1

    def test_empty_path_env(self, monkeypatch):
        monkeypatch.setenv("PATH", "")
        ff._ensure_path(Path("/only"))
        assert os.environ["PATH"].startswith(str(Path("/only")))


class TestCandidateBins:
    def test_env_location_directory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FFMPEG_LOCATION", str(tmp_path))
        dirs = ff._candidate_bins()
        assert tmp_path in dirs

    def test_env_path_file_uses_parent(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FFMPEG_LOCATION", raising=False)  # FFMPEG_PATH is the fallback
        exe = tmp_path / "bin" / "ffmpeg.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"x")
        monkeypatch.setenv("FFMPEG_PATH", str(exe))
        dirs = ff._candidate_bins()
        assert exe.parent in dirs

    def test_install_roots_always_present(self, monkeypatch):
        monkeypatch.delenv("FFMPEG_LOCATION", raising=False)
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        dirs = ff._candidate_bins()
        assert len(dirs) >= 10  # common roots + winget links
        assert Path("/usr/bin") in dirs


class TestFindFfmpeg:
    def test_found_on_path(self, monkeypatch, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        monkeypatch.setattr(ff.shutil, "which", lambda name: str(bindir / "ffmpeg.exe"))
        monkeypatch.setattr(os.path, "sep", os.path.sep)
        result = ff.find_ffmpeg()
        assert result == bindir / "ffmpeg.exe"
        assert str(bindir) in os.environ["PATH"], "bin dir must be prepended for children"

    def test_found_in_candidate_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ff.shutil, "which", lambda name: None)
        cand = tmp_path / "ffbin"
        cand.mkdir()
        (cand / "ffmpeg").write_bytes(b"x")  # unix-style binary name
        monkeypatch.setattr(ff, "_candidate_bins", lambda: [cand])
        assert ff.find_ffmpeg() == cand / "ffmpeg"

    def test_skips_missing_dirs_and_others(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ff.shutil, "which", lambda name: None)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(ff, "_candidate_bins", lambda: [tmp_path / "nope", empty])
        assert ff.find_ffmpeg() is None

    def test_oserror_in_candidate_scan_is_ignored(self, monkeypatch):
        monkeypatch.setattr(ff.shutil, "which", lambda name: None)

        class BadDir:
            def exists(self):
                raise OSError("gone")

        monkeypatch.setattr(ff, "_candidate_bins", lambda: [BadDir()])
        assert ff.find_ffmpeg() is None

    def test_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(ff.shutil, "which", lambda name: None)
        monkeypatch.setattr(ff, "_candidate_bins", lambda: [])
        assert ff.find_ffmpeg() is None


class TestFfmpegLocationDir:
    def test_dir_of_found_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ff, "find_ffmpeg", lambda: tmp_path / "ffmpeg.exe")
        assert ff.ffmpeg_location_dir() == str(tmp_path)

    def test_none_when_missing(self, monkeypatch):
        monkeypatch.setattr(ff, "find_ffmpeg", lambda: None)
        assert ff.ffmpeg_location_dir() is None
