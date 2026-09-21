"""
The analysis pass skips YouTube's HLS manifest and translated subtitles.

Benchmarked on the server (4 videos x 4 rounds): p50 3.82s -> 2.76s and
p90 14.22s -> 3.46s, with identical height lists offered on every video. The
p90 is the tail users report as "still taking a lot".
"""
from __future__ import annotations

import bot.services.downloader as dl


def _yt_skips(opts):
    return set((((opts.get("extractor_args") or {}).get("youtube") or {}).get("skip")) or [])


class TestLeanMetadataArgs:
    def test_merge_keeps_the_pot_provider_block(self):
        """
        Assigning instead of merging here would silently drop the PO-token
        provider args, which is what makes the full format ladder reachable —
        the exact failure that bot-walled YouTube for days.
        """
        merged = dl._merge_extractor_args(
            {"youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}},
            {"youtube": {"skip": ["hls", "translated_subs"]}},
        )
        assert merged["youtubepot-bgutilhttp"]["base_url"] == ["http://127.0.0.1:4416"]
        assert set(merged["youtube"]["skip"]) == {"hls", "translated_subs"}

    def test_merge_does_not_mutate_the_caller_dict(self):
        base = {"youtube": {"player_client": ["android"]}}
        dl._merge_extractor_args(base, {"youtube": {"skip": ["hls"]}})
        assert "skip" not in base["youtube"], "base dict must not be mutated in place"

    def test_flag_is_on_by_default(self):
        from bot.config import YT_LEAN_METADATA

        assert YT_LEAN_METADATA is True

    def test_flag_controls_the_real_metadata_options(self, monkeypatch):
        """
        Prove the flag reaches the actual extraction, in both positions.

        Deliberately does NOT importlib.reload(bot.config): reloading a config
        module mid-suite swaps objects other modules captured at import, which
        broke the cleanup-job tests when this was first written. Patch the name
        the code reads instead.
        """
        seen: list[dict] = []

        class _Boom(Exception):
            pass

        class FakeYDL:
            def __init__(self, opts):
                seen.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, *a, **k):
                raise _Boom("captured")

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
        monkeypatch.setattr(dl, "_cookie_jar_for_job", lambda: None)

        for enabled, expected in ((True, {"hls", "translated_subs"}), (False, set())):
            seen.clear()
            dl._META_CACHE.clear()
            monkeypatch.setattr(dl, "YT_LEAN_METADATA", enabled)
            try:
                dl._extract_info_sync("https://www.youtube.com/watch?v=abc12345678")
            except Exception:
                pass
            assert seen, "no extraction was attempted"
            assert _yt_skips(seen[0]) == expected, (
                f"YT_LEAN_METADATA={enabled} produced skips "
                f"{_yt_skips(seen[0])}, expected {expected}"
            )

    def test_non_youtube_is_untouched(self, monkeypatch):
        """The skips are a YouTube extractor arg; other platforms must not see them."""
        seen: list[dict] = []

        class FakeYDL:
            def __init__(self, opts):
                seen.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, *a, **k):
                raise RuntimeError("captured")

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
        monkeypatch.setattr(dl, "_cookie_jar_for_job", lambda: None)
        monkeypatch.setattr(dl, "YT_LEAN_METADATA", True)
        dl._META_CACHE.clear()
        try:
            dl._extract_info_sync("https://vimeo.com/12345")
        except Exception:
            pass
        assert seen and _yt_skips(seen[0]) == set()

    def test_skips_are_exactly_the_two_measured_ones(self):
        """
        Only hls and translated_subs were shown not to change offered
        qualities. Adding more (e.g. `dash`) would drop real formats.
        """
        merged = dl._merge_extractor_args(
            None, {"youtube": {"skip": ["hls", "translated_subs"]}}
        )
        opts = {"extractor_args": merged}
        assert _yt_skips(opts) == {"hls", "translated_subs"}
        assert "dash" not in _yt_skips(opts)


class TestProxyBlipBackoff:
    """
    A proxy refusal retries the SAME attempt after a short wait.

    Advancing down the ladder cannot help: every strategy shares the one
    proxy, so they all fail within milliseconds against a relay that is
    briefly refusing. WARP measured healthy moments later (60/60 sequential,
    30/30 concurrent), so a short backoff is what absorbs it.
    """

    def test_budget_is_bounded_so_a_dead_relay_still_fails_fast(self):
        from bot.config import PROXY_BLIP_BACKOFF, PROXY_BLIP_RETRIES

        assert 0 < PROXY_BLIP_RETRIES <= 3
        assert 0 < PROXY_BLIP_BACKOFF <= 3
        # Worst case added latency when the relay is genuinely down.
        assert PROXY_BLIP_RETRIES * PROXY_BLIP_BACKOFF <= 6

    def test_retry_is_shared_across_the_whole_ladder(self):
        """
        The counter must be initialised once per call, not per strategy —
        otherwise a down relay sleeps the budget again at every rung.
        """
        import inspect

        src = inspect.getsource(dl.DownloadManager._extract_with_format_fallback)
        init_at = src.index("proxy_retries = 0")
        loop_at = src.index("for si, strat in enumerate(strategies)")
        assert init_at < loop_at, "proxy_retries must be initialised before the loop"

    def test_proxy_blip_does_not_advance_the_strategy(self):
        import inspect

        src = inspect.getsource(dl.DownloadManager._extract_with_format_fallback)
        blk = src[src.index("if proxy_blip and proxy_retries"):]
        body = blk[: blk.index("# One format fallback")]
        assert "continue" in body, "must retry the same attempt"
        assert "break" not in body, "must not fall through to the next strategy"
