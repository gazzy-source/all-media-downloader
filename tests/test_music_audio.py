"""Offline song policy regressions, including real codec-copy verification."""

import json
import shutil
import subprocess

import pytest

from bot.services.music_audio import classify_music, music_metadata, select_best_music_audio
from bot.services.downloader import _audio_download_options
from bot.handlers.inline import _key
from bot.services.inline_cache import repeat_key


@pytest.mark.parametrize("info,url,music", [
    ({}, "https://music.youtube.com/watch?v=test", True),
    ({"track": "Song", "artist": "Artist"}, "", True),
    ({"categories": ["Music"]}, "", True),
    ({"title": "Song (Official Music Video)", "artist": "Artist"}, "", True),
    ({"title": "Song (Lyric Video)", "album": "Album"}, "", True),
    ({"title": "Podcast", "categories": ["Music"]}, "", False),
    ({"title": "Interview with a singer", "artist": "Artist"}, "", False),
    ({"title": "Audio tutorial"}, "", False),
    ({"title": "Official Audio"}, "", False),
    ({"categories": ["Music"]}, "https://youtube.com/shorts/test", False),
    ({"track": "Song", "artist": "Artist"}, "https://youtube.com/shorts/test", True),
    ({"title": "Something"}, "", False),
])
def test_classification(info, url, music):
    assert classify_music(info, url).is_music is music


def fmt(codec, bitrate=None, ext=None, **kw):
    return {"format_id": codec + str(bitrate), "acodec": codec, "vcodec": "none",
            "ext": ext or codec, "abr": bitrate, **kw}


def test_telegram_native_vs_original():
    aac, opus = fmt("aac", 128, "m4a"), fmt("opus", 160, "webm")
    assert select_best_music_audio([aac, opus]) == aac
    assert select_best_music_audio([aac, opus], "original") == opus


def test_constrained_aac_does_not_displace_good_mp3():
    aac, mp3 = fmt("aac", 32), fmt("mp3", 192)
    assert select_best_music_audio([aac, mp3]) == mp3


def test_drm_preferred_format_is_unavailable():
    assert select_best_music_audio([fmt("aac", 256, has_drm=True), fmt("mp3", 192)])["acodec"] == "mp3"


def test_lossless_and_mp3():
    flac, mp3 = fmt("flac", 800), fmt("mp3", 192)
    assert select_best_music_audio([flac], "telegram") == flac
    assert select_best_music_audio([flac, fmt("aac", 128, "m4a")], "telegram") == flac
    assert select_best_music_audio([flac, mp3], "original") == flac
    assert select_best_music_audio([mp3]) == mp3


def test_missing_quality_and_channels():
    mono = fmt("aac", audio_channels=1)
    stereo = fmt("aac", audio_channels=2)
    assert select_best_music_audio([mono, stereo]) == stereo
    assert select_best_music_audio([fmt("opus")])["acodec"] == "opus"


def test_size_fallback_and_no_excessive_degradation():
    high, lower = fmt("aac", 160, filesize=60), fmt("aac", 128, filesize=40)
    assert select_best_music_audio([high, lower], max_bytes=50) == lower
    with pytest.raises(ValueError, match="size limit"):
        select_best_music_audio([high, fmt("aac", 32, filesize=10)], max_bytes=50)


def test_metadata_structured_wins_no_channel_artist():
    info = {"track": "Track", "title": "Decorated title", "artist": "Artist",
            "album": "Album", "uploader": "Wrong channel"}
    tagged = music_metadata(info, classify_music(info))
    assert tagged["title"] == "Track" and tagged["artist"] == "Artist"
    assert tagged["album"] == "Album"
    generic = music_metadata({"title": "Lecture", "uploader": "Channel", "album": "Wrong"},
                             classify_music({"title": "Lecture"}))
    assert generic["meta_artist"] == "" and generic["artist"] is None
    assert "album" not in generic


def test_only_obvious_music_title_decorations_removed():
    info = {"title": "Song (Official Audio)", "categories": ["Music"]}
    assert music_metadata(info, classify_music(info))["title"] == "Song"
    info["track"] = "Song (Official Audio)"
    assert music_metadata(info, classify_music(info))["title"] == "Song (Official Audio)"


def options(policy="best"):
    return {"_audio_policy": policy, "format": "bestaudio[ext=m4a]/bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]}


@pytest.mark.parametrize("codec,ext,output", [("aac", "m4a", "m4a"),
    ("mp3", "mp3", "mp3"), ("opus", "webm", "m4a"), ("flac", "flac", "best")])
def test_song_options(codec, ext, output):
    source = fmt(codec, 128, ext)
    configured, clean = _audio_download_options(options(),
        {"track": "Track", "artist": "Artist", "formats": [source]}, "")
    assert configured["format"] == source["format_id"]
    assert configured["postprocessors"][0]["preferredcodec"] == output
    assert clean["title"] == "Track"
    assert clean["_transcoded"] is (codec == "opus")


def test_generic_keeps_legacy_policy_and_cache_distinct():
    opts = options()
    configured, _ = _audio_download_options(opts, {"title": "Lecture"}, "")
    assert configured["format"] == opts["format"]
    assert _key("audio") == "audio@best"
    assert repeat_key("audio", audio_format="m4a") == "audio@m4a"
    from bot.config import INLINE_QUALITY
    assert _key("video") == repeat_key("video", INLINE_QUALITY)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs FFmpeg")
@pytest.mark.parametrize("codec,ext", [("aac", "m4a"), ("libmp3lame", "mp3")])
def test_native_extract_does_not_encode(tmp_path, codec, ext):
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP
    source = tmp_path / f"tiny.{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.2", "-c:a", codec, str(source)], check=True)
    before = source.read_bytes()
    with YoutubeDL({"quiet": True}) as ydl:
        pp = FFmpegExtractAudioPP(ydl, preferredcodec=ext, preferredquality="0")
        pp.run({"filepath": str(source), "ext": ext})
    assert source.read_bytes() == before
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                            "-of", "json", str(source)], capture_output=True, text=True, check=True)
    assert json.loads(probe.stdout)["streams"][0]["codec_name"] == ("aac" if ext == "m4a" else "mp3")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs FFmpeg")
@pytest.mark.parametrize("codec,ext,target,encodes", [
    ("aac", "aac", "m4a", 0), ("libopus", "opus", "mp3", 1),
    ("flac", "flac", "best", 0)])
def test_remux_single_encode_and_lossless(tmp_path, codec, ext, target, encodes):
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP
    source = tmp_path / f"tiny.{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.2", "-c:a", codec, str(source)], check=True)
    operations = []
    with YoutubeDL({"quiet": True}) as ydl:
        pp = FFmpegExtractAudioPP(ydl, preferredcodec=target, preferredquality="0")
        original = pp.run_ffmpeg
        def tracked(path, output, acodec, more_opts):
            operations.append(acodec)
            return original(path, output, acodec, more_opts)
        pp.run_ffmpeg = tracked
        _, result = pp.run({"filepath": str(source), "ext": ext})
    assert sum(c != "copy" for c in operations) == encodes
    if ext == "aac":
        assert operations == ["copy"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                            "-of", "json", result["filepath"]], capture_output=True, text=True, check=True)
    expected = {"m4a": "aac", "mp3": "mp3", "best": "flac"}[target]
    assert json.loads(probe.stdout)["streams"][0]["codec_name"] == expected


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs FFmpeg")
def test_complete_local_song_pipeline_with_optional_artwork_failure(tmp_path, monkeypatch):
    from bot.services.downloader import _process_audio_download
    from yt_dlp.postprocessor.embedthumbnail import EmbedThumbnailPP
    from yt_dlp.utils import PostProcessingError
    source = tmp_path / "source.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.2", "-c:a", "aac", str(source)], check=True)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"broken cover")
    def fail(*args):
        raise PostProcessingError("fixture artwork failure")
    monkeypatch.setattr(EmbedThumbnailPP, "run", fail)
    opts = options()
    opts.update({"enable_file_urls": True, "quiet": True,
                 "outtmpl": str(tmp_path / "delivered.%(ext)s")})
    opts["postprocessors"] += [
        {"key": "FFmpegMetadata", "add_metadata": True},
        {"key": "EmbedThumbnail", "already_have_thumbnail": True}]
    native = fmt("aac", 128, "m4a", url=source.as_uri())
    info = {"id": "fixture", "title": "Wrong video title", "track": "Track",
            "artist": "Artist", "album": "Album", "album_artist": "Album artist",
            "track_number": 2, "release_year": 2020, "uploader": "Wrong channel",
            "formats": [native], "thumbnails": [{"url": cover.as_uri(), "filepath": str(cover)}]}
    result = _process_audio_download(opts, info, "https://music.youtube.com/watch?v=fixture")
    delivered = tmp_path / "delivered.m4a"
    assert delivered.exists() and result["_transcoded"] is False
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
        "stream=codec_name:format_tags=title,artist,album,album_artist,track,date", "-of", "json",
        str(delivered)], capture_output=True, text=True, check=True)
    data = json.loads(probe.stdout)
    assert data["streams"][0]["codec_name"] == "aac"
    assert data["format"]["tags"]["title"] == "Track"
    assert data["format"]["tags"]["artist"] == "Artist"
    assert data["format"]["tags"]["album"] == "Album"
    def audio_hash(path):
        return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a",
            "-f", "hash", "-"], capture_output=True, text=True, check=True).stdout
    assert audio_hash(source) == audio_hash(delivered)
