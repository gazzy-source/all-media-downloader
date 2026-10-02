"""Inline card: name-only caption, one live status on the button, quick taps."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bot.handlers.download as hd
import bot.handlers.inline as inl
from bot.services import inline_cache, yt_search
from bot.services.downloader import DownloadResult
from bot.services.yt_search import SearchHit
from tests.conftest import FakeCallbackQuery
from tests.test_inline import FakeInlineQuery, InlineBot, _update

HITS = [SearchHit(id=f"vid{i:08d}", title=f"Song {i}", channel="C", duration=200, views=1)
        for i in range(3)]


@pytest.fixture
def env(monkeypatch, tmp_path):
    inline_cache._reset_for_tests(tmp_path / "c.json")
    monkeypatch.setattr(yt_search, "search", lambda t: HITS)
    monkeypatch.setattr(yt_search, "cached", lambda t: None)
    monkeypatch.setattr(yt_search, "title_for",
                        lambda vid: next((h.title for h in HITS if h.id == vid), None))
    monkeypatch.setattr(inl, "_DEBOUNCE", 0)
    monkeypatch.setattr(inl, "INLINE_SEARCH_ENABLED", True)
    monkeypatch.setattr(inl, "INLINE_ENABLED", True)
    monkeypatch.setattr(inl.inline_query_limiter, "allow", lambda uid: (True, 0))
    monkeypatch.setattr(inl, "check_public_url", lambda u: None)
    monkeypatch.setattr(inl, "record_download", lambda *a, **k: None)
    monkeypatch.setattr(inl.rate_limiter, "allow", lambda uid: (True, 0))
    monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
    prefetched = []

    async def fake_prefetch(url):
        prefetched.append(url)

    monkeypatch.setattr(inl, "_prefetch", fake_prefetch)
    inl._LATEST.clear()
    return SimpleNamespace(ctx=SimpleNamespace(bot=InlineBot()), prefetched=prefetched)


async def _search(env, text):
    q = FakeInlineQuery(text)
    await inl.handle_inline_query(_update(inline_query=q), env.ctx)
    return q


class TestCard:
    async def test_card_shows_the_name_only(self, env):
        q = await _search(env, "audio song")
        card = q.answers[0][0][0]
        assert card.caption == "<b>Song 0</b>"
        assert card.reply_markup.inline_keyboard[0][0].text == "⏳ Working…"

    async def test_progress_is_one_status_on_the_button(self, env, monkeypatch):
        async def fake_download(**kw):
            for pct, msg in ((10, "⬇ 10% · 1MB/s"), (10, "⬇ 10% · 1MB/s"),
                             (99, "🎵 Converting audio…"), (99, "🎵 Converting audio…")):
                await kw["progress_cb"](pct, msg)
            return DownloadResult(success=False, error="nope", mode="audio")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        chosen = SimpleNamespace(result_id="sa:vid00000001", query="audio song",
                                 inline_message_id="IM", from_user=SimpleNamespace(id=1))
        await _search(env, "audio song")  # so the card's title is known
        await inl.handle_chosen_inline_result(SimpleNamespace(chosen_inline_result=chosen), env.ctx)
        # Status = one line under the name in the caption; no repeats.
        lines = [c.split(chr(10), 1)[1] for c in env.ctx.bot.captions[:-1] if chr(10) in c]
        assert len(lines) == 2 and lines[0].startswith("⬇ 10%")
        assert lines[1].startswith("🎵 Converting audio…")
        assert all(c.startswith("<b>Song 1</b>") for c in env.ctx.bot.captions)
        assert env.ctx.bot.captions[-1].startswith("<b>Song 1</b>")  # failure keeps the name
        assert "nope" in env.ctx.bot.captions[-1]

    async def test_tapping_the_status_tells_the_same_status(self, fx):
        inl._STATUS["IM9"] = "⬇ 42%"
        q = FakeCallbackQuery(data="inl:wait")
        q.inline_message_id = "IM9"
        await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
        assert q.answers[0][0].startswith("⬇ 42%")
        inl._STATUS.pop("IM9", None)


class TestSearchTyping:
    async def test_short_queries_do_not_search(self, env, monkeypatch):
        calls = []
        monkeypatch.setattr(yt_search, "search", lambda t: calls.append(t) or HITS)
        for text in ("ab", "audio", "audio ab", "mp3"):
            q = await _search(env, text)
            assert q.answers[0][0] == [] and q.answers[0][1]["button"] is not None
        assert calls == []

    async def test_top_result_prefetched_after_a_pause(self, env, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
        await _search(env, "song name")
        await asyncio.gather(*list(inl._BACKGROUND))
        assert env.prefetched == [HITS[0].url]


async def _instant_sleep(*_a, **_k):
    return None


async def test_settings_buttons_are_answered(fx, tmp_path):
    from bot.services import user_prefs

    user_prefs._reset_for_tests(tmp_path / "p.json")
    q = FakeCallbackQuery(data="pref:mode:video")
    await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
    assert q.answers, "unanswered taps leave a spinner on the button"
