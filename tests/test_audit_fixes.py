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
         "192.168.0.10", "100.64.0.10", "::1", "::ffff:127.0.0.1"],
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
        # Prove the guard stopped it — not a closed port refusing the connection.
        monkeypatch.setattr(sf._OPENER, "open",
                            lambda *a, **k: pytest.fail("guard let the request through"))
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
        monkeypatch.setattr(warp, "_LAST_SUCCESS", 0.0)
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
        monkeypatch.setattr(warp, "_LAST_SUCCESS", warp.time.monotonic() - 60)
        assert warp.rotate_warp_ip() is False
        assert calls.count("disconnect") == 1

    def test_cli_failure_is_not_fatal(self, monkeypatch):
        calls: list[str] = []
        self._arm(monkeypatch, calls)

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, "warp-cli")

        monkeypatch.setattr(warp, "_warp_cli", boom)
        assert warp.rotate_warp_ip() is False

    def test_failed_rotation_reconnects_and_no_one_rides_it(self, monkeypatch):
        calls: list[str] = []
        self._arm(monkeypatch, calls)

        def connect_fails_once(*args, timeout=15):
            calls.append(args[0])
            if args[0] == "connect" and calls.count("connect") == 1:
                raise subprocess.CalledProcessError(1, "warp-cli")
            return "Status update: Disconnected"

        monkeypatch.setattr(warp, "_warp_cli", connect_fails_once)
        assert warp.rotate_warp_ip() is False
        assert calls[-1] == "connect", "tunnel must be brought back up"
        # A concurrent job must not "ride" a rotation that failed.
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


# ---------------------------------------------------------------------------
# Second pass: channel posts, size floor, image uploads, history, logging
# ---------------------------------------------------------------------------
from telegram.error import TimedOut  # noqa: E402

import bot.handlers.download as hd  # noqa: E402
from bot.services.downloader import DownloadResult, _min_sizes_by_quality  # noqa: E402
from bot.services.session import DownloadSession, sessions  # noqa: E402
from tests.conftest import FakeCallbackQuery, FakeChat, FakeMessage  # noqa: E402


def _video(tmp_path, name="clip.mp4") -> DownloadResult:
    f = tmp_path / name
    f.write_bytes(b"v" * 2048)
    return DownloadResult(success=True, files=[f], primary=f, title="Clip",
                          mode="video", quality="720", file_size=2048, is_video=True)


def _quiet(monkeypatch, recorded=None):
    monkeypatch.setattr(
        hd, "record_download",
        lambda *a, **k: recorded.append(a) if recorded is not None else None,
    )
    monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
    monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: None)


class TestMultiLinkChannelPost:
    async def _post(self, fx, monkeypatch, tmp_path, fail_urls=()):
        _quiet(monkeypatch)

        async def fake_download(url, **kw):
            if url in fail_urls:
                return DownloadResult(success=False, error="nope", mode="video")
            return _video(tmp_path, f"{url[-1]}.mp4")

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        fx.chat.type = "channel"
        msg = fx.msg("https://youtu.be/a https://youtu.be/b")
        await hd.handle_message(fx.update(msg), fx.ctx)
        return msg

    async def test_one_failure_keeps_the_source_post(self, fx, monkeypatch, tmp_path):
        msg = await self._post(fx, monkeypatch, tmp_path, fail_urls=("https://youtu.be/b",))
        assert (fx.chat.id, msg.message_id) not in fx.ctx.bot.deletes

    async def test_all_succeed_deletes_the_source_once(self, fx, monkeypatch, tmp_path):
        msg = await self._post(fx, monkeypatch, tmp_path)
        assert fx.ctx.bot.deletes.count((fx.chat.id, msg.message_id)) == 1

    async def test_media_post_with_link_caption_is_never_deleted(self, fx, monkeypatch, tmp_path):
        _quiet(monkeypatch)

        async def fake_download(**kw):
            return _video(tmp_path)

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        fx.chat.type = "channel"
        msg = FakeMessage(text=None, caption="https://youtu.be/a", chat=fx.chat)
        await hd.handle_message(fx.update(msg), fx.ctx)
        assert any(c[0] == "send_video" for c in fx.ctx.bot.sent)
        assert (fx.chat.id, msg.message_id) not in fx.ctx.bot.deletes

    async def test_crashing_job_is_logged(self, fx, monkeypatch, caplog):
        async def boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(hd, "auto_download_flow", boom)
        fx.chat.type = "supergroup"
        await hd.handle_message(fx.update(fx.msg("https://youtu.be/a")), fx.ctx)
        assert "kaboom" in caplog.text


class TestAutoHistoryFields:
    async def test_records_real_mode_and_platform(self, fx, monkeypatch, tmp_path):
        recorded: list = []
        _quiet(monkeypatch, recorded)

        async def fake_download(**kw):
            return _video(tmp_path)

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        fx.chat.type = "supergroup"
        await hd.auto_download_flow(
            fx.update(fx.msg("x")), fx.ctx, "https://music.youtube.com/watch?v=1"
        )
        _, _, _, platform, mode, *_ = recorded[0]
        assert platform != "?"
        assert mode == "video"


class TestSizeFloor:
    def _info(self):
        return {"formats": [
            {"format_id": "137", "height": 720, "vcodec": "avc1", "acodec": "none",
             "filesize": 52_000_000},
            {"format_id": "398", "height": 720, "vcodec": "av01", "acodec": "none",
             "filesize": 30_000_000},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 3_000_000},
            {"format_id": "251", "vcodec": "none", "acodec": "opus", "filesize": 2_000_000},
        ]}

    def test_floor_is_smallest_top_height_plus_smallest_audio(self):
        assert _min_sizes_by_quality(self._info())["720"] == 32_000_000

    async def test_download_proceeds_when_a_rendition_fits(self, fx, monkeypatch, tmp_path):
        monkeypatch.setattr(hd, "MAX_FILE_SIZE_BYTES", 50_000_000)
        _quiet(monkeypatch)
        started = []

        async def fake_download(**kw):
            started.append(1)
            return _video(tmp_path)

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        s = DownloadSession(session_id="sF", user_id=42, chat_id=100,
                            url="https://youtu.be/x", title="V", platform="YouTube",
                            mode="video", quality="720",
                            estimated_sizes={"720": 55_000_000},
                            min_sizes={"720": 32_000_000})
        sessions.put(s)
        q = FakeCallbackQuery(data="quality:sF:720")
        q.message = FakeMessage(chat=FakeChat(id=100))
        await hd.execute_download(q, fx.ctx, s)
        assert started == [1]

    async def test_refusal_keeps_the_session_usable(self, fx, monkeypatch):
        monkeypatch.setattr(hd, "MAX_FILE_SIZE_BYTES", 50_000_000)
        s = DownloadSession(session_id="sR", user_id=42, chat_id=100,
                            url="https://youtu.be/x", title="V", platform="YouTube",
                            mode="video", quality="1080",
                            estimated_sizes={"1080": 90_000_000, "480": 20_000_000},
                            min_sizes={"1080": 80_000_000})
        sessions.put(s)
        q = FakeCallbackQuery(data="quality:sR:1080")
        q.message = FakeMessage(chat=FakeChat(id=100))
        await hd.execute_download(q, fx.ctx, s)
        assert sessions.get("sR") is s
        assert s.started is False and s.quality is None
        sessions.remove("sR")


class TestImageUploadNoDuplicate:
    async def test_timeout_is_not_followed_by_a_document_resend(self, fx, monkeypatch, tmp_path):
        f = tmp_path / "p.jpg"
        f.write_bytes(b"i" * 1024)
        res = DownloadResult(success=True, files=[f], primary=f, title="P",
                             mode="image", is_image=True, file_size=1024)
        sends = []

        async def photo_timeout(chat_id, photo=None, **kw):
            sends.append("photo")
            raise TimedOut()

        async def doc(chat_id, document=None, **kw):
            sends.append("document")

        monkeypatch.setattr(fx.ctx.bot, "send_photo", photo_timeout)
        monkeypatch.setattr(fx.ctx.bot, "send_document", doc)
        with pytest.raises(TimedOut):
            await hd._send_media_once(fx.ctx, 1, f, res, "", f.name)
        assert sends == ["photo"]
