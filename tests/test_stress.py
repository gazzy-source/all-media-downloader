"""Stress and system tests: concurrency, retries, locks, app wiring, cleanup."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import bot.handlers.download as hd
import bot.services.downloader as dl
from bot.services.downloader import DownloadManager, DownloadResult
from bot.services.session import DownloadSession, sessions


# ---------------------------------------------------------------------------
# DownloadManager concurrency limit (semaphore + active counting)
# ---------------------------------------------------------------------------
class TestDownloadConcurrency:
    async def test_semaphore_caps_parallel_downloads(self, monkeypatch, tmp_path):
        mgr = DownloadManager(max_concurrent=3)
        state = {"in_flight": 0, "max_seen": 0}

        def fake_sync(self, *, url, mode, quality, subtitle_lang, audio_format,
                      title_hint, progress_cb, loop):
            state["in_flight"] += 1
            state["max_seen"] = max(state["max_seen"], state["in_flight"])
            time.sleep(0.15)
            state["in_flight"] -= 1
            f = tmp_path / "out.mp4"
            f.write_bytes(b"x" * 10)
            return DownloadResult(success=True, files=[f], primary=f,
                                  title="t", mode=mode, file_size=10)

        monkeypatch.setattr(DownloadManager, "_download_sync", fake_sync)
        results = await asyncio.gather(*[
            mgr.download(url=f"https://x.com/{i}", mode="video", quality="720")
            for i in range(10)
        ])
        assert all(r.success for r in results)
        assert state["max_seen"] <= 3, f"concurrency breach: {state['max_seen']}"
        assert mgr.active == 0, "active count must return to zero"

    async def test_active_waiting_counters_settle(self, monkeypatch, tmp_path):
        mgr = DownloadManager(max_concurrent=2)

        async def fake_sync_async(*a, **kw):
            return DownloadResult(success=True, mode="video")

        def fake_sync(*a, **kw):
            time.sleep(0.02)
            return DownloadResult(success=True, mode="video")

        monkeypatch.setattr(DownloadManager, "_download_sync", fake_sync)
        await asyncio.gather(*[
            mgr.download(url=f"https://x.com/{i}", mode="video") for i in range(6)
        ])
        assert mgr.active == 0
        assert mgr.waiting == 0
        assert mgr.free_slots == 2

    async def test_image_fallback_on_video_error(self, monkeypatch):
        """Video request on image-only post must auto-retry in image mode."""
        mgr = DownloadManager(max_concurrent=1)
        calls = []

        def fake_sync(self, *, url, mode, **kw):
            calls.append(mode)
            if mode == "video":
                return DownloadResult(success=False, mode="video",
                                      error="no video formats found")
            f = Path(os.devnull)  # placeholder; success result needs no real file for this path
            return DownloadResult(success=True, mode="image", files=[], primary=None)

        monkeypatch.setattr(DownloadManager, "_download_sync", fake_sync)
        res = await mgr.download(url="https://pin.it/abc", mode="video", quality="720")
        assert calls == ["video", "image"], "must retry once as image"
        assert res.success is True and res.mode == "image"

    async def test_403_never_triggers_image_retry(self, monkeypatch):
        mgr = DownloadManager(max_concurrent=1)
        calls = []

        def fake_sync(self, *, url, mode, **kw):
            calls.append(mode)
            return DownloadResult(success=False, mode=mode,
                                  error="HTTP Error 403: Forbidden")

        monkeypatch.setattr(DownloadManager, "_download_sync", fake_sync)
        res = await mgr.download(url="https://youtu.be/x", mode="video", quality="480")
        assert calls == ["video"], "403 is not an image-only error"
        assert res.success is False

    async def test_rate_limiter_loop_concurrency_exact(self):
        """allow() is called from a single event loop; N parallel coroutines
        must yield exactly max_per_hour admissions."""
        from bot.services.rate_limit import RateLimiter
        r = RateLimiter(max_per_hour=5)

        async def hit():
            return r.allow(1)[0]

        got = await asyncio.gather(*[hit() for _ in range(20)])
        assert sum(got) == 5


# ---------------------------------------------------------------------------
# Single-instance lock (real subprocess)
# ---------------------------------------------------------------------------
LOCK_SCRIPT = """
import sys
sys.path.insert(0, r"{root}")
from pathlib import Path
from bot.utils.instance_lock import acquire_single_instance
acquire_single_instance(Path(sys.argv[1]))
print("acquired")
"""


class TestInstanceLock:
    def test_second_process_blocked_while_held(self, tmp_path):
        from bot.utils import instance_lock as il

        lock = tmp_path / "bot.lock"
        il.acquire_single_instance(lock)  # hold in this process
        try:
            script = LOCK_SCRIPT.format(root=str(Path(__file__).resolve().parent.parent))
            p = subprocess.run(
                [sys.executable, "-c", script, str(lock)],
                capture_output=True, text=True, timeout=30,
            )
            assert p.returncode == 1, "second instance must exit(1)"
            assert "already running" in (p.stderr + p.stdout).lower()
        finally:
            il._release()

    def test_acquires_after_release(self, tmp_path):
        from bot.utils import instance_lock as il

        lock = tmp_path / "bot2.lock"
        il.acquire_single_instance(lock)
        il._release()
        script = LOCK_SCRIPT.format(root=str(Path(__file__).resolve().parent.parent))
        p = subprocess.run(
            [sys.executable, "-c", script, str(lock)],
            capture_output=True, text=True, timeout=30,
        )
        assert p.returncode == 0, p.stderr
        assert "acquired" in p.stdout
        assert lock.read_text().strip().isdigit(), "lock file records pid"


# ---------------------------------------------------------------------------
# Application wiring (build_app)
# ---------------------------------------------------------------------------
class TestAppWiring:
    def test_build_app_registers_all_handlers(self, monkeypatch):
        from bot import config
        from bot.main import build_app

        monkeypatch.setattr("bot.main.BOT_TOKEN", "12345:TESTTOKEN")
        app = build_app()
        total = sum(len(v) for v in app.handlers.values())
        # 7 commands + 1 callback + 4 message handlers = 12
        assert total >= 12
        assert app.error_handlers, "error handler must be registered"
        assert app.job_queue is not None, "job-queue extra must be installed"

    def test_build_app_exits_without_token(self, monkeypatch):
        from bot.main import build_app

        monkeypatch.setattr("bot.main.BOT_TOKEN", "")
        with pytest.raises(SystemExit):
            build_app()

    def test_custom_api_url_normalization(self, monkeypatch):
        from bot.main import build_app

        monkeypatch.setattr("bot.main.BOT_TOKEN", "12345:TESTTOKEN")
        monkeypatch.setattr("bot.main.TELEGRAM_API_URL", "http://127.0.0.1:8081")
        app = build_app()  # must not raise; base url gets /bot appended


# ---------------------------------------------------------------------------
# Periodic cleanup job
# ---------------------------------------------------------------------------
class TestCleanupJob:
    async def test_removes_old_files_keeps_new(self, monkeypatch):
        from bot import config
        from bot.main import cleanup_job

        old = config.TEMP_DIR / "old.bin"
        new = config.TEMP_DIR / "new.bin"
        old.write_bytes(b"o" * 100)
        new.write_bytes(b"n" * 100)
        ancient = time.time() - (config.TEMP_CLEANUP_HOURS + 1) * 3600
        os.utime(old, (ancient, ancient))

        # one expired session
        s = DownloadSession(session_id="old_s", user_id=1, chat_id=1,
                            url="u", title="t", platform="p")
        s.created_at = time.time() - 10_000
        sessions.put(s)

        await cleanup_job(None)  # context unused

        assert not old.exists()
        assert new.exists()
        assert sessions.get("old_s") is None

    async def test_keeps_nonempty_dirs_removes_empty(self, monkeypatch):
        from bot import config
        from bot.main import cleanup_job

        d = config.TEMP_DIR / "dl_abc"
        d.mkdir(exist_ok=True)
        keep = d / "fresh.mp4"
        keep.write_bytes(b"k" * 50)

        empty = config.TEMP_DIR / "dl_empty"
        empty.mkdir(exist_ok=True)

        await cleanup_job(None)
        assert d.exists() and keep.exists()
        assert not empty.exists()

    async def test_swallows_errors(self, monkeypatch):
        from bot.main import cleanup_job
        # should never raise regardless of state
        await cleanup_job(None)
        await cleanup_job(None)


# ---------------------------------------------------------------------------
# on_error handler
# ---------------------------------------------------------------------------
class TestOnError:
    async def test_replies_and_does_not_raise(self, fx):
        from bot.main import on_error

        msg = fx.msg("https://x.com/1")
        upd = fx.update(msg)
        fx.ctx.error = RuntimeError("boom")
        await on_error(upd, fx.ctx)
        assert msg.replies and "Something went wrong" in msg.replies[0][0]

    async def test_tolerates_missing_message(self, fx):
        from bot.main import on_error

        upd = fx.update(None)
        fx.ctx.error = RuntimeError("boom")
        await on_error(upd, fx.ctx)  # must not raise


# ---------------------------------------------------------------------------
# File size accounting
# ---------------------------------------------------------------------------
class TestLimits:
    def test_max_file_size_bytes_matches_mb(self):
        from bot.config import MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB

        assert MAX_FILE_SIZE_BYTES == int(MAX_FILE_SIZE_MB * 1024 * 1024)

    def test_max_concurrent_positive(self):
        from bot.config import MAX_CONCURRENT_DOWNLOADS

        assert 1 <= MAX_CONCURRENT_DOWNLOADS <= 20
