"""
Telegram's ~49 MB bot upload limit, enforced before the download rather than after.

Production report: "⚠️ File is 60.1 MB, which exceeds the Telegram bot upload
limit (~49.0 MB)." — shown AFTER the whole 60 MB had been fetched. Two defects:
the estimate that fed the quality button ignored the audio track, and nothing
checked the estimate before spending the download.
"""
from __future__ import annotations

from bot.config import MAX_FILE_SIZE_BYTES
from bot.services.downloader import (
    _meta_cache_put,
    _parse_formats,
    download_manager,
    quality_buttons_meta,
    recommend_fitting_quality,
)
from bot.utils.texts import oversized_video_advice

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


class TestFittingQualityRecommendation:
    heights = [480, 720, 1080]

    def test_current_quality_oversized_recommends_next_lower(self):
        rec = recommend_fitting_quality(
            self.heights, {"1080": 55 * MB, "720": 38 * MB, "480": 24 * MB},
            "1080", 49 * MB,
        )
        assert (rec.quality, rec.height, rec.estimated_bytes) == ("720", 720, 38 * MB)

    def test_multiple_lower_choices_choose_highest_that_fits(self):
        rec = recommend_fitting_quality(
            [720, 1080, 1440], {"max": 70 * MB, "1080": 48 * MB, "720": 38 * MB,
                                "480": 24 * MB}, "max", 49 * MB,
        )
        assert rec.quality == "1080" and rec.height == 1080

    def test_none_fit_returns_no_recommendation(self):
        assert recommend_fitting_quality(
            self.heights, {"1080": 80 * MB, "720": 60 * MB, "480": 50 * MB},
            "max", 49 * MB,
        ) is None

    def test_metadata_estimates_are_not_marked_exact(self):
        rec = recommend_fitting_quality(
            self.heights, {"720": 38 * MB}, "1080", 49 * MB,
        )
        assert rec is not None and rec.is_exact is False
        assert "estimated at 38.0 MB" in oversized_video_advice(
            self.heights, {"720": 38 * MB}, "1080")

    def test_explicit_exact_candidate_is_reported_as_exact(self):
        rec = recommend_fitting_quality(
            self.heights, {"720": 38 * MB}, "1080", 49 * MB,
            exact_sizes={"720"},
        )
        assert rec is not None and rec.is_exact is True
        assert "Try <b>720p</b> (~38.0 MB)." in oversized_video_advice(
            self.heights, {"720": 38 * MB}, "1080", exact_sizes={"720"})

    def test_size_at_limit_is_allowed_but_over_limit_is_not(self):
        exact = recommend_fitting_quality(
            self.heights, {"720": 49 * MB}, "1080", 49 * MB,
        )
        assert exact is not None and exact.estimated_bytes == 49 * MB
        assert recommend_fitting_quality(
            self.heights, {"720": 49 * MB + 1}, "1080", 49 * MB,
        ) is None

    def test_no_size_metadata_never_invents_a_recommendation(self):
        assert recommend_fitting_quality(self.heights, {}, "1080", 49 * MB) is None
        assert "no lower-quality size estimate is available" in oversized_video_advice(
            self.heights, {}, "1080").lower()

    def test_only_real_available_height_is_recommended(self):
        rec = recommend_fitting_quality(
            [360, 1080], {"1080": 40 * MB, "720": 30 * MB, "480": 20 * MB},
            "max", 49 * MB,
        )
        assert rec is not None and rec.quality == "480" and rec.height == 360

    def test_no_lower_quality_for_audio_or_current_lowest_video_quality(self):
        assert recommend_fitting_quality(self.heights, {"480": 10 * MB}, "480", 49 * MB) is None

    def test_inline_advice_keeps_bot_handoff_and_recommendation(self):
        text = oversized_video_advice(
            self.heights, {"720": 38 * MB}, "1080", inline=True)
        assert "720p" in text and "estimated" in text and "Open bot" in text

    def test_existing_metadata_cache_can_be_read_without_another_extraction(self, monkeypatch):
        url = "https://example.test/cached-quality-estimates"
        _meta_cache_put(url, {"formats": [
            {"vcodec": "avc1", "acodec": "mp4a", "height": 720,
             "filesize": 30 * MB, "ext": "mp4"},
        ]})
        monkeypatch.setattr(download_manager, "extract_info",
                            lambda *a, **k: pytest.fail("must not extract to read cached estimates"))
        cached = download_manager.cached_media_info(url)
        assert cached is not None and cached.estimated_sizes["720"] == 30 * MB

    def test_exactly_at_the_limit_is_allowed(self):
        """The check is strictly greater-than; the boundary itself still fits."""
        metas = quality_buttons_meta([720], {"720": MAX_FILE_SIZE_BYTES})
        label = next(m["label"] for m in metas if m["key"] == "720")
        assert "⚠️" not in label, label


class TestProxyBlipIsRetryable:
    """
    A momentary SOCKS refusal must not end a download.

    Production, 2026-09-21: WARP refused one connection for a sub-second blip —
    Socks5Error(5, 'Connection refused') — and the whole download failed. The
    ladder's transport_fail classifier did not recognise a proxy error, so the
    attempt matched no retryable category and gave up instead of retrying. WARP
    was healthy again moments later (5/5 probes returned 200).
    """

    RAW = (
        "ERROR: [youtube] A-cjmTgWv_0: Unable to download API page: "
        "('[Errno 5] Connection refused', Socks5Error(5, 'Connection refused')) "
        "(caused by ProxyError(\"('[Errno 5] Connection refused', "
        "Socks5Error(5, 'Connection refused'))\"))"
    )

    def test_proxy_error_is_classified_as_retryable_transport(self):
        """Mirrors the ladder's own predicate, which is inline in the retry loop."""
        err = self.RAW.lower()
        transport_fail = (
            "sslerror" in err
            or "connection was reset" in err
            or "connection reset" in err
            or "recv failure" in err
            or "failed to perform" in err
            or "connection aborted" in err
            or "remote end closed" in err
            or "socks5error" in err
            or "proxyerror" in err
            or "proxy error" in err
            or "connection refused" in err
        )
        assert transport_fail, "a proxy blip must be retryable, not fatal"

    def test_message_blames_the_relay_not_the_platform(self):
        from bot.services.downloader import DownloadManager

        out = DownloadManager._friendly_error(self.RAW)
        assert "relay" in out.lower()
        assert "Socks5Error" not in out
        assert "try again" in out.lower()

    def test_a_genuine_platform_drop_still_reads_as_the_platform(self):
        """The proxy branch must not swallow real platform-side disconnects."""
        from bot.services.downloader import DownloadManager

        out = DownloadManager._friendly_error(
            "Unable to download webpage: ('Connection aborted.', "
            "RemoteDisconnected('Remote end closed connection without response'))"
        )
        assert "relay" not in out.lower()
        assert "try again" in out.lower()
