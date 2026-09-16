"""Unit tests for bot.utils.cookies — Netscape cookie sanitizing."""
from __future__ import annotations

import time

import pytest

from bot.utils.cookies import (
    make_runtime_cookie_copy,
    prepare_cookies,
    sanitize_cookie_file,
)

HEADER = "# Netscape HTTP Cookie File\n"


def _line(domain, name="SID", value="v", expiry=None, flag="TRUE", path="/",
          secure="TRUE"):
    exp = int(time.time()) + 86400 if expiry is None else expiry
    return f"{domain}\t{flag}\t{path}\t{secure}\t{exp}\t{name}\t{value}"


@pytest.fixture
def jar(tmp_path):
    src = tmp_path / "cookies.txt"
    src.write_text(
        HEADER
        + _line(".youtube.com", "SID", "yt-token") + "\n"
        + _line(".google.com", "HSID", "g-token") + "\n"
        + _line(".instagram.com", "ds_user_id", "ig-token") + "\n"
        + _line(".tiktok.com", "tt-target", "tk") + "\n"
        + _line("mail.google.com", "MAIL", "x") + "\n"          # dropped
        + _line(".doubleclick.net", "IDE", "track") + "\n"      # dropped
        + _line(".reddit.com", "token", "rd") + "\n"
        # long expired row
        + f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time()) - 999999}\tOLD\tgone\n"
        # session cookie (expiry 0) must be kept
        + ".pinterest.com\tTRUE\t/\tTRUE\t0\tSESS\tlive\n"
        + "\n# a comment line\n\n"
    )
    return src


class TestSanitize:
    def test_keeps_media_domains_drops_noise(self, jar, tmp_path):
        dest = tmp_path / "out.txt"
        result = sanitize_cookie_file(jar, dest)
        assert result == dest
        text = dest.read_text()
        assert "SID\tyt-token" in text
        assert "g-token" in text
        assert "ig-token" in text
        assert "tt-target" in text
        assert "token\trd" in text
        assert "SESS\tlive" in text  # session cookie kept
        assert "MAIL" not in text
        assert "doubleclick" not in text
        assert "OLD\tgone" not in text  # expired dropped

    def test_header_written(self, jar, tmp_path):
        dest = tmp_path / "out.txt"
        sanitize_cookie_file(jar, dest)
        assert dest.read_text().startswith("# Netscape HTTP Cookie File")

    def test_all_noise_returns_none(self, tmp_path):
        src = tmp_path / "junk.txt"
        src.write_text(HEADER + _line("mail.google.com", "M", "v") + "\n"
                       + _line(".ads.example.com", "A", "v") + "\n")
        assert sanitize_cookie_file(src, tmp_path / "o.txt") is None

    def test_missing_file_returns_none(self, tmp_path):
        assert sanitize_cookie_file(tmp_path / "nope.txt", tmp_path / "o.txt") is None

    def test_short_file_returns_none(self, tmp_path):
        src = tmp_path / "tiny.txt"
        src.write_text("# Netscape HTTP Cookie File\n")
        assert sanitize_cookie_file(src, tmp_path / "o.txt") is None

    def test_space_separated_rows_normalized(self, tmp_path):
        src = tmp_path / "spaces.txt"
        src.write_text(
            HEADER
            + ".youtube.com TRUE / TRUE 1999999999 SID yt-token\n"
        )
        dest = tmp_path / "o.txt"
        assert sanitize_cookie_file(src, dest) == dest
        assert "SID\tyt-token" in dest.read_text()

    def test_value_containing_tabs_preserved(self, tmp_path):
        src = tmp_path / "tabs.txt"
        src.write_text(HEADER
                       + f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time())+9999}\tN\tval\twith\ttabs\n")
        dest = tmp_path / "o.txt"
        sanitize_cookie_file(src, dest)
        assert "val\twith\ttabs" in dest.read_text()


class TestPrepareAndRuntimeCopy:
    def test_prepare_picks_first_valid(self, tmp_path):
        good = tmp_path / "good.txt"
        good.write_text(HEADER + _line(".youtube.com") + "\n")
        bad = tmp_path / "bad.txt"
        bad.write_text("garbage, not cookies")
        dest = tmp_path / "sanitized.txt"
        out = prepare_cookies([bad, good, tmp_path / "missing.txt"], dest)
        assert out == dest
        assert "SID" in dest.read_text()

    def test_prepare_none_when_no_valid_source(self, tmp_path):
        assert prepare_cookies([tmp_path / "x.txt"], tmp_path / "o.txt") is None

    def test_runtime_copy_writable_and_isolated(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text(HEADER + _line(".youtube.com") + "\n")
        before = src.read_text()
        runtime = tmp_path / "job1.txt"
        out = make_runtime_cookie_copy(src, runtime)
        assert out == runtime
        runtime.write_text("mutated by yt-dlp")
        assert src.read_text() == before, "source must stay immutable"

    def test_runtime_copy_rejects_tiny_source(self, tmp_path):
        src = tmp_path / "t.txt"
        src.write_text("x")
        assert make_runtime_cookie_copy(src, tmp_path / "r.txt") is None
