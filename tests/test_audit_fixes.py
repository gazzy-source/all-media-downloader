"""Regression tests for the 2026-10 audit fixes."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.services.media_detect as md
import bot.utils.safe_fetch as sf
import bot.utils.warp as warp
from bot.services.downloader import DownloadManager
from bot.utils.helpers import safe_filename


def _resolve_to(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(
        sf.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))],
    )


class TestSafeFetch:
    @pytest.mark.parametrize(
        "ip",
        ["127.0.0.1", "169.254.169.254", "10.0.0.5", "172.17.0.1",
         "192.168.1.104", "100.77.65.98", "::1", "::ffff:127.0.0.1"],
    )
    def test_refuses_internal_addresses(self, monkeypatch, ip):
        _resolve_to(monkeypatch, ip)
        with pytest.raises(sf.UnsafeURLError):
            sf.check_public_url("http://evil.example/x.jpg")

    def test_allows_public_address(self, monkeypatch):
        _resolve_to(monkeypatch, "151.101.0.84")
        sf.check_public_url("https://i.pinimg.com/originals/a.jpg")

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y.jpg", "http:///x"])
    def test_refuses_non_http(self, url):
        with pytest.raises(sf.UnsafeURLError):
            sf.check_public_url(url)

    def test_redirect_hops_are_checked(self, monkeypatch):
        _resolve_to(monkeypatch, "127.0.0.1")
        h = sf._GuardedRedirect()
        with pytest.raises(sf.UnsafeURLError):
            h.redirect_request(None, None, 302, "Found", {}, "http://localhost:4416/")


class TestFragmentDetection:
    def test_fragment_cannot_fake_an_image_extension(self):
        assert md.is_direct_image_url("http://169.254.169.254/meta#x.jpg") is False
        assert md.is_direct_image_url("https://i.pinimg.com/a.jpg#frag") is True

    def test_discover_ignores_fragment_extension(self, monkeypatch):
        monkeypatch.setattr(md, "_fetch_html_cached", lambda url: "")
        dm = DownloadManager()
        assert dm._discover_image_urls("http://10.0.0.1/secret#pinterest.x.jpg", html="") == []


class TestFetchUrlFile:
    def test_internal_target_fetches_nothing_and_leaves_no_file(self, monkeypatch, tmp_path):
        _resolve_to(monkeypatch, "127.0.0.1")
        dest = tmp_path / "x.jpg"
        assert DownloadManager()._fetch_url_file("http://127.0.0.1:4416/", dest) is None
        assert not dest.exists()

    def test_oversized_body_is_cut_off_and_removed(self, monkeypatch, tmp_path):
        import bot.services.downloader as dl

        monkeypatch.setattr(dl, "MAX_FILE_SIZE_BYTES", 1000)

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                return b"x" * n

        monkeypatch.setattr(sf, "open_public", lambda req, timeout=0: Resp())
        dest = tmp_path / "big.jpg"
        assert DownloadManager()._fetch_url_file("https://cdn.example/big.jpg", dest) is None
        assert not dest.exists()


class TestHtmlCache:
    def test_failures_are_not_cached(self, monkeypatch):
        calls = {"n": 0}

        def flaky(url):
            calls["n"] += 1
            return "" if calls["n"] == 1 else "<html>ok</html>"

        monkeypatch.setattr(md, "_fetch_html", flaky)
        md._HTML_CACHE.clear()
        assert md._fetch_html_cached("https://pinterest.com/pin/flaky") == ""
        assert md._fetch_html_cached("https://pinterest.com/pin/flaky") == "<html>ok</html>"
        assert md._fetch_html_cached("https://pinterest.com/pin/flaky") == "<html>ok</html>"
        assert calls["n"] == 2

    def test_cache_is_bounded(self, monkeypatch):
        monkeypatch.setattr(md, "_fetch_html", lambda url: "<html/>")
        md._HTML_CACHE.clear()
        for i in range(md._HTML_CACHE_MAX + 20):
            md._fetch_html_cached(f"https://pinterest.com/pin/{i}")
        assert len(md._HTML_CACHE) <= md._HTML_CACHE_MAX


class TestImageTempCleanup:
    def test_failed_image_download_removes_work_dir(self, monkeypatch, tmp_path):
        import bot.services.downloader as dl

        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
        dm = DownloadManager()
        monkeypatch.setattr(
            dm,
            "_download_image_page",
            lambda **k: dl.DownloadResult(success=False, error="No image", mode="image"),
        )
        res = dm._download_sync(
            url="https://pinterest.com/pin/1",
            mode="image",
            quality="720",
            subtitle_lang=None,
            audio_format="mp3",
            title_hint="t",
            progress_cb=None,
            loop=None,
        )
        assert res.success is False
        assert list(tmp_path.iterdir()) == []


class TestSafeFilename:
    def test_cjk_title_fits_linux_name_limit(self):
        name = safe_filename("视" * 80)
        # room for yt-dlp's ".f30080.mp4.part" and friends
        assert len((name + ".f30080.mp4.part").encode()) <= 255

    def test_ascii_unchanged(self):
        assert safe_filename("My Video") == "My Video"


class TestFriendlyError:
    def test_missing_ffmpeg_is_not_blamed_on_the_link(self):
        msg = DownloadManager._friendly_error(
            "ERROR: Postprocessing: ffprobe and ffmpeg not found."
        )
        assert "FFmpeg" in msg


class TestWarpRotation:
    def _arm(self, monkeypatch, calls):
        monkeypatch.setattr(warp, "WARP_ROTATE_ON_BOTCHECK", True)
        monkeypatch.setattr(warp, "PROXY", "socks5://127.0.0.1:40000")
        monkeypatch.setattr(warp, "WARP_ROTATE_COOLDOWN", 120)
        monkeypatch.setattr(warp, "_LAST_ROTATION", 0.0)
        monkeypatch.setattr(warp.shutil, "which", lambda n: "/usr/bin/warp-cli")

        def fake(*args, timeout=15):
            calls.append(args[0])
            return "Status update: Connected" if args[0] == "status" else "Success"

        monkeypatch.setattr(warp, "_warp_cli", fake)

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(warp, "WARP_ROTATE_ON_BOTCHECK", False)
        assert warp.rotate_warp_ip() is False

    def test_rotates_then_respects_cooldown(self, monkeypatch):
        calls: list[str] = []
        self._arm(monkeypatch, calls)
        assert warp.rotate_warp_ip() is True
        assert calls[:2] == ["disconnect", "connect"]
        # A second job within 10s rides the fresh IP without another rotation.
        assert warp.rotate_warp_ip() is True
        assert calls.count("disconnect") == 1
        # Later, still inside the cooldown: no flapping.
        monkeypatch.setattr(warp, "_LAST_ROTATION", warp.time.monotonic() - 60)
        assert warp.rotate_warp_ip() is False
        assert calls.count("disconnect") == 1

    def test_cli_failure_is_not_fatal(self, monkeypatch):
        calls: list[str] = []
        self._arm(monkeypatch, calls)

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, "warp-cli")

        monkeypatch.setattr(warp, "_warp_cli", boom)
        assert warp.rotate_warp_ip() is False


class TestEditedMessagesIgnored:
    async def test_edit_does_not_start_a_download(self, monkeypatch):
        import bot.handlers.download as hd

        started = []
        monkeypatch.setattr(hd, "start_url_flow", lambda *a, **k: started.append(1))
        monkeypatch.setattr(hd, "auto_download_flow", lambda *a, **k: started.append(1))
        msg = SimpleNamespace(text="https://youtu.be/dQw4w9WgXcQ", caption=None)
        update = SimpleNamespace(
            effective_message=msg,
            edited_message=msg,
            edited_channel_post=None,
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(type="private", id=1),
        )
        await hd.handle_message(update, SimpleNamespace())
        assert started == []


def test_dockerignore_keeps_secrets_out():
    text = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text()
    for needle in (".env", "cookies*.txt", "data/", ".git"):
        assert needle in text.splitlines()
