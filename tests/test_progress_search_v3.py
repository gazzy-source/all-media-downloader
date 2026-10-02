"""Clear progress view, API search parsing, VISIONOS-first, merge-first YouTube."""

from __future__ import annotations

import json

import pytest

import bot.services.yt_search as yt_search
from bot.config import QUALITY_MAP
from bot.utils.progress_view import ProgressView


class TestProgressView:
    def test_steps_move_forward_with_their_timings(self):
        v = ProgressView()
        assert "⏳ <b>Finding source</b>" in v.render("H")
        assert v.update(10, "⬇ 10% · 1.0 MB of 10.0 MB · 2.0 MB/s · ~5s left")
        text = v.render("H")
        assert "✅ Finding source" in text and "⏳ <b>Downloading</b> · 10%" in text
        assert "1.0 MB of 10.0 MB" in text and "◻️ Sending" in text
        assert v.update(99, "🎵 Converting audio…")
        assert "⏳ <b>Finishing</b> · 🎵 Converting audio…" in v.render("H")
        v.sending("4.3 MB")
        text = v.render("H")
        assert text.count("✅") == 3 and "⏳ <b>Sending</b> · 4.3 MB" in text and "⏱" in text

    def test_never_goes_backwards(self):
        v = ProgressView()
        v.update(99, "🎨 Adding cover art…")
        assert not v.update(5, "🔎 Trying another source… (2/4)")
        assert v.stage == "process"

    def test_queue_then_start_resets_the_clock(self):
        v = ProgressView()
        assert v.update(0, "Queued — you're #3 in line")
        assert "#3" in v.render("H") and v.short() == "⏳ Queued #3"
        v.t0 -= 30  # waited in line
        v.update(2, "Resolving…")
        assert v.elapsed() < 5, "the job's clock starts when it leaves the queue"

    @pytest.mark.parametrize("events,short", [
        ([], "🔎 Finding source"),
        ([(40, "⬇ 40% · 1 MB of 2 MB · 1 MB/s · ~3s left")], "⬇ 40% · ~3s left"),
        ([(99, "🎵 Converting audio…")], "🎵 Converting audio…"),
    ])
    def test_short_button_text(self, events, short):
        v = ProgressView()
        for e in events:
            v.update(*e)
        assert v.short().startswith(short) and len(v.short()) <= 60


def _renderer(vid, title, secs, views, live=False):
    r = {"videoId": vid, "title": {"runs": [{"text": title}]},
         "ownerText": {"runs": [{"text": "Chan"}]},
         "viewCountText": {"simpleText": f"{views:,} views"}}
    if not live:
        r["lengthText"] = {"simpleText": secs}
    return {"videoRenderer": r}


class TestApiSearch:
    def test_parses_videos_and_drops_live(self, monkeypatch):
        page = {"contents": [_renderer("a" * 11, "Song A", "4:40", 56_300_000),
                             _renderer("b" * 11, "Live", "", 1, live=True),
                             _renderer("c" * 11, "Mix", "1:30:00", 5)]}
        calls = []

        def fake_urlopen(req, timeout=0):
            calls.append(json.loads(req.data))

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self, n=-1):
                    return json.dumps(page).encode()

            return R()

        monkeypatch.undo()  # drop the autouse offline stub for this test
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(yt_search, "INLINE_SEARCH_MAX_DURATION", 3600)
        hits = yt_search._innertube_search("song")
        assert [(h.id, h.duration, h.views) for h in hits] == [("a" * 11, 280, 56_300_000)]
        assert calls[0]["params"] == yt_search._VIDEOS_ONLY

    def test_api_failure_falls_back_to_ytdlp(self, monkeypatch):
        class Y:
            def __init__(self, o):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, q, download=False):
                return {"entries": [{"id": "d" * 11, "ie_key": "Youtube", "duration": 60,
                                     "title": "Fallback"}]}

        monkeypatch.setattr(yt_search.yt_dlp, "YoutubeDL", Y)
        yt_search._CACHE.clear()
        assert [h.title for h in yt_search.search("anything here")] == ["Fallback"]
        yt_search._CACHE.clear()


def test_youtube_selectors_merge_first():
    for q in ("480", "720", "1080"):
        assert QUALITY_MAP[q]["format"].startswith(f"bv*[height<={q}]")
    assert QUALITY_MAP["max"]["format"] == "bv*+ba/b"


def test_visionos_leads_the_youtube_ladder():
    import bot.services.downloader as dl

    first = dl._yt_strategies(has_cookies=False)[0]
    assert first["extractor_args"]["youtube"]["player_client"] == ["visionos"]
