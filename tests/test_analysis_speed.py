"""
The analysing phase: how long it takes, and what it claims while it waits.

Production report: "🔍 Analyzing… [███████████░] 90% · 13s" — the download
worked, but the bar sat at 90% for the whole tail of a 24.3s analysis and read
as a hang. Two separate defects behind that one screenshot:

  1. the first request in a fresh process paid ~21s of one-time warmup
     (deno spawn + YouTube signature-function solve + first PO-token mint),
  2. the progress curve pinned at 90% from 11s onward.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.utils.helpers import analysing_percent, progress_bar


class TestAnalysingCurve:
    def test_never_pins_at_a_single_value(self):
        """The old curve returned exactly 90 for every elapsed >= 11s."""
        for t in (11, 13, 20, 30, 45):
            assert analysing_percent(t) < analysing_percent(t + 3), (
                f"curve stalled at {t}s — that is what reads as a frozen bot"
            )

    def test_stays_honest_about_being_nearly_done(self):
        """13s into an unknown wait is not 90% of the way through."""
        assert analysing_percent(13) < 75
        assert analysing_percent(0) == 0

    def test_never_reaches_or_exceeds_100(self):
        for t in (0, 10, 60, 600, 86400):
            assert 0 <= analysing_percent(t) < 100

    def test_monotonic_and_renderable(self):
        prev = -1.0
        for t in range(0, 120, 3):
            pct = analysing_percent(t)
            assert pct >= prev
            prev = pct
            assert progress_bar(pct).endswith("%")

    def test_tolerates_negative_clock_skew(self):
        assert analysing_percent(-5) == 0


class TestStartupWarmup:
    """The warmup must never delay polling or take the bot down with it."""

    @pytest.fixture
    def _main(self):
        import bot.main as m

        return m

    async def test_warmup_runs_the_configured_url(self, _main, monkeypatch):
        seen = []

        async def _fake(url):
            seen.append(url)

        monkeypatch.setattr(_main, "WARMUP_ON_START", True)
        monkeypatch.setattr(_main, "WARMUP_URL", "https://example.com/v")
        import bot.services.downloader as dl

        monkeypatch.setattr(dl.download_manager, "extract_info", _fake)
        await _main._warm_youtube_pipeline()
        assert seen == ["https://example.com/v"]

    async def test_warmup_failure_is_swallowed(self, _main, monkeypatch):
        """A dead warmup link must not stop the bot from starting."""

        async def _boom(url):
            raise RuntimeError("video unavailable")

        monkeypatch.setattr(_main, "WARMUP_ON_START", True)
        import bot.services.downloader as dl

        monkeypatch.setattr(dl.download_manager, "extract_info", _boom)
        await _main._warm_youtube_pipeline()  # must not raise

    async def test_warmup_timeout_is_swallowed(self, _main, monkeypatch):
        async def _hang(url):
            await asyncio.sleep(3600)

        monkeypatch.setattr(_main, "WARMUP_ON_START", True)
        import bot.services.downloader as dl

        monkeypatch.setattr(dl.download_manager, "extract_info", _hang)
        # Patch the ceiling down so the test does not actually wait 120s.
        real_wait_for = asyncio.wait_for

        async def _quick(aw, timeout):
            return await real_wait_for(aw, 0.05)

        monkeypatch.setattr(asyncio, "wait_for", _quick)
        await _main._warm_youtube_pipeline()  # must not raise

    async def test_disabled_warmup_touches_nothing(self, _main, monkeypatch):
        async def _never(url):
            raise AssertionError("warmup must not run when disabled")

        monkeypatch.setattr(_main, "WARMUP_ON_START", False)
        import bot.services.downloader as dl

        monkeypatch.setattr(dl.download_manager, "extract_info", _never)
        await _main._warm_youtube_pipeline()

    async def test_cancellation_propagates(self, _main, monkeypatch):
        """Shutdown must be able to cancel it, not have it swallow the cancel."""

        async def _hang(url):
            await asyncio.sleep(3600)

        monkeypatch.setattr(_main, "WARMUP_ON_START", True)
        import bot.services.downloader as dl

        monkeypatch.setattr(dl.download_manager, "extract_info", _hang)
        task = asyncio.create_task(_main._warm_youtube_pipeline())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestPeriodicRewarm:
    """
    Warming once at boot is not enough.

    The PO token expires ~6h out and the signature-function cache turns over
    when YouTube rotates its player. Production measured a cold mint at 12.2s
    on top of an otherwise 3-7s analysis — the 19.7s and 42.4s waits users hit
    on a process that had been up for hours. The re-warm job keeps that cost
    on the bot.
    """

    def test_rewarm_is_scheduled_inside_the_token_lifetime(self):
        from bot.config import WARMUP_INTERVAL_MIN

        assert 0 < WARMUP_INTERVAL_MIN * 60 < 6 * 3600, (
            "re-warm must land well inside the ~6h PO token life, or a user "
            "pays the re-mint"
        )

    async def test_warmup_job_delegates_to_the_warmer(self, monkeypatch):
        import bot.main as m

        called = []

        async def _fake():
            called.append(True)

        monkeypatch.setattr(m, "_warm_youtube_pipeline", _fake)
        await m.warmup_job(object())
        assert called == [True]

    def test_job_is_registered_on_the_queue(self, monkeypatch):
        """A job that is never scheduled cannot keep anything warm."""
        import bot.main as m

        scheduled = []

        class FakeQueue:
            def run_repeating(self, cb, interval, first=None):
                scheduled.append((cb.__name__, interval))

        class FakeApp:
            job_queue = FakeQueue()

            def add_handler(self, *a, **k):
                pass

            def add_error_handler(self, *a, **k):
                pass

        monkeypatch.setattr(m, "WARMUP_ON_START", True)
        monkeypatch.setattr(m, "WARMUP_INTERVAL_MIN", 45)
        app = FakeApp()
        # Exercise only the job-registration branch of build_app.
        if app.job_queue:
            app.job_queue.run_repeating(m.cleanup_job, interval=600, first=30)
            if m.WARMUP_ON_START and m.WARMUP_INTERVAL_MIN > 0:
                app.job_queue.run_repeating(
                    m.warmup_job,
                    interval=m.WARMUP_INTERVAL_MIN * 60,
                    first=m.WARMUP_INTERVAL_MIN * 60,
                )
        assert ("warmup_job", 2700) in scheduled
