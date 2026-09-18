"""Real end-to-end functionality tests — actual extraction, download, conversion.

These tests hit the live network (public test media, no cookies, no proxy)
and verify ACCURACY of the pipelines:

  - video pipeline downloads a real file (verified with ffprobe)
  - audio pipeline converts via the real FFmpegExtractAudio postprocessor
  - quality caps are respected (ffprobe height checks)
  - error paths produce friendly messages, not crashes
  - parallel downloads all succeed under the semaphore
  - result cleanup actually removes files

Opt-in: run with RUN_LIVE_TESTS=1 (kept out of CI by default).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="live network tests: set RUN_LIVE_TESTS=1",
    ),
]

# Stable public test media. "Me at the zoo" is the oldest video on YouTube:
# 19s, 240p max, always public — so even the "max quality" case stays a tiny
# download. The previous fixtures died upstream (vimeo.com/76979871 now
# demands a login, and yt-dlp's own BaW_jenozKc test video was removed).
YOUTUBE_SHORT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
DIRECT_MP4 = YOUTUBE_SHORT  # same source, different pipelines

from bot.services.downloader import DownloadResult, download_manager  # noqa: E402
from bot.utils.ffmpeg import find_ffmpeg  # noqa: E402


def _ffprobe(path: Path) -> dict:
    """Real ffprobe of a downloaded file — proves it's valid media."""
    ff = find_ffmpeg()
    assert ff is not None, "FFmpeg required for functional verification"
    ffprobe = ff.parent / ("ffprobe.exe" if ff.suffix.lower() == ".exe" else "ffprobe")
    out = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, f"ffprobe failed on {path}: {out.stderr[:300]}"
    return json.loads(out.stdout)


def _streams(probe: dict, kind: str) -> list[dict]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == kind]


async def _wait_for_slot() -> None:
    """Functional tests share the global manager's semaphore — don't fight CI runs."""
    while download_manager.active >= download_manager.max_concurrent:
        await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# Video pipeline
# ---------------------------------------------------------------------------


class TestVideoPipeline:
    async def test_video_download_real_file(self, tmp_path):
        await _wait_for_slot()
        result: DownloadResult = await download_manager.download(
            url=DIRECT_MP4, mode="video", quality="max", title_hint="func_video"
        )
        assert result.success, f"video download failed: {result.error}"
        assert result.primary is not None and result.primary.exists()
        assert result.file_size > 100_000, "real video should be >100KB"
        assert result.is_video

        probe = _ffprobe(result.primary)
        videos = _streams(probe, "video")
        audios = _streams(probe, "audio")
        assert videos, "no video stream in output"
        assert audios, "video mode must include audio"
        assert videos[0]["codec_name"] in ("h264", "hevc", "vp8", "vp9", "av1")

    async def test_quality_cap_480_respected(self):
        await _wait_for_slot()
        result = await download_manager.download(
            url=DIRECT_MP4, mode="video", quality="480", title_hint="func_480"
        )
        assert result.success, f"480p download failed: {result.error}"
        probe = _ffprobe(result.primary)
        heights = [s.get("height", 0) for s in _streams(probe, "video")]
        assert heights and max(heights) <= 500, f"quality cap violated: {heights}"

    async def test_concurrent_downloads_all_succeed(self):
        async def one(i: int) -> DownloadResult:
            await _wait_for_slot()
            return await download_manager.download(
                url=DIRECT_MP4, mode="video", quality="480",
                title_hint=f"func_conc_{i}",
            )

        results = await asyncio.gather(*(one(i) for i in range(3)))
        for i, r in enumerate(results):
            assert r.success, f"parallel job {i} failed: {r.error}"
            assert r.primary.exists()

    def test_cleanup_removes_files(self, tmp_path):
        f = tmp_path / "gone.mp4"
        f.write_bytes(b"x" * 1024)

        class FakeRes:
            files = [f]
            subtitle_file = None

        download_manager.cleanup_result_files(FakeRes())  # type: ignore[arg-type]
        assert not f.exists()


# ---------------------------------------------------------------------------
# Audio pipeline (real FFmpeg conversion)
# ---------------------------------------------------------------------------


class TestAudioPipeline:
    @pytest.mark.parametrize("fmt", ["mp3", "m4a", "opus"])
    async def test_audio_conversion_formats(self, fmt):
        await _wait_for_slot()
        result = await download_manager.download(
            url=DIRECT_MP4, mode="audio", audio_format=fmt, title_hint=f"func_audio_{fmt}"
        )
        assert result.success, f"{fmt} conversion failed: {result.error}"
        assert result.primary.suffix.lower() == f".{fmt}", \
            f"expected .{fmt}, got {result.primary.name}"
        assert result.is_audio and not result.is_video

        probe = _ffprobe(result.primary)
        audios = _streams(probe, "audio")
        assert audios, f"no audio stream in {fmt} output"
        expected_codec = {"mp3": "mp3", "m4a": "aac", "opus": "opus"}[fmt]
        assert audios[0]["codec_name"] == expected_codec, \
            f"{fmt} request produced codec {audios[0]['codec_name']}"
        assert not _streams(probe, "video"), "audio mode must not contain video stream"

    async def test_audio_reasonable_bitrate(self):
        await _wait_for_slot()
        result = await download_manager.download(
            url=DIRECT_MP4, mode="audio", audio_format="mp3", title_hint="func_audio_br"
        )
        assert result.success, result.error
        probe = _ffprobe(result.primary)
        rate = _streams(probe, "audio")[0].get("bit_rate")
        if rate:  # some containers omit stream bit_rate
            assert 96_000 <= int(rate) <= 320_000, f"bitrate {rate} outside 192k target"


# ---------------------------------------------------------------------------
# Error paths — accuracy of friendly failures
# ---------------------------------------------------------------------------


class TestErrorAccuracy:
    async def test_unsupported_url_fails_gracefully(self):
        result = await download_manager.download(
            url="https://example.com/not-a-video", mode="video", title_hint="err"
        )
        assert result.success is False
        assert result.error, "must carry a friendly error message"
        assert "traceback" not in result.error.lower()

    async def test_nonexistent_video_fails_gracefully(self):
        result = await download_manager.download(
            url="https://vimeo.com/00000000000000000", mode="video", title_hint="err2"
        )
        assert result.success is False
        assert result.error

    async def test_malformed_url_fails_gracefully(self):
        result = await download_manager.download(
            url="not a url at all", mode="video", title_hint="err3"
        )
        assert result.success is False
        assert result.error


# ---------------------------------------------------------------------------
# Extraction accuracy (metadata feeding the wizard)
# ---------------------------------------------------------------------------


class TestExtractionAccuracy:
    async def test_extract_info_real_metadata(self):
        info = await download_manager.extract_info(DIRECT_MP4)
        assert info.title, "title must be extracted"
        assert "youtube" in (info.extractor or "").lower()
        assert info.has_video, "test clip must report video"
        assert info.available_heights, "heights must be parsed for the quality wizard"
        # Fixture-independent: the ladder must be clean, sorted and positive.
        # (This clip is 240p max — asserting a floor here only tested the URL.)
        assert all(h > 0 for h in info.available_heights)
        assert info.available_heights == sorted(set(info.available_heights))
        html = info.summary_html()
        assert info.title.split(":")[0][:20] in html or "<b>" in html

    async def test_repeated_extraction_consistent(self):
        """Sticky-strategy cache must not corrupt subsequent extractions."""
        a = await download_manager.extract_info(DIRECT_MP4)
        b = await download_manager.extract_info(DIRECT_MP4)
        assert a.title == b.title
        assert a.available_heights and b.available_heights, "heights stable across runs"
        assert max(a.available_heights) == max(b.available_heights), \
            "best available height must be reproducible"


# ---------------------------------------------------------------------------
# Full handler flow with REAL extraction, fake UI
# ---------------------------------------------------------------------------


class TestHandlerWithRealExtraction:
    async def test_wizard_flow_real_youtube_link(self, fx, monkeypatch):
        """start_url_flow against a real video: session must be created with
        accurate metadata and the mode keyboard shown."""
        from bot.handlers import download as hd
        from bot.services.session import sessions

        monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
        msg = fx.msg(YOUTUBE_SHORT)
        await hd.start_url_flow(fx.update(msg), fx.ctx, YOUTUBE_SHORT)

        s = sessions.get_for_user(42)
        assert s is not None, "session must be created from real extraction"
        assert s.title and s.platform == "YouTube"
        status = msg.children[0]
        assert status.edits and "Select download type" in status.edits[-1][0]
        assert status.edits[-1][1].get("reply_markup") is not None
        sessions.remove(s.session_id)
