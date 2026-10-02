"""Forward-only progress across video+audio parts, step labels, Telegram cover."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

import bot.handlers.download as hd
import bot.services.downloader as dl


def test_part_detection():
    assert dl._part_kind({"filename": "/t/song.f137.mp4", "info_dict": {"vcodec": "avc1"}}) == "video"
    assert dl._part_kind({"filename": "/t/song.f140.m4a.part", "info_dict": {"vcodec": "none"}}) == "audio"
    assert dl._part_kind({"filename": "/t/song.mp4", "info_dict": {"vcodec": "avc1"}}) == "single"


def test_progress_never_goes_backwards_across_parts(monkeypatch, tmp_path):
    import yt_dlp

    monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
    emitted: list[tuple[float, str]] = []
    hooks = {}

    def capture(self, opts, url, title_hint, **kw):
        hooks["progress"] = opts["progress_hooks"][1]
        hooks["pp"] = opts["postprocessor_hooks"][0]
        raise yt_dlp.utils.DownloadError("stop")

    monkeypatch.setattr(dl.DownloadManager, "_extract_with_format_fallback", capture)
    dl.DownloadManager()._download_sync(
        url="https://youtu.be/x", mode="video", quality="720", subtitle_lang=None,
        audio_format="mp3", title_hint="t",
        progress_cb=lambda pct, msg: emitted.append((pct, msg)), loop=None)

    vid = {"filename": "t.f137.mp4", "info_dict": {"vcodec": "avc1"}, "total_bytes": 100}
    aud = {"filename": "t.f140.m4a", "info_dict": {"vcodec": "none"}, "total_bytes": 10}
    for done in (10, 50, 100):
        hooks["progress"]({**vid, "status": "downloading", "downloaded_bytes": done})
    hooks["progress"]({**vid, "status": "finished"})
    for done in (1, 10):
        hooks["progress"]({**aud, "status": "downloading", "downloaded_bytes": done})
    hooks["progress"]({**aud, "status": "finished"})
    hooks["pp"]({"status": "started", "postprocessor": "Merger"})

    pcts = [p for p, _ in emitted if p > 0]
    assert pcts == sorted(pcts), f"bar went backwards: {pcts}"
    assert sum("Finishing" in m for _, m in emitted) == 1, "video half must not say Finishing"
    assert "Joining video + audio" in emitted[-1][1]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_cover_is_square_small_jpeg(tmp_path):
    import subprocess

    src = tmp_path / "Song.jpg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=1280x720", "-frames:v", "1", str(src)], check=True)
    cover = dl._telegram_cover(tmp_path)
    assert cover is not None and cover.stat().st_size < 200_000
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(cover)],
                           capture_output=True, text=True).stdout.strip()
    assert probe == "320,320"


async def test_audio_is_sent_with_its_cover(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"a" * 10)
    cover = tmp_path / "tg_cover.jpg"
    cover.write_bytes(b"\xff\xd8jpeg")
    res = dl.DownloadResult(success=True, files=[f], primary=f, title="Song", mode="audio",
                            is_audio=True, artist="A", cover=cover)
    seen = {}

    class Bot:
        async def send_chat_action(self, *a, **k):
            pass

        async def send_audio(self, chat_id, **kw):
            seen.update(kw)

    await hd._send_media_once(SimpleNamespace(bot=Bot()), 1, f, res, "", f.name)
    assert seen["thumbnail"] is not None and seen["performer"] == "A"


def test_audio_keeps_the_cover_jpg_for_telegram(monkeypatch, tmp_path):
    import yt_dlp

    monkeypatch.setattr(dl, "TEMP_DIR", tmp_path)
    seen = {}

    def capture(self, opts, url, title_hint, **kw):
        seen.update(opts)
        raise yt_dlp.utils.DownloadError("stop")

    monkeypatch.setattr(dl.DownloadManager, "_extract_with_format_fallback", capture)
    dl.DownloadManager()._download_sync(
        url="https://youtu.be/x", mode="audio", quality="720", subtitle_lang=None,
        audio_format="mp3", title_hint="t", progress_cb=None, loop=None)
    embed = [p for p in seen["postprocessors"] if p["key"] == "EmbedThumbnail"][0]
    assert embed["already_have_thumbnail"] is True
