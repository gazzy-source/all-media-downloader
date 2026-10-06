"""Instant repeat sends, warmup WARP policy, quiet network errors, health endpoint."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.handlers.download as hd
import bot.handlers.inline as inl
from bot.services import inline_cache
from bot.services.downloader import DownloadResult
from bot.services.session import DownloadSession, sessions
from tests.conftest import FakeCallbackQuery, FakeChat, FakeMessage


@pytest.fixture
def cache(tmp_path):
    inline_cache._reset_for_tests(tmp_path / "c.json")
    return inline_cache


def _session(fx, sid, url, quality="720"):
    s = DownloadSession(session_id=sid, user_id=fx.user.id, chat_id=100, url=url,
                        title="T", platform="YouTube", mode="video", quality=quality)
    sessions.put(s)
    q = FakeCallbackQuery(data=f"quality:{sid}:{quality}")
    q.message = FakeMessage(chat=FakeChat(id=100))
    return s, q


def test_one_key_per_link_quality_everywhere():
    assert inline_cache.repeat_key("video", "720") == "video@720"
    assert inline_cache.repeat_key("audio", audio_format="m4a") == "audio@m4a"
    assert inline_cache.repeat_key("video_subs", "720") is None
    assert inl._key("video") == inline_cache.repeat_key("video", inl.INLINE_QUALITY)


def _capture_video(fx, monkeypatch):
    sent = []

    async def send_video(chat_id, video=None, **kw):
        sent.append(video)
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(fx.ctx.bot, "send_video", send_video)
    return sent


class TestInstantRepeats:
    async def test_dm_serves_a_cached_file_without_downloading(self, fx, cache, monkeypatch):
        cache.put("https://youtu.be/r", "video@720", file_id="FID", kind="video", title="Song")

        async def must_not(**k):
            pytest.fail("must not download")

        monkeypatch.setattr(hd.download_manager, "download", must_not)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        sent = _capture_video(fx, monkeypatch)
        s, q = _session(fx, "sI", "https://youtu.be/r")
        await hd.execute_download(q, fx.ctx, s)
        assert sent == ["FID"]
        assert q.message.deleted and sessions.get("sI") is None

    async def test_dm_upload_is_remembered_for_next_time(self, fx, cache, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"v" * 64)

        async def fake_download(**kw):
            return DownloadResult(success=True, files=[f], primary=f, title="T", mode="video",
                                  file_size=64, is_video=True, actual_height=1080)

        async def fake_send(*a, **k):
            return SimpleNamespace(video=SimpleNamespace(file_id="UP1"), animation=None,
                                   audio=None, document=None, photo=None)

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: None)
        monkeypatch.setattr(hd, "_send_media", fake_send)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        s, q = _session(fx, "sJ", "https://youtu.be/new", "1080")
        await hd.execute_download(q, fx.ctx, s)
        assert cache.get("https://youtu.be/new", "video@1080")["file_id"] == "UP1"

    async def test_refused_cached_file_falls_back_to_download(self, fx, cache, monkeypatch):
        from telegram.error import BadRequest

        cache.put("https://youtu.be/b", "video@720", file_id="BAD", kind="video")

        async def refuse(*a, **k):
            raise BadRequest("wrong file identifier")

        monkeypatch.setattr(fx.ctx.bot, "send_video", refuse)
        started = []

        async def fake_download(**kw):
            started.append(1)
            return DownloadResult(success=False, error="x", mode="video")

        monkeypatch.setattr(hd.download_manager, "download", fake_download)
        monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: None)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        s, q = _session(fx, "sK", "https://youtu.be/b")
        await hd.execute_download(q, fx.ctx, s)
        assert started == [1]
        assert cache.get("https://youtu.be/b", "video@720") is None

    async def test_group_serves_cached_file_instantly(self, fx, cache, monkeypatch):
        cache.put("https://youtu.be/g", f"video@{hd.AUTO_QUALITY}", file_id="GF", kind="video")

        async def must_not(**k):
            pytest.fail("must not download")

        monkeypatch.setattr(hd.download_manager, "download", must_not)
        monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        sent = _capture_video(fx, monkeypatch)
        fx.chat.type = "supergroup"
        msg = fx.msg("https://youtu.be/g")
        assert await hd.auto_download_flow(fx.update(msg), fx.ctx, "https://youtu.be/g") is True
        assert sent == ["GF"]
        assert msg.replies == []  # no "Downloading" status for an instant send


def test_warmup_rotates_only_when_the_bot_is_idle(monkeypatch):
    from pathlib import Path

    import bot.services.downloader as dl
    from bot.services import activity
    from bot.services.dl_queue import download_queue

    src = Path(dl.__file__).read_text(encoding="utf-8")
    assert "_bot_idle() if url == WARMUP_URL else _rotation_harmless(0)" in src
    activity.touch()
    assert not dl._bot_idle(), "a user just did something"
    monkeypatch.setattr(activity, "_last", activity.time.monotonic() - 120)
    assert dl._bot_idle()
    monkeypatch.setattr(download_queue, "running", 1)
    assert not dl._bot_idle(), "a download is running"


async def test_telegram_polling_hiccup_is_not_an_unhandled_error(caplog):
    from telegram.error import NetworkError

    import bot.main as main_mod

    await main_mod.on_error(None, SimpleNamespace(error=NetworkError("Bad Gateway")))
    assert "Unhandled error" not in caplog.text
    assert "hiccup" in caplog.text


def test_health_endpoint_flags_a_dead_po_token_provider(monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    import time as _t

    monkeypatch.setattr(health, "service_active", lambda: True)
    monkeypatch.setattr(health, "pot_provider_up", lambda: False)
    monkeypatch.setattr(health, "proxy_up", lambda: True)
    monkeypatch.setattr(health, "heartbeat", lambda: {"ts": _t.time(), "polling": True})

    class Req:
        path = "/health"
        code = None
        body = b""

        def send_response(self, c):
            self.code = c

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    r = Req()
    r.wfile = SimpleNamespace(write=lambda b: setattr(r, "body", b))
    health.Handler.do_GET(r)
    assert r.code == 503 and b"degraded" in r.body


def test_health_endpoint_treats_malformed_heartbeat_as_down(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health_malformed", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    (tmp_path / "heartbeat.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(health, "_env", lambda name: str(tmp_path) if name == "DATA_DIR" else None)
    monkeypatch.setattr(health, "service_active", lambda: True)
    monkeypatch.setattr(health, "pot_provider_up", lambda: True)
    monkeypatch.setattr(health, "proxy_up", lambda: True)

    assert health.heartbeat() == {}
    code, body = health.check()
    assert code == 503 and body["status"] == "down"
    assert body["heartbeat_age_s"] is None


def test_health_endpoint_fails_closed_on_invalid_heartbeat_fields(monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health_invalid_fields", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    monkeypatch.setattr(health, "service_active", lambda: True)
    monkeypatch.setattr(health, "pot_provider_up", lambda: True)
    monkeypatch.setattr(health, "proxy_up", lambda: True)
    monkeypatch.setattr(health, "heartbeat", lambda: {
        "ts": float("nan"), "polling": True, "warmup_fail_streak": "invalid",
    })

    code, body = health.check()
    assert code == 503 and body["status"] == "down"
    assert body["heartbeat_age_s"] is None
    assert body["youtube_warmup_fail_streak"] == 2


def test_health_endpoint_marks_configured_invalid_proxy_down(monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health_bad_proxy", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    monkeypatch.setattr(health, "_env", lambda name: "not-a-proxy" if name == "PROXY" else None)
    assert health.proxy_up() is False


def test_health_endpoint_degrades_when_disk_usage_cannot_be_read(monkeypatch):
    import importlib.util
    import time as _t
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health_disk_error", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    monkeypatch.setattr(health, "service_active", lambda: True)
    monkeypatch.setattr(health, "pot_provider_up", lambda: True)
    monkeypatch.setattr(health, "proxy_up", lambda: True)
    monkeypatch.setattr(health, "heartbeat", lambda: {"ts": _t.time(), "polling": True})

    def unreadable(_path):
        raise PermissionError("access denied")

    monkeypatch.setattr(health.shutil, "disk_usage", unreadable)
    code, body = health.check()
    assert code == 503 and body["status"] == "degraded"
    assert body["disk_free_gb"] == 0.0


def test_health_degrades_after_two_consecutive_youtube_warmup_failures(monkeypatch):
    import importlib.util
    import time as _t
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health_youtube_failures", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    monkeypatch.setattr(health, "service_active", lambda: True)
    monkeypatch.setattr(health, "pot_provider_up", lambda: True)
    monkeypatch.setattr(health, "proxy_up", lambda: True)
    state = {"ts": _t.time(), "polling": True, "warmup_fail_streak": 1}
    monkeypatch.setattr(health, "heartbeat", lambda: state)

    code, body = health.check()
    assert code == 200 and body["status"] == "ok"

    state["warmup_fail_streak"] = 2
    code, body = health.check()
    assert code == 503 and body["status"] == "degraded"


def test_user_bot_wall_never_rotates_under_someone_elses_transfer(monkeypatch):
    import bot.services.downloader as dl
    from bot.services.dl_queue import download_queue

    monkeypatch.setattr(download_queue, "running", 0)
    monkeypatch.setattr(dl, "_META_INFLIGHT", 1)
    assert dl._rotation_harmless(0), "only this analysis is running"
    monkeypatch.setattr(dl, "_META_INFLIGHT", 2)
    assert not dl._rotation_harmless(0), "another user's analysis is mid-read"
    monkeypatch.setattr(dl, "_META_INFLIGHT", 0)
    monkeypatch.setattr(download_queue, "running", 1)
    assert dl._rotation_harmless(1), "only this download is running"
    monkeypatch.setattr(download_queue, "running", 2)
    assert not dl._rotation_harmless(1), "someone else is downloading"



def test_health_says_down_when_the_bot_stops_beating(monkeypatch):
    """A hung event loop: systemd still says "active"; the heartbeat goes stale."""
    import importlib.util
    import time as _t
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "health3", Path(__file__).resolve().parents[1] / "scripts" / "bot_health_server.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    monkeypatch.setattr(health, "service_active", lambda: True)
    monkeypatch.setattr(health, "pot_provider_up", lambda: True)
    monkeypatch.setattr(health, "proxy_up", lambda: True)
    monkeypatch.setattr(health, "heartbeat", lambda: {"ts": _t.time() - 600, "polling": True})
    code, body = health.check()
    assert code == 503 and body["status"] == "down"
    monkeypatch.setattr(health, "heartbeat", lambda: {"ts": _t.time(), "polling": True})
    code, body = health.check()
    assert code == 200 and body["status"] == "ok"
