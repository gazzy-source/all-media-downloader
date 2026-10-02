"""Regression tests for the second code sweep (cookies, helpers, cache, search…)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden, TimedOut

import bot.handlers.download as hd
from bot.services import history, inline_cache, yt_search
from bot.services.rate_limit import RateLimiter
from bot.utils import cookies, helpers


# ----------------------------------------------------------------- cookies
JAR = "\n".join([
    "# Netscape HTTP Cookie File",
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t9999999999\tLOGIN_INFO\tsecret",
    ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tPREF\tf1=1",
    "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tig",
    ".netflix.com\tTRUE\t/\tTRUE\t9999999999\tNetflixId\tnope",
    ".x.com\tTRUE\t/\tTRUE\tinf\tauth_token\ttw",
    "# a real comment",
]) + "\n"


class TestCookieSanitizer:
    def _run(self, tmp_path):
        src = tmp_path / "cookies.txt"
        src.write_text(JAR, encoding="utf-8")
        out = cookies.sanitize_cookie_file(src, tmp_path / "out.txt")
        return out.read_text(encoding="utf-8")

    def test_httponly_login_cookies_survive(self, tmp_path):
        text = self._run(tmp_path)
        assert "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t9999999999\tLOGIN_INFO\tsecret" in text
        assert "sessionid\tig" in text and "PREF" in text

    def test_x_com_keeps_x_but_not_netflix(self, tmp_path):
        text = self._run(tmp_path)
        assert "auth_token" in text and "NetflixId" not in text

    def test_infinite_expiry_does_not_crash(self, tmp_path):
        assert "auth_token" in self._run(tmp_path)

    def test_output_round_trips_through_yt_dlp(self, tmp_path):
        from yt_dlp.cookies import YoutubeDLCookieJar

        self._run(tmp_path)
        jar = YoutubeDLCookieJar(str(tmp_path / "out.txt"))
        jar.load(ignore_discard=True, ignore_expires=True)
        names = {c.name for c in jar}
        assert {"LOGIN_INFO", "sessionid", "PREF"} <= names

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_written_private(self, tmp_path):
        self._run(tmp_path)
        assert oct((tmp_path / "out.txt").stat().st_mode & 0o777) == "0o600"


# ----------------------------------------------------------------- helpers
class TestHelpers:
    @pytest.mark.parametrize("text,url", [
        ("https://youtu.be/dQw4w9WgXcQ! wow", "https://youtu.be/dQw4w9WgXcQ"),
        ("see https://youtu.be/dQw4w9WgXcQ…", "https://youtu.be/dQw4w9WgXcQ"),
        ("(https://youtu.be/dQw4w9WgXcQ)", "https://youtu.be/dQw4w9WgXcQ"),
        ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
        ("视频https://youtu.be/dQw4w9WgXcQ。", "https://youtu.be/dQw4w9WgXcQ"),
    ])
    def test_trailing_punctuation(self, text, url):
        assert helpers.extract_urls(text, expand=False) == [url]

    def test_bare_link_needs_a_real_domain_start(self):
        assert helpers.extract_urls("netflix.com/watch/123", expand=False) == []
        assert helpers.extract_urls("x.com/user/status/1", expand=False) == [
            "https://x.com/user/status/1"]

    @pytest.mark.parametrize("url,name", [
        ("https://www.netflix.com/title/1", "Netflix"),
        ("https://dropbox.com/s/x", "Dropbox"),
        ("https://x.com/a/status/1", "X / Twitter"),
        ("https://mobile.x.com/a", "X / Twitter"),
    ])
    def test_platform_label_is_by_domain(self, url, name):
        assert helpers.platform_from_url(url) == name

    def test_infinite_numbers_format_safely(self):
        assert helpers.format_duration(float("inf")) == "—"
        assert helpers.format_views(float("nan")) == "—"


def test_rate_limit_zero_means_unlimited():
    assert RateLimiter(0).allow(1) == (True, 0)


def test_explicit_ffmpeg_path_wins(monkeypatch, tmp_path):
    from bot.utils import ffmpeg

    custom = tmp_path / "ffmpeg7"
    custom.write_bytes(b"")
    monkeypatch.setenv("FFMPEG_LOCATION", str(custom))
    ffmpeg.find_ffmpeg.cache_clear()
    try:
        assert ffmpeg.find_ffmpeg() == custom
    finally:
        ffmpeg.find_ffmpeg.cache_clear()


def test_local_bot_api_file_url_keeps_the_host():
    base = "http://botapi:8081/bot"
    assert base[: -len("/bot")] + "/file/bot/" == "http://botapi:8081/file/bot/"
    from pathlib import Path

    import bot.main as main_mod
    assert 'base[: -len("/bot")] + "/file/bot/"' in Path(main_mod.__file__).read_text(encoding="utf-8")


# ----------------------------------------------------------------- history
def test_history_is_bounded_and_counts_users_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "_HISTORY_FILE", tmp_path / "h.json")
    monkeypatch.setattr(history, "_STATS_FILE", tmp_path / "s.json")
    monkeypatch.setattr(history, "_MAX_HISTORY_USERS", 3)
    for uid in (1, 2, 3, 4, 2):
        history._record_download_sync(uid, "u", "t", "YouTube", "video", "720", True, 1, None)
    assert history.history_users() == [3, 4, 2]  # 1 (least recent) evicted, 2 refreshed
    stats = history.get_stats()
    assert stats["unique_user_count"] == 4


# ---------------------------------------------------------- shared cache
class TestQualityGate:
    @pytest.mark.parametrize("quality,got,ok", [
        ("1080", 1080, True), ("1080", 360, False), ("720", 720, True),
        ("1080", 480, True),   # the video's own best is 480p: still the real thing
        ("max", 360, False), ("480", None, False),
    ])
    def test_only_the_promised_quality_is_cached(self, quality, got, ok):
        assert inline_cache.good_enough("video", quality, SimpleNamespace(actual_height=got)) is ok

    def test_audio_is_always_fine(self):
        assert inline_cache.good_enough("audio", "", SimpleNamespace(actual_height=None))

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/embed/videoseries?list=PLabc",
        "https://www.youtube.com/embed/live_stream?channel=UC1",
        "https://youtu.be/dQw4w9WgXcQXX",
    ])
    def test_playlist_embeds_and_long_ids_are_not_videos(self, url):
        assert inline_cache._norm(url) == url.split("#")[0]


class TestSendCached:
    async def _send(self, monkeypatch, tmp_path, exc):
        inline_cache._reset_for_tests(tmp_path / "c.json")
        inline_cache.put("https://youtu.be/dQw4w9WgXcQ", "video@720", file_id="F", kind="video")

        async def send_video(chat_id, **kw):
            raise exc

        ctx = SimpleNamespace(bot=SimpleNamespace(send_video=send_video))
        out = await hd._send_cached(ctx, 1, inline_cache.get("https://youtu.be/dQw4w9WgXcQ", "video@720"),
                                    "", url="https://youtu.be/dQw4w9WgXcQ", key="video@720")
        return out, inline_cache.get("https://youtu.be/dQw4w9WgXcQ", "video@720")

    async def test_refused_file_is_forgotten(self, monkeypatch, tmp_path):
        out, left = await self._send(monkeypatch, tmp_path, BadRequest("Wrong file identifier"))
        assert out is None and left is None

    async def test_chat_restrictions_keep_the_entry(self, monkeypatch, tmp_path):
        out, left = await self._send(monkeypatch, tmp_path, Forbidden("not enough rights"))
        assert out is None and left is not None

    async def test_timeout_is_not_resent(self, monkeypatch, tmp_path):
        out, left = await self._send(monkeypatch, tmp_path, TimedOut())
        assert out is not None and left is not None


# ------------------------------------------------------------------ search
def test_search_drops_duplicate_ids(monkeypatch):
    class Y:
        def __init__(self, o):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, q, download=False):
            e = {"id": "d" * 11, "ie_key": "Youtube", "duration": 60, "title": "T"}
            return {"entries": [e, dict(e), {**e, "id": "e" * 11}]}

    monkeypatch.setattr(yt_search.yt_dlp, "YoutubeDL", Y)
    yt_search._CACHE.clear()
    assert [h.id for h in yt_search.search("dupes")] == ["d" * 11, "e" * 11]
    yt_search._CACHE.clear()


def test_health_without_a_configured_provider_is_ok(monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health2", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    monkeypatch.setattr(health, "_env", lambda name: None)
    assert health.pot_provider_up() is True

