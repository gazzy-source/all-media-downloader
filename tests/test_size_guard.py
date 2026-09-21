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
