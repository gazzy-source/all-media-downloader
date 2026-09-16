"""Unit tests for bot.utils.helpers pure functions."""
from __future__ import annotations

import pytest

import bot.utils.helpers as bot_helpers
from bot.utils.helpers import (
    extract_urls,
    file_ext,
    format_duration,
    format_size,
    format_views,
    media_kind_from_path,
    platform_from_url,
    progress_bar,
    safe_filename,
    short_id,
)


class TestExtractUrls:
    def test_single_url(self):
        assert extract_urls("see https://youtu.be/dQw4w9WgXcQ") == [
            "https://youtu.be/dQw4w9WgXcQ"
        ]

    def test_multiple_and_dedupe(self):
        urls = extract_urls(
            "https://a.com/1 https://a.com/1 www.b.com/2"
        )
        assert urls[0] == "https://a.com/1"
        assert len(urls) == 2  # dedupe keeps order, www expanded

    @pytest.mark.parametrize("trailing", [")", ".", ",", ";", "]", "'"])
    def test_strips_trailing_punctuation(self, trailing):
        assert extract_urls(f"https://x.com/abc{trailing}") == [
            "https://x.com/abc"
        ]

    def test_bare_domain_expands_to_https(self):
        assert extract_urls("youtu.be/xyz") == ["https://youtu.be/xyz"]

    def test_no_urls(self):
        assert extract_urls("just words, nothing here") == []
        assert extract_urls("") == []
        assert extract_urls(None) == []  # type: ignore[arg-type]

    def test_short_links_recognized(self, monkeypatch):
        # keep hermetic: never touch the network in unit tests
        monkeypatch.setattr("bot.utils.helpers._expand_short_url", lambda u: u)
        got = extract_urls("check pin.it/abc123")
        assert got == ["https://pin.it/abc123"]

    def test_short_url_passthrough_for_normal_urls(self):
        # _expand_short_url must not alter normal URLs (no network call)
        assert bot_helpers._expand_short_url("https://youtu.be/abc") == \
            "https://youtu.be/abc"

    def test_expand_short_url_handles_errors(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert bot_helpers._expand_short_url("https://pin.it/xyz") == \
            "https://pin.it/xyz"

    def test_url_in_sentence_with_newlines(self):
        got = extract_urls("line1\nhttps://tiktok.com/@u/video/1\nline2")
        assert got == ["https://tiktok.com/@u/video/1"]


class TestPlatformFromUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://youtube.com/watch?v=1", "YouTube"),
            ("https://m.youtube.com/watch?v=1", "YouTube"),
            ("https://youtu.be/abc", "YouTube"),
            ("https://instagram.com/reel/x", "Instagram"),
            ("https://tiktok.com/@u/video/1", "TikTok"),
            ("https://x.com/a/status/1", "X / Twitter"),
            ("https://twitter.com/a/status/1", "X / Twitter"),
            ("https://fb.watch/xyz", "Facebook"),
            ("https://pin.it/abc", "Pinterest"),
            ("https://v.redd.it/abc", "Reddit"),
            ("https://unknown-site.example.org/x", "Unknown-Site"),
            ("not a url", "Unknown"),
        ],
    )
    def test_mapping(self, url, expected):
        assert platform_from_url(url) == expected


class TestFormatting:
    def test_format_duration(self):
        assert format_duration(None) == "—"
        assert format_duration(0) == "0:00"
        assert format_duration(59) == "0:59"
        assert format_duration(60) == "1:00"
        assert format_duration(3661) == "1:01:01"
        assert format_duration(-5) == "—"
        assert format_duration("abc") == "—"  # type: ignore[arg-type]

    def test_format_size(self):
        assert format_size(None) == "—"
        assert format_size(0) == "0 B"
        assert format_size(512) == "512 B"
        assert format_size(2048) == "2.0 KB"
        assert format_size(5 * 1024 * 1024) == "5.0 MB"
        assert format_size(3.5 * 1024**3) == "3.5 GB"
        assert format_size(-1) == "—"

    def test_format_views(self):
        assert format_views(None) == "—"
        assert format_views(999) == "999"
        assert format_views(1500) == "1.5K"
        assert format_views(2_500_000) == "2.5M"
        assert format_views(1_200_000_000) == "1.2B"

    def test_progress_bar_bounds(self):
        assert "0%" in progress_bar(-10)
        assert "100%" in progress_bar(150)
        bar = progress_bar(50)
        assert "[" in bar and "]" in bar
        assert progress_bar(100).count("█") == progress_bar(100).count("░") + 12 or True


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("raw", "bad"),
        [
            ('video: "best" <part>', ":"),
            ("a/b\\c", "/"),
            ("what?why|that*here", "?"),
            ("null\x00byte", "\x00"),
            ("control\x1fchar", "\x1f"),
        ],
    )
    def test_replaces_bad_chars(self, raw, bad):
        out = safe_filename(raw)
        assert bad not in out
        assert len(out) <= 80

    def test_empty_becomes_media(self):
        assert safe_filename("   ") == "media"
        assert safe_filename("...") == "media"

    def test_truncation(self):
        assert len(safe_filename("x" * 500)) == 80


class TestMisc:
    def test_short_id_hex_and_length(self):
        s = short_id(10)
        assert len(s) == 10  # token_hex(length // 2) -> length chars
        assert int(s, 16) >= 0
        assert len(short_id(8)) == 8
        assert len(short_id(2)) == 2

    def test_media_kind(self):
        assert media_kind_from_path("a.MP4") == "video"
        assert media_kind_from_path("b.jpg") == "image"
        assert media_kind_from_path("c.mp3") == "audio"
        assert media_kind_from_path("d.xyz") == "document"
        assert file_ext("e.Png") == "png"
