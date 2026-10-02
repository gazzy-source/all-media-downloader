"""
Uploads stream the file instead of loading it into memory (Step 0).

Most tests drive the REAL python-telegram-bot stack (Bot -> HTTPXRequest ->
httpx multipart) against an in-memory transport, so what is checked is the
request Telegram would actually receive, byte for byte.
"""

from __future__ import annotations

import gc
import json
import os
import weakref
from types import SimpleNamespace

import httpx
import pytest
from telegram import Bot, InputFile
from telegram.error import TelegramError, TimedOut
from telegram.request import HTTPXRequest

import bot.handlers.download as hd
from bot.services.downloader import DownloadResult

MSG = {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}}
ME = {"id": 7, "is_bot": True, "first_name": "Bot", "username": "bot"}


def _parts(request: httpx.Request, body: bytes) -> dict[str, dict]:
    """Minimal multipart/form-data parser: {field: {filename, type, data}}."""
    ctype = request.headers["content-type"]
    boundary = ctype.split("boundary=", 1)[1].strip('"').encode()
    out = {}
    for chunk in body.split(b"--" + boundary)[1:-1]:
        head, _, data = chunk.lstrip(b"\r\n").partition(b"\r\n\r\n")
        data = data[:-2] if data.endswith(b"\r\n") else data
        headers = head.decode("utf-8", "replace")
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        filename = headers.split('filename="', 1)[1].split('"', 1)[0] if 'filename="' in headers else None
        ftype = None
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-type:"):
                ftype = line.split(":", 1)[1].strip()
        out[name] = {"filename": filename, "type": ftype, "data": data}
    return out


class FakeTelegram:
    """In-memory Bot API: records every request; per-method scripted failures."""

    def __init__(self):
        self.requests: list[tuple[str, httpx.Request, bytes]] = []
        self.script: dict[str, list] = {}  # method -> outcomes to use in order
        self.during: dict[str, callable] = {}  # method -> hook run while handling

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        method = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((method, request, body))
        if method in self.during:
            self.during[method]()
        outcome = (self.script.get(method) or ["ok"]).pop(0) if self.script.get(method) else "ok"
        if outcome == "timeout":
            raise httpx.WriteTimeout("simulated", request=request)
        if outcome == "forbidden":
            return httpx.Response(403, json={"ok": False, "error_code": 403,
                                             "description": "Forbidden: not allowed"})
        if outcome == "badrequest":
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: wrong file type"})
        result = ME if method == "getMe" else (True if method == "sendChatAction" else MSG)
        return httpx.Response(200, content=json.dumps({"ok": True, "result": result}).encode())

    def uploads(self, method: str):
        return [(r, b) for m, r, b in self.requests if m == method]


@pytest.fixture
async def tg():
    fake = FakeTelegram()
    req = HTTPXRequest()
    req._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    bot = Bot("123:TEST", request=req, get_updates_request=HTTPXRequest())
    await bot.initialize()
    fake.ctx = SimpleNamespace(bot=bot)
    yield fake
    await bot.shutdown()


def _video(tmp_path, size=3 * 1024 * 1024):
    data = os.urandom(size)
    f = tmp_path / "clip.mp4"
    f.write_bytes(data)
    res = DownloadResult(success=True, files=[f], primary=f, is_video=True, title="Clip")
    return f, data, res


async def _nosleep(_s):
    return None


# 1 ----------------------------------------------------- every call site streams
class _Recorder(InputFile):
    seen: list = []

    def __init__(self, obj, *a, **kw):
        _Recorder.seen.append(kw.get("read_file_handle", True))
        super().__init__(obj, *a, **kw)


@pytest.mark.parametrize("kind,fail,expect_methods", [
    ("audio", None, ["send_audio"]),
    ("image", None, ["send_photo"]),
    ("image", "send_photo", ["send_photo", "send_document"]),  # photo -> document fallback
    ("video", None, ["send_video"]),
    ("video", "send_video", ["send_video", "send_document"]),  # video -> document fallback
    ("other", None, ["send_document"]),
])
async def test_all_six_upload_sites_stream_the_file(tmp_path, monkeypatch, kind, fail, expect_methods):
    f = tmp_path / "x.bin"
    f.write_bytes(b"z" * 1000)
    res = DownloadResult(success=True, files=[f], primary=f, title="T",
                         is_audio=kind == "audio", is_image=kind == "image",
                         is_video=kind == "video")
    called, contents = [], []

    def method(name):
        async def send(chat_id, **kw):
            called.append(name)
            media = kw.get(name.removeprefix("send_"))
            contents.append(media.input_file_content)
            if name == fail:
                # A non-network Telegram error: the kind the fallback handles
                # today (see the xfail below for BadRequest).
                raise TelegramError("refused")
            return SimpleNamespace()
        return send

    class B:
        async def send_chat_action(self, *a, **k):
            return True

    b = B()
    for m in ("send_audio", "send_photo", "send_video", "send_document"):
        setattr(b, m, method(m))
    _Recorder.seen = []
    monkeypatch.setattr(hd, "InputFile", _Recorder)
    await hd._send_media_once(SimpleNamespace(bot=b), 1, f, res, "cap", f.name)
    assert called == expect_methods
    assert _Recorder.seen and all(flag is False for flag in _Recorder.seen)
    # The handle itself goes to httpx — never the file's bytes.
    assert all(not isinstance(c, bytes) for c in contents)


# 2 + 3 ----------------------------------- real request: whole file, right length
async def test_real_request_carries_the_whole_file_with_correct_length(tg, tmp_path):
    f, data, res = _video(tmp_path)
    await hd._send_media(tg.ctx, 1, f, res, "my caption", attempts=1)
    (request, body), = tg.uploads("sendVideo")
    assert int(request.headers["content-length"]) == len(body)
    parts = _parts(request, body)
    assert parts["video"]["data"] == data
    assert parts["video"]["filename"] == "clip.mp4"
    assert parts["video"]["type"] == "video/mp4"
    assert parts["caption"]["data"] == b"my caption"
    assert parts["supports_streaming"]["data"] == b"true"


async def test_audio_title_artist_and_mime_survive(tg, tmp_path):
    data = os.urandom(200_000)
    f = tmp_path / "song.m4a"
    f.write_bytes(data)
    res = DownloadResult(success=True, files=[f], primary=f, is_audio=True,
                         title="Song", artist="Singer")
    await hd._send_media(tg.ctx, 1, f, res, "", attempts=1)
    (request, body), = tg.uploads("sendAudio")
    parts = _parts(request, body)
    assert parts["audio"]["data"] == data and parts["audio"]["filename"] == "song.m4a"
    assert parts["title"]["data"] == b"Song" and parts["performer"]["data"] == b"Singer"
    assert int(request.headers["content-length"]) == len(body)


# 4 ------------------------------------------------- retry reopens and resends
async def test_retry_resends_the_complete_file(tg, tmp_path, monkeypatch):
    f, data, res = _video(tmp_path)
    tg.script["sendVideo"] = ["timeout", "ok"]
    monkeypatch.setattr(hd, "_sleep", _nosleep)
    await hd._send_media(tg.ctx, 1, f, res, "cap", attempts=3)
    sends = tg.uploads("sendVideo")
    assert len(sends) == 2
    for request, body in sends:  # attempt 2 starts from byte 0 again
        assert _parts(request, body)["video"]["data"] == data
        assert int(request.headers["content-length"]) == len(body)


async def test_all_attempts_failing_still_raises_timed_out(tg, tmp_path, monkeypatch):
    f, _, res = _video(tmp_path, size=10_000)
    tg.script["sendVideo"] = ["timeout", "timeout"]
    monkeypatch.setattr(hd, "_sleep", _nosleep)
    with pytest.raises(TimedOut):
        await hd._send_media(tg.ctx, 1, f, res, "cap", attempts=2)
    assert len(tg.uploads("sendVideo")) == 2


# 5 --------------------------------------------- video -> document fallback
async def test_video_refused_falls_back_to_document_with_the_whole_file(tg, tmp_path):
    f, data, res = _video(tmp_path)
    tg.script["sendVideo"] = ["forbidden"]  # a non-network TelegramError (403)
    await hd._send_media(tg.ctx, 1, f, res, "cap", attempts=1)
    (vreq, vbody), = tg.uploads("sendVideo")
    (dreq, dbody), = tg.uploads("sendDocument")
    # The same handle was read to the end by the first request; the fallback
    # must still send every byte (httpx seeks back to 0).
    assert _parts(dreq, dbody)["document"]["data"] == data
    assert int(dreq.headers["content-length"]) == len(dbody)


# 6 ---------------------------------- a failed attempt is not kept alive
class _Tracked(InputFile):
    """InputFile that can be weak-referenced (PTB's uses __slots__)."""

    __slots__ = ("__weakref__",)
    alive: list = []

    def __init__(self, obj, *a, **kw):
        super().__init__(obj, *a, **kw)
        _Tracked.alive.append(weakref.ref(self))


async def test_failed_attempt_is_released_before_the_retry(tg, tmp_path, monkeypatch):
    f, _, res = _video(tmp_path, size=100_000)
    _Tracked.alive = []
    monkeypatch.setattr(hd, "InputFile", _Tracked)
    monkeypatch.setattr(hd, "_sleep", _nosleep)
    first_still_alive = []

    def during_second_attempt():
        if len(tg.uploads("sendVideo")) == 2:
            gc.collect()
            first_still_alive.append(_Tracked.alive[0]() is not None)

    tg.during["sendVideo"] = during_second_attempt
    tg.script["sendVideo"] = ["timeout", "ok"]
    await hd._send_media(tg.ctx, 1, f, res, "cap", attempts=3)
    assert first_still_alive == [False], (
        "the failed attempt's request (and its InputFile) must not survive into the retry")


async def test_kept_error_carries_no_traceback(tmp_path, monkeypatch):
    f, _, res = _video(tmp_path, size=1000)
    kept = []

    async def boom(*a, **k):
        raise TimedOut()

    async def fake_sleep(_s):
        # During the backoff, the loop holds `last_err` in its frame.
        import sys
        frame = sys._getframe(1)
        kept.append(frame.f_locals.get("last_err"))

    monkeypatch.setattr(hd, "_sleep", fake_sleep)
    ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=_ok, send_video=boom))
    with pytest.raises(TimedOut):
        await hd._send_media(ctx, 1, f, res, "cap", attempts=2)
    assert kept and kept[0] is not None and kept[0].__traceback__ is None


async def _ok(*a, **k):
    return True


@pytest.mark.xfail(strict=True, reason=(
    "PRE-EXISTING (not Step 0): PTB's BadRequest subclasses NetworkError, so "
    "`except NetworkError: raise` in _send_media_once runs before the document "
    "fallback, and _send_media then retries the same send. Fails the same on "
    "the code before Step 0. Strict: this starts failing once it is fixed."))
async def test_video_bad_request_falls_back_to_document(tg, tmp_path):
    f, data, res = _video(tmp_path, size=10_000)
    tg.script["sendVideo"] = ["badrequest"]
    await hd._send_media(tg.ctx, 1, f, res, "cap", attempts=1)
    assert _parts(*tg.uploads("sendDocument")[0])["document"]["data"] == data


# The point of Step 0: memory does not grow with the file size.
async def test_upload_memory_does_not_scale_with_the_file(tmp_path):
    import hashlib
    import tracemalloc

    size = 40 * 1024 * 1024
    f = tmp_path / "big.mp4"
    with open(f, "wb") as out:  # written in chunks: the test itself holds no 40 MB
        for _ in range(size // (1 << 20)):
            out.write(os.urandom(1 << 20))
    got = {}

    async def streaming_server(request: httpx.Request) -> httpx.Response:
        h, n = hashlib.sha256(), 0
        async for chunk in request.stream:  # consumed as it arrives, never stored
            h.update(chunk)
            n += len(chunk)
        got.update(n=n, length=int(request.headers["content-length"]))
        method = request.url.path.rsplit("/", 1)[-1]
        result = ME if method == "getMe" else (True if method == "sendChatAction" else MSG)
        return httpx.Response(200, content=json.dumps({"ok": True, "result": result}).encode())

    class StreamingTransport(httpx.AsyncBaseTransport):
        # httpx.MockTransport reads the whole body before calling its handler,
        # which would measure the test, not the bot. This consumes it in
        # chunks, the way a socket does.
        async def handle_async_request(self, request):
            return await streaming_server(request)

    req = HTTPXRequest()
    req._client = httpx.AsyncClient(transport=StreamingTransport())
    bot = Bot("123:TEST", request=req, get_updates_request=HTTPXRequest())
    await bot.initialize()
    res = DownloadResult(success=True, files=[f], primary=f, is_video=True)
    tracemalloc.start()
    try:
        await hd._send_media(SimpleNamespace(bot=bot), 1, f, res, "", attempts=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        await bot.shutdown()
    assert got["n"] == got["length"] and got["n"] > size  # whole body, as declared
    assert peak < 8 * 1024 * 1024, f"upload held {peak / 2**20:.1f} MB (file is 40 MB)"
