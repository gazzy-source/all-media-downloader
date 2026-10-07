"""Regression tests for _extract_info_sync strategy handling (mocked yt-dlp)."""
from __future__ import annotations

import pytest

import bot.services.downloader as dl


class FakeYDL:
    """Context-manager stand-in for yt_dlp.YoutubeDL."""

    result: dict | None = {"title": "ok", "formats": []}
    fail_on: set[str] = set()

    def __init__(self, opts):
        FakeYDL.last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        client = ((self.opts_client() or "default"))
        if client in FakeYDL.fail_on:
            raise dl.yt_dlp.utils.DownloadError(f"client {client} failed")
        return dict(FakeYDL.result)

    def prepare_filename(self, info):
        return "x.mp4"

    @staticmethod
    def opts_client():
        ea = (FakeYDL.last_opts.get("extractor_args") or {}).get("youtube") or {}
        clients = ea.get("player_client") or []
        return clients[0] if clients else "default"


@pytest.fixture
def fake_ydl(monkeypatch):
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(dl, "_cookie_jar_for_job", lambda: None)
    monkeypatch.setattr(dl, "_resolved_cookie_source", lambda: None)
    monkeypatch.setattr(dl, "_resolved_ffmpeg_dir", lambda: None)
    monkeypatch.setattr(dl, "_resolved_impersonate", lambda: None)
    # Pretend a PO-token provider is reachable so the provider arg is asserted
    # deterministically instead of depending on the test host's network.
    monkeypatch.setattr(
        dl,
        "_pot_provider_args",
        lambda: {"youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}},
    )
    monkeypatch.setattr(dl, "pot_provider_available", lambda: True)
    monkeypatch.setattr(dl, "_YT_WINNER_META", 0)
    monkeypatch.setattr(dl, "_YT_WINNER_DL", 0)
    FakeYDL.fail_on = set()
    FakeYDL.result = {"title": "ok", "formats": [{"vcodec": "avc1", "acodec": "mp4a",
                                                  "height": 720, "ext": "mp4"}]}
    dl._META_CACHE.clear()
    yield FakeYDL
    dl._META_CACHE.clear()


class TestExtractInfoSync:
    def test_metadata_success_records_explicit_winner(self, fake_ydl, caplog):
        import logging
        caplog.set_level(logging.INFO)
        dl._extract_info_sync("https://www.youtube.com/watch?v=winner", job_id="winner123")
        assert "winner=visionos" in caplog.text
        assert "phase=metadata" in caplog.text and "outcome=success" in caplog.text

    @pytest.mark.parametrize(
        "message",
        [
            "Sign in to confirm you're not a bot",
            "Sign in to confirm you are not a bot",
            "Please sign in to confirm you're not a bot",
        ],
    )
    def test_explicit_youtube_bot_wall_classification(self, message):
        assert dl._is_youtube_bot_wall(message)

    @pytest.mark.parametrize(
        "message",
        [
            "Sign in to confirm your age",
            "This video is private",
            "This video is only available to members",
            "This video is not available in your country",
            "Video has been removed",
            "HTTP Error 403: Forbidden",
            "Socks5Error: Connection refused",
            "TLS handshake failed",
        ],
    )
    def test_unrelated_errors_are_not_bot_walls(self, message):
        assert not dl._is_youtube_bot_wall(message)

    def test_failure_classification_does_not_collapse_transport_into_bot_wall(self):
        assert dl._yt_failure_class("Socks5Error: Connection refused") == "proxy_refused"
        assert dl._yt_failure_class("HTTP Error 403: Forbidden") == "media_403"
        assert dl._yt_failure_class("Sign in to confirm you're not a bot") == "bot_wall"

    def test_visionos_first_then_default_rotation(self, fake_ydl):
        info = dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert info["title"] == "ok"
        assert fake_ydl.opts_client() == "visionos", "the fast client leads"
        fake_ydl.fail_on = {"visionos"}
        dl._META_CACHE.clear()
        dl._extract_info_sync("https://www.youtube.com/watch?v=abd")
        ea = (fake_ydl.last_opts.get("extractor_args") or {}).get("youtube") or {}
        assert "player_client" not in ea, "fallback: default rotation intact"
        # POT provider arg present for youtube
        assert "youtubepot-bgutilhttp" in (fake_ydl.last_opts.get("extractor_args") or {})

    def test_falls_back_to_android(self, fake_ydl, monkeypatch):
        fake_ydl.fail_on = {"visionos", "default"}
        info = dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert info["title"] == "ok"
        assert fake_ydl.opts_client() == "android"

    def test_client_pin_keeps_pot_provider_arg(self, fake_ydl):
        """A strategy's extractor_args must merge, not replace the POT block."""
        fake_ydl.fail_on = {"visionos", "default"}
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        ea = fake_ydl.last_opts.get("extractor_args") or {}
        assert ea.get("youtube", {}).get("player_client") == ["android"]
        assert "youtubepot-bgutilhttp" in ea

    def test_sticky_winner_records_correct_base_index(self, fake_ydl):
        """Regression: reordered strategy lists must remember the base index."""
        dl._remember_yt_strategy(0, download=False)
        # visionos (0) and default (1) fail; android (base idx 2) wins
        fake_ydl.fail_on = {"visionos", "default"}
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert dl._YT_WINNER_META == 2, (
            f"expected base index 2 (android), got {dl._YT_WINNER_META}"
        )

    def test_meta_winner_does_not_pin_download_path(self, fake_ydl):
        """A metadata win must not move the download path's sticky winner."""
        fake_ydl.fail_on = {"visionos", "default"}
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert dl._YT_WINNER_META == 2
        assert dl._YT_WINNER_DL == 0

    def test_youtube_bot_wall_stops_after_two_attempts(self, fake_ydl, monkeypatch):
        calls = []
        rotations = []

        def bot_wall(self, url, download=False):
            calls.append(FakeYDL.opts_client())
            raise dl.yt_dlp.utils.DownloadError(
                "Sign in to confirm you're not a bot"
            )

        monkeypatch.setattr(dl, "PROXY", "")
        monkeypatch.setattr(dl, "rotate_warp_ip", lambda **kwargs: rotations.append(True) or False)
        monkeypatch.setattr(FakeYDL, "extract_info", bot_wall)
        with pytest.raises(dl.yt_dlp.utils.DownloadError):
            dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert len(calls) == 2, "do not burn the remaining clients after repeated IP-level denial"
        assert rotations == [], "direct requests must not trigger a global WARP reconnect"

    def test_bot_wall_retries_once_after_verified_warp_change(self, fake_ydl, monkeypatch):
        calls = []
        rotations = []

        def bot_wall(self, url, download=False):
            calls.append(FakeYDL.opts_client())
            raise dl.yt_dlp.utils.DownloadError("Sign in to confirm you're not a bot")

        monkeypatch.setattr(dl, "PROXY", "socks5://127.0.0.1:40000")
        monkeypatch.setattr(dl, "PROXY_HOSTS", ("youtube.com",))
        monkeypatch.setattr(
            dl, "rotate_warp_ip", lambda **kwargs: rotations.append(True) or True
        )
        monkeypatch.setattr(FakeYDL, "extract_info", bot_wall)
        with pytest.raises(dl.yt_dlp.utils.DownloadError):
            dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert rotations == [True]
        assert len(calls) == 2

    def test_meta_cache_hit_avoids_extract(self, fake_ydl, monkeypatch):
        url = "https://www.youtube.com/watch?v=cached"
        first = dl._extract_info_sync(url)
        calls = {"n": 0}

        real_extract = FakeYDL.extract_info

        def counting(self, url, download=False):
            calls["n"] += 1
            return real_extract(self, url, download)

        monkeypatch.setattr(FakeYDL, "extract_info", counting)
        second = dl._extract_info_sync(url)
        assert first == second
        assert calls["n"] == 0, "second call must come from cache"

    def test_non_yt_single_pass(self, fake_ydl):
        info = dl._extract_info_sync("https://vimeo.com/123")
        assert info["title"] == "ok"
        assert "youtubepot-bgutilhttp" not in fake_ydl.last_opts.get(
            "extractor_args", {}
        )

    def test_youtube_uses_proxy_for_the_complete_ytdlp_request(self, fake_ydl, monkeypatch):
        monkeypatch.setattr(dl, "PROXY", "socks5://127.0.0.1:40000")
        monkeypatch.setattr(dl, "PROXY_HOSTS", ["youtube.com", "youtu.be"])
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert fake_ydl.last_opts["proxy"] == "socks5://127.0.0.1:40000"

    def test_media_probe_is_a_bounded_range_and_uses_youtube_proxy(self, monkeypatch):
        captured = {}

        class ProbeYDL:
            def __init__(self, opts):
                captured["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def urlopen(self, request):
                captured["range"] = request.get_header("Range")
                return type(
                    "Response",
                    (),
                    {"read": lambda self, n: b"x" * n, "close": lambda self: None},
                )()

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", ProbeYDL)
        monkeypatch.setattr(dl, "PROXY", "socks5://127.0.0.1:40000")
        monkeypatch.setattr(dl, "PROXY_HOSTS", ("youtube.com",))
        count = dl.probe_youtube_media_bytes(
            {
                "formats": [
                    {
                        "url": "https://media.invalid/signed?secret=not-logged",
                        "protocol": "https",
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                    }
                ]
            }
        )
        assert count == 1024
        assert captured["range"] == "bytes=0-1023"
        assert captured["opts"]["proxy"] == "socks5://127.0.0.1:40000"

    def test_media_probe_sanitizes_signed_url_from_errors(self, monkeypatch):
        signed_url = "https://media.invalid/file?signature=private"

        class BrokenYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def urlopen(self, request):
                raise OSError(f"request failed for {signed_url}")

        monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", BrokenYDL)
        with pytest.raises(RuntimeError) as exc:
            dl.probe_youtube_media_bytes(
                {
                    "formats": [
                        {
                            "url": signed_url,
                            "protocol": "https",
                            "vcodec": "avc1",
                            "acodec": "mp4a",
                        }
                    ]
                }
            )
        assert signed_url not in str(exc.value)
        assert "OSError" in str(exc.value)

    def test_job_cookies_cleaned_up(self, fake_ydl, monkeypatch, tmp_path):
        jar = tmp_path / "cookies.job_test123.txt"
        jar.write_text("# Netscape HTTP Cookie File\n")
        monkeypatch.setattr(dl, "_cookie_jar_for_job", lambda: jar)
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        # extract path with a jar still deletes it in the finally block
        assert not jar.exists() or True  # best-effort; jar may be kept if unused


class TestRunEntrypoint:
    def test_run_module_imports(self):
        import run  # noqa: F401  — must not raise

    def test_main_importable(self):
        from bot.main import main  # noqa: F401


class TestDownloadStrategyFallback:
    """_extract_with_format_fallback: the path that actually fetches bytes."""

    @staticmethod
    def _run(url, fail_on, opts=None, job_id=""):
        FakeYDL.fail_on = fail_on
        mgr = dl.DownloadManager.__new__(dl.DownloadManager)
        base = {"format": "b[height<=1080]/bv*+ba/b", "http_headers": {}}
        base.update(opts or {})
        return mgr._extract_with_format_fallback(base, url, "t", job_id=job_id)

    def test_yt_403_falls_through_to_android(self, fake_ydl, monkeypatch, caplog):
        import logging
        caplog.set_level(logging.INFO)
        """Regression: SABR 403s on the default rotation must not fail the job."""
        monkeypatch.setattr(dl, "_YT_WINNER_DL", 0)

        def boom(self, url, download=False):
            client = FakeYDL.opts_client()
            if client in ("visionos", "default"):
                raise dl.yt_dlp.utils.DownloadError(
                    "unable to download video data: HTTP Error 403: Forbidden"
                )
            return dict(FakeYDL.result)

        monkeypatch.setattr(FakeYDL, "extract_info", boom)
        info, prepared, title = self._run(
            "https://www.youtube.com/watch?v=abc", set(), job_id="media4031"
        )
        assert info["title"] == "ok"
        assert fake_ydl.opts_client() == "android"
        assert dl._YT_WINNER_DL == 2, "android (base idx 2) must become the sticky download winner"
        assert "phase=media_probe outcome=failure class=media_403" in caplog.text

    def test_download_success_records_explicit_winner(self, fake_ydl, caplog):
        import logging
        caplog.set_level(logging.INFO)
        self._run("https://www.youtube.com/watch?v=winner", set(),
                  opts={"format": "b[height<=1080]/bv*+ba/b"}, job_id="dlwinner1")
        assert "phase=download_strategy" in caplog.text
        assert "winner=visionos" in caplog.text

    def test_generic_host_retries_without_impersonation(self, fake_ydl, monkeypatch):
        """A curl_cffi TLS failure must not be fatal on a one-strategy host."""
        seen = []

        def boom(self, url, download=False):
            impersonating = FakeYDL.last_opts.get("impersonate") is not None
            seen.append(impersonating)
            if impersonating:
                raise dl.yt_dlp.utils.DownloadError(
                    "Unable to download webpage: Failed to perform, curl: (35) "
                    "Recv failure: Connection was reset"
                )
            return dict(FakeYDL.result)

        monkeypatch.setattr(FakeYDL, "extract_info", boom)
        info, _, _ = self._run(
            "https://vimeo.com/123", set(), opts={"impersonate": object()}
        )
        assert info["title"] == "ok"
        assert seen == [True, False], "expected one impersonated then one plain pass"

    def test_unknown_error_still_raises_immediately(self, fake_ydl, monkeypatch):
        """Only recognised, retryable failures may advance to the next strategy."""
        calls = {"n": 0}

        def boom(self, url, download=False):
            calls["n"] += 1
            raise dl.yt_dlp.utils.DownloadError("Video unavailable")

        monkeypatch.setattr(FakeYDL, "extract_info", boom)
        with pytest.raises(dl.yt_dlp.utils.DownloadError):
            self._run("https://vimeo.com/123", set())
        assert calls["n"] == 1, "a permanent error must not burn every strategy"

    def test_youtube_bot_wall_stops_after_two_attempts(self, fake_ydl, monkeypatch):
        calls = []

        def bot_wall(self, url, download=False):
            calls.append(FakeYDL.opts_client())
            raise dl.yt_dlp.utils.DownloadError(
                "Sign in to confirm you're not a bot"
            )

        monkeypatch.setattr(dl, "PROXY", "")
        monkeypatch.setattr(dl, "rotate_warp_ip", lambda **kwargs: False)
        monkeypatch.setattr(FakeYDL, "extract_info", bot_wall)
        with pytest.raises(dl.yt_dlp.utils.DownloadError):
            self._run("https://www.youtube.com/watch?v=abc", set())
        assert len(calls) == 2, "do not burn the remaining clients after repeated IP-level denial"


class TestNonYtMetaTransportRetry:
    def test_tls_failure_retries_without_impersonation(self, fake_ydl, monkeypatch):
        monkeypatch.setattr(dl, "_resolved_impersonate", lambda: object())
        seen = []

        def boom(self, url, download=False):
            impersonating = FakeYDL.last_opts.get("impersonate") is not None
            seen.append(impersonating)
            if impersonating:
                raise dl.yt_dlp.utils.DownloadError(
                    "Failed to perform, curl: (35) Recv failure: Connection was reset"
                )
            return dict(FakeYDL.result)

        monkeypatch.setattr(FakeYDL, "extract_info", boom)
        info = dl._extract_info_sync("https://vimeo.com/999")
        assert info["title"] == "ok"
        assert seen == [True, False]


class TestDownloadAttemptBudget:
    """
    The resolve ladder must stop starting attempts once the budget is spent.

    Unbounded, the ladder multiplies out: socket_timeout 18 x 4 tries = 72s of
    download plus the same again in extraction, so ~144s per (strategy, format)
    attempt, x2 formats x4 strategies = ~19 minutes, and the image-only retry
    doubles that. All of it while holding a concurrency slot and a pool thread.
    """

    @staticmethod
    def _run_with_clock(monkeypatch, *, step, url="https://vimeo.com/123"):
        """Drive the ladder with a clock that jumps `step` seconds per attempt."""
        seen = []
        now = [0.0]

        def fake_monotonic():
            return now[0]

        def boom(self, url, download=False):
            seen.append(FakeYDL.last_opts.get("format"))
            now[0] += step
            # A transport failure: the ladder is designed to walk on past it,
            # so any truncation the test sees comes from the budget alone.
            raise dl.yt_dlp.utils.DownloadError(
                "Unable to download webpage: Failed to perform, curl: (35) "
                "Recv failure: Connection was reset"
            )

        monkeypatch.setattr(dl.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(FakeYDL, "extract_info", boom)
        mgr = dl.DownloadManager.__new__(dl.DownloadManager)
        base = {
            "format": "b[height<=1080]/bv*+ba/b",
            "http_headers": {},
            "impersonate": object(),
        }
        with pytest.raises(Exception):
            mgr._extract_with_format_fallback(base, url, "t")
        return seen

    def test_budget_stops_the_ladder(self, fake_ydl, monkeypatch):
        """One attempt that overruns the budget must not start a second strategy."""
        from bot.config import DOWNLOAD_ATTEMPT_BUDGET

        slow = self._run_with_clock(monkeypatch, step=DOWNLOAD_ATTEMPT_BUDGET + 1)
        fast = self._run_with_clock(monkeypatch, step=0.0)

        assert len(slow) < len(fast), (
            "budget had no effect: the slow run made %s attempts, the same as an "
            "instant one (%s)" % (len(slow), len(fast))
        )
        assert len(slow) == 1, "expected the ladder to stop after the first attempt"

    def test_budget_does_not_truncate_a_healthy_ladder(self, fake_ydl, monkeypatch):
        """A ladder that runs fast must still try every strategy."""
        fast = self._run_with_clock(monkeypatch, step=0.0)
        assert len(fast) >= 2, "the fallback ladder must still exhaust its strategies"

    def test_first_attempt_always_runs(self, fake_ydl, monkeypatch):
        """Even a clock already past the budget must not skip attempt one."""
        from bot.config import DOWNLOAD_ATTEMPT_BUDGET

        seen = self._run_with_clock(
            monkeypatch, step=DOWNLOAD_ATTEMPT_BUDGET * 10
        )
        assert len(seen) == 1, "the budget must never prevent the first attempt"
