"""Unit tests for cookie sanitizer, downloader internals, and strategies."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import bot.services.downloader as dl
from bot.services.downloader import (
    DownloadResult,
    _parse_formats,
    _platform_flags,
    _video_format_for_host,
    _yt_strategies,
    available_qualities_for,
    quality_buttons_meta,
)


class TestYtStrategies:
    """The cookieless-first strategy config — the heart of the no-cookies fix."""

    def test_cookieless_first_strategy(self):
        strats = _yt_strategies(has_cookies=False)
        assert strats[0] == {"use_cookies": False}

    def test_no_dead_clients_anywhere(self):
        # tv_embedded was removed from yt-dlp >= 2026.x and must never return
        for with_cookies in (True, False):
            for s in _yt_strategies(has_cookies=with_cookies):
                assert "tv_embedded" not in str(s)

    def test_cookie_strategy_keeps_cookies_before_fallbacks(self):
        strats = _yt_strategies(has_cookies=True)
        assert strats[0]["use_cookies"] is False   # cookieless default first
        assert strats[1]["use_cookies"] is True    # cookies for age-restricted
        assert sum(1 for s in strats if "extractor_args" in s) == 2

    def test_fallbacks_drop_impersonate(self):
        for s in _yt_strategies(has_cookies=False)[1:]:
            assert s.get("drop_impersonate") is True


class TestPlatformFlags:
    @pytest.mark.parametrize(
        ("host", "flag"),
        [
            ("www.youtube.com", "yt"),
            ("youtu.be", "yt"),
            ("music.youtube.com", "yt"),
            ("instagram.com", "ig"),
            ("tiktok.com", "tt"),
            ("x.com", "x"),
            ("mobile.twitter.com", "x"),
            ("facebook.com", "fb"),
            ("fb.watch", "fb"),
            ("pinterest.com", "pin"),
            ("i.pinimg.com", "pin"),
            ("reddit.com", "rd"),
            ("v.redd.it", "rd"),
        ],
    )
    def test_flags(self, host, flag):
        assert _platform_flags(host)[flag] is True

    def test_unknown_host_all_false(self):
        flags = _platform_flags("example.org")
        assert not any(flags.values())


class TestVideoFormatForHost:
    def test_quality_cap_respected_generic(self):
        f = _video_format_for_host("example.org", "480")
        assert "height<=480" in f

    def test_max_quality(self):
        f = _video_format_for_host("example.org", "max")
        assert f == dl.FORMAT_FALLBACK

    def test_platform_specific_simple_selectors(self):
        assert _video_format_for_host("tiktok.com", "1080") == "b/best"
        assert _video_format_for_host("x.com", "480") == "b/best"
        assert _video_format_for_host("reddit.com", "max") == "b/best"

    def test_instagram_caps_height(self):
        f = _video_format_for_host("instagram.com", "720")
        assert "height<=720" in f

    def test_youtube_uses_quality_map(self):
        f = _video_format_for_host("youtube.com", "1080")
        assert "height<=1080" in f

    def test_unknown_quality_falls_back_to_1080(self):
        f = _video_format_for_host("example.org", "nonsense")
        assert "height<=1080" in f


class TestAvailableQualities:
    def test_empty_heights_full_menu(self):
        assert available_qualities_for([]) == ["max", "1080", "720", "480"]

    def test_low_res_source(self):
        qs = available_qualities_for([360])
        assert "480" in qs and "max" in qs

    def test_hd_source(self):
        qs = available_qualities_for([720, 1080])
        assert "720" in qs
        assert "max" in qs
        # dedupe / ordering sanity
        assert len(qs) == len(set(qs))

    def test_4k_source(self):
        qs = available_qualities_for([2160])
        assert "1080" in qs and "720" in qs and "max" in qs

    def test_buttons_meta_structure(self):
        metas = quality_buttons_meta([720, 1080], {"720": 10_000_000})
        assert all({"key", "label"} <= set(m) for m in metas)
        assert any("10" in m["label"] for m in metas)  # size estimate included


class TestParseFormats:
    def _info(self, formats=None, **kw):
        info = {"formats": formats or [], "title": "t"}
        info.update(kw)
        return info

    def test_video_and_audio_detection(self):
        hv, ha, hi, heights, _, _ = _parse_formats(self._info([
            {"vcodec": "avc1", "acodec": "mp4a", "height": 720, "ext": "mp4"},
            {"vcodec": "avc1", "acodec": "none", "height": 1080, "ext": "mp4"},
        ]))
        assert hv and ha
        assert 720 in heights and 1080 in heights

    def test_image_by_ext(self):
        hv, ha, hi, _, imgs, _ = _parse_formats(
            self._info(ext="jpg", width=800, height=600)
        )
        assert hi and not hv
        assert (800, 600) in imgs

    def test_thumbnails_fallback_when_no_media(self):
        hv, ha, hi, _, imgs, _ = _parse_formats(self._info(
            thumbnails=[{"width": 100, "height": 100}, {"width": 200, "height": 200}]
        ))
        assert hi and not hv and not ha
        assert len(imgs) == 2

    def test_audio_only(self):
        hv, ha, hi, _, _, _ = _parse_formats(self._info([
            {"vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
        ]))
        assert ha and not hv

    def test_storyboard_not_image(self):
        # storyboard mhtml entries must not be treated as downloadable images
        hv, ha, hi, _, _, _ = _parse_formats(self._info([
            {"vcodec": "none", "acodec": "none", "ext": "mhtml",
             "format_note": "storyboard", "width": 640, "height": 360},
            {"vcodec": "avc1", "acodec": "mp4a", "height": 480, "ext": "mp4"},
        ]))
        assert hv and not hi

    def test_formats_fallback_marks_video_audio(self):
        hv, ha, hi, _, _, _ = _parse_formats(self._info([
            {"vcodec": "unknown", "acodec": "unknown", "ext": "mp4"},
        ]))
        assert hv and ha

    def test_estimated_sizes_track_quality(self):
        *_, sizes = _parse_formats(self._info([
            {"vcodec": "avc1", "acodec": "none", "height": 480,
             "ext": "mp4", "filesize": 100},
            {"vcodec": "avc1", "acodec": "none", "height": 480,
             "ext": "mp4", "filesize": 300},
        ]))
        assert sizes["480"] == 300  # largest seen per tier


class TestFriendlyError:
    @pytest.mark.parametrize(
        ("msg", "expect"),
        [
            ("Sign in to confirm you're not a bot", "YouTube blocked"),
            ("HTTP Error 403: Forbidden", "403"),
            ("This video is unavailable", "unavailable"),
            ("Unsupported URL", "not supported"),
            ("ffprobe failed", "FFmpeg"),
            ("The download timed out", "timed out"),
            ("requested format is not available", "quality/format"),
        ],
    )
    def test_mapping(self, msg, expect):
        out = dl.DownloadManager._friendly_error(msg)
        assert expect.lower() in out.lower()

    def test_format_error_not_mislabeled_region(self):
        out = dl.DownloadManager._friendly_error("Requested format is not available")
        assert "region" not in out.lower()
        assert "country" not in out.lower()

    def test_passthrough_unknown(self):
        assert dl.DownloadManager._friendly_error("weird custom failure") == \
            "weird custom failure"


class TestImageErrorHeuristics:
    def test_looks_like_image_only(self):
        f = dl.DownloadManager._looks_like_image_only_error
        assert f("There is no video on this link") is True
        assert f("pinterest: no video formats") is True
        # 403 must NOT trigger image retry
        assert f("HTTP Error 403: Forbidden") is False
        assert f("") is False

    def test_cleanup_result_files_removes_files_and_dirs(self, tmp_path):
        work = tmp_path / "dl_x"
        work.mkdir()
        f1 = work / "a.mp4"
        f1.write_bytes(b"x" * 10)
        res = DownloadResult(success=True, files=[f1], primary=f1)
        dl.DownloadManager.cleanup_result_files(res)
        assert not f1.exists()
        assert not work.exists()


class TestStickyWinnerOrdering:
    def test_remember_and_order_roundtrip(self):
        # ensure the remembered index round-trips through module globals
        old = dl._YT_WINNER_SI
        try:
            dl._remember_yt_strategy(2)
            assert dl._YT_WINNER_SI == 2
        finally:
            dl._remember_yt_strategy(old)


class TestBaseOpts:
    def test_no_forced_yt_clients_in_base_opts(self):
        """Regression: _base_opts must not pin player clients (breaks cookieless)."""
        opts = dl._base_opts(host="www.youtube.com", cookiefile=None)
        ea = opts.get("extractor_args") or {}
        yt = ea.get("youtube") or {}
        assert "player_client" not in yt, "forced clients break cookieless path"

    def test_yt_gets_pot_provider_arg(self, monkeypatch):
        monkeypatch.setattr(dl, "POT_PROVIDER_URL", "http://pot:4416")
        opts = dl._base_opts(host="www.youtube.com")
        assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == "http://pot:4416"

    def test_pot_default_localhost(self, monkeypatch):
        monkeypatch.setattr(dl, "POT_PROVIDER_URL", None)
        opts = dl._base_opts(host="www.youtube.com")
        assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == \
            "http://127.0.0.1:4416"

    def test_non_yt_no_pot_arg(self):
        opts = dl._base_opts(host="vimeo.com")
        assert "youtubepot-bgutilhttp" not in (opts.get("extractor_args") or {})

    def test_impersonate_skipped_for_yt(self, monkeypatch):
        monkeypatch.setattr(dl, "_IMPERSONATE", object())
        monkeypatch.setattr(dl, "_IMPERSONATE_RESOLVED", True)
        assert "impersonate" not in dl._base_opts(host="youtube.com")
        assert "impersonate" in dl._base_opts(host="instagram.com")

    def test_cookies_attached_only_if_file_exists(self, tmp_path):
        real = tmp_path / "c.txt"
        real.write_text("# Netscape HTTP Cookie File\n")
        opts = dl._base_opts(host="example.org", cookiefile=real)
        assert opts["cookiefile"] == str(real)
        fake = tmp_path / "nope.txt"
        opts2 = dl._base_opts(host="example.org", cookiefile=fake)
        assert "cookiefile" not in opts2

    def test_proxy_passed_through(self, monkeypatch):
        monkeypatch.setattr(dl, "PROXY", "socks5://u:p@h:1080")
        assert dl._base_opts(host="x.com")["proxy"] == "socks5://u:p@h:1080"


class TestMetaCache:
    def test_put_get_expire(self, monkeypatch):
        dl._META_CACHE.clear()
        dl._meta_cache_put("u1", {"a": 1})
        assert dl._meta_cache_get("u1") == {"a": 1}
        # expire
        dl._META_CACHE["u1"] = (time.time() - dl.META_CACHE_TTL - 1, {"a": 1})
        assert dl._meta_cache_get("u1") is None

    def test_evicts_oldest_at_capacity(self, monkeypatch):
        dl._META_CACHE.clear()
        base = time.time() - 1000
        for i in range(dl._META_CACHE_MAX):
            dl._META_CACHE[f"k{i}"] = (base + i, {"i": i})
        dl._meta_cache_put("new", {"n": 1})
        assert dl._meta_cache_get("new") == {"n": 1}
        assert len(dl._META_CACHE) <= dl._META_CACHE_MAX
        # oldest quarter evicted
        assert dl._meta_cache_get("k0") is None


class TestConfigSanity:
    def test_quality_map_complete(self):
        from bot.config import QUALITY_MAP
        for key in ("480", "720", "1080", "max"):
            assert key in QUALITY_MAP
            assert "label" in QUALITY_MAP[key] and "height" in QUALITY_MAP[key]

    def test_auto_quality_validated(self):
        from bot import config
        assert config.AUTO_QUALITY in ("480", "720", "1080", "max")

    def test_dirs_exist_after_import(self):
        from bot.config import DATA_DIR, DOWNLOAD_DIR, TEMP_DIR
        for d in (DATA_DIR, DOWNLOAD_DIR, TEMP_DIR):
            assert Path(d).exists()

    def test_pot_provider_url_default(self):
        from bot.config import POT_PROVIDER_URL
        assert POT_PROVIDER_URL is None  # empty env -> None -> localhost default
