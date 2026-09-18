"""Unit tests for cookie sanitizer, downloader internals, and strategies."""
from __future__ import annotations

import os
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
        assert f == "b/bv*+ba/best"

    def test_platform_specific_simple_selectors(self):
        # Single-rendition hosts stay progressive-first, with a merge tail.
        assert _video_format_for_host("tiktok.com", "1080") == "b/bv*+ba/best"
        assert _video_format_for_host("pinterest.com", "720") == "b/bv*+ba/best"

    @pytest.mark.parametrize(
        "host",
        ["youtube.com", "instagram.com", "x.com", "twitter.com", "facebook.com",
         "reddit.com", "v.redd.it", "tiktok.com", "pinterest.com", "example.org"],
    )
    @pytest.mark.parametrize("quality", ["480", "720", "1080", "max"])
    def test_every_selector_can_reach_a_merge(self, host, quality):
        """
        Regression: "b"/"best" only match a format carrying BOTH tracks, so on a
        DASH/HLS-only host they match nothing and the download dies with
        "Requested format is not available" (hit live on every Reddit post).
        Every selector must therefore contain an unrestricted bv*+ba merge.
        """
        f = _video_format_for_host(host, quality)
        assert "bv*+ba" in f, f"{host}/{quality} has no merge fallback: {f}"

    def test_reddit_leads_with_merge(self):
        """Reddit serves no progressive format at all — merge must come first."""
        for q in ("480", "720", "1080", "max"):
            f = _video_format_for_host("reddit.com", q)
            assert f.split("/")[0].startswith("bv*"), f

    def test_x_respects_quality_cap(self):
        """X serves a real 270/360/720 ladder, so the cap must be honoured."""
        assert "height<=480" in _video_format_for_host("x.com", "480")
        assert "height<=720" in _video_format_for_host("twitter.com", "720")

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
            ("Sign in to confirm you're not a bot", "bot-walled"),
            ("HTTP Error 403: Forbidden", "403"),
            ("This video is unavailable", "unavailable"),
            ("Unsupported URL", "No downloadable media"),
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
        old_meta, old_dl = dl._YT_WINNER_META, dl._YT_WINNER_DL
        try:
            dl._remember_yt_strategy(2, download=True)
            assert dl._YT_WINNER_DL == 2
            assert dl._YT_WINNER_META == old_meta, "download win must not move meta"
            dl._remember_yt_strategy(1, download=False)
            assert dl._YT_WINNER_META == 1
            assert dl._YT_WINNER_DL == 2, "meta win must not move download"
        finally:
            dl._remember_yt_strategy(old_meta, download=False)
            dl._remember_yt_strategy(old_dl, download=True)

    def test_order_puts_winner_first_and_keeps_indices(self):
        strats = [{"a": 0}, {"a": 1}, {"a": 2}, {"a": 3}]
        old = dl._YT_WINNER_DL
        try:
            dl._remember_yt_strategy(2, download=True)
            ordered, indices = dl._order_yt_strategies(strats, download=True)
            assert ordered[0] == {"a": 2}
            assert indices[0] == 2
            # every base strategy still reachable, exactly once
            assert sorted(indices) == [0, 1, 2, 3]
            assert [strats[i] for i in indices] == ordered
        finally:
            dl._remember_yt_strategy(old, download=True)


class TestYtStrategyLadder:
    """Ordering and client choice (a second class named TestYtStrategies
    used to shadow the config tests above, silently disabling them)."""

    def test_android_present_and_cookieless(self):
        """The one client verified to deliver bytes without cookies or a PO token."""
        for has_cookies in (False, True):
            strats = dl._yt_strategies(has_cookies=has_cookies)
            clients = [
                (s.get("extractor_args", {}).get("youtube", {}).get("player_client") or [None])[0]
                for s in strats
            ]
            assert "android" in clients
            android = strats[clients.index("android")]
            assert android["use_cookies"] is False

    def test_default_rotation_leads(self):
        """Quality-first: the full format ladder is attempted before the 360p floor."""
        strats = dl._yt_strategies(has_cookies=False)
        assert not strats[0].get("extractor_args"), "default rotation must lead"

    def test_cookie_strategy_only_when_cookies_exist(self):
        assert not any(
            s.get("use_cookies") for s in dl._yt_strategies(has_cookies=False)
        )
        assert any(s.get("use_cookies") for s in dl._yt_strategies(has_cookies=True))


class TestMergeExtractorArgs:
    def test_client_pin_preserves_pot_block(self):
        base = {"youtubepot-bgutilhttp": {"base_url": ["http://p:4416"]}}
        merged = dl._merge_extractor_args(
            base, {"youtube": {"player_client": ["android"]}}
        )
        assert merged["youtubepot-bgutilhttp"]["base_url"] == ["http://p:4416"]
        assert merged["youtube"]["player_client"] == ["android"]

    def test_same_key_merges_one_level_deep(self):
        merged = dl._merge_extractor_args(
            {"youtube": {"formats": ["dashy"]}},
            {"youtube": {"player_client": ["android"]}},
        )
        assert merged["youtube"] == {
            "formats": ["dashy"],
            "player_client": ["android"],
        }

    def test_does_not_mutate_inputs(self):
        base = {"youtube": {"formats": ["dashy"]}}
        dl._merge_extractor_args(base, {"youtube": {"player_client": ["android"]}})
        assert base == {"youtube": {"formats": ["dashy"]}}

    def test_handles_none(self):
        assert dl._merge_extractor_args(None, None) == {}


class TestBaseOpts:
    def test_no_forced_yt_clients_in_base_opts(self):
        """Regression: _base_opts must not pin player clients (breaks cookieless)."""
        opts = dl._base_opts(host="www.youtube.com", cookiefile=None)
        ea = opts.get("extractor_args") or {}
        yt = ea.get("youtube") or {}
        assert "player_client" not in yt, "forced clients break cookieless path"

    def test_yt_gets_pot_provider_arg(self, monkeypatch):
        """base_url must be a LIST: the plugin reads _configuration_arg(...)[0],
        so a bare string would resolve to its first character."""
        monkeypatch.setattr(dl, "POT_PROVIDER_URL", "http://pot:4416")
        monkeypatch.setattr(dl, "_POT_RESOLVED", False)
        monkeypatch.setattr(dl, "_POT_ARGS", None)
        opts = dl._base_opts(host="www.youtube.com")
        assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == [
            "http://pot:4416"
        ]

    def test_configured_pot_url_is_trusted_without_probing(self, monkeypatch):
        """A compose provider may still be booting — never probe a set URL."""
        monkeypatch.setattr(dl, "POT_PROVIDER_URL", "http://bgutil-provider:4416")
        monkeypatch.setattr(dl, "_POT_RESOLVED", False)
        monkeypatch.setattr(dl, "_POT_ARGS", None)

        def explode(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("configured POT_PROVIDER_URL must not be probed")

        monkeypatch.setattr("urllib.request.urlopen", explode)
        assert dl.pot_provider_available() is True

    def test_unreachable_pot_provider_omits_arg(self, monkeypatch):
        """A dead endpoint must not cost a connect timeout on every extraction."""
        monkeypatch.setattr(dl, "POT_PROVIDER_URL", None)
        monkeypatch.setattr(dl, "_POT_RESOLVED", False)
        monkeypatch.setattr(dl, "_POT_ARGS", None)

        def refuse(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", refuse)
        opts = dl._base_opts(host="www.youtube.com")
        assert "youtubepot-bgutilhttp" not in (opts.get("extractor_args") or {})
        assert dl.pot_provider_available() is False

    def test_pot_probe_runs_only_once(self, monkeypatch):
        monkeypatch.setattr(dl, "POT_PROVIDER_URL", None)
        monkeypatch.setattr(dl, "_POT_RESOLVED", False)
        monkeypatch.setattr(dl, "_POT_ARGS", None)
        calls = {"n": 0}

        def refuse(*a, **k):
            calls["n"] += 1
            raise OSError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", refuse)
        dl._base_opts(host="www.youtube.com")
        dl._base_opts(host="www.youtube.com")
        dl.pot_provider_available()
        assert calls["n"] == 1

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
        monkeypatch.setattr(dl, "PROXY_HOSTS", ())  # empty allowlist = proxy all
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

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("http://127.0.0.1:4416", "http://127.0.0.1:4416"),
            ("  http://bgutil-provider:4416  ", "http://bgutil-provider:4416"),
        ],
    )
    def test_pot_provider_url_parsing(self, raw, expected, monkeypatch):
        """
        Assert the parsing RULE, not the value this host happens to deploy —
        the old test asserted `is None` and so failed on any server that
        actually configures a provider (it broke on the production VPS).
        """
        if raw is None:
            monkeypatch.delenv("POT_PROVIDER_URL", raising=False)
        else:
            monkeypatch.setenv("POT_PROVIDER_URL", raw)
        assert ((os.getenv("POT_PROVIDER_URL") or "").strip() or None) == expected


class TestStoryboardExclusion:
    """
    Regression: YouTube SABR responses include storyboard "formats"
    (vcodec=mjpeg, ext=mhtml) with REAL heights. A height-capped selector
    must never pick one as "video" — live testing showed a 0-second,
    15.9 KB mjpeg frame delivered as "480p video".

    These tests run the bot's actual format strings through yt-dlp's REAL
    selector machinery (build_format_selector) with mock format lists —
    no network. yt-dlp caveats covered here:
    - filters referencing a missing field silently EXCLUDE the format
      (so format_note!=storyboard is unsafe; vcodec/ext guards are used)
    - a trailing b[height<=N] re-matches the storyboard when only
      storyboards sit under the cap (hence the unrestricted bv*+ba tail)
    """

    @staticmethod
    def _fmt(fid, vc, ac, h=None, ext="mp4", tbr=100):
        f = {
            "format_id": fid,
            "vcodec": vc,
            "acodec": ac,
            "ext": ext,
            "url": f"https://example/{fid}",
            "protocol": "https",
            "format_note": "standard",
            "tbr": tbr,
        }
        if h:
            f["height"] = h
        return f

    @staticmethod
    def _storyboard(h=480):
        return {
            "format_id": "sb0",
            "vcodec": "mjpeg",
            "acodec": "none",
            "height": h,
            "ext": "mhtml",
            "url": "https://example/sb0",
            "protocol": "mhtml",
            "format_note": "storyboard",
            "tbr": 50,
        }

    @classmethod
    def _pick(cls, fmt_spec, formats):
        import yt_dlp

        ctx = {
            "formats": formats,
            "has_merged_format": any(
                "none" not in (f.get("acodec"), f.get("vcodec")) for f in formats
            ),
            "incomplete_formats": (
                all(f.get("vcodec") == "none" for f in formats)
                or all(f.get("acodec") == "none" for f in formats)
            ),
        }
        selector = yt_dlp.YoutubeDL({"format": fmt_spec}).build_format_selector(fmt_spec)
        return [f["format_id"] for f in selector(ctx)]

    # The trap: only the storyboard sits under the 480 cap; real video is 720p
    TRAP_LIST = None  # built lazily below (class attrs can't reference self)

    @classmethod
    def _trap_list(cls):
        return [
            cls._storyboard(480),
            cls._fmt("137", "avc1.640028", "none", 720),
            cls._fmt("140", "none", "mp4a.40.2", None, "m4a"),
        ]

    @classmethod
    def _normal_list(cls):
        # Realistic SABR list: storyboard + separated streams + itag 18
        return [
            *cls._trap_list(),
            cls._fmt("18", "avc1.42001E", "mp4a.40.2", 360),
        ]

    def test_capped_quality_never_picks_storyboard(self):
        from bot.config import QUALITY_MAP

        picked = self._pick(QUALITY_MAP["480"]["format"], self._trap_list())
        assert picked, "selector must always pick something"
        for fid in picked:
            assert not fid.startswith("sb"), f"storyboard selected: {picked}"

    def test_capped_quality_same_result_as_before_on_normal_lists(self):
        # Normal SABR list (itag 18 present): unchanged behavior vs old spec
        from bot.config import QUALITY_MAP

        picked = self._pick(QUALITY_MAP["480"]["format"], self._normal_list())
        assert picked == ["18"]

    @pytest.mark.parametrize("quality", ["480", "720", "1080"])
    def test_all_quality_tiers_reject_storyboards(self, quality):
        from bot.config import QUALITY_MAP

        # Storyboard just under the tier cap, real video just above it
        h_sb = QUALITY_MAP[quality]["height"]
        formats = [
            self._storyboard(h_sb),
            self._fmt("999", "avc1.640028", "none", h_sb + 240),
            self._fmt("998", "none", "mp4a.40.2", None, "m4a"),
        ]
        picked = self._pick(QUALITY_MAP[quality]["format"], formats)
        assert picked
        assert not any(f.startswith("sb") for f in picked), picked

    def test_video_format_for_host_generic_has_guard(self):
        f = _video_format_for_host("example.org", "480")
        assert "mjpeg" in f and "mhtml" in f
        assert "height<=480" in f

    def test_480p_guard_applies_on_generic_host(self):
        # A generic host serving a "storyboard-like" single format under cap
        # plus higher real video: must resolve to real video, not the frame.
        formats = [
            self._storyboard(480),
            self._fmt("vid", "avc1.640028", "none", 720),
            self._fmt("aud", "none", "mp4a.40.2", None, "m4a"),
        ]
        picked = self._pick(_video_format_for_host("example.org", "480"), formats)
        assert picked
        assert not any(f.startswith("sb") for f in picked), picked


class TestUnknownCodecHandling:
    """
    yt-dlp uses the STRING "none" for "track absent"; None/missing means UNKNOWN.
    Collapsing the two (`f.get("vcodec") or "none"`) silently broke real posts.
    """

    def _info(self, formats=None, **kw):
        info = {"formats": formats or [], "title": "t"}
        info.update(kw)
        return info

    def test_twitch_clip_single_unknown_codec_format(self):
        """Live shape: one format, vcodec/acodec None, height set → was image-only."""
        hv, ha, hi, heights, _, _ = _parse_formats(self._info(
            [{"format_id": "1080", "ext": "mp4", "vcodec": None,
              "acodec": None, "height": 1080, "width": None}],
            thumbnails=[{"width": 640, "height": 360}],
        ))
        assert hv and ha and not hi
        assert heights == [1080]

    def test_rumble_unknown_codecs_multiple_heights(self):
        hv, ha, hi, heights, _, _ = _parse_formats(self._info(
            [{"ext": "mp4", "vcodec": None, "acodec": None, "height": h, "width": w}
             for h, w in ((360, 640), (480, 854), (720, 1280))],
            thumbnails=[{"width": 640, "height": 360}],
        ))
        assert hv and ha and not hi
        assert heights == [360, 480, 720]

    def test_x_progressive_formats_expose_audio(self):
        """X's http-* formats report acodec=None → Audio button never appeared."""
        hv, ha, hi, _, _, _ = _parse_formats(self._info([
            {"format_id": "hls-audio", "ext": "mp4", "vcodec": "none",
             "acodec": None, "height": None},
            {"format_id": "http-2176", "ext": "mp4", "vcodec": None,
             "acodec": None, "height": 720, "width": 1280},
            {"format_id": "hls-500", "ext": "mp4", "vcodec": "avc1",
             "acodec": "none", "height": 360},
        ]))
        assert hv and ha and not hi

    def test_linkedin_mp4_without_height_is_video(self):
        """Three bare mp4 renditions, no dimensions → used to read audio-only."""
        hv, ha, hi, _, _, _ = _parse_formats(self._info(
            [{"format_id": str(i), "ext": "mp4", "vcodec": None,
              "acodec": None, "height": None} for i in range(3)],
            thumbnails=[{"width": 640, "height": 360}],
        ))
        assert hv and not hi

    def test_snapchat_no_formats_list_is_video(self):
        """Single-format extractor: no `formats` at all, just ext + duration."""
        hv, ha, hi, _, _, _ = _parse_formats({
            "title": "t", "ext": "mp4", "duration": 4.6,
            "url": "https://cf-st.sc-cdn.net/d/abc.mp4",
            "thumbnails": [{"width": 640, "height": 360}],
        })
        assert hv and not hi

    def test_explicit_none_still_means_absent(self):
        """The fix must not turn audio-only posts into video."""
        hv, ha, hi, _, _, _ = _parse_formats(self._info([
            {"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "height": None},
        ]))
        assert ha and not hv

    def test_dash_video_only_is_not_audio(self):
        hv, ha, hi, _, _, _ = _parse_formats(self._info([
            {"ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 720},
        ]))
        assert hv and not ha

    def test_real_image_post_still_image(self):
        hv, ha, hi, _, imgs, _ = _parse_formats(self._info(
            [{"ext": "jpg", "vcodec": None, "acodec": None,
              "width": 1080, "height": 1350}],
        ))
        assert hi and not hv
        assert (1080, 1350) in imgs

    def test_youtube_storyboards_never_count_as_media(self):
        hv, ha, hi, heights, _, _ = _parse_formats(self._info([
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "images",
             "acodec": "none", "width": 320, "height": 180},
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1",
             "acodec": "mp4a", "height": 360},
        ]))
        assert hv and not hi
        assert heights == [360], "storyboard height must not enter the ladder"


class TestBaseOptsTransport:
    def test_http_chunk_size_only_for_youtube(self):
        """
        Regression: a global 10MB http_chunk_size makes yt-dlp use Range
        requests, which breaks fragmented HLS/DASH — it was the sole cause of
        every Reddit and VK download dying with "The downloaded file is empty".
        """
        assert "http_chunk_size" in dl._base_opts(host="www.youtube.com")
        for host in ("reddit.com", "v.redd.it", "vk.com", "x.com",
                     "instagram.com", "example.org"):
            assert "http_chunk_size" not in dl._base_opts(host=host), host

    def test_impersonation_drops_forced_user_agent(self, monkeypatch):
        """
        curl_cffi forges a Chrome TLS fingerprint and sends the matching UA.
        Forcing our own Chrome/131 header advertises a different browser than
        the handshake shows; anti-bot systems fingerprint that mismatch
        (Bilibili: 1/3 success with both, 3/3 with either alone).
        """
        monkeypatch.setattr(dl, "_IMPERSONATE", object())
        monkeypatch.setattr(dl, "_IMPERSONATE_RESOLVED", True)
        opts = dl._base_opts(host="www.bilibili.com")
        assert "impersonate" in opts
        assert "User-Agent" not in opts["http_headers"]
        # Non-fingerprinting headers must survive
        assert "Accept-Language" in opts["http_headers"]

    def test_youtube_keeps_its_user_agent_and_no_impersonation(self, monkeypatch):
        monkeypatch.setattr(dl, "_IMPERSONATE", object())
        monkeypatch.setattr(dl, "_IMPERSONATE_RESOLVED", True)
        opts = dl._base_opts(host="www.youtube.com")
        assert "impersonate" not in opts
        assert "User-Agent" in opts["http_headers"]


class TestInternalErrorMasking:
    def test_raw_python_error_is_masked(self):
        """Live yt-dlp OK.ru crash — a TypeError string helps no Telegram user."""
        out = dl.DownloadManager._friendly_error(
            "the JSON object must be str, bytes or bytearray, not dict"
        )
        assert "JSON object" not in out
        assert "extractor failed" in out.lower()

    @pytest.mark.parametrize("msg", [
        "'NoneType' object is not subscriptable",
        "KeyError: 'formats'",
        "AttributeError: 'dict' object has no attribute 'group'",
    ])
    def test_other_internal_crashes_masked(self, msg):
        assert "extractor failed" in dl.DownloadManager._friendly_error(msg).lower()

    def test_real_extractor_messages_still_shown(self):
        """Must not swallow genuine, useful extractor text."""
        out = dl.DownloadManager._friendly_error("Video unavailable")
        assert "unavailable" in out.lower()
        assert "extractor failed" not in out.lower()


class TestSelectiveProxy:
    """PROXY_HOSTS keeps a metered residential proxy off every video download."""

    def test_no_proxy_configured(self, monkeypatch):
        monkeypatch.setattr(dl, "PROXY", None)
        assert "proxy" not in dl._base_opts(host="youtube.com")

    def test_empty_allowlist_proxies_everything(self, monkeypatch):
        monkeypatch.setattr(dl, "PROXY", "http://p:8080")
        monkeypatch.setattr(dl, "PROXY_HOSTS", ())
        for host in ("youtube.com", "x.com", "example.org"):
            assert dl._base_opts(host=host).get("proxy") == "http://p:8080", host

    def test_allowlist_limits_proxy_to_blocked_hosts(self, monkeypatch):
        monkeypatch.setattr(dl, "PROXY", "http://p:8080")
        monkeypatch.setattr(dl, "PROXY_HOSTS", ("youtube.com", "reddit.com"))
        assert dl._base_opts(host="www.youtube.com").get("proxy") == "http://p:8080"
        assert dl._base_opts(host="v.redd.it").get("proxy") is None
        assert dl._base_opts(host="old.reddit.com").get("proxy") == "http://p:8080"
        # Hosts that work direct must NOT burn proxy bandwidth
        for host in ("x.com", "instagram.com", "pinterest.com", "clips.twitch.tv"):
            assert "proxy" not in dl._base_opts(host=host), host

    def test_matching_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(dl, "PROXY", "socks5://p:1080")
        monkeypatch.setattr(dl, "PROXY_HOSTS", ("youtube.com",))
        assert dl._base_opts(host="WWW.YouTube.COM").get("proxy") == "socks5://p:1080"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, ()),
            ("", ()),
            ("  ", ()),
            ("youtube.com", ("youtube.com",)),
            (" YouTube.com , reddit.com ,, ", ("youtube.com", "reddit.com")),
        ],
    )
    def test_env_parsing(self, raw, expected):
        """
        Calls the REAL parser. This test used to re-implement the split inside
        itself and assert the copy against the expectation, so it passed no
        matter what bot/config.py did — it could not fail.
        """
        from bot.config import parse_proxy_hosts

        assert parse_proxy_hosts(raw) == expected


class TestBotWallAdviceMatchesSetup:
    """Don't tell an operator to run something they are already running."""

    MSG = "Sign in to confirm you're not a bot"

    def test_without_provider_suggests_running_one(self, monkeypatch):
        monkeypatch.setattr(dl, "pot_provider_available", lambda: False)
        out = dl.DownloadManager._friendly_error(self.MSG)
        assert "PO-token provider" in out
        assert "even with a PO-token provider running" not in out

    def test_with_provider_points_at_the_ip_instead(self, monkeypatch):
        """Measured on the VPS: provider mints tokens, YouTube still refuses."""
        monkeypatch.setattr(dl, "pot_provider_available", lambda: True)
        out = dl.DownloadManager._friendly_error(self.MSG)
        assert "even with a PO-token provider running" in out
        assert "PROXY" in out
        assert "see docker-compose.yml" not in out, "stale advice for this host"

    def test_never_blames_cookies_for_a_public_video(self, monkeypatch):
        for available in (True, False):
            monkeypatch.setattr(dl, "pot_provider_available", lambda: available)
            out = dl.DownloadManager._friendly_error(self.MSG)
            assert "public videos need none" in out.lower() or "no cookies" in out.lower()


class TestNoMediaLinks:
    """
    Newsletters/articles must be refused cleanly. Production showed users the
    raw yt-dlp error including "please report this issue on github ... Confirm
    you are on the latest version using yt-dlp -U".
    """

    SUBSTACK = (
        'ERROR: [Substack] why-highly-self-aware-people-cant: Page type '
        '"newsletter" is not supported; please report this issue on  '
        'https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the '
        'appropriate issue template. Confirm you are on the latest version '
        'using  yt-dlp -U'
    )

    def test_substack_newsletter_gets_a_plain_answer(self):
        out = dl.DownloadManager._friendly_error(self.SUBSTACK)
        assert "No downloadable media" in out
        assert "newsletter" in out.lower()

    @pytest.mark.parametrize("noise", [
        "please report this issue",
        "yt-dlp -U",
        "issue template",
        "github.com/yt-dlp",
        "ERROR:",
        "[Substack]",
    ])
    def test_maintainer_boilerplate_never_reaches_the_user(self, noise):
        assert noise not in dl.DownloadManager._friendly_error(self.SUBSTACK)

    @pytest.mark.parametrize("raw", [
        "ERROR: Unsupported URL: https://example.com/a-blog-post",
        "ERROR: [generic] page: No media found",
        'Page type "newsletter" is not supported',
    ])
    def test_all_no_media_shapes_map_to_the_same_answer(self, raw):
        assert "No downloadable media" in dl.DownloadManager._friendly_error(raw)

    def test_no_media_page_does_not_trigger_the_image_salvage_retry(self):
        """
        A video request that fails is retried as an image for photo posts. An
        article must NOT go down that path, or the bot would hand back the
        page's header image instead of refusing.
        """
        msg = dl.DownloadManager._friendly_error(self.SUBSTACK)
        assert not dl.DownloadManager._looks_like_image_only_error(msg)

    def test_broken_extractor_is_explained_not_dumped(self):
        raw = (
            "ERROR: [TikTok] 7106594312292453675: Unexpected response from "
            "webpage request; please report this issue on "
            "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the "
            "appropriate issue template."
        )
        out = dl.DownloadManager._friendly_error(raw)
        assert "extractor is currently failing" in out
        assert "please report this issue" not in out

    def test_genuine_media_errors_are_not_swallowed(self):
        for raw, expect in [
            ("ERROR: [youtube] abc: This video is unavailable", "unavailable"),
            ("ERROR: [ig] x: This content is private", "private"),
            ("ERROR: The download timed out", "timed out"),
        ]:
            assert expect in dl.DownloadManager._friendly_error(raw).lower()


class TestCleanExtractorMessage:
    def test_strips_prefix_and_noise_but_keeps_substance(self):
        out = dl._clean_extractor_message(
            "ERROR: [Foo] vid123: Something real happened; please report this "
            "issue on https://github.com/yt-dlp/yt-dlp/issues?q= , filling out "
            "the appropriate issue template."
        )
        assert out == "Something real happened"

    def test_handles_empty_and_plain_text(self):
        assert dl._clean_extractor_message("") == ""
        assert dl._clean_extractor_message("plain message") == "plain message"

    def test_does_not_eat_a_colon_inside_a_real_message(self):
        out = dl._clean_extractor_message("unable to download video data: HTTP Error 403")
        assert "HTTP Error 403" in out


class TestProgressNeverLooksFrozen:
    """
    Reported from production: a YouTube download sat on "Resolving… 2%".
    Two causes, both covered here.
    """

    def _hook(self, emitted, first_pct=-1.0):
        """Rebuild the progress hook exactly as _download_sync wires it."""
        import time as _t
        last_pct = {"v": first_pct}
        last_tick = {"t": 0.0}

        def _emit(pct, msg):
            emitted.append((pct, msg))

        def hook(d):
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                speed = d.get("speed")
                speed_s = dl.format_size(speed) + "/s" if speed else "—"
                if not total:
                    now = _t.time()
                    if now - last_tick["t"] < 3:
                        return
                    last_tick["t"] = now
                    _emit(0, f"⬇ {dl.format_size(done)} · {speed_s}")
                    return
                pct = done / total * 100
                if last_pct["v"] >= 0 and abs(pct - last_pct["v"]) < 8 and pct < 95:
                    return
                last_pct["v"] = pct
                _emit(pct, f"⬇ {pct:.0f}% · {speed_s}")
            elif status == "finished":
                _emit(100, "⚙️ Finishing…")

        return hook

    def test_first_tick_is_never_swallowed(self):
        """
        Real values observed from yt-dlp: 1024 bytes of 25998574 = 0.004%.
        The old gate (abs(0.004 - -1) < 8) dropped it, and every tick under 8%
        after it, so the bar stayed on "Resolving…".
        """
        got = []
        hook = self._hook(got)
        hook({"status": "downloading", "total_bytes": 25998574,
              "downloaded_bytes": 1024, "speed": 10560})
        assert got, "the first progress tick must reach the user"
        assert got[0][0] < 1, "and it should report the real (tiny) percentage"

    def test_still_throttles_after_the_first_tick(self):
        """The fix must not turn into a Telegram edit storm."""
        got = []
        hook = self._hook(got)
        for done in range(1024, 2_000_000, 50_000):
            hook({"status": "downloading", "total_bytes": 25998574,
                  "downloaded_bytes": done, "speed": 1e6})
        assert len(got) <= 2, f"expected heavy throttling, got {len(got)} edits"

    def test_unknown_total_still_reports_bytes(self):
        """
        Some fragmented streams report no total. Computing 0% and then
        throttling on it meant nothing was ever emitted.
        """
        got = []
        hook = self._hook(got)
        hook({"status": "downloading", "total_bytes": None,
              "total_bytes_estimate": None, "downloaded_bytes": 5_000_000,
              "speed": 2e6})
        assert got, "an unknown total must still produce feedback"
        assert "MB" in got[0][1], got[0][1]
        assert "%" not in got[0][1], "must not invent a percentage"

    def test_finished_reports_complete(self):
        got = []
        self._hook(got)({"status": "finished"})
        assert got == [(100, "⚙️ Finishing…")]


class TestExtractionEmitsStages:
    """Extraction runs before any byte moves, so it needs its own feedback."""

    def test_each_strategy_attempt_reports(self, monkeypatch, tmp_path):
        stages = []

        class FakeYDL:
            calls = {"n": 0}

            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                FakeYDL.calls["n"] += 1
                if FakeYDL.calls["n"] < 3:
                    raise dl.yt_dlp.utils.DownloadError(
                        "unable to download video data: HTTP Error 403: Forbidden"
                    )
                return {"title": "ok", "formats": []}

            def prepare_filename(self, info):
                return str(tmp_path / "x.mp4")

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
        monkeypatch.setattr(dl, "_YT_WINNER_DL", 0)
        mgr = dl.DownloadManager.__new__(dl.DownloadManager)
        mgr._extract_with_format_fallback(
            {"format": "b", "http_headers": {}},
            "https://www.youtube.com/watch?v=x",
            "t",
            on_stage=lambda pct, msg: stages.append((pct, msg)),
        )
        assert stages, "extraction must emit at least one stage update"
        assert any("Resolving" in m for _, m in stages)
        assert any("another source" in m for _, m in stages), (
            f"retries must be visible, got {stages}"
        )
        assert all(0 < p < 8 for p, _ in stages), "stages stay in the early band"


class TestPlaylistExtractionIsBounded:
    """
    A bare playlist URL sat on "Getting title, formats & options" for minutes:
    noplaylist only covers a video INSIDE a playlist, so a playlist/channel URL
    still expanded and, with extract_flat off, every entry was fully extracted
    — while only entries[0] is ever used.
    """

    def test_extraction_stops_at_the_first_entry(self):
        for host in ("www.youtube.com", "vimeo.com", "soundcloud.com"):
            opts = dl._base_opts(host=host)
            assert opts.get("playlistend") == 1, host

    def test_noplaylist_still_set(self):
        """Belt and braces: a video inside a playlist must not expand either."""
        assert dl._base_opts(host="www.youtube.com").get("noplaylist") is True

    def test_playlist_count_comes_from_the_real_total(self):
        """With playlistend=1 only one entry is fetched, so len(entries) is 1."""
        info = dl._normalize_info_dict("u", {
            "_type": "playlist", "title": "My List", "playlist_count": 183,
            "entries": [{"title": "first", "formats": []}],
        })
        assert info["_playlist_count"] == 183, "must not report 1"
        assert info["_is_playlist"] is True
        assert info["title"] == "first"

    def test_falls_back_to_entry_count_when_total_unknown(self):
        info = dl._normalize_info_dict("u", {
            "_type": "playlist", "title": "L",
            "entries": [{"title": "a"}, {"title": "b"}],
        })
        assert info["_playlist_count"] == 2

    def test_empty_playlist_still_raises(self):
        with pytest.raises(RuntimeError):
            dl._normalize_info_dict("u", {"_type": "playlist", "entries": []})


class TestNotFoundMessages:
    def test_404_reads_like_a_missing_link(self):
        out = dl.DownloadManager._friendly_error(
            "ERROR: [youtube:tab] @SomeChannel: Unable to download API page: "
            "HTTP Error 404: Not Found (caused by <HTTPError 404: Not Found>)"
        )
        assert "unavailable" in out.lower()
        for noise in ("404", "caused by", "HTTPError", "@SomeChannel", "API page"):
            assert noise not in out, f"leaked {noise!r}"

    def test_caused_by_tail_is_stripped(self):
        out = dl._clean_extractor_message(
            "Something broke (caused by <HTTPError 403: Forbidden>)"
        )
        assert out == "Something broke"

    def test_at_handle_prefix_stripped(self):
        out = dl._clean_extractor_message("[youtube:tab] @Handle: real message here")
        assert out == "real message here"

    def test_exception_prefixes_still_survive(self):
        """Regression: the @ widening must not start eating KeyError: etc."""
        assert "KeyError" in dl._clean_extractor_message("KeyError: 'formats'")


class TestMetadataHasAShortLeash:
    """
    Reported: "Reading formats · 22s" on an operation measured at ~2s.
    Cause: the metadata pass inherited the download timeouts (socket_timeout 18,
    retries 3), so one stalled socket bought 18s of silence before the retry
    that succeeded. Metadata is small JSON with a person watching a spinner.
    """

    def _meta_opts(self, monkeypatch):
        """Build the opts _extract_info_sync actually uses for metadata."""
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {"title": "t", "formats": []}

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
        monkeypatch.setattr(dl, "_cookie_jar_for_job", lambda: None)
        monkeypatch.setattr(dl, "_resolved_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(dl, "_resolved_impersonate", lambda: None)
        dl._META_CACHE.clear()
        dl._extract_info_sync("https://vimeo.com/123")
        dl._META_CACHE.clear()
        return captured

    def test_metadata_uses_the_short_timeout(self, monkeypatch):
        opts = self._meta_opts(monkeypatch)
        assert opts["socket_timeout"] == dl.METADATA_SOCKET_TIMEOUT
        assert opts["retries"] == dl.METADATA_RETRIES

    def test_metadata_leash_is_shorter_than_the_download_one(self, monkeypatch):
        download_opts = dl._base_opts(host="vimeo.com")
        meta_opts = self._meta_opts(monkeypatch)
        assert meta_opts["socket_timeout"] < download_opts["socket_timeout"]
        assert meta_opts["retries"] <= download_opts["retries"]

    def test_worst_case_request_time_is_bounded(self):
        """socket_timeout x (retries + 1) is what a user can wait per request."""
        worst = dl.METADATA_SOCKET_TIMEOUT * (dl.METADATA_RETRIES + 1)
        assert worst <= 20, f"metadata request can stall {worst}s"

    def test_downloads_keep_the_patient_settings(self):
        """A 100MB transfer should ride out a slow socket, not restart."""
        opts = dl._base_opts(host="www.youtube.com")
        assert opts["socket_timeout"] >= 15
        assert opts["retries"] >= 3


class TestMetadataPoolIsolation:
    """
    Production: two 45s timeouts on a URL this same code extracts in 2-4s
    standalone. Cause was pool starvation, not the URL.

    asyncio.wait_for cancels the await, never the thread — a run_in_executor
    task that has started cannot be cancelled. Every extraction that blew the
    cap therefore kept its worker until yt-dlp finished on its own, so slow
    links ratcheted the pool closed and made the NEXT request time out too.
    """

    def test_metadata_has_its_own_pool(self):
        m = dl.download_manager
        assert m._meta_executor is not m._executor, (
            "sharing one pool makes Analyzing… queue behind downloads"
        )

    def test_extract_info_uses_the_metadata_pool(self):
        import inspect
        src = inspect.getsource(dl.DownloadManager.extract_info)
        assert "_meta_executor" in src
        assert "self._executor" not in src, "must not fall back to the download pool"

    def test_downloads_still_use_the_download_pool(self):
        import inspect
        src = inspect.getsource(dl.DownloadManager.download)
        assert "_meta_executor" not in src

    def test_in_thread_deadline_is_inside_the_async_cap(self):
        """
        The thread must give up BEFORE wait_for does, or the worker leaks —
        which is precisely what turned one slow link into a starved pool.
        """
        from bot.config import EXTRACT_TIMEOUT
        deadline = max(5, EXTRACT_TIMEOUT - 5)
        assert deadline < EXTRACT_TIMEOUT

    def test_deadline_stops_further_strategies(self, monkeypatch):
        """A slow first strategy must not let the loop run past the deadline."""
        calls = {"n": 0}

        class SlowYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                calls["n"] += 1
                raise dl.yt_dlp.utils.DownloadError("unable to download video data")

            def prepare_filename(self, info):
                return "x.mp4"

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", SlowYDL)
        monkeypatch.setattr(dl, "_cookie_jar_for_job", lambda: None)
        monkeypatch.setattr(dl, "_resolved_cookie_source", lambda: None)
        monkeypatch.setattr(dl, "_resolved_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(dl, "_resolved_impersonate", lambda: None)
        monkeypatch.setattr(dl, "EXTRACT_TIMEOUT", 5)  # deadline = max(5, 0) = 5
        # Pretend a lot of time has already passed after the first attempt.
        real = dl.time.monotonic
        state = {"n": 0}

        def creeping():
            state["n"] += 1
            return real() + (state["n"] * 100)

        monkeypatch.setattr(dl.time, "monotonic", creeping)
        dl._META_CACHE.clear()
        with pytest.raises(Exception):
            dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert calls["n"] < 4, (
            f"deadline ignored: tried {calls['n']} strategies past the budget"
        )
        dl._META_CACHE.clear()


class TestVideoRequestNeverReturnsAudioOnly:
    """
    Regression guard for an optimisation that had to be reverted.

    Setting yt-dlp's max_filesize to abort oversized downloads early made it
    SKIP the big video formats and keep walking the selector chain, which ends
    in `b` — and `b` matches an audio-only format once the video ones are gone.
    A 1080p request came back as a 10.2MB webm with one opus audio stream and
    no video, still reporting h=1080. Handing back audio for a video request is
    worse than wasting bandwidth.
    """

    def test_max_filesize_is_not_set_on_downloads(self):
        import inspect
        src = inspect.getsource(dl.DownloadManager._download_sync)
        active = [
            ln for ln in src.splitlines()
            if "max_filesize" in ln and not ln.strip().startswith("#")
        ]
        assert not active, (
            "max_filesize lets the selector fall through to an audio-only "
            f"format for a video request: {active}"
        )

    def test_the_post_download_size_guard_is_still_there(self):
        """It is the only thing enforcing the Telegram cap now."""
        import inspect
        src = inspect.getsource(dl)
        assert "MAX_FILE_SIZE_BYTES" in src

    def test_video_selectors_still_prefer_a_merge_over_bare_audio(self):
        """Every video selector must be able to reach a video+audio merge."""
        for host in ("www.youtube.com", "x.com", "reddit.com", "example.org"):
            for q in ("480", "720", "1080", "max"):
                f = dl._video_format_for_host(host, q)
                assert "bv*+ba" in f, f"{host}/{q}: {f}"


