"""Unit tests for bot.services: rate_limit, url_tokens, session, history, media_detect."""
from __future__ import annotations

import time

import pytest

import bot.services.rate_limit as rl_mod
from bot.services.rate_limit import RateLimiter
from bot.services.session import DownloadSession, SessionStore
from bot.services.url_tokens import get_url, put_url


class TestRateLimiter:
    def test_allows_within_limit(self):
        r = RateLimiter(max_per_hour=3)
        for _ in range(3):
            allowed, retry = r.allow(1)
            assert allowed is True
            assert retry == 0

    def test_blocks_over_limit_with_reset_time(self):
        r = RateLimiter(max_per_hour=2)
        r.allow(1)
        r.allow(1)
        allowed, retry = r.allow(1)
        assert allowed is False
        assert 1 <= retry <= 3601

    def test_per_user_isolation(self):
        r = RateLimiter(max_per_hour=1)
        assert r.allow(1)[0] is True
        assert r.allow(1)[0] is False
        assert r.allow(2)[0] is True

    def test_window_expiry_frees_slots(self, monkeypatch):
        r = RateLimiter(max_per_hour=1)
        r.allow(1)
        assert r.allow(1)[0] is False
        # jump clock past the 1h window
        future = time.time() + 3700
        real_time = time.time
        monkeypatch.setattr(rl_mod.time, "time", lambda: future)
        assert r.allow(1)[0] is True
        monkeypatch.setattr(rl_mod.time, "time", real_time)

    def test_remaining_counts_down(self):
        r = RateLimiter(max_per_hour=5)
        assert r.remaining(9) == 5
        r.allow(9)
        r.allow(9)
        assert r.remaining(9) == 3


class TestUrlTokens:
    def test_roundtrip(self):
        tok = put_url("https://x.com/1", 42)
        assert len(tok) <= 16
        assert get_url(tok, 42) == "https://x.com/1"

    def test_unknown_token(self):
        assert get_url("nonexistent-token-xyz", 42) is None

    def test_token_callback_data_size_safe(self):
        # Telegram callback_data limit is 64 bytes
        tok = put_url("https://" + "a" * 300 + ".com/x", 1)
        assert len(f"again:{tok}".encode()) <= 64


class TestSessionStore:
    def _sess(self, sid="s1", user=1):
        return DownloadSession(session_id=sid, user_id=user, chat_id=100,
                               url="https://x.com/1", title="t", platform="X / Twitter")

    def test_put_get_remove(self):
        store = SessionStore()
        s = self._sess()
        store.put(s)
        assert store.get("s1") is s
        store.remove("s1")
        assert store.get("s1") is None

    def test_expiry(self, monkeypatch):
        store = SessionStore()
        s = self._sess()
        store.put(s)
        monkeypatch.setattr(s, "created_at", time.time() - 10_000)
        assert store.get("s1") is None  # expired -> removed
        assert store.get_for_user(1) is None

    def test_new_session_replaces_old_for_user(self):
        store = SessionStore()
        a = self._sess("a", user=7)
        b = self._sess("b", user=7)
        store.put(a)
        store.put(b)
        assert store.get("a") is None, "old session must be evicted"
        assert store.get("b") is not None

    def test_cleanup_expired_counts(self, monkeypatch):
        store = SessionStore()
        s1 = self._sess("x1", user=1)
        s2 = self._sess("x2", user=2)
        store.put(s1)
        store.put(s2)
        monkeypatch.setattr(s2, "created_at", time.time() - 10_000)
        assert store.cleanup_expired() == 1
        assert store.get("x1") is not None
        assert store.get("x2") is None

    def test_remove_clears_user_index(self):
        store = SessionStore()
        s = self._sess("del", user=5)
        store.put(s)
        store.remove("del")
        assert store.get_for_user(5) is None


class TestHistory:
    def test_record_and_read(self, tmp_path, monkeypatch):
        import bot.services.history as hist

        monkeypatch.setattr(hist, "_HISTORY_FILE", tmp_path / "h.json")
        monkeypatch.setattr(hist, "_STATS_FILE", tmp_path / "s.json")
        hist._record_download_sync(1, "https://x.com/1", "Title", "X / Twitter",
                                   "video", "720", True, file_size=1234)
        hist._record_download_sync(1, "https://x.com/2", "Bad", "X / Twitter",
                                   "video", "720", False, error="boom")
        items = hist.get_user_history(1)
        assert len(items) == 2
        assert items[0]["title"] == "Bad"  # newest first
        assert items[0]["success"] is False
        assert items[1]["success"] is True

        stats = hist.get_stats()
        assert stats["total_downloads"] == 2
        assert stats["successful"] == 1
        assert stats["failed"] == 1
        assert stats["bytes_served"] == 1234
        assert stats["unique_user_count"] == 1
        assert "unique_users" not in stats  # stripped from public view

    def test_history_capped(self, tmp_path, monkeypatch):
        import bot.services.history as hist

        monkeypatch.setattr(hist, "_HISTORY_FILE", tmp_path / "h.json")
        monkeypatch.setattr(hist, "_STATS_FILE", tmp_path / "s.json")
        for i in range(60):
            hist._record_download_sync(2, f"https://x.com/{i}", f"t{i}", "p", "video", None, True)
        assert len(hist.get_user_history(2, limit=100)) == 50  # _MAX_USER_HISTORY


class TestMediaDetect:
    def test_direct_image_url(self):
        from bot.services.media_detect import detect_mode, is_direct_image_url

        assert detect_mode("https://i.pinimg.com/originals/a/b/c.jpg") == "image"
        assert detect_mode("https://i.imgur.com/abc.png") == "image"
        assert detect_mode("https://x.com/a.mp4") == "video"
        assert detect_mode("https://youtu.be/abc") == "video"
        assert detect_mode("https://tiktok.com/@u/video/1") == "video"
        assert detect_mode("") == "video"
        assert is_direct_image_url("https://x/y.webp") is True
        assert is_direct_image_url("https://x/y.mp4") is False

    def test_video_regex_signals(self):
        from bot.services.media_detect import _VIDEO_RE

        assert _VIDEO_RE.search('property="og:video:secure_url" content="https://v/x.mp4"')
        assert _VIDEO_RE.search('"@type":"VideoObject"') or _VIDEO_RE.search(
            '"@type" : "VideoObject"'
        )
        assert _VIDEO_RE.search('https://v.pinimg.com/videos/mc/720p/x.mp4')
        assert not _VIDEO_RE.search("<html><body>plain text</body></html>")
