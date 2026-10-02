"""Inline search: @bot <words> → YouTube results → pick → the real file."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telegram import (
    InlineQueryResultAudio,
    InlineQueryResultCachedVideo,
    InlineQueryResultVideo,
)

import bot.handlers.inline as inl
from bot.services import inline_cache, yt_search
from bot.services.downloader import DownloadResult
from bot.services.yt_search import SearchHit
from tests.test_inline import FakeInlineQuery, InlineBot, _update

HITS = [SearchHit(id=f"vid{i:08d}", title=f"Song {i}", channel="Chan", duration=200 + i,
                  views=1_500_000) for i in range(25)]


@pytest.fixture
def env(monkeypatch, tmp_path):
    inline_cache._reset_for_tests(tmp_path / "c.json")
    calls = []

    def fake_search(terms):
        calls.append(terms)
        return HITS

    monkeypatch.setattr(yt_search, "search", fake_search)
    monkeypatch.setattr(yt_search, "cached", lambda terms: None)
    monkeypatch.setattr(inl, "_DEBOUNCE", 0)
    monkeypatch.setattr(inl, "INLINE_SEARCH_ENABLED", True)
    monkeypatch.setattr(inl, "INLINE_ENABLED", True)
    monkeypatch.setattr(inl.inline_query_limiter, "allow", lambda uid: (True, 0))
    inl._LATEST.clear()
    return SimpleNamespace(calls=calls, ctx=SimpleNamespace(bot=InlineBot()))


async def _ask(env, text, offset=""):
    q = FakeInlineQuery(text, offset=offset)
    await inl.handle_inline_query(_update(inline_query=q), env.ctx)
    return q


class TestSearchResults:
    async def test_words_become_a_song_list(self, env):
        q = await _ask(env, "lofi beats")
        results, _ = q.answers[0]
        assert env.calls == ["lofi beats"]
        first = results[0]
        assert isinstance(first, InlineQueryResultAudio) and first.id == "sa:vid00000000"
        row = first.reply_markup.inline_keyboard[0]
        assert [b.callback_data for b in row] == ["inl:wait", "inl:x"]

    async def test_video_prefix_becomes_a_video_result_list(self, env):
        q = await _ask(env, "video lofi beats")
        results, kw = q.answers[0]
        assert env.calls == ["lofi beats"]
        assert len(results) == inl.INLINE_SEARCH_PAGE
        first = results[0]
        assert isinstance(first, InlineQueryResultVideo)
        assert first.id == "sv:vid00000000" and first.title == "Song 0"
        assert first.thumbnail_url.endswith("/vid00000000/hqdefault.jpg")
        assert "Chan" in first.description and "1.5M views" in first.description
        assert first.video_url.endswith("placeholder_v1.mp4?v=vid00000000")
        assert first.mime_type == "video/mp4"
        assert first.reply_markup.inline_keyboard[0][0].callback_data == "inl:wait"
        assert kw["is_personal"] is False and kw["next_offset"] == str(inl.INLINE_SEARCH_PAGE)

    async def test_audio_prefix_gives_audio_results(self, env):
        q = await _ask(env, "audio lofi beats")
        results, _ = q.answers[0]
        assert env.calls == ["lofi beats"]
        assert isinstance(results[0], InlineQueryResultAudio) and results[0].id == "sa:vid00000000"
        assert results[0].performer == "Chan"
        # Each result its own placeholder URL, or Telegram shows a cached title.
        assert len({r.audio_url for r in results}) == len(results)

    async def test_scrolling_pages_through_results(self, env):
        q = await _ask(env, "video lofi beats", offset="20")
        results, kw = q.answers[0]
        assert [r.id for r in results] == [f"sv:vid{i:08d}" for i in range(20, 25)]
        assert kw["next_offset"] is None

    async def test_previously_fetched_video_comes_back_finished(self, env):
        inline_cache.put(HITS[0].url, inl._key("video"), file_id="DONE", kind="video", title="Song 0")
        q = await _ask(env, "video lofi beats")
        assert isinstance(q.answers[0][0][0], InlineQueryResultCachedVideo)

    async def test_typing_letter_by_letter_searches_once(self, env, monkeypatch):
        monkeypatch.setattr(inl, "_DEBOUNCE", 0.05)
        queries = [FakeInlineQuery(t) for t in ("lo", "lof", "lofi")]
        await asyncio.gather(*(inl.handle_inline_query(_update(inline_query=q), env.ctx)
                               for q in queries))
        assert env.calls == ["lofi"]
        assert queries[-1].answers and not queries[1].answers  # "lof" stood down

    async def test_search_failure_is_a_hint_not_a_crash(self, env, monkeypatch):
        def boom(terms):
            raise RuntimeError("network")

        monkeypatch.setattr(yt_search, "search", boom)
        q = await _ask(env, "lofi beats")
        results, kw = q.answers[0]
        assert results == [] and "unavailable" in kw["button"].text

    async def test_no_results(self, env, monkeypatch):
        monkeypatch.setattr(yt_search, "search", lambda t: [])
        q = await _ask(env, "zzqqxx nothing")
        assert q.answers[0][0] == [] and "No results" in q.answers[0][1]["button"].text


class TestPickingASearchResult:
    async def test_pick_downloads_that_video(self, env, monkeypatch):
        seen = {}

        async def fake_download(**kw):
            seen.update(kw)
            return DownloadResult(success=False, error="x", mode=kw["mode"])

        monkeypatch.setattr(inl, "check_public_url", lambda u: None)
        monkeypatch.setattr(inl, "record_download", lambda *a, **k: None)
        monkeypatch.setattr(inl.rate_limiter, "allow", lambda uid: (True, 0))
        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: None)
        chosen = SimpleNamespace(result_id="sa:vid00000003", query="audio lofi beats",
                                 inline_message_id="IM", from_user=SimpleNamespace(id=7))
        await inl.handle_chosen_inline_result(SimpleNamespace(chosen_inline_result=chosen), env.ctx)
        assert seen["url"] == "https://www.youtube.com/watch?v=vid00000003"
        assert seen["mode"] == "audio"

    async def test_forged_search_id_is_ignored(self, env, monkeypatch):
        async def must_not(**k):
            pytest.fail("must not download")

        monkeypatch.setattr(inl.download_manager, "download", must_not)
        chosen = SimpleNamespace(result_id="sv:../../etc/passwd", query="x",
                                 inline_message_id="IM", from_user=SimpleNamespace(id=7))
        await inl.handle_chosen_inline_result(SimpleNamespace(chosen_inline_result=chosen), env.ctx)


class TestSearchService:
    def test_live_and_overlong_entries_are_dropped(self):
        assert yt_search._hit({"id": "a" * 11, "ie_key": "Youtube", "duration": None}) is None
        assert yt_search._hit({"id": "a" * 11, "ie_key": "Youtube", "duration": 99999}) is None
        assert yt_search._hit({"id": "a" * 11, "ie_key": "YoutubeTab", "duration": 60}) is None
        h = yt_search._hit({"id": "a" * 11, "ie_key": "Youtube", "duration": 61,
                            "title": "T", "channel": "C", "view_count": 5})
        assert h and h.url.endswith("aaaaaaaaaaa")

    def test_all_youtube_url_forms_share_one_cache_slot(self, tmp_path):
        inline_cache._reset_for_tests(tmp_path / "c.json")
        inline_cache.put("https://youtu.be/dQw4w9WgXcQ?si=x", "video@720", file_id="F", kind="video")
        for u in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                  "https://youtube.com/shorts/dQw4w9WgXcQ",
                  "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ"):
            assert inline_cache.get(u, "video@720")["file_id"] == "F"

    def test_results_over_an_hour_are_left_out(self, monkeypatch):
        monkeypatch.setattr(yt_search, "INLINE_SEARCH_MAX_DURATION", 3600)
        base = {"id": "b" * 11, "ie_key": "Youtube", "title": "T"}
        assert yt_search._hit({**base, "duration": 3600}) is not None
        assert yt_search._hit({**base, "duration": 3601}) is None

    def test_identical_concurrent_searches_hit_youtube_once(self, monkeypatch):
        import threading

        calls = []
        gate = threading.Event()

        class Y:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, q, download=False):
                calls.append(q)
                gate.wait(5)
                return {"entries": [{"id": "c" * 11, "ie_key": "Youtube", "duration": 60,
                                     "title": "T"}]}

        monkeypatch.setattr(yt_search.yt_dlp, "YoutubeDL", Y)
        yt_search._CACHE.clear()
        out = []
        threads = [threading.Thread(target=lambda: out.append(yt_search.search("dup query")))
                   for _ in range(3)]
        for th in threads:
            th.start()
        gate.set()
        for th in threads:
            th.join(10)
        assert len(calls) == 1 and len(out) == 3 and all(o == out[0] for o in out)
        yt_search._CACHE.clear()

    def test_human_formats(self):
        assert yt_search.human_views(136_639_906) == "136.6M views"
        assert yt_search.human_views(1_000) == "1K views"
        assert yt_search.human_duration(3674) == "1:01:14"
        assert yt_search.human_duration(65) == "1:05"


def test_placeholder_assets_exist():
    from pathlib import Path

    assets = Path(inl.__file__).resolve().parent.parent / "assets"
    assert (assets / "placeholder_v1.mp4").stat().st_size > 1000
    assert (assets / "preparing_audio_v3.mp3").stat().st_size > 1000
