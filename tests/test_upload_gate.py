"""Step 1: UploadGate — bounded uploads to Telegram, FIFO, cancellable, leak-free."""

from __future__ import annotations

import asyncio
import random
import threading
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter, TimedOut

import bot.handlers.download as hd
from bot.services import jobs
from bot.services.downloader import DownloadResult
from bot.services.upload_gate import UploadCancelled, UploadGate

MB = 1024 * 1024


async def _settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _until(cond, timeout: float = 2.0) -> None:
    async def spin():
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(spin(), timeout)


class Holder:
    """Takes a reservation and keeps it until told to finish; records what it saw."""

    def __init__(self, gate, size, tag, log, cancel=None):
        self.gate, self.size, self.tag, self.log = gate, size, tag, log
        self.done = asyncio.Event()
        self.admitted = asyncio.Event()
        self.task = asyncio.create_task(self._run(cancel))

    async def _run(self, cancel):
        async with self.gate.reserve(self.size, cancel):
            self.log.append(self.tag)
            assert self.gate.running_uploads <= self.gate.max_concurrent
            assert self.gate.in_flight_bytes <= self.gate.budget_bytes
            self.admitted.set()
            await self.done.wait()


def _assert_empty(gate):
    assert (gate.running_uploads, gate.in_flight_bytes, gate.waiters) == (0, 0, 0)


# 1 + 2 ------------------------------------------------- two run, a third waits
async def test_two_uploads_run_and_a_third_waits():
    gate, log = UploadGate(2, 100 * MB), []
    a, b, c = (Holder(gate, 10 * MB, t, log) for t in "abc")
    await _settle()
    assert log == ["a", "b"] and gate.running_uploads == 2 and gate.waiters == 1
    a.done.set()
    await _until(lambda: log == ["a", "b", "c"])
    b.done.set(), c.done.set()
    await asyncio.gather(a.task, b.task, c.task)
    _assert_empty(gate)


# 3 + 4 + 6 ----------------------- caps hold under load; five 49 MB all complete
async def test_five_49mb_uploads_all_complete_within_both_limits():
    gate, log = UploadGate(2, 100 * MB), []
    seen = {"running": 0, "bytes": 0}

    async def upload(tag):
        async with gate.reserve(49 * MB):
            seen["running"] = max(seen["running"], gate.running_uploads)
            seen["bytes"] = max(seen["bytes"], gate.in_flight_bytes)
            log.append(tag)
            await asyncio.sleep(0.01)

    await asyncio.gather(*(upload(i) for i in range(5)))
    assert sorted(log) == [0, 1, 2, 3, 4]
    assert seen == {"running": 2, "bytes": 98 * MB}
    _assert_empty(gate)


async def test_random_load_never_exceeds_either_limit():
    rnd = random.Random(7)
    gate = UploadGate(2, 100 * MB)
    worst = {"running": 0, "bytes": 0}

    async def upload(size):
        await asyncio.sleep(rnd.random() / 200)
        async with gate.reserve(size):
            worst["running"] = max(worst["running"], gate.running_uploads)
            worst["bytes"] = max(worst["bytes"], gate.in_flight_bytes)
            await asyncio.sleep(rnd.random() / 200)

    await asyncio.gather(*(upload(rnd.randint(1, 60) * MB) for _ in range(60)))
    assert worst["running"] <= 2 and worst["bytes"] <= 100 * MB
    _assert_empty(gate)


# 5 ----------------------------------------------------------------- FIFO order
async def test_fifo_a_small_upload_never_overtakes_a_waiting_big_one():
    gate, log = UploadGate(2, 100 * MB), []
    a = Holder(gate, 60 * MB, "a", log)
    await _settle()
    b = Holder(gate, 60 * MB, "b", log)   # doesn't fit beside a: waits
    await _settle()
    c = Holder(gate, 10 * MB, "c", log)   # WOULD fit beside a — but b is first
    await _settle()
    assert log == ["a"] and gate.waiters == 2
    a.done.set()
    await _until(lambda: log == ["a", "b", "c"])
    b.done.set(), c.done.set()
    await asyncio.gather(a.task, b.task, c.task)
    _assert_empty(gate)


# 7 ------------------------------------------------- bigger than the budget
async def test_oversized_upload_runs_alone_and_keeps_its_place():
    gate, log = UploadGate(2, 100 * MB), []
    a = Holder(gate, 30 * MB, "a", log)
    await _settle()
    big = Holder(gate, 150 * MB, "big", log)  # costs the whole budget
    await _settle()
    small = Holder(gate, 5 * MB, "small", log)
    await _settle()
    assert log == ["a"], "big waits for the gate to empty; small stays behind big"
    a.done.set()
    await _until(lambda: log == ["a", "big"])
    assert gate.running_uploads == 1 and gate.in_flight_bytes == 100 * MB  # alone
    await _settle()
    assert log == ["a", "big"]
    big.done.set()
    await _until(lambda: log == ["a", "big", "small"])
    small.done.set()
    await asyncio.gather(a.task, big.task, small.task)
    _assert_empty(gate)


async def test_oversized_upload_on_an_idle_gate_starts_at_once():
    gate, log = UploadGate(2, 100 * MB), []
    big = Holder(gate, 400 * MB, "big", log)
    await _settle()
    assert log == ["big"]
    big.done.set()
    await big.task
    _assert_empty(gate)


# 8 + 9 -------------------------------------------------------- cancellation
async def test_cancelled_waiter_leaves_promptly_and_the_line_moves_on():
    gate, log = UploadGate(1, 100 * MB), []
    a = Holder(gate, 10 * MB, "a", log)
    await _settle()
    cancel = threading.Event()
    b = Holder(gate, 10 * MB, "b", log, cancel=cancel)
    c = Holder(gate, 10 * MB, "c", log)
    await _settle()
    assert gate.waiters == 2
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    cancel.set()
    with pytest.raises(UploadCancelled):
        await b.task
    assert loop.time() - t0 < 1.0 and gate.waiters == 1
    a.done.set()
    await _until(lambda: log == ["a", "c"])  # c, not the cancelled b
    c.done.set()
    await asyncio.gather(a.task, c.task)
    _assert_empty(gate)


async def test_cancelled_head_of_line_wakes_the_one_behind_it():
    gate, log = UploadGate(2, 100 * MB), []
    a = Holder(gate, 60 * MB, "a", log)
    await _settle()
    cancel = threading.Event()
    big = Holder(gate, 60 * MB, "big", log, cancel=cancel)  # head, doesn't fit
    small = Holder(gate, 10 * MB, "small", log)              # fits, but behind big
    await _settle()
    cancel.set()
    with pytest.raises(UploadCancelled):
        await big.task
    await _until(lambda: log == ["a", "small"])  # admitted without waiting for a
    a.done.set(), small.done.set()
    await asyncio.gather(a.task, small.task)
    _assert_empty(gate)


async def test_task_cancellation_while_waiting_leaks_nothing():
    gate, log = UploadGate(1, 100 * MB), []
    a = Holder(gate, 10 * MB, "a", log)
    await _settle()
    b = Holder(gate, 10 * MB, "b", log)
    await _settle()
    b.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b.task
    assert gate.waiters == 0 and gate.running_uploads == 1
    a.done.set()
    await a.task
    _assert_empty(gate)


async def test_admitted_and_cancelled_in_the_same_instant_gives_the_slot_back():
    gate, log = UploadGate(1, 100 * MB), []
    a = Holder(gate, 10 * MB, "a", log)
    await _settle()
    b = Holder(gate, 10 * MB, "b", log)
    await _settle()
    # a releases -> b's future is granted; b's task is cancelled before it runs.
    a.done.set()
    await a.task
    b.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b.task
    assert log == ["a"]
    _assert_empty(gate)


async def test_already_cancelled_job_never_queues():
    gate = UploadGate(1, 100 * MB)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(UploadCancelled):
        async with gate.reserve(10 * MB, cancel):
            pytest.fail("must not be admitted")
    _assert_empty(gate)


# 10 --------------------------------------------------------- exceptions release
async def test_an_exception_while_sending_releases_the_reservation():
    gate = UploadGate(2, 100 * MB)
    with pytest.raises(RuntimeError):
        async with gate.reserve(49 * MB):
            assert gate.running_uploads == 1
            raise RuntimeError("upload blew up")
    _assert_empty(gate)
    async with gate.reserve(49 * MB):  # and the gate still works
        pass
    _assert_empty(gate)


# ------------------------------------------------------- _send_media integration
def _video(tmp_path, name="v.mp4", size=600):
    f = tmp_path / name
    f.write_bytes(b"v" * size)
    return f, DownloadResult(success=True, files=[f], primary=f, is_video=True, title="V")


async def _ok(*a, **k):
    return True


@pytest.fixture
def small_gate(monkeypatch):
    gate = UploadGate(1, 1000)  # one upload at a time, 1000-byte budget
    monkeypatch.setattr(hd, "upload_gate", gate)
    return gate


# 11 ------------------- the reservation surrounds every retry attempt and backoff
async def test_retries_keep_the_same_reservation_and_their_place(small_gate, tmp_path, monkeypatch):
    fa, ra = _video(tmp_path, "a.mp4")
    fb, rb = _video(tmp_path, "b.mp4")
    events = []
    attempts = {"a": 0}

    async def send_video(chat_id, video=None, **kw):
        name = video.filename[0]
        events.append((name, "send", small_gate.running_uploads, small_gate.in_flight_bytes))
        if name == "a":
            attempts["a"] += 1
            if attempts["a"] == 1:
                await _until(lambda: small_gate.waiters == 1)  # B is now in line
                raise RetryAfter(1)
            if attempts["a"] == 2:
                raise TimedOut()
        return SimpleNamespace(video=SimpleNamespace(file_id=name))

    async def sleep_and_check(_s):
        # During every backoff A still holds the gate and B is still waiting.
        events.append(("a", "backoff", small_gate.running_uploads, small_gate.waiters))
        await asyncio.sleep(0.02)

    monkeypatch.setattr(hd, "_sleep", sleep_and_check)
    bot = SimpleNamespace(send_chat_action=_ok, send_video=send_video)
    ctx = SimpleNamespace(bot=bot)
    reserved = []
    ta = asyncio.create_task(hd._send_media(ctx, 1, fa, ra, "", attempts=3,
                                            on_reserved=lambda: reserved.append("a")))
    await _until(lambda: reserved == ["a"])  # A holds the gate before B arrives
    tb = asyncio.create_task(hd._send_media(ctx, 1, fb, rb, "", attempts=3,
                                            on_reserved=lambda: reserved.append("b")))
    await asyncio.gather(ta, tb)
    sends = [(n, r, b) for n, kind, r, b in events if kind == "send"]
    assert [n for n, *_ in sends] == ["a", "a", "a", "b"], "B never ran between A's retries"
    assert all(r == 1 and b == 600 for _, r, b in sends)
    backoffs = [(r, w) for n, kind, r, w in events if kind == "backoff"]
    assert backoffs == [(1, 1), (1, 1)], "A held its reservation through both backoffs"
    assert reserved == ["a", "b"]
    _assert_empty(small_gate)


async def test_failed_upload_releases_the_gate(small_gate, tmp_path, monkeypatch):
    f, res = _video(tmp_path)

    async def always_timeout(*a, **k):
        raise TimedOut()

    async def nosleep(_s):
        return None

    monkeypatch.setattr(hd, "_sleep", nosleep)
    ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=_ok, send_video=always_timeout))
    with pytest.raises(TimedOut):
        await hd._send_media(ctx, 1, f, res, "", attempts=2)
    _assert_empty(small_gate)


# 12 + cancellation through a real flow (group auto-download)
def _group_flow(fx, monkeypatch, tmp_path, sent):
    f, res = _video(tmp_path, "g.mp4")

    async def fake_download(**kw):
        return res

    async def send_video(chat_id, video=None, **kw):
        sent.append(chat_id)
        return SimpleNamespace(video=SimpleNamespace(file_id="F"))

    monkeypatch.setattr(hd.download_manager, "download", fake_download)
    monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
    monkeypatch.setattr(hd.rate_limiter, "allow", lambda uid: (True, 0))
    monkeypatch.setattr(hd.download_manager, "cleanup_result_files",
                        lambda r: sent.append("cleanup"))
    monkeypatch.setattr(fx.ctx.bot, "send_video", send_video)
    fx.chat.type = "supergroup"
    return res


async def test_waiting_to_send_holds_no_download_slot(fx, monkeypatch, tmp_path, small_gate):
    sent = []
    _group_flow(fx, monkeypatch, tmp_path, sent)
    blocker = Holder(small_gate, 10, "blocker", [])  # the gate is busy
    await _settle()
    flow = asyncio.create_task(hd.auto_download_flow(
        fx.update(fx.msg("https://youtu.be/x")), fx.ctx, "https://youtu.be/x"))
    await _until(lambda: small_gate.waiters == 1)
    # Downloaded, now waiting to send: its download slot is already free.
    assert hd.download_queue.running == 0
    assert "cleanup" not in sent and fx.chat.id not in sent
    blocker.done.set()
    assert await flow is True
    assert sent == [fx.chat.id, "cleanup"]
    await blocker.task
    _assert_empty(small_gate)


async def test_cancel_while_waiting_to_send_ends_quietly(fx, monkeypatch, tmp_path, small_gate):
    sent = []
    _group_flow(fx, monkeypatch, tmp_path, sent)
    blocker = Holder(small_gate, 10, "blocker", [])
    await _settle()
    flow = asyncio.create_task(hd.auto_download_flow(
        fx.update(fx.msg("https://youtu.be/x")), fx.ctx, "https://youtu.be/x"))
    await _until(lambda: small_gate.waiters == 1)
    key, = [k for k, j in jobs._JOBS.items() if j.owner == fx.user.id]
    assert await jobs.cancel(key, fx.user.id) == "ok"  # still cancellable while waiting
    assert await asyncio.wait_for(flow, 2) is False
    assert fx.chat.id not in sent, "nothing was uploaded"
    assert "cleanup" in sent, "its files were still cleaned up"
    assert key not in jobs._JOBS
    assert small_gate.waiters == 0 and small_gate.running_uploads == 1  # only the blocker
    blocker.done.set()
    await blocker.task
    _assert_empty(small_gate)


async def test_once_sending_cancel_answers_late(fx, monkeypatch, tmp_path, small_gate):
    sent = []
    _group_flow(fx, monkeypatch, tmp_path, sent)
    release = asyncio.Event()
    outcome = {}

    async def slow_send(chat_id, video=None, **kw):
        key, = [k for k, j in jobs._JOBS.items() if j.owner == fx.user.id]
        outcome["cancel"] = await jobs.cancel(key, fx.user.id)
        await release.wait()
        sent.append(chat_id)
        return SimpleNamespace(video=SimpleNamespace(file_id="F"))

    monkeypatch.setattr(fx.ctx.bot, "send_video", slow_send)
    flow = asyncio.create_task(hd.auto_download_flow(
        fx.update(fx.msg("https://youtu.be/x")), fx.ctx, "https://youtu.be/x"))
    await _until(lambda: "cancel" in outcome)
    assert outcome["cancel"] == "late"
    release.set()
    assert await flow is True and fx.chat.id in sent
    _assert_empty(small_gate)


# The same cancel-while-waiting contract in the other two paths.
async def test_dm_cancel_while_waiting_to_send(fx, monkeypatch, tmp_path, small_gate):
    from bot.services.session import DownloadSession, sessions
    from tests.conftest import FakeCallbackQuery, FakeChat, FakeMessage

    f, res = _video(tmp_path, "dm.mp4")
    sent, cleaned = [], []

    async def fake_download(**kw):
        return res

    async def send_video(*a, **k):
        sent.append(1)

    monkeypatch.setattr(hd.download_manager, "download", fake_download)
    monkeypatch.setattr(hd.download_manager, "cleanup_result_files", lambda r: cleaned.append(r))
    monkeypatch.setattr(hd, "record_download", lambda *a, **k: None)
    monkeypatch.setattr(fx.ctx.bot, "send_video", send_video)
    blocker = Holder(small_gate, 10, "blocker", [])
    await _settle()
    s = DownloadSession(session_id="sUG", user_id=fx.user.id, chat_id=100,
                        url="https://youtu.be/x", title="V", platform="YouTube",
                        mode="video", quality="720")
    sessions.put(s)
    q = FakeCallbackQuery(data="quality:sUG:720")
    q.message = FakeMessage(chat=FakeChat(id=100), message_id=777)
    flow = asyncio.create_task(hd.execute_download(q, fx.ctx, s))
    await _until(lambda: small_gate.waiters == 1)
    assert hd.download_queue.running == 0
    assert await jobs.cancel("dm:100:777", fx.user.id) == "ok"
    await asyncio.wait_for(flow, 2)
    assert not sent and cleaned == [res] and "dm:100:777" not in jobs._JOBS
    assert sessions.get("sUG") is None
    blocker.done.set()
    await blocker.task
    _assert_empty(small_gate)


async def test_inline_cancel_while_waiting_to_send(monkeypatch, tmp_path, small_gate):
    import bot.handlers.inline as inl
    from tests.test_inline import InlineBot, _chosen, _update

    f, res = _video(tmp_path, "il.mp4")
    cleaned = []
    monkeypatch.setattr(inl, "STORAGE_CHAT_ID", None)
    monkeypatch.setattr(inl, "ADMIN_IDS", {1})
    monkeypatch.setattr(inl, "INLINE_ENABLED", True)
    monkeypatch.setattr(inl, "check_public_url", lambda url: None)
    monkeypatch.setattr(inl, "_fetch_title", lambda url: "Clip")
    monkeypatch.setattr(inl, "record_download", lambda *a, **k: None)
    monkeypatch.setattr(inl.rate_limiter, "allow", lambda uid: (True, 0))

    async def fake_download(**kw):
        return res

    monkeypatch.setattr(inl.download_manager, "download", fake_download)
    monkeypatch.setattr(inl.download_manager, "cleanup_result_files", lambda r: cleaned.append(r))
    ctx = SimpleNamespace(bot=InlineBot())
    blocker = Holder(small_gate, 10, "blocker", [])
    await _settle()
    flow = asyncio.create_task(inl.handle_chosen_inline_result(
        _update(chosen_inline_result=_chosen("vp:x", imid="IMUG")), ctx))
    await _until(lambda: small_gate.waiters == 1)
    assert await jobs.cancel("i:IMUG", 42) == "ok"
    await asyncio.wait_for(flow, 2)
    assert ctx.bot.captions[-1] == "✖ Cancelled" and not ctx.bot.media_edits
    assert cleaned == [res] and "i:IMUG" not in jobs._JOBS
    blocker.done.set()
    await blocker.task
    _assert_empty(small_gate)


async def test_cancel_in_the_instant_of_admission_sends_nothing():
    gate, log = UploadGate(1, 100 * MB), []
    a = Holder(gate, 10 * MB, "a", log)
    await _settle()
    cancel = threading.Event()
    b = Holder(gate, 10 * MB, "b", log, cancel=cancel)
    await _settle()
    a.done.set()
    await a.task        # releases: b is granted in this same step...
    cancel.set()        # ...and its job is cancelled before b's task resumes
    with pytest.raises(UploadCancelled):
        await b.task
    assert log == ["a"], "b must not start sending after its job was cancelled"
    _assert_empty(gate)
