"""
extract_info: one in-flight extraction per URL, safe after a caller timeout.

asyncio.wait_for can't stop a yt-meta thread that has started, so a caller
that gives up leaves the extraction running. These pin what happens then:
the same URL joins that work instead of stacking a second ladder on the pool,
an abandoned result can't overwrite a newer one, and nothing leaks.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import bot.services.downloader as dl

URL = "https://www.youtube.com/watch?v=abc"


class FakeExtract:
    """Stands in for _extract_info_sync: blocks until released, counts runs."""

    def __init__(self):
        self.runs: list[str] = []
        self.release = threading.Event()
        self.fail: Exception | None = None
        self._lock = threading.Lock()

    def __call__(self, url):
        with self._lock:
            self.runs.append(url)
            n = len(self.runs)
        assert self.release.wait(5), "test never released the extraction"
        if self.fail is not None:
            raise self.fail
        info = {"id": url, "run": n}
        dl._meta_cache_put(url, info)  # the real function caches on success
        return info


@pytest.fixture
def mgr(monkeypatch):
    fake = FakeExtract()
    monkeypatch.setattr(dl, "_extract_info_sync", fake)
    monkeypatch.setattr(dl, "build_media_info", lambda url, info: info)
    m = dl.DownloadManager(max_concurrent=1)
    m.fake = fake
    with dl._META_CACHE_LOCK:
        dl._META_CACHE.clear()
    yield m
    fake.release.set()
    m._meta_executor.shutdown(wait=True)
    with dl._META_CACHE_LOCK:
        dl._META_CACHE.clear()


async def _until(cond, timeout=2.0):
    async def spin():
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(spin(), timeout)


# 1 ------------------------------------------------------------ normal analysis
async def test_normal_analysis_returns_and_clears_inflight(mgr):
    mgr.fake.release.set()
    info = await mgr.extract_info(URL)
    assert info == {"id": URL, "run": 1}
    assert mgr._meta_inflight == {}


# 2 + 3 --------------------------- caller timeout; the worker finishes safely
async def test_caller_timeout_leaves_worker_running_then_caches(mgr):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.extract_info(URL), 0.1)
    assert mgr.fake.runs == [URL], "the worker keeps going after the caller left"
    assert URL in mgr._meta_inflight, "still in flight until the thread returns"
    mgr.fake.release.set()
    await _until(lambda: not mgr._meta_inflight)
    assert dl._meta_cache_get(URL) == {"id": URL, "run": 1}


# 4 + 5 ---------------- a retry joins the abandoned work: no second extraction
async def test_retry_after_timeout_joins_instead_of_stacking(mgr):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.extract_info(URL), 0.1)
    retry = asyncio.create_task(mgr.extract_info(URL))
    await asyncio.sleep(0.05)
    mgr.fake.release.set()
    assert await retry == {"id": URL, "run": 1}
    assert mgr.fake.runs == [URL], "one extraction for both callers"


async def test_abandoned_result_cannot_overwrite_a_newer_one(mgr):
    # old starts -> caller times out -> new request -> old finishes. With one
    # extraction per URL there is no "newer" run racing the old one: exactly
    # one cache write happens, so nothing older can land on top of it.
    writes = []
    real_put = dl._meta_cache_put

    def counting_put(url, info):
        writes.append(info["run"])
        real_put(url, info)

    dl._meta_cache_put = counting_put
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(mgr.extract_info(URL), 0.1)
        newer = asyncio.create_task(mgr.extract_info(URL))
        await asyncio.sleep(0.05)
        mgr.fake.release.set()
        await newer
        await _until(lambda: not mgr._meta_inflight)
    finally:
        dl._meta_cache_put = real_put
    assert writes == [1]


async def test_same_url_at_once_shares_one_extraction(mgr):
    calls = [asyncio.create_task(mgr.extract_info(URL)) for _ in range(4)]
    await asyncio.sleep(0.05)
    mgr.fake.release.set()
    results = await asyncio.gather(*calls)
    assert all(r == {"id": URL, "run": 1} for r in results)
    assert mgr.fake.runs == [URL]
    assert dl._META_INFLIGHT == 0


async def test_whitespace_variants_are_the_same_key(mgr):
    a = asyncio.create_task(mgr.extract_info(URL))
    b = asyncio.create_task(mgr.extract_info(f"  {URL}\n"))
    await asyncio.sleep(0.05)
    mgr.fake.release.set()
    await asyncio.gather(a, b)
    assert len(mgr.fake.runs) == 1


async def test_failure_reaches_every_joiner_and_frees_the_url(mgr):
    mgr.fake.fail = RuntimeError("bot wall")
    calls = [asyncio.create_task(mgr.extract_info(URL)) for _ in range(3)]
    await asyncio.sleep(0.05)
    mgr.fake.release.set()
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert mgr._meta_inflight == {}
    mgr.fake.fail = None  # a later try starts fresh
    assert (await mgr.extract_info(URL))["run"] == 2


async def test_one_callers_timeout_does_not_cancel_the_others(mgr):
    patient = asyncio.create_task(mgr.extract_info(URL))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.extract_info(URL), 0.1)
    mgr.fake.release.set()
    assert await patient == {"id": URL, "run": 1}


# 6 + 7 -------------------------- four slow analyses can't create more work
async def test_four_slow_analyses_bound_the_work_and_later_ones_complete(mgr):
    urls = [f"https://www.youtube.com/watch?v={i}" for i in range(4)]
    for u in urls:  # four slow links, every caller gives up
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(mgr.extract_info(u), 0.05)
    await _until(lambda: len(mgr.fake.runs) == 4)
    # Pool full of abandoned work: a fifth link queues, its caller gives up,
    # and the queued job is dropped instead of running later for nobody.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.extract_info("https://x.com/i/status/5"), 0.1)
    # The same four links again: they join, they don't stack.
    rejoin = [asyncio.create_task(mgr.extract_info(u)) for u in urls]
    await asyncio.sleep(0.05)
    assert len(mgr.fake.runs) == 4
    mgr.fake.release.set()
    assert [r["run"] for r in await asyncio.gather(*rejoin)] != []
    later = await mgr.extract_info("https://x.com/i/status/6")
    assert later["id"] == "https://x.com/i/status/6"
    assert "https://x.com/i/status/5" not in mgr.fake.runs, "dropped work never ran"
    await _until(lambda: not mgr._meta_inflight)


async def test_dropped_queued_job_does_not_trap_the_next_caller(mgr):
    # Fill the pool, then queue a URL whose only caller gives up: the queued
    # job is dropped. Asking for that URL right away must start a fresh run,
    # never join the dropped one (which would answer CancelledError).
    busy = [f"https://www.youtube.com/watch?v=b{i}" for i in range(4)]
    for u in busy:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(mgr.extract_info(u), 0.05)
    await _until(lambda: len(mgr.fake.runs) == 4)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.extract_info(URL), 0.05)
    assert URL not in mgr._meta_inflight
    fresh = asyncio.create_task(mgr.extract_info(URL))
    await asyncio.sleep(0.05)
    mgr.fake.release.set()
    assert (await fresh)["id"] == URL
    await _until(lambda: not mgr._meta_inflight)


# 8 ------------------------------------------- nobody waiting / shutdown safety
async def test_failure_with_no_waiters_is_not_reported_as_unretrieved(mgr):
    loop = asyncio.get_running_loop()
    reported = []
    loop.set_exception_handler(lambda _loop, ctx: reported.append(ctx))
    mgr.fake.fail = RuntimeError("late failure")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.extract_info(URL), 0.05)
    mgr.fake.release.set()
    await _until(lambda: not mgr._meta_inflight)
    import gc
    gc.collect()
    await asyncio.sleep(0.05)
    assert reported == []


async def test_shutdown_with_an_abandoned_worker_is_safe(mgr):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.extract_info(URL), 0.05)
    mgr.fake.release.set()
    t0 = time.monotonic()
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: mgr._meta_executor.shutdown(wait=True))
    assert time.monotonic() - t0 < 2
    await _until(lambda: not mgr._meta_inflight)
    assert dl._META_INFLIGHT == 0
