"""Downloader engine: fixes from the 2026-11-23 review."""

from __future__ import annotations

import concurrent.futures
import threading

import pytest

import bot.services.downloader as dl


class TestStrategyPin:
    def test_a_winning_pin_still_expires(self, monkeypatch):
        # Re-stamped on every win, the 3-min TTL slid forever: the pinned rung
        # is tried first and android always succeeds (360p for everyone).
        t = [1000.0]
        monkeypatch.setattr(dl.time, "monotonic", lambda: t[0])
        monkeypatch.setattr(dl, "_YT_WINNER_META", 0)
        dl._remember_yt_strategy(3, download=False)
        for _ in range(3):
            t[0] += 170
            assert dl._yt_winner(download=False) in (3, 0)
            dl._remember_yt_strategy(3, download=False)  # it wins again
        assert dl._yt_winner(download=False) == 0, "re-probe the fast rung"
        dl._remember_yt_strategy(0, download=False)


class TestYoutubeHost:
    @pytest.mark.parametrize("host,yt", [
        ("www.youtube.com", True), ("youtu.be", True), ("music.youtube.com", True),
        ("m.youtube.com:443", True), ("www.youtube-nocookie.com", True),
        ("youtu.example.net", False), ("notyoutube.com", False),
        ("youtube.com.evil.io", False), ("", False),
    ])
    def test_exact_hosts_only(self, host, yt):
        assert dl._platform_flags(host)["yt"] is yt


class TestMergedFormatsArePrivateChecked:
    def test_parts_of_a_merged_pick_are_checked(self, monkeypatch):
        from bot.utils import safe_fetch

        monkeypatch.setattr(dl, "check_public_url", safe_fetch.check_public_url)
        info = {"title": "x", "requested_formats": [
            {"url": "https://93.184.216.34/v.mp4"},  # IP literals: no DNS
            {"url": "http://127.0.0.1:9/a.m4a"},
        ]}
        with pytest.raises(dl.JobRefused):
            dl._download_match_filter(info)


class TestPartialsDropped:
    def test_only_partials_go(self, tmp_path, monkeypatch):
        job = tmp_path / "job1"
        job.mkdir()
        for name in ("Clip.mp4.part", "Clip.f137.mp4.ytdl", "Clip.mp4.part-Frag3",
                     "cover.jpg", "Clip.mp4"):
            (job / name).write_text("x")
        dl._drop_partials({"outtmpl": str(job / "%(title)s.%(ext)s")})
        assert sorted(p.name for p in job.iterdir()) == ["Clip.mp4", "cover.jpg"]

    def test_never_the_shared_temp_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
        (tmp_path / "other-job.part").write_text("x")
        dl._drop_partials({"outtmpl": str(tmp_path / "%(title)s.%(ext)s")})
        assert (tmp_path / "other-job.part").exists()


class TestCoverRace:
    def test_a_cover_still_running_is_not_made_twice(self, tmp_path, monkeypatch):
        gate = threading.Event()
        pool = concurrent.futures.ThreadPoolExecutor(1)
        fut = pool.submit(gate.wait)
        made = []
        monkeypatch.setattr(dl, "_telegram_cover", lambda d: made.append(d))

        class Fut:
            def result(self, timeout=None):
                raise concurrent.futures.TimeoutError

            def cancel(self):
                return False  # already running

        assert dl._finish_cover({"fut": Fut()}, tmp_path) is None
        assert made == [], "no second ffmpeg on the same tg_cover.jpg"
        gate.set()
        fut.result()
        pool.shutdown()


class TestResolveOnce:
    def test_concurrent_first_calls_see_the_value(self, monkeypatch):
        monkeypatch.setattr(dl, "_POT_RESOLVED", False)
        monkeypatch.setattr(dl, "_POT_ARGS", None)
        started = threading.Event()

        def slow():
            started.set()
            threading.Event().wait(0.2)
            return {"youtubepot-bgutilhttp": {"base_url": ["http://x"]}}

        monkeypatch.setattr(dl, "_load_pot_args", slow)
        out = []
        t = threading.Thread(target=lambda: out.append(dl._pot_provider_args()))
        t.start()
        started.wait()
        out.append(dl._pot_provider_args())  # arrives mid-resolution
        t.join()
        assert all(o for o in out), "nobody ran without the PO token"
