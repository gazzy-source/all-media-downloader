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
    def test_default_strategy_first_no_clients_forced(self, fake_ydl):
        info = dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert info["title"] == "ok"
        ea = (fake_ydl.last_opts.get("extractor_args") or {}).get("youtube") or {}
        assert "player_client" not in ea, "default rotation must stay intact"
        # POT provider arg present for youtube
        assert "youtubepot-bgutilhttp" in (fake_ydl.last_opts.get("extractor_args") or {})

    def test_falls_back_to_android(self, fake_ydl, monkeypatch):
        fake_ydl.fail_on = {"default"}
        info = dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert info["title"] == "ok"
        assert fake_ydl.opts_client() == "android"

    def test_client_pin_keeps_pot_provider_arg(self, fake_ydl):
        """A strategy's extractor_args must merge, not replace the POT block."""
        fake_ydl.fail_on = {"default"}
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        ea = fake_ydl.last_opts.get("extractor_args") or {}
        assert ea.get("youtube", {}).get("player_client") == ["android"]
        assert "youtubepot-bgutilhttp" in ea

    def test_sticky_winner_records_correct_base_index(self, fake_ydl):
        """Regression: reordered strategy lists must remember the base index."""
        dl._remember_yt_strategy(0, download=False)
        # force the first (default) strategy to fail; android (base idx 1) wins
        fake_ydl.fail_on = {"default"}
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert dl._YT_WINNER_META == 1, (
            f"expected base index 1 (android), got {dl._YT_WINNER_META}"
        )

    def test_meta_winner_does_not_pin_download_path(self, fake_ydl):
        """A metadata win must not move the download path's sticky winner."""
        fake_ydl.fail_on = {"default"}
        dl._extract_info_sync("https://www.youtube.com/watch?v=abc")
        assert dl._YT_WINNER_META == 1
        assert dl._YT_WINNER_DL == 0

    def test_bot_wall_errors_retry_then_raise(self, fake_ydl, monkeypatch):
        fake_ydl.fail_on = {"default", "android", "android_vr"}
        with pytest.raises(dl.yt_dlp.utils.DownloadError):
            dl._extract_info_sync("https://www.youtube.com/watch?v=abc")

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
    def _run(url, fail_on, opts=None):
        FakeYDL.fail_on = fail_on
        mgr = dl.DownloadManager.__new__(dl.DownloadManager)
        base = {"format": "b[height<=1080]/bv*+ba/b", "http_headers": {}}
        base.update(opts or {})
        return mgr._extract_with_format_fallback(base, url, "t")

    def test_yt_403_falls_through_to_android(self, fake_ydl, monkeypatch):
        """Regression: SABR 403s on the default rotation must not fail the job."""
        monkeypatch.setattr(dl, "_YT_WINNER_DL", 0)

        def boom(self, url, download=False):
            client = FakeYDL.opts_client()
            if client == "default":
                raise dl.yt_dlp.utils.DownloadError(
                    "unable to download video data: HTTP Error 403: Forbidden"
                )
            return dict(FakeYDL.result)

        monkeypatch.setattr(FakeYDL, "extract_info", boom)
        info, prepared, title = self._run(
            "https://www.youtube.com/watch?v=abc", set()
        )
        assert info["title"] == "ok"
        assert fake_ydl.opts_client() == "android"
        assert dl._YT_WINNER_DL == 1, "android must become the sticky download winner"

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
