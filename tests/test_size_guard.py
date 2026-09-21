"""
Telegram's ~49 MB bot upload limit, enforced before the download rather than after.

Production report: "⚠️ File is 60.1 MB, which exceeds the Telegram bot upload
limit (~49.0 MB)." — shown AFTER the whole 60 MB had been fetched. Two defects:
the estimate that fed the quality button ignored the audio track, and nothing
checked the estimate before spending the download.
"""
from __future__ import annotations

from bot.config import MAX_FILE_SIZE_BYTES
from bot.services.downloader import _parse_formats, quality_buttons_meta

MB = 1024 * 1024


def _info(formats):
    return {"duration": 120, "formats": formats}


class TestEstimateIncludesAudio:
    def test_dash_estimate_adds_the_audio_track(self):
        """
        A video-only 1080p track is muxed with audio, so the delivered file is
        the sum. Reporting only the video track is what made a 60 MB file look
        like 55 MB on the button.
        """
        _, _, _, _, _, sizes = _parse_formats(_info([
            {"vcodec": "avc1", "acodec": "none", "height": 1080,
             "filesize": 55 * MB, "ext": "mp4"},
            {"vcodec": "none", "acodec": "mp4a", "filesize": 5 * MB, "ext": "m4a"},
        ]))
        assert sizes["1080"] == 60 * MB

    def test_progressive_estimate_is_not_double_counted(self):
        """A progressive format already carries audio — adding it again inflates."""
        _, _, _, _, _, sizes = _parse_formats(_info([
            {"vcodec": "avc1", "acodec": "mp4a", "height": 480,
             "filesize": 10 * MB, "ext": "mp4"},
            {"vcodec": "none", "acodec": "mp4a", "filesize": 5 * MB, "ext": "m4a"},
        ]))
        # QUALITY_MAP tiers are 480/720/1080/max — a 480p source fills the 480 tier.
        assert sizes["480"] == 10 * MB

    def test_no_audio_track_leaves_estimate_alone(self):
        _, _, _, _, _, sizes = _parse_formats(_info([
            {"vcodec": "avc1", "acodec": "none", "height": 720,
             "filesize": 20 * MB, "ext": "mp4"},
        ]))
        assert sizes["720"] == 20 * MB

    def test_missing_filesize_produces_no_estimate(self):
        """Never invent a number — an absent filesize must stay absent."""
        _, _, _, _, _, sizes = _parse_formats(_info([
            {"vcodec": "avc1", "acodec": "none", "height": 720, "ext": "mp4"},
            {"vcodec": "none", "acodec": "mp4a", "filesize": 5 * MB, "ext": "m4a"},
        ]))
        assert "720" not in sizes or not sizes.get("720")


class TestOverLimitIsVisible:
    def test_over_limit_quality_is_marked(self):
        metas = quality_buttons_meta([1080], {"1080": MAX_FILE_SIZE_BYTES + MB})
        label = next(m["label"] for m in metas if m["key"] == "1080")
        assert "⚠️" in label, label

    def test_fitting_quality_is_not_marked(self):
        metas = quality_buttons_meta([720], {"720": 10 * MB})
        label = next(m["label"] for m in metas if m["key"] == "720")
        assert "⚠️" not in label, label
        assert "10.0 MB" in label

    def test_exactly_at_the_limit_is_allowed(self):
        """The check is strictly greater-than; the boundary itself still fits."""
        metas = quality_buttons_meta([720], {"720": MAX_FILE_SIZE_BYTES})
        label = next(m["label"] for m in metas if m["key"] == "720")
        assert "⚠️" not in label, label
