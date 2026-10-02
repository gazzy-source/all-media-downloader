"""
The PO-token provider self-check.

Production failure this covers: the bgutil provider ran in a bridge-network
container while PROXY pointed at a host-local SOCKS port. `/ping` answered 200,
so the bot logged "PO-token provider reachable" on every boot, while *every*
real mint failed with ECONNREFUSED and YouTube answered "Sign in to confirm
you're not a bot" for days. A liveness probe that never mints cannot see that.
"""
from __future__ import annotations

import io
import json

import pytest

import bot.services.downloader as dl


@pytest.fixture(autouse=True)
def _provider_configured(monkeypatch):
    monkeypatch.setattr(dl, "_POT_RESOLVED", True)
    monkeypatch.setattr(
        dl, "_POT_ARGS", {"youtubepot-bgutilhttp": {"base_url": ["http://p:4416"]}}
    )


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload, captured=None):
    def _fake(req, timeout=None):
        if captured is not None:
            captured.append(json.loads(req.data.decode()))
        return _Resp(json.dumps(payload).encode())

    return _fake


def test_minting_provider_reports_ok(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", _urlopen_returning({"poToken": "abc123"})
    )
    ok, detail = dl.pot_provider_mint_check()
    assert ok, detail


def test_provider_that_cannot_reach_the_proxy_is_not_ok(monkeypatch):
    """The exact production shape: HTTP 200, but the body carries an error."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _urlopen_returning(
            {"error": "Could not get BotGuard challenge (ECONNREFUSED 127.0.0.1:40000)"}
        ),
    )
    ok, detail = dl.pot_provider_mint_check()
    assert not ok
    assert "ECONNREFUSED" in detail


def test_check_sends_the_same_proxy_youtube_would_use(monkeypatch):
    """
    The proxy must be part of the probe. Without it the check mints happily via
    the direct route and still misses the broken proxied path users hit.
    """
    seen: list[dict] = []
    monkeypatch.setattr(dl, "PROXY", "socks5://127.0.0.1:40000")
    monkeypatch.setattr(dl, "PROXY_HOSTS", ("youtube.com",))
    monkeypatch.setattr(
        "urllib.request.urlopen", _urlopen_returning({"poToken": "t"}, seen)
    )
    dl.pot_provider_mint_check()
    assert seen and seen[0].get("proxy") == "socks5://127.0.0.1:40000"


def test_no_proxy_key_when_youtube_is_not_proxied(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(dl, "PROXY", "socks5://127.0.0.1:40000")
    monkeypatch.setattr(dl, "PROXY_HOSTS", ("reddit.com",))  # YouTube excluded
    monkeypatch.setattr(
        "urllib.request.urlopen", _urlopen_returning({"poToken": "t"}, seen)
    )
    dl.pot_provider_mint_check()
    assert seen and "proxy" not in seen[0]


def test_transport_failure_never_raises(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    ok, detail = dl.pot_provider_mint_check()
    assert not ok
    assert "connection refused" in detail


def test_unconfigured_provider_short_circuits(monkeypatch):
    monkeypatch.setattr(dl, "_POT_ARGS", {})

    def _never(*a, **k):
        raise AssertionError("must not probe when no provider is configured")

    monkeypatch.setattr("urllib.request.urlopen", _never)
    ok, detail = dl.pot_provider_mint_check()
    assert not ok and "no provider" in detail


class TestFriendlyErrorGaps:
    """
    Messages the live platform matrix showed reaching users verbatim.

    Each of these arrived in a Telegram chat as a raw yt-dlp / Python string,
    which reads as a crash rather than something the user can act on.
    """

    def _f(self, msg):
        return dl.DownloadManager._friendly_error(msg)

    def test_bilibili_412_is_explained(self):
        out = self._f(
            "[BiliBili] 1GJ411x7h7: Unable to download webpage: "
            "HTTP Error 412: Precondition Failed"
        )
        assert "refused" in out
        assert "Precondition Failed" not in out

    def test_tumblr_remote_disconnect_is_explained(self):
        out = self._f(
            "[generic] video: Unable to download webpage: ('Connection aborted.', "
            "RemoteDisconnected('Remote end closed connection without response'))"
        )
        assert "RemoteDisconnected" not in out
        assert "try again" in out.lower()

    def test_connection_reset_is_explained(self):
        out = self._f("Recv failure: Connection was reset")
        assert "Recv failure" not in out
        assert "try again" in out.lower()

    def test_non_youtube_403_does_not_advise_a_po_token_provider(self):
        """Rumble 403 used to tell the reader to run a YouTube PO-token server."""
        out = self._f("[Rumble] v2b2xtu: Unable to download webpage: HTTP Error 403: Forbidden")
        assert "refused" in out
        assert "PO-token" not in out
        assert "bgutil" not in out

    def test_youtube_403_asks_for_a_retry_not_server_setup(self, monkeypatch):
        out = self._f("[youtube] abc: unable to download video data: HTTP Error 403: Forbidden")
        assert "try again" in out and "PO-token" not in out

    def test_specific_matches_still_win_over_the_new_branches(self):
        """
        'Sign in to confirm you're not a bot' also contains no 403/412, but the
        bot-wall branch must keep priority over anything added below it.
        """
        out = self._f("[youtube] x: Sign in to confirm you’re not a bot")
        assert "anti-bot" in out


class TestMetaPlatformMessages:
    """
    Meta platforms are authentication-blocked, not broken.

    Verified on the server: Instagram reels fail identically direct, through
    the WARP proxy, and on yt-dlp master. So the old advice ("yt-dlp needs an
    update on the server") was actively wrong, and Instagram's own wording was
    reaching users verbatim — truncated, and telling them to pass
    --cookies-from-browser, a CLI flag with no meaning in a Telegram chat.
    """

    def _f(self, msg):
        return dl.DownloadManager._friendly_error(msg)

    def test_instagram_login_wall_is_explained_without_cli_flags(self):
        out = self._f(
            "ERROR: [Instagram] C0kfPZ0Ry1S: Instagram sent an empty media "
            "response. Check if this post is accessible in your browser without "
            "being logged-in. If it is not, then use --cookies-from-browser or "
            "--cookies for the authentication."
        )
        assert "--cookies" not in out, "CLI flags must not reach a chat user"
        assert "cookies.txt" not in out, "server setup is not the user's job"
        assert "signed-in" in out.lower()

    def test_unparseable_page_does_not_promise_an_update_will_fix_it(self):
        out = self._f("ERROR: [facebook] 1122176382566360: Cannot parse data")
        assert "needs an update" not in out.lower()
        assert "nothing you did wrong" in out.lower()

    def test_image_only_instagram_post_still_routes_to_image_mode(self):
        """A post with no video is a different case and must keep its own advice."""
        out = self._f("ERROR: [Instagram] CUbHfeGsrRj: No video formats found!")
        assert "image" in out.lower()

    def test_non_instagram_empty_response_is_not_blamed_on_instagram(self):
        out = self._f(
            "ERROR: [OnDemandKorea] 123: Got empty response from playback API"
        )
        assert "instagram" not in out.lower()

    def test_other_instagram_login_walls_get_the_same_advice(self):
        hint = (
            " Use --cookies-from-browser or --cookies for the authentication. "
            "See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-"
            "cookies-to-yt-dlp  for how to manually pass cookies"
        )
        for body in (
            "This content is only available for registered users who follow "
            "this account.",
            "The webpage request was redirected to the login page. You have "
            "exceeded the rate-limit for accessing posts anonymously.",
        ):
            out = self._f(f"ERROR: [Instagram] abc: {body}{hint}")
            assert "signed-in" in out.lower()
            assert "github.com" not in out
            assert "--cookies" not in out

    def test_generic_extractor_failure_does_not_claim_updates_are_useless(self):
        out = self._f("ERROR: [youtube] abc: Unable to extract uploader id")
        assert "can't read this link" in out
        assert "updating would not help" not in out.lower()
