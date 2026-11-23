"""Core services and lifecycle: fixes from the 2026-11-23 review."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading

import pytest

from bot.services import dl_queue, jobs


class TestShutdown:
    def test_a_job_started_during_shutdown_is_stopped(self, monkeypatch):
        monkeypatch.setattr(jobs, "SHUTTING_DOWN", True)
        job = jobs.start("dm:1:2", 1)
        try:
            assert job.cancelled, "would otherwise run past systemd's stop timeout"
        finally:
            jobs.drop(job)

    async def test_and_never_queues(self, monkeypatch):
        monkeypatch.setattr(jobs, "SHUTTING_DOWN", True)
        job = jobs.start("dm:1:3", 1)
        try:
            with pytest.raises(asyncio.CancelledError):
                await jobs.run_queued(job, dl_queue.DownloadQueue(1), lambda: None)
        finally:
            jobs.drop(job)


class TestBusySignal:
    def test_uploads_and_inline_jobs_count_as_busy(self, monkeypatch):
        import bot.main as m
        from bot.services import inflight
        from bot.services.upload_gate import upload_gate

        monkeypatch.setattr(inflight, "count", lambda: 0)
        assert m._busy_count() == 0
        monkeypatch.setattr(upload_gate, "running_uploads", 1)
        assert m._busy_count() == 1, "the download slot is free while it uploads"
        monkeypatch.setattr(upload_gate, "running_uploads", 0)
        job = jobs.start("i:IMID", 5)
        try:
            assert m._busy_count() == 1
        finally:
            jobs.drop(job)
        monkeypatch.setattr(inflight, "count", lambda: 2)  # links being read
        assert m._busy_count() == 2

    def test_heartbeat_carries_it(self, tmp_path, monkeypatch):
        import json

        from bot.services import heartbeat

        monkeypatch.setattr(heartbeat, "PATH", tmp_path / "hb.json")
        heartbeat.write(True, 0, 0, 3)
        assert json.loads((tmp_path / "hb.json").read_text())["busy"] == 3


class TestQueueAging:
    async def test_a_free_job_is_not_starved_by_paid_ones(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(dl_queue.time, "monotonic", lambda: t[0])
        q = dl_queue.DownloadQueue(slots=1)
        free = (1, 0, "free", 1000.0)
        paid = (0, 1, "paid", 1000.0 + 59)
        q._waiting = [free, paid]
        t[0] = 1000.0 + 59
        assert q._next() == paid, "paid goes first at first"
        t[0] = 1000.0 + 61
        assert q._next() == free, "a minute later the free job's turn has come"


class TestConfigNeverCrashes:
    def _config(self, **env):
        code = ("import bot.config as c; print(c.FREE_DOWNLOADS, c.EXTRACT_TIMEOUT, "
                "c.AUTO_DOWNLOAD_GROUPS, c.CHANNEL_REPLACE_LINK, c.STORAGE_CHAT_ID)")
        full = {**os.environ, **env}
        r = subprocess.run([sys.executable, "-c", code], env=full, capture_output=True,
                           text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
        assert r.returncode == 0, r.stderr
        return r.stdout.split(), r.stderr

    def test_bad_numbers_fall_back(self):
        out, err = self._config(FREE_DOWNLOADS="", EXTRACT_TIMEOUT="4x",
                                STORAGE_CHAT_ID="@chan")
        assert out[0] == "100" and out[1] == "45" and out[4] == "None"
        assert "EXTRACT_TIMEOUT" in err and "STORAGE_CHAT_ID" in err

    @pytest.mark.parametrize("raw,expect", [
        ("off", "False"), ("FALSE", "False"), ("No", "False"), ("0", "False"),
        ("ON", "True"), ("yes", "True"), ("1", "True"), ("", "True")])
    def test_booleans_in_any_spelling(self, raw, expect):
        out, _ = self._config(AUTO_DOWNLOAD_GROUPS=raw, CHANNEL_REPLACE_LINK=raw)
        assert out[2] == expect and out[3] == expect


class TestUrlTokensWriteOutsideTheLock:
    def test_keyboards_are_not_blocked_by_a_slow_write(self, tmp_path, monkeypatch):
        from bot.services import url_tokens

        url_tokens._reset_for_tests(tmp_path / "t.json")
        url_tokens.put_url("https://a", 1)
        writing, release = threading.Event(), threading.Event()

        def slow_write(snapshot):
            writing.set()
            release.wait(5)

        monkeypatch.setattr(url_tokens, "_write", slow_write)
        t = threading.Thread(target=url_tokens.flush)
        t.start()
        writing.wait(5)
        done = threading.Event()
        threading.Thread(target=lambda: (url_tokens.put_url("https://b", 1), done.set())).start()
        assert done.wait(1), "put_url waited for the disk write"
        release.set()
        t.join()


class TestHistoryWriter:
    def test_one_writer_thread_and_flush(self, tmp_path, monkeypatch):
        import bot.services.history as hist

        monkeypatch.setattr(hist, "_HISTORY_FILE", tmp_path / "h.json")
        monkeypatch.setattr(hist, "_STATS_FILE", tmp_path / "s.json")
        before = threading.active_count()
        for i in range(20):
            hist.record_download(7, f"https://x/{i}", "t", "p", "video", "720", True)
        assert threading.active_count() <= before + 1
        hist.flush()
        assert len(hist.get_user_history(7, limit=50)) == 20
