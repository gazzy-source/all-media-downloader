"""Speed paths: reuse the analysis extraction, lean first attempt, inline prefetch."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
import yt_dlp

import bot.handlers.inline as inl
import bot.services.downloader as dl
from bot.services import inline_cache
from bot.services.downloader import DownloadManager


class FakeYDL:
    """Records params; behaviour set per test."""

    created: list = []
    process_raises: Exception | None = None
    extract_raises: Exception | None = None

    def __init__(self, params):
        self.params = params
        FakeYDL.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def process_ie_result(self, info, download=True):
        if FakeYDL.process_raises:
            raise FakeYDL.process_raises
        info["_reused"] = True
        return info

    def extract_info(self, url, download=True):
        if FakeYDL.extract_raises:
            raise FakeYDL.extract_raises
        return {"id": "x", "title": "Fresh", "ext": "mp4"}

    def prepare_filename(self, info):
        return "/tmp/x.mp4"


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.created = []
    FakeYDL.process_raises = None
    FakeYDL.extract_raises = None
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
    dl._META_CACHE.clear()
    yield FakeYDL
    dl._META_CACHE.clear()


INFO = {"id": "x", "title": "Analysed", "formats": [{"format_id": "18"}], "extractor": "youtube"}


class TestReuseAnalysis:
    def test_download_reuses_a_fresh_analysis(self, fake_ydl, tmp_path):
        dl._meta_cache_put("https://youtu.be/x", dict(INFO))
        info, _, title = DownloadManager()._extract_with_format_fallback(
            {"format": "b", "outtmpl": str(tmp_path / "t.%(ext)s")}, "https://youtu.be/x", "hint")
        assert info.get("_reused") and title == "Analysed"
        assert len(fake_ydl.created) == 1, "no second extraction"
        assert "_reused" not in dl._META_CACHE["https://youtu.be/x"][1], "cache entry not mutated"

    def test_failed_reuse_falls_back_to_fresh_extraction(self, fake_ydl, tmp_path):
        dl._meta_cache_put("https://youtu.be/x", dict(INFO))
        (tmp_path / "t.mp4.part").write_bytes(b"half")
        fake_ydl.process_raises = yt_dlp.utils.DownloadError("HTTP Error 403: Forbidden")
        info, _, title = DownloadManager()._extract_with_format_fallback(
            {"format": "b", "outtmpl": str(tmp_path / "t.%(ext)s")}, "https://youtu.be/x", "hint")
        assert title == "Fresh"
        assert not (tmp_path / "t.mp4.part").exists(), "partial file must not be resumed"

    def test_no_reuse_without_cache_or_for_playlists(self, fake_ydl, tmp_path):
        dl._meta_cache_put("https://youtu.be/p", {"_type": "playlist", "formats": [1]})
        for url in ("https://youtu.be/none", "https://youtu.be/p"):
            assert DownloadManager()._download_from_analysis(
                {"outtmpl": str(tmp_path / "t")}, url, "h", "b") is None

    def test_reuse_window_is_longer_than_the_wizard_cache(self, monkeypatch):
        dl._META_CACHE.clear()
        dl._META_CACHE["u"] = (time.time() - 600, {"formats": [1]})
        monkeypatch.setattr(dl, "META_CACHE_TTL", 180)
        monkeypatch.setattr(dl, "DOWNLOAD_REUSE_TTL", 1800)
        assert dl._meta_cache_get("u") is None  # too old to show in the wizard
        assert dl._meta_cache_get("u", max_age=1800) is not None  # fine to download
        dl._META_CACHE.clear()


class TestLeanFirstAttempt:
    def test_only_the_first_youtube_attempt_skips_hls(self, fake_ydl, monkeypatch, tmp_path):
        monkeypatch.setattr(dl, "YT_LEAN_DOWNLOAD", True)
        fake_ydl.extract_raises = yt_dlp.utils.DownloadError("Sign in to confirm you're not a bot")
        with pytest.raises(yt_dlp.utils.DownloadError):
            DownloadManager()._extract_with_format_fallback(
                {"format": "b", "outtmpl": str(tmp_path / "t")}, "https://youtu.be/x", "h")
        skips = [((y.params.get("extractor_args") or {}).get("youtube") or {}).get("skip")
                 for y in fake_ydl.created]
        assert skips[0] == ["hls", "translated_subs"]
        assert all(not s for s in skips[1:]), "retries must extract in full (HLS fallback)"


class TestInlinePrefetch:
    async def test_query_starts_a_prefetch(self, monkeypatch, tmp_path):
        inline_cache._reset_for_tests(tmp_path / "c.json")
        seen = []

        async def fake_extract(url):
            seen.append(url)

        monkeypatch.setattr(inl.download_manager, "extract_info", fake_extract)
        inl._PREFETCHING.clear()
        await inl._prefetch("https://youtu.be/x")
        await inl._prefetch("https://youtu.be/x")
        assert seen == ["https://youtu.be/x", "https://youtu.be/x"]  # sequential: allowed again

    async def test_concurrent_prefetch_of_one_link_runs_once(self, monkeypatch):
        gate = asyncio.Event()
        calls = []

        async def slow_extract(url):
            calls.append(url)
            await gate.wait()

        monkeypatch.setattr(inl.download_manager, "extract_info", slow_extract)
        inl._PREFETCHING.clear()
        t1 = asyncio.create_task(inl._prefetch("https://youtu.be/y"))
        await asyncio.sleep(0)
        await inl._prefetch("https://youtu.be/y")  # duplicate while running
        gate.set()
        await t1
        assert calls == ["https://youtu.be/y"]

    async def test_inline_downloads_use_inline_quality(self, monkeypatch, tmp_path):
        inline_cache._reset_for_tests(tmp_path / "c.json")
        seen = {}

        async def fake_download(**kw):
            seen.update(kw)
            return dl.DownloadResult(success=False, error="x", mode="video")

        monkeypatch.setattr(inl, "check_public_url", lambda u: None)
        monkeypatch.setattr(inl, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(inl.rate_limiter, "allow", lambda uid: (True, 0))
        monkeypatch.setattr(inl, "INLINE_QUALITY", "720")
        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)

        class Bot:
            username = "b"

            async def edit_message_caption(self, **kw):
                pass

        chosen = SimpleNamespace(result_id="vp:x", query="https://youtu.be/x",
                                 inline_message_id="I", from_user=SimpleNamespace(id=5))
        await inl.handle_chosen_inline_result(
            SimpleNamespace(chosen_inline_result=chosen), SimpleNamespace(bot=Bot()))
        assert seen["quality"] == "720"


class TestWebClientFormatsNotReplayed:
    def test_web_client_urls_are_dropped_before_reuse(self, fake_ydl, tmp_path, monkeypatch):
        seen = {}

        def capture(self, info, download=True):
            seen["formats"] = [f["format_id"] for f in info["formats"]]
            seen["requested"] = "requested_formats" in info
            return info

        monkeypatch.setattr(FakeYDL, "process_ie_result", capture)
        dl._meta_cache_put("https://youtu.be/w", {
            "id": "w", "title": "T", "extractor": "youtube",
            "requested_formats": [{"format_id": "137"}],
            "formats": [
                {"format_id": "web", "url": "https://rr1.googlevideo.com/videoplayback?c=WEB&itag=18"},
                {"format_id": "mweb", "url": "https://rr1.googlevideo.com/videoplayback?c=MWEB&itag=18"},
                {"format_id": "vis", "url": "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=18"},
            ],
        })
        DownloadManager()._download_from_analysis(
            {"outtmpl": str(tmp_path / "t")}, "https://youtu.be/w", "h", "b")
        assert seen["formats"] == ["vis"] and seen["requested"] is False

    def test_only_web_formats_means_no_reuse(self, fake_ydl, tmp_path):
        dl._meta_cache_put("https://youtu.be/o", {
            "id": "o", "extractor": "youtube",
            "formats": [{"format_id": "web", "url": "https://x.googlevideo.com/v?c=WEB"}],
        })
        assert DownloadManager()._download_from_analysis(
            {"outtmpl": str(tmp_path / "t")}, "https://youtu.be/o", "h", "b") is None
