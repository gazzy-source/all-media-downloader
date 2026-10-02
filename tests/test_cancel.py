"""✖ Cancel: stops an inline or DM download; songs are the inline default."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import bot.handlers.download as hd
import bot.handlers.inline as inl
from bot.services import jobs
from bot.services.downloader import DownloadResult
from tests.conftest import FakeCallbackQuery
from tests.test_card_ux import env  # noqa: F401  (fixture)


@pytest.mark.parametrize(("text", "expect"), [
    ("lofi beats", ("audio", "lofi beats")),
    ("audio lofi beats", ("audio", "lofi beats")),
    ("song kesariya", ("audio", "kesariya")),
    ("video lofi beats", ("video", "lofi beats")),
    ("🎬 trailer", ("video", "trailer")),
    ("vid", ("video", "")),
    ("video", ("video", "")),
    ("audio", ("audio", "")),
    ("vi", ("audio", "vi")),  # too short to mean anything yet
])
def test_songs_are_the_default(text, expect):
    assert inl._split_mode(text) == expect


async def _pick(env, user_id=42):
    chosen = SimpleNamespace(result_id="sa:vid00000001", query="song",
                             inline_message_id="IMC", from_user=SimpleNamespace(id=user_id))
    return asyncio.create_task(inl.handle_chosen_inline_result(
        SimpleNamespace(chosen_inline_result=chosen), env.ctx))


async def _tap(fx, data, imid="IMC"):
    q = FakeCallbackQuery(data=data)
    q.inline_message_id = imid
    await hd.handle_callback(fx.update(callback_query=q), fx.ctx)
    return q.answers[0][0]


class TestInlineCancel:
    async def test_cancel_stops_the_running_download(self, env, fx, monkeypatch):
        seen: dict[str, threading.Event] = {}
        stopped = asyncio.Event()

        async def fake_download(**kw):
            seen["ev"] = kw["cancel"]
            while not kw["cancel"].is_set():
                await asyncio.sleep(0.01)
            stopped.set()
            return DownloadResult(success=False, error="Cancelled", mode="audio")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        task = await _pick(env)
        while "ev" not in seen:
            await asyncio.sleep(0.01)
        assert await _tap(fx, "inl:x") == "✖ Cancelled"
        await asyncio.wait_for(task, 2)
        assert stopped.is_set()
        assert env.ctx.bot.captions[-1] == "✖ Cancelled"  # no name left either
        assert env.ctx.bot.markups[-1] is None  # no buttons left on a cancelled card
        assert jobs.get("i:IMC") is None

    async def test_only_the_sender_can_cancel(self, env, fx, monkeypatch):
        release = asyncio.Event()

        async def fake_download(**kw):
            await release.wait()
            return DownloadResult(success=False, error="nope", mode="audio")

        monkeypatch.setattr(inl.download_manager, "download", fake_download)
        task = await _pick(env, user_id=7)  # fx's user is 42
        while jobs.get("i:IMC") is None:
            await asyncio.sleep(0.01)
        assert "Only the person" in await _tap(fx, "inl:x")
        release.set()
        await asyncio.wait_for(task, 2)

    async def test_cancel_after_it_finished(self, fx):
        assert "already finished" in await _tap(fx, "inl:x", imid="gone")

    async def test_cards_offer_cancel_next_to_working(self):
        row = inl._preparing_markup().inline_keyboard[0]
        assert [b.text for b in row] == ["⏳ Working…", "✖ Cancel"]


class TestQueuedCancel:
    async def test_cancel_while_queued_leaves_the_queue(self):
        from bot.services.dl_queue import DownloadQueue

        q = DownloadQueue(slots=1)
        gate = asyncio.Event()
        first = asyncio.create_task(q.run(gate.wait))
        await asyncio.sleep(0)
        job = jobs.start("t:1", 1)
        ran = []
        waiting = asyncio.create_task(jobs.run_queued(job, q, lambda: ran.append(1)))
        await asyncio.sleep(0.01)
        assert await jobs.cancel("t:1", 1) == "ok"
        with pytest.raises(asyncio.CancelledError):
            await waiting
        gate.set()
        await first
        assert ran == [] and q.waiting == 0
        jobs.drop(job)


async def test_cmd_cancel_stops_a_running_dm_download(fx):
    from bot.handlers.start import cmd_cancel

    job = jobs.start(f"dm:{fx.chat.id}:5", fx.user.id)
    notes = []

    async def on_cancel():
        notes.append("card")

    job.on_cancel = on_cancel
    job.started = True
    msg = fx.msg("/cancel")
    await cmd_cancel(fx.update(msg), fx.ctx)
    assert job.cancelled and notes == ["card"]
    assert msg.replies[0][0].startswith("✖ Cancelled")
    jobs.drop(job)


def test_only_the_users_own_link_is_deleted_on_cancel():
    me = SimpleNamespace(id=42)
    link = "https://youtu.be/abc"
    assert hd._own_message_id(SimpleNamespace(from_user=me, message_id=9, text=link), me) == 9
    # Two links, or a link with commentary: the user's own content stays.
    for text in (f"{link} https://youtu.be/def", f"look at this {link}"):
        assert hd._own_message_id(SimpleNamespace(from_user=me, message_id=9, text=text), me) is None
    bot_msg = SimpleNamespace(from_user=SimpleNamespace(id=777), message_id=10, text=link)
    assert hd._own_message_id(bot_msg, me) is None  # "Download Again": the bot's file


class TestFairQueue:
    async def test_one_user_cannot_take_every_slot(self):
        from bot.services.dl_queue import DownloadQueue

        q = DownloadQueue(slots=3, per_user=2)
        gate = asyncio.Event()
        order = []

        def job(tag):
            async def run():
                order.append(tag)
                await gate.wait()
            return run

        hog = [asyncio.create_task(q.run(job(f"A{i}"), owner="A")) for i in range(4)]
        await asyncio.sleep(0.01)
        other = asyncio.create_task(q.run(job("B0"), owner="B"))
        await asyncio.sleep(0.05)
        # A gets two slots; B's job (queued after all of A's) takes the third.
        assert order == ["A0", "A1", "B0"]
        gate.set()
        await asyncio.gather(*hog, other)
        assert q.running == 0 and q.waiting == 0
